"""A fake Pecron BLE peripheral speaking the Quectel frame protocol."""
from __future__ import annotations

import asyncio
import base64
from typing import Any

from custom_components.pecron_ble import protocol as p

AUTH_KEY = bytes(range(16))
AUTH_KEY_B64 = base64.b64encode(AUTH_KEY).decode()
RANDOM = "Abc123Xyz789Qwe4"

# A realistic E300LFP-like status: battery % top level, AC out struct, switches.
STATUS_A = (
    p.encode_int(1, 87)
    + p.encode_int(4, 120)
    + p.encode_int(5, 45)
    + p.encode_int(2, 300)
    + p.encode_int(3, 300)
    + p.encode_bool(40, True)
    + p.encode_bool(38, False)
    + p.encode_int(37, 1)
)
# AC output struct (31): hz, V (1 decimal), pf (2 decimals), W
STATUS_B = ((31 << 3) | 4).to_bytes(2, "big") + (4).to_bytes(2, "big") + p.encode_int(1, 60) + p.encode_int(2, 120.1, 1) + p.encode_int(3, 0.98, 2) + p.encode_int(4, 45) \
    + p.encode_int(47, 24)


class FakePecron:
    """Duck-types the bits of BleakClient the client uses."""

    def __init__(self, key: bytes = AUTH_KEY, mtu_chunk: int = 20) -> None:
        self.key = key  # key the device checks the login against
        self.session_key = AUTH_KEY  # key it encrypts the session with
        self.chunk = mtu_chunk
        self.is_connected = False
        self._cb = None
        self.iv = p.iv_from_random(RANDOM)
        self.asm = p.FrameAssembler()
        self.writes: list[p.Frame] = []
        self.props: dict[int, Any] = {}
        self.reject_writes = False
        self.disconnected_callback = None

    # bleak API ------------------------------------------------------------
    async def start_notify(self, _uuid, cb) -> None:
        self._cb = cb

    async def disconnect(self) -> None:
        self.is_connected = False

    async def write_gatt_char(self, _uuid, data: bytes, response: bool = True) -> None:
        for frame in self.asm.feed(bytes(data)):
            assert frame.crc_ok
            self.writes.append(frame)
            asyncio.get_running_loop().call_soon(self._respond, frame)

    # device behaviour -------------------------------------------------------
    def indicate(self, cmd: int, payload: bytes = b"", pid: int = 1) -> None:
        raw = p.build_frame(cmd, payload, pid)
        for i in range(0, len(raw), self.chunk):
            self._cb(None, bytearray(raw[i : i + self.chunk]))

    def enc(self, data: bytes) -> bytes:
        return p.aes_encrypt(self.session_key, self.iv, data)

    def _respond(self, f: p.Frame) -> None:
        if f.cmd == p.CMD_RANDOM_REQ:
            self.indicate(p.CMD_RANDOM_RESP, p.encode_bytes(1, RANDOM.encode()), f.packet_id)
        elif f.cmd == p.CMD_LOGIN_REQ:
            ok = f.payload == p.login_payload(self.key, RANDOM)
            self.indicate(p.CMD_LOGIN_RESP, p.encode_int(1, 0 if ok else 1), f.packet_id)
        elif f.cmd == p.CMD_READ_STATUS:
            self.indicate(p.CMD_STATUS_ACK, self.enc(STATUS_A), f.packet_id)
            self.indicate(p.CMD_REPORT_PROPS, self.enc(STATUS_B), f.packet_id)
        elif f.cmd == p.CMD_WRITE_PROPS:
            plain = p.aes_decrypt(self.session_key, self.iv, f.payload)
            self.props.update(p.fields_to_dict(p.decode_fields(plain)))
            self.indicate(p.CMD_WRITE_RESULT, p.encode_int(1, 1 if self.reject_writes else 0), f.packet_id)
