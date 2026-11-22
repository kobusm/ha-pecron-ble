"""Config flow."""
from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_manual_key_on_adapter_without_indications(hass: HomeAssistant, fake_device):
    fake_device.allow_cccd_write = False  # BlueZ adapter
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_KEY: AUTH_KEY_B64})
    assert result["errors"] == {"base": "needs_proxy"}


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


def _adv(address, name=None, mfg=None, connectable=True):
    info = MagicMock(address=address, manufacturer_data=mfg or {}, connectable=connectable)
    info.name = name
    return info


def _discovered(*infos):
    return patch("custom_components.pecron_ble.config_flow.async_discovered_service_info", return_value=list(infos))


async def test_user_flow_lists_name_and_manufacturer_matches(hass: HomeAssistant, fake_device):
    infos = [
        _adv("11:11:11:11:11:11", "QUEC_BLE_1111"),
        _adv(ADDRESS, None, {0x5551: b"\x01"}),  # no name, Quectel mfg data only
        _adv("22:22:22:22:22:22", "SomeSpeaker"),  # unrelated
    ]
    with _discovered(*infos):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    choices = result["data_schema"].schema[CONF_ADDRESS].container
    assert set(choices) == {"11:11:11:11:11:11", ADDRESS, "manual"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["type"] is FlowResultType.MENU


async def test_user_flow_nothing_found_falls_back_to_manual_address(hass: HomeAssistant, fake_device):
    with _discovered():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "manual_address"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: "nope"})
    assert result["errors"] == {CONF_ADDRESS: "invalid_address"}

    with patch("custom_components.pecron_ble.config_flow.async_ble_device_from_address", return_value=None):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})
    assert result["errors"] == {CONF_ADDRESS: "not_in_range"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS.lower().replace(":", "-")}
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    with patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_AUTH_KEY: AUTH_KEY_B64})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ADDRESS] == ADDRESS
    assert result["title"] == NAME  # name taken from the BLE device


async def test_user_flow_only_passive_scanner_sees_it(hass: HomeAssistant, fake_device):
    with _discovered(_adv(ADDRESS, "QUEC_BLE_EEFF", connectable=False)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_connectable"
    assert result["description_placeholders"] == {"addresses": ADDRESS}


async def test_cloud_key_error_is_shown(hass: HomeAssistant, fake_device):
    from custom_components.pecron_ble.cloud import CloudError

    devices = [CloudDevice("E300LFP", "pk1", "12345678EEFF")]
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with _cloud(devices) as cloud_cls:
        cloud_cls.return_value.auth_key.side_effect = CloudError("cloud answered code 5032: No permission")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "auth_key_unavailable"
    assert result["description_placeholders"] == {
        "device": "E300LFP (12345678EEFF)",
        "error": "cloud answered code 5032: No permission",
    }


async def test_cloud_key_wrong_length_is_explained(hass: HomeAssistant, fake_device):
    import base64

    devices = [CloudDevice("E300LFP", "pk1", "12345678EEFF")]
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "cloud"})
    with _cloud(devices) as cloud_cls:
        cloud_cls.return_value.auth_key.return_value = base64.b64encode(b"x" * 20).decode()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDS)
    assert result["reason"] == "auth_key_unavailable"
    assert result["description_placeholders"]["error"] == "the key decoded to 20 bytes, expected 16, 24 or 32"


def _pecron_local_entry(hass, title, key, device_key=None, mac=None):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data = {"auth_key": key}
    if device_key:
        data["device_key"] = device_key
    if mac:
        data["mac"] = mac
    MockConfigEntry(domain="pecron_local", title=title, data=data).add_to_hass(hass)


async def test_reuse_pecron_local_key_matching_e300lfp_macs(hass: HomeAssistant, fake_device):
    # Real E300LFP: Wi-Fi device key and BLE MAC share only the first 4 bytes.
    import base64

    other = base64.b64encode(b"\x09" * 16).decode()
    _pecron_local_entry(hass, "Other station", other, device_key="112233440001")
    _pecron_local_entry(hass, "Pecron @ 192.168.86.79", AUTH_KEY_B64, device_key="AABBCCDD42F9")
    result = await _to_menu(hass)
    assert result["menu_options"][0] == "pecron_local"
    with patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pecron_local"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_AUTH_KEY: AUTH_KEY_B64, CONF_DEVICE_KEY: "AABBCCDD42F9"}


async def test_reuse_pecron_local_key_lets_user_pick(hass: HomeAssistant, fake_device):
    import base64

    _pecron_local_entry(hass, "A", base64.b64encode(b"\x09" * 16).decode(), device_key="111111111111")
    _pecron_local_entry(hass, "B", AUTH_KEY_B64, device_key="222222222222")
    result = await _to_menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "pecron_local"})
    assert result["step_id"] == "pecron_local"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry": "A"})
    assert result["errors"] == {"base": "invalid_auth"}  # wrong key is caught by the BLE login
    with patch("custom_components.pecron_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"entry": "B"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_pecron_local_option_without_entries(hass: HomeAssistant, fake_device):
    result = await _to_menu(hass)
    assert result["menu_options"] == ["cloud", "manual"]
