"""Config flow."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS, CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo

from custom_components.pecron_ble.cloud import CloudAuthError, CloudDevice
from custom_components.pecron_ble.const import CONF_AUTH_KEY, CONF_DEVICE_KEY, CONF_REGION, DOMAIN

from .conftest import ADDRESS, NAME
from .fake_device import AUTH_KEY_B64

INFO = BluetoothServiceInfo(
    name=NAME, address=ADDRESS, rssi=-60, manufacturer_data={}, service_data={},
    service_uuids=[], source="local",
)


async def _to_menu(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=INFO)
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.MENU
    return result


async def test_bluetooth_manual_key(hass: HomeAssistant, fake_device):
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_KEY: "not base64!"})
    assert result["errors"] == {CONF_AUTH_KEY: "invalid_key_format"}

    with patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_KEY: AUTH_KEY_B64})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_AUTH_KEY: AUTH_KEY_B64}
    assert result["result"].unique_id == ADDRESS
    assert not fake_device.is_connected  # verification session closed


async def test_manual_wrong_key(hass: HomeAssistant, fake_device):
    fake_device.key = b"\x09" * 16
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_KEY: AUTH_KEY_B64})
    assert result["errors"] == {"base": "invalid_auth"}


def _cloud(devices, login_exc=None):
    cloud = AsyncMock()
    cloud.login.side_effect = login_exc
    cloud.devices.return_value = devices
    cloud.auth_key.return_value = AUTH_KEY_B64
    return patch("custom_components.pecron_ble.config_flow.PecronCloud", return_value=cloud)


CREDS = {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "pw", CONF_REGION: "na"}


async def test_cloud_fetches_key(hass: HomeAssistant, fake_device):
    devices = [CloudDevice("E300LFP", "pk1", "112233440000"), CloudDevice("Other", "pk2", "12345678EEFF")]
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with _cloud(devices), patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] == "12345678EEFF"  # matched by QUEC_BLE_EEFF suffix
    assert CONF_PASSWORD not in result["data"]


async def test_cloud_bad_login(hass: HomeAssistant, fake_device):
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with _cloud([], login_exc=CloudAuthError("nope")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["errors"] == {"base": "cloud_auth"}


async def test_cloud_asks_which_device_when_ambiguous(hass: HomeAssistant, fake_device):
    devices = [CloudDevice("A", "pk", "000000000001"), CloudDevice("B", "pk", "000000000002")]
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with _cloud(devices), patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
        assert result["step_id"] == "cloud_device"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DEVICE_KEY: "000000000002"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DEVICE_KEY] == "000000000002"


async def test_user_flow_no_devices(hass: HomeAssistant, fake_device):
    with patch("custom_components.pecron_ble.config_flow.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
