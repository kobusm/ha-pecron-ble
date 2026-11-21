"""Coordinator: owns the BLE session, polls, and merges pushed reports."""
from __future__ import annotations

import base64
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import PecronAuthError, PecronBleClient, PecronError
from .const import CONF_AUTH_KEY, DEFAULT_SCAN_INTERVAL, DOMAIN, DP_AC_SWITCH

_LOGGER = logging.getLogger(__name__)

type PecronConfigEntry = ConfigEntry[PecronCoordinator]


def value_at(data: dict[int, Any] | None, path: tuple[int, ...]) -> Any:
    """Look up a (struct, member) path; for repeated structs use the first."""
    cur: Any = data
    for key in path:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


class PecronCoordinator(DataUpdateCoordinator[dict[int, Any]]):
    config_entry: PecronConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PecronConfigEntry, address: str) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {address}",
            update_interval=DEFAULT_SCAN_INTERVAL, always_update=False,
        )
        self.address = address
        self._key = base64.b64decode(entry.data[CONF_AUTH_KEY])
        self.client: PecronBleClient | None = None
        self._values: dict[int, Any] = {}
        self._ac_before_drop: bool | None = None
        self._reconnects = 0
        self.ac_flips_on_reconnect = 0

    # The BLE session is created lazily because it needs a BLEDevice.
    def _ensure_client(self) -> PecronBleClient:
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="not_in_range",
                translation_placeholders={"address": self.address},
            )
        if self.client is None:
            self.client = PecronBleClient(
                ble_device, self._key,
                on_update=self._handle_push, on_disconnect=self._handle_disconnect,
            )
        else:
            self.client.set_ble_device(ble_device)
        return self.client

    async def _async_update_data(self) -> dict[int, Any]:
        client = self._ensure_client()
        try:
            if not client.is_connected:
                await client.connect()
                self._reconnects += 1
            values = await client.read_status()
        except PecronAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PecronError as err:
            await client.disconnect()
            raise UpdateFailed(str(err)) from err
        self._check_ac_flip(values)
        self._values.update(values)
        return dict(self._values)

    def _check_ac_flip(self, values: dict[int, Any]) -> None:
        """E300LFP firmware has been seen to drop AC output on BLE (re)connect.

        We do not auto-restore it (switching mains output on unattended is the
        user's call), but we log loudly and count it for diagnostics.
        """
        if self._ac_before_drop is None or DP_AC_SWITCH not in values:
            return
        if self._ac_before_drop and values[DP_AC_SWITCH] is False:
            self.ac_flips_on_reconnect += 1
            _LOGGER.warning(
                "%s: AC output went OFF right after a BLE reconnect. This is a known "
                "Pecron firmware quirk on some models (seen on E300LFP)", self.address,
            )
        self._ac_before_drop = None

    @callback
    def _handle_push(self, values: dict[int, Any]) -> None:
        self._values.update(values)
        self.async_set_updated_data(dict(self._values))

    @callback
    def _handle_disconnect(self) -> None:
        if (ac := self._values.get(DP_AC_SWITCH)) is not None:
            self._ac_before_drop = bool(ac)

    async def async_write_bool(self, field_id: int, value: bool) -> None:
        client = self._ensure_client()
        try:
            if not client.is_connected:
                await client.connect()
            await client.write_bool(field_id, value)
        except PecronError as err:
            raise HomeAssistantError(f"Pecron write failed: {err}") from err
        # Optimistic until the device's own report confirms it.
        self._values[field_id] = value
        self.async_set_updated_data(dict(self._values))
        await self.async_request_refresh()

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self.client is not None:
            await self.client.disconnect()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "connected": bool(self.client and self.client.is_connected),
            "connects": self._reconnects,
            "ac_flips_on_reconnect": self.ac_flips_on_reconnect,
            "unknown_frames": self.client.unknown_frames if self.client else [],
        }
