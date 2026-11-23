"""BLE session against a fake device that chunks indications to 20 bytes."""
from unittest.mock import MagicMock, patch

import pytest

from custom_components.pecron_ble import client as c
from custom_components.pecron_ble import protocol as p

from .fake_device import AUTH_KEY, FakePecron


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(c, "READ_SETTLE", 0.05)
    monkeypatch.setattr(c, "HANDSHAKE_TIMEOUT", 1.0)
    monkeypatch.setattr(c, "WRITE_RESULT_TIMEOUT", 0.5)


def make(fake: FakePecron, key: bytes = AUTH_KEY, **kw) -> c.PecronBleClient:
    dev = MagicMock(address="AA:BB:CC:DD:EE:FF")
    dev.name = "QUEC_BLE_EEFF"

    async def establish(_cls, _dev, _name, disconnected_callback=None, **_):
        fake.is_connected = True
        fake.disconnected_callback = disconnected_callback
        return fake

    patcher = patch.object(c, "establish_connection", establish)
    patcher.start()
    cli = c.PecronBleClient(dev, key, **kw)
    cli._patcher = patcher
    return cli


async def test_handshake_and_read():
    fake = FakePecron()
    cli = make(fake)
    await cli.connect()
    assert cli.is_connected
    assert [f.cmd for f in fake.writes] == [p.CMD_RANDOM_REQ, p.CMD_LOGIN_REQ]
    assert fake.cccd == c.CCCD_INDICATE and cli.indications_forced is True
    data = await cli.read_status()
    # 0x0012 and 0x0014 replies are merged.
    assert data[1] == 87 and data[40] is True and data[38] is False
    assert data[31] == {1: 60, 2: 120.1, 3: 0.98, 4: 45}
    assert data[47] == 24
    cli._patcher.stop()


async def test_wrong_key_rejected():
    fake = FakePecron(key=b"\x09" * 16)  # device expects a different key
    cli = make(fake)
    with pytest.raises(c.PecronAuthError):
        await cli.connect()
    assert not cli.is_connected and not fake.is_connected
    cli._patcher.stop()


async def test_write_bool_encrypted_and_confirmed():
    fake = FakePecron()
    cli = make(fake)
    await cli.connect()
    await cli.write_bool(40, False)
    assert fake.props == {40: False}
    fake.reject_writes = True
    with pytest.raises(c.PecronError):
        await cli.write_bool(38, True)
    cli._patcher.stop()


async def test_write_number_uses_ttlv_number():
    fake = FakePecron()
    cli = make(fake)
    await cli.connect()
    await cli.write_number(51, 3)
    assert fake.props == {51: 3}
    cli._patcher.stop()


async def test_unsolicited_push_reaches_callback():
    fake = FakePecron()
    got = []
    cli = make(fake, on_update=got.append)
    await cli.connect()
    fake.indicate(p.CMD_REPORT_PROPS, fake.enc(p.encode_int(5, 999)))
    assert got == [{5: 999}]
    cli._patcher.stop()


async def test_read_requires_connection():
    cli = make(FakePecron())
    with pytest.raises(c.PecronError):
        await cli.read_status()
    cli._patcher.stop()


def test_rejects_bad_key_length():
    with pytest.raises(ValueError):
        c.PecronBleClient(MagicMock(), b"short")


async def test_aes256_key_handshake_and_read():
    key = bytes(range(32))
    fake = FakePecron(key=key)
    fake.session_key = key
    cli = make(fake, key=key)
    await cli.connect()
    data = await cli.read_status()
    assert data[1] == 87
    cli._patcher.stop()


async def test_bluez_cannot_enable_indications():
    """BlueZ refuses CCCD writes; the station then refuses every write."""
    fake = FakePecron()
    fake.allow_cccd_write = False
    cli = make(fake)
    with pytest.raises(c.PecronIndicationError, match="ESPHome Bluetooth proxy"):
        await cli.connect()
    assert cli.indications_forced is False
    assert not cli.is_connected and not fake.is_connected
    cli._patcher.stop()


async def test_cccd_write_refused_by_bleak_without_backend_is_not_fatal():
    """No reachable backend: bleak's own ValueError is handled like a refusal."""
    fake = FakePecron()
    fake.require_indications = False
    cli = make(fake)
    with patch.object(FakePecron, "_backend", None):
        await cli.connect()
    assert cli.is_connected and cli.indications_forced is False
    cli._patcher.stop()


async def test_station_that_accepts_notifications_still_works_without_cccd_write():
    fake = FakePecron()
    fake.allow_cccd_write = False
    fake.require_indications = False
    cli = make(fake)
    await cli.connect()
    assert cli.is_connected and cli.indications_forced is False
    cli._patcher.stop()
