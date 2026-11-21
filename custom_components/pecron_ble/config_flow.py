"""Config flow: discover over BLE, get the auth key once (cloud or manual), verify locally."""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import PecronAuthError, PecronBleClient, PecronError
from .cloud import CloudAuthError, CloudDevice, CloudError, PecronCloud, match_device
from .const import (
    BLE_NAME_PREFIX,
    CONF_AUTH_KEY,
    CONF_DEVICE_KEY,
    CONF_PRODUCT_KEY,
    CONF_REGION,
    DOMAIN,
    QUECTEL_MANUFACTURER_ID,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)


MANUAL_ADDRESS = "manual"
MAC_RE = re.compile(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}")


def _is_pecron(info: BluetoothServiceInfoBleak) -> bool:
    return (info.name or "").startswith(BLE_NAME_PREFIX) or QUECTEL_MANUFACTURER_ID in info.manufacturer_data


def _decode_key(text: str) -> bytes | None:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return key if len(key) == 16 else None


class PecronConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, str] = {}
        self._cloud: PecronCloud | None = None
        self._cloud_devices: list[CloudDevice] = []
        self._region = "na"

    # -- discovery ----------------------------------------------------------

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address
        self._name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_auth_method()
        self._set_confirm_only()
        return self.async_show_form(step_id="bluetooth_confirm", description_placeholders={"name": self._name})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input[CONF_ADDRESS] == MANUAL_ADDRESS:
                return await self.async_step_manual_address()
            return await self._select_address(
                user_input[CONF_ADDRESS], self._discovered[user_input[CONF_ADDRESS]]
            )

        current = self._async_current_ids(include_ignore=False)
        seen_unconnectable: set[str] = set()
        # connectable=False returns every advertisement, from active and passive scanners.
        for info in async_discovered_service_info(self.hass, connectable=False):
            if not _is_pecron(info) or info.address in current:
                continue
            if info.connectable:
                self._discovered[info.address] = info.name or info.address
            else:
                seen_unconnectable.add(info.address)

        if not self._discovered:
            if seen_unconnectable - set(self._discovered):
                return self.async_abort(
                    reason="not_connectable",
                    description_placeholders={"addresses": ", ".join(sorted(seen_unconnectable))},
                )
            return await self.async_step_manual_address()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_ADDRESS): vol.In(
                    {addr: f"{name} ({addr})" for addr, name in self._discovered.items()}
                    | {MANUAL_ADDRESS: "Enter address manually"}
                )
            }),
        )

    async def async_step_manual_address(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Fallback when discovery found nothing: take the MAC the user reads from the Pecron app or nRF Connect."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper().replace("-", ":")
            if not MAC_RE.fullmatch(address):
                errors[CONF_ADDRESS] = "invalid_address"
            elif async_ble_device_from_address(self.hass, address, connectable=True) is None:
                errors[CONF_ADDRESS] = "not_in_range"
            else:
                return await self._select_address(address, None)
        return self.async_show_form(
            step_id="manual_address",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): str}),
            errors=errors,
        )

    async def _select_address(self, address: str, name: str | None) -> ConfigFlowResult:
        await self.async_set_unique_id(address, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        self._address = address
        if name is None and (ble := async_ble_device_from_address(self.hass, address, connectable=True)):
            name = ble.name
        self._name = name or address
        return await self.async_step_auth_method()

    # -- auth key -------------------------------------------------------------

    async def async_step_auth_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="auth_method", menu_options=["cloud", "manual"])

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = _decode_key(user_input[CONF_AUTH_KEY])
            if key is None:
                errors[CONF_AUTH_KEY] = "invalid_key_format"
            elif not (errors := await self._verify(key)):
                return await self._finish({CONF_AUTH_KEY: user_input[CONF_AUTH_KEY].strip()})
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_KEY): str}),
            errors=errors,
        )

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            self._cloud = PecronCloud(async_get_clientsession(self.hass), self._region)
            try:
                await self._cloud.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                self._cloud_devices = await self._cloud.devices()
            except CloudAuthError:
                errors["base"] = "cloud_auth"
            except CloudError as err:
                _LOGGER.debug("cloud error: %s", err)
                errors["base"] = "cloud_unreachable"
            else:
                if not self._cloud_devices:
                    errors["base"] = "no_cloud_devices"
                elif match := match_device(self._cloud_devices, self._name, self._address or ""):
                    return await self._from_cloud(match)
                else:
                    return await self.async_step_cloud_device()
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema({
                vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
                vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(CONF_REGION, default=self._region): SelectSelector(
                    SelectSelectorConfig(
                        options=list(REGIONS), translation_key="region", mode=SelectSelectorMode.DROPDOWN
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_cloud_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        by_dk = {d.device_key: d for d in self._cloud_devices}
        if user_input is not None:
            return await self._from_cloud(by_dk[user_input[CONF_DEVICE_KEY]])
        return self.async_show_form(
            step_id="cloud_device",
            data_schema=vol.Schema({
                vol.Required(CONF_DEVICE_KEY): vol.In({dk: f"{d.name} ({dk})" for dk, d in by_dk.items()})
            }),
            description_placeholders={"name": self._name or ""},
        )

    async def _from_cloud(self, device: CloudDevice) -> ConfigFlowResult:
        assert self._cloud is not None
        try:
            key_b64 = await self._cloud.auth_key(device)
        except CloudError as err:
            _LOGGER.debug("auth key fetch failed: %s", err)
            return self.async_abort(reason="auth_key_unavailable")
        key = _decode_key(key_b64)
        if key is None:
            return self.async_abort(reason="auth_key_unavailable")
        if errors := await self._verify(key):
            # Show the manual form with the error so the user can retry or paste.
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema({vol.Required(CONF_AUTH_KEY, default=key_b64): str}),
                errors=errors,
            )
        return await self._finish({
            CONF_AUTH_KEY: key_b64,
            CONF_PRODUCT_KEY: device.product_key,
            CONF_DEVICE_KEY: device.device_key,
        })

    async def _verify(self, key: bytes) -> dict[str, str]:
        """Log in over BLE once to prove the key works."""
        assert self._address
        ble_device = async_ble_device_from_address(self.hass, self._address, connectable=True)
        if ble_device is None:
            return {"base": "not_in_range"}
        client = PecronBleClient(ble_device, key)
        try:
            await client.connect()
        except PecronAuthError:
            return {"base": "invalid_auth"}
        except PecronError as err:
            _LOGGER.debug("verify failed: %s", err)
            return {"base": "cannot_connect"}
        finally:
            await client.disconnect()
        return {}

    async def _finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        assert self._address
        if self.source == "reauth":
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data_updates=data)
        return self.async_create_entry(
            title=self._name or self._address, data={CONF_ADDRESS: self._address, **data}
        )

    # -- reauth ---------------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._address = entry_data[CONF_ADDRESS]
        self._name = self._get_reauth_entry().title
        return await self.async_step_auth_method()
