"""Entry setup, entities, switches, diagnostics."""
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pecron_ble.const import CONF_AUTH_KEY, DOMAIN
from custom_components.pecron_ble.diagnostics import async_get_config_entry_diagnostics

from .conftest import ADDRESS, NAME
from .fake_device import AUTH_KEY_B64


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, title=NAME,
        data={CONF_ADDRESS: ADDRESS, CONF_AUTH_KEY: AUTH_KEY_B64},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_creates_only_reported_entities(hass: HomeAssistant, fake_device):
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.quec_ble_eeff_battery").state == "87"
    assert hass.states.get("sensor.quec_ble_eeff_ac_output_voltage").state == "120.1"
    assert hass.states.get("sensor.quec_ble_eeff_battery_temperature").state == "24"
    assert hass.states.get("sensor.quec_ble_eeff_status").state == "charging"
    # in 120 > out 45 -> charging: time_to_full set, time_to_empty unknown
    assert hass.states.get("sensor.quec_ble_eeff_time_to_full").state == "300"
    assert hass.states.get("sensor.quec_ble_eeff_time_to_empty").state == "unknown"
    assert hass.states.get("switch.quec_ble_eeff_ac_output").state == "on"
    assert hass.states.get("switch.quec_ble_eeff_dc_output").state == "off"

    # Not reported by this device -> no entity.
    ents = er.async_get(hass)
    assert ents.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}_ups_mode") is None
    assert ents.async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_battery_voltage") is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not fake_device.is_connected


async def test_switch_writes_to_device(hass: HomeAssistant, fake_device):
    await _setup(hass)
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.quec_ble_eeff_dc_output"}, blocking=True
    )
    assert fake_device.props == {38: True}


async def test_wrong_key_starts_reauth(hass: HomeAssistant, fake_device):
    fake_device.key = b"\x09" * 16
    entry = await _setup_expect_fail(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def _setup_expect_fail(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, title=NAME,
        data={CONF_ADDRESS: ADDRESS, CONF_AUTH_KEY: AUTH_KEY_B64},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_diagnostics_redacts_key(hass: HomeAssistant, fake_device):
    entry = await _setup(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"][CONF_AUTH_KEY] == "**REDACTED**"
    assert diag["data_points"]["1:battery_percentage"] == 87
    assert diag["session"]["connected"] is True
