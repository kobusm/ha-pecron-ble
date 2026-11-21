"""Shared fixtures."""
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.pecron_ble import client as client_mod

from .fake_device import FakePecron

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "QUEC_BLE_EEFF"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(client_mod, "READ_SETTLE", 0.05)
    monkeypatch.setattr(client_mod, "HANDSHAKE_TIMEOUT", 1.0)
    monkeypatch.setattr(client_mod, "WRITE_RESULT_TIMEOUT", 0.5)


@pytest.fixture
def ble_device():
    dev = MagicMock(address=ADDRESS)
    dev.name = NAME
    return dev


@pytest.fixture
def fake_device(hass: HomeAssistant, ble_device):
    """Patch the BLE stack so every connection lands on one FakePecron."""
    fake = FakePecron()

    async def establish(_cls, _dev, _name, disconnected_callback=None, **_):
        fake.is_connected = True
        return fake

    # bluetooth_adapters is a manifest dependency; mark it loaded so HA
    # doesn't try to bring up real adapters in tests.
    hass.config.components.add("bluetooth_adapters")
    hass.config.components.add("bluetooth")
    with (
        patch.object(client_mod, "establish_connection", establish),
        patch("custom_components.pecron_ble.coordinator.bluetooth.async_ble_device_from_address",
              return_value=ble_device),
        patch("custom_components.pecron_ble.config_flow.async_ble_device_from_address",
              return_value=ble_device),
    ):
        yield fake
