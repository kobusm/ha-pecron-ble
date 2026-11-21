"""Diagnostics: raw data points with names, so unmapped E300LFP fields can be identified."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_AUTH_KEY, CONF_DEVICE_KEY, KNOWN_IDS
from .coordinator import PecronConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PecronConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    raw = coordinator.data or {}
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_AUTH_KEY, CONF_DEVICE_KEY}),
        "session": coordinator.diagnostics(),
        "data_points": {f"{k}:{KNOWN_IDS.get(k, 'unknown')}": v for k, v in sorted(raw.items())},
    }
