"""BLE session with a Pecron power station.

Depends only on bleak / bleak-retry-connector (both ship with Home Assistant)
so it can be exercised without HA.

Lifecycle: connect -> enable indications on 0x9C40 -> 0x7032 random ->
0x7034 login -> 0x7035. Then 0x0011 polls; the device answers with 0x0012
and/or 0x0014 frames whose payloads are AES-CBC encrypted TTLV. The device
also pushes unsolicited 0x0014 reports while connected; those are delivered
through ``on_update``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

CHAR_UUID = "00009c40-0000-1000-8000-00805f9b34fb"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"
# 0x9C40 offers both notify and indicate. The station only accepts writes from
# a client that subscribed for *indications* (CCCD 0x0002); with notifications
# (0x0001, which BlueZ's StartNotify always picks) every write fails with ATT
# error 0x08 Insufficient Authorization.
CCCD_INDICATE = b"\x02\x00"

# AES-128 is what's been seen in the wild; accept the other AES sizes too in
# case some models' cloud returns a longer key.
AES_KEY_SIZES = (16, 24, 32)

HANDSHAKE_TIMEOUT = 8.0
# Status replies trickle in as several indications over a few seconds.
READ_TIMEOUT = 15.0
READ_SETTLE = 1.5
WRITE_RESULT_TIMEOUT = 8.0


class PecronError(Exception):
    """Communication failure."""


class PecronAuthError(PecronError):
    """Device rejected the auth key."""


class PecronIndicationError(PecronError):
    """Station refused writes because indications could not be enabled."""


def _is_insufficient_authorization(err: BaseException) -> bool:
    text = str(err).lower()
    return "insufficient authorization" in text or "insufficient_authorization" in text


class PecronBleClient:
    def __init__(
        self,
        ble_device: BLEDevice,
        auth_key: bytes,
        on_update: Callable[[dict[int, Any]], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        if len(auth_key) not in AES_KEY_SIZES:
            raise ValueError("auth key must decode to 16, 24 or 32 bytes")
        self._device = ble_device
        self._key = auth_key
        self._on_update = on_update
        self._on_disconnect = on_disconnect
        self._client: BleakClient | None = None
        self._iv: bytes | None = None
        self._pid = 0
        self._assembler = p.FrameAssembler()
        self._frames: asyncio.Queue[p.Frame] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._polling = False
        self.unknown_frames: list[str] = []
        self.indications_forced: bool | None = None

    # -- properties -------------------------------------------------------

    @property
    def address(self) -> str:
        return self._device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected and self._iv is not None

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Swap in a fresher BLEDevice (e.g. a different proxy is now closer)."""
        self._device = ble_device

    # -- connection -------------------------------------------------------

    async def connect(self) -> None:
        async with self._lock:
            if self.is_connected:
                return
            await self._disconnect_locked()
            self._assembler.reset()
            _drain(self._frames)
            try:
                self._client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    self._device.name or self._device.address,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=3,
                )
                await self._client.start_notify(CHAR_UUID, self._handle_indication)
                self.indications_forced = await self._force_indications()
                await self._handshake()
            except PecronError:
                await self._disconnect_locked()
                raise
            except (BleakError, asyncio.TimeoutError, OSError) as err:
                await self._disconnect_locked()
                raise PecronError(f"connect failed: {err}") from err

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        self._iv = None
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except (BleakError, asyncio.TimeoutError, OSError):
                pass

    def _handle_disconnect(self, _client: BleakClient) -> None:
        _LOGGER.debug("%s: disconnected", self.address)
        self._iv = None
        if self._on_disconnect:
            self._on_disconnect()

    async def _force_indications(self) -> bool:
        """Rewrite the CCCD to 0x0002 so the station sees an indication subscriber.

        Works through ESPHome proxies (and any backend that allows descriptor
        writes). BlueZ refuses CCCD writes over D-Bus; that's logged and the
        handshake is still attempted.
        """
        assert self._client is not None
        char = self._client.services.get_characteristic(CHAR_UUID)
        desc = char.get_descriptor(CCCD_UUID) if char is not None else None
        if desc is None:
            _LOGGER.debug("%s: no CCCD found on 0x9C40", self.address)
            return False
        try:
            await self._client.write_gatt_descriptor(desc.handle, CCCD_INDICATE)
        except (BleakError, asyncio.TimeoutError, OSError, NotImplementedError) as err:
            _LOGGER.debug("%s: could not switch CCCD to indications: %s", self.address, err)
            return False
        _LOGGER.debug("%s: indications enabled", self.address)
        return True

    async def _handshake(self) -> None:
        await self._send(p.CMD_RANDOM_REQ)
        frame = await self._wait_for(p.CMD_RANDOM_RESP, HANDSHAKE_TIMEOUT)
        random_str = p.random_from_frame(frame)
        if not random_str:
            raise PecronError(f"no random in 0x7033: {frame.payload.hex()}")

        await self._send(p.CMD_LOGIN_REQ, p.login_payload(self._key, random_str))
        frame = await self._wait_for(p.CMD_LOGIN_RESP, HANDSHAKE_TIMEOUT)
        if p.result_ok(frame.payload) is False:
            raise PecronAuthError(f"login rejected: {frame.payload.hex()}")
        self._iv = p.iv_from_random(random_str)
        _LOGGER.debug("%s: logged in", self.address)

    # -- I/O --------------------------------------------------------------

    def _next_pid(self) -> int:
        self._pid = self._pid % 0xFFFF + 1
        return self._pid

    async def _send(self, cmd: int, payload: bytes = b"") -> None:
        assert self._client is not None
        frame = p.build_frame(cmd, payload, self._next_pid())
        try:
            # Write Request (with response). The 77-byte login frame exceeds the
            # default ATT MTU; BlueZ/ESPHome fall back to a long write.
            await self._client.write_gatt_char(CHAR_UUID, frame, response=True)
        except (BleakError, asyncio.TimeoutError, OSError) as err:
            if _is_insufficient_authorization(err) and not self.indications_forced:
                raise PecronIndicationError(
                    "station refused the write (insufficient authorization): it needs "
                    "indications enabled, which this Bluetooth adapter can't do; "
                    "connect through an ESPHome Bluetooth proxy"
                ) from err
            raise PecronError(f"write failed: {err}") from err

    def _handle_indication(self, _sender: Any, data: bytearray) -> None:
        for frame in self._assembler.feed(bytes(data)):
            if not frame.crc_ok:
                _LOGGER.debug("%s: dropping frame with bad CRC: %s", self.address, frame)
                continue
            if frame.cmd == p.CMD_REPORT_PROPS and self._iv and not self._polling:
                # Unsolicited push while idle.
                values = self._decrypt_values(frame)
                if values and self._on_update:
                    self._on_update(values)
                continue
            self._frames.put_nowait(frame)

    async def _wait_for(self, cmd: int, timeout: float) -> p.Frame:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PecronError(f"timeout waiting for 0x{cmd:04x}")
            try:
                frame = await asyncio.wait_for(self._frames.get(), remaining)
            except asyncio.TimeoutError as err:
                raise PecronError(f"timeout waiting for 0x{cmd:04x}") from err
            if frame.cmd == cmd:
                return frame
            self._note_unknown(frame)

    def _note_unknown(self, frame: p.Frame) -> None:
        entry = f"0x{frame.cmd:04x}:{frame.payload.hex()}"
        _LOGGER.debug("%s: unexpected frame %s", self.address, entry)
        self.unknown_frames = (self.unknown_frames + [entry])[-20:]

    def _decrypt_values(self, frame: p.Frame) -> dict[int, Any]:
        if not frame.payload:
            return {}
        assert self._iv is not None
        try:
            plain = p.aes_decrypt(self._key, self._iv, frame.payload)
        except p.ProtocolError:
            # Some firmware sends short settings frames in the clear.
            plain = frame.payload
        try:
            return p.fields_to_dict(p.decode_fields(plain))
        except p.ProtocolError as err:
            _LOGGER.debug("%s: undecodable payload %s: %s", self.address, plain.hex(), err)
            return {}

    # -- public API -------------------------------------------------------

    async def read_status(self) -> dict[int, Any]:
        """Request a full status dump and merge every frame of the reply."""
        if not self.is_connected:
            raise PecronError("not connected")
        async with self._lock:
            self._polling = True
            try:
                _drain(self._frames)
                await self._send(p.CMD_READ_STATUS)
                merged: dict[int, Any] = {}
                loop = asyncio.get_running_loop()
                deadline = loop.time() + READ_TIMEOUT
                while True:
                    # Before the first data frame wait the full timeout; after
                    # it only wait READ_SETTLE for stragglers.
                    wait = deadline - loop.time() if not merged else READ_SETTLE
                    if wait <= 0:
                        break
                    try:
                        frame = await asyncio.wait_for(self._frames.get(), wait)
                    except asyncio.TimeoutError:
                        break
                    if frame.cmd in (p.CMD_STATUS_ACK, p.CMD_REPORT_PROPS):
                        merged.update(self._decrypt_values(frame))
                    else:
                        self._note_unknown(frame)
            finally:
                self._polling = False
        if not merged:
            raise PecronError("no status data received")
        return merged

    async def write_bool(self, field_id: int, value: bool) -> None:
        await self._write(p.encode_bool(field_id, value))

    async def write_number(self, field_id: int, value: int, decimals: int = 0) -> None:
        await self._write(p.encode_int(field_id, value, decimals))

    async def _write(self, ttlv: bytes) -> None:
        if not self.is_connected:
            raise PecronError("not connected")
        assert self._iv is not None
        async with self._lock:
            self._polling = True
            try:
                _drain(self._frames)
                await self._send(p.CMD_WRITE_PROPS, p.aes_encrypt(self._key, self._iv, ttlv))
                try:
                    frame = await self._wait_for(p.CMD_WRITE_RESULT, WRITE_RESULT_TIMEOUT)
                except PecronError:
                    _LOGGER.warning("%s: write %s unconfirmed", self.address, ttlv.hex())
                    return
                payload = frame.payload
                if payload and len(payload) % 16 == 0:
                    try:
                        payload = p.aes_decrypt(self._key, self._iv, payload)
                    except p.ProtocolError:
                        pass
                if p.result_ok(payload) is False:
                    raise PecronError(f"device rejected write {ttlv.hex()}: {payload.hex()}")
            finally:
                self._polling = False


def _drain(q: asyncio.Queue) -> None:
    while not q.empty():
        q.get_nowait()
