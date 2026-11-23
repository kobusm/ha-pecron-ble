"""Quectel (QuecThing) BLE/TCP frame protocol as used by Pecron power stations.

Pure Python, no Home Assistant imports, so it can be unit tested in isolation.

Wire format of one frame (before byte stuffing)::

    AA AA | LEN (u16 BE) | CRC (u8) | PID (u16 BE) | CMD (u16 BE) | PAYLOAD...

* ``LEN`` counts CRC + PID + CMD + PAYLOAD (i.e. everything after LEN).
* ``CRC`` is the 8-bit sum of PID + CMD + PAYLOAD.
* Byte stuffing applies to everything after the ``AA AA`` sync word: whenever
  an ``AA`` is followed by ``55`` or ``AA``, a ``55`` is inserted after the
  ``AA``. The receiver drops a ``55`` that directly follows an ``AA``. This
  guarantees ``AA AA`` only ever appears as the sync word.

Payloads are TTLV: a 16-bit tag word ``(id << 3) | type`` followed by a
type-specific value. After login, data payloads are AES-128-CBC (PKCS7)
encrypted with the device auth key, using the login random string as IV.

Protocol reverse-engineered by attractify-logan/pecron-monitor (MIT); the
number encoder follows pecron-monitor issue #112.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import struct
from typing import Any

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SYNC = b"\xaa\xaa"

# Command words
CMD_READ_STATUS = 0x0011  # app -> device: request full status
CMD_STATUS_ACK = 0x0012  # device -> app: response to 0x0011 (may carry data)
CMD_WRITE_PROPS = 0x0013  # app -> device: set TSL properties
CMD_REPORT_PROPS = 0x0014  # device -> app: TSL property report
CMD_RANDOM_REQ = 0x7032  # app -> device: request login random
CMD_RANDOM_RESP = 0x7033  # device -> app: random string (field 1)
CMD_LOGIN_REQ = 0x7034  # app -> device: SHA-256 login hash (field 2)
CMD_LOGIN_RESP = 0x7035  # device -> app: login result
CMD_WRITE_RESULT = 0x7036  # device -> app: result of a 0x0013 write

# TTLV value types
T_BOOL_FALSE = 0
T_BOOL_TRUE = 1
T_NUMBER = 2
T_BYTES = 3
T_STRUCT = 4
T_STRING = 5


class ProtocolError(Exception):
    """Malformed frame or payload."""


# ---------------------------------------------------------------------------
# Byte stuffing and frames
# ---------------------------------------------------------------------------


def stuff(raw: bytes) -> bytes:
    """Apply byte stuffing to a frame (sync word is left untouched)."""
    out = bytearray(raw[:2])
    body = raw[2:]
    for i, b in enumerate(body):
        out.append(b)
        if b == 0xAA and i + 1 < len(body) and body[i + 1] in (0x55, 0xAA):
            out.append(0x55)
    return bytes(out)


def unstuff(raw: bytes) -> bytes:
    """Reverse :func:`stuff`."""
    out = bytearray(raw[:2])
    prev_aa = False
    for b in raw[2:]:
        if prev_aa and b == 0x55:
            prev_aa = False
            continue
        out.append(b)
        prev_aa = b == 0xAA
    return bytes(out)


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_frame(cmd: int, payload: bytes = b"", packet_id: int = 1) -> bytes:
    inner = struct.pack(">HH", packet_id & 0xFFFF, cmd) + payload
    raw = SYNC + struct.pack(">HB", len(inner) + 1, checksum(inner)) + inner
    return stuff(raw)


@dataclass(slots=True)
class Frame:
    cmd: int
    packet_id: int
    payload: bytes
    crc_ok: bool = True


class FrameAssembler:
    """Reassemble frames from a stream of BLE indication / TCP chunks.

    Indications are at most MTU-3 bytes, so one frame can span several
    callbacks and one callback can carry the tail of one frame and the head of
    the next. Feed every chunk in; complete frames come out.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[Frame]:
        self._buf.extend(chunk)
        frames: list[Frame] = []
        while True:
            start = self._buf.find(SYNC)
            if start < 0:
                # Keep a trailing AA, it may be the first half of a sync word.
                del self._buf[: max(0, len(self._buf) - 1)]
                return frames
            if start:
                del self._buf[:start]
            end, body = self._scan(self._buf)
            if end is None:
                return frames
            del self._buf[:end]
            frame = _decode_body(body)
            if frame is not None:
                frames.append(frame)

    @staticmethod
    def _scan(buf: bytearray) -> tuple[int | None, bytes]:
        """Return (stuffed end index, unstuffed body after sync) or (None, b"")."""
        body = bytearray()
        needed = 2
        prev_aa = False
        i = 2
        while len(body) < needed:
            if i >= len(buf):
                return None, b""
            b = buf[i]
            i += 1
            if prev_aa and b == 0x55:
                prev_aa = False
                continue
            body.append(b)
            prev_aa = b == 0xAA
            if needed == 2 and len(body) == 2:
                needed = 2 + struct.unpack(">H", bytes(body))[0]
        # A stuffing byte may trail the final AA of the frame; swallow it.
        if prev_aa and i < len(buf) and buf[i] == 0x55:
            i += 1
        return i, bytes(body)


def _decode_body(body: bytes) -> Frame | None:
    # body = LEN(2) CRC(1) PID(2) CMD(2) PAYLOAD
    if len(body) < 7:
        return None
    crc = body[2]
    inner = body[3:]
    pid, cmd = struct.unpack(">HH", inner[:4])
    return Frame(cmd=cmd, packet_id=pid, payload=bytes(inner[4:]), crc_ok=checksum(inner) == crc)


def parse_frame(raw: bytes) -> Frame:
    """Parse exactly one stuffed frame."""
    frames = FrameAssembler().feed(raw)
    if not frames:
        raise ProtocolError(f"no complete frame in {raw.hex()}")
    return frames[0]


# ---------------------------------------------------------------------------
# TTLV
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Field:
    id: int
    type: int
    value: Any
    children: list[Field] = field(default_factory=list)


def encode_number(value: int | float, decimals: int = 0) -> bytes:
    """TTLV number value: meta ``(sign<<7)|(decimals<<3)|(len-1)`` + BE magnitude."""
    if not 0 <= decimals <= 15:
        raise ValueError("decimals must be 0..15")
    scaled = round(abs(value) * (10**decimals))
    length = max(1, (scaled.bit_length() + 7) // 8)
    if length > 8:
        raise ValueError("number too large")
    meta = ((1 if value < 0 else 0) << 7) | (decimals << 3) | (length - 1)
    return bytes([meta]) + scaled.to_bytes(length, "big")


def _tag(field_id: int, ftype: int) -> bytes:
    if not 1 <= field_id <= 0x1FFF:
        raise ValueError(f"field id {field_id} out of range")
    return struct.pack(">H", (field_id << 3) | ftype)


def encode_bool(field_id: int, value: bool) -> bytes:
    return _tag(field_id, T_BOOL_TRUE if value else T_BOOL_FALSE)


def encode_int(field_id: int, value: int | float, decimals: int = 0) -> bytes:
    return _tag(field_id, T_NUMBER) + encode_number(value, decimals)


def encode_bytes(field_id: int, data: bytes) -> bytes:
    return _tag(field_id, T_BYTES) + struct.pack(">H", len(data)) + data


def decode_fields(payload: bytes) -> list[Field]:
    """Decode a TTLV payload into a tree of :class:`Field`.

    Structs carry an element count; their children are the following
    ``count`` fields (recursively).
    """
    fields, _ = _decode(payload, 0, None)
    return fields


def _decode(buf: bytes, pos: int, count: int | None) -> tuple[list[Field], int]:
    out: list[Field] = []
    while pos + 2 <= len(buf) and (count is None or len(out) < count):
        tag = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
        fid, ftype = tag >> 3, tag & 0x07
        if ftype in (T_BOOL_FALSE, T_BOOL_TRUE):
            out.append(Field(fid, ftype, ftype == T_BOOL_TRUE))
        elif ftype == T_NUMBER:
            if pos >= len(buf):
                raise ProtocolError("truncated number meta")
            meta = buf[pos]
            pos += 1
            n = (meta & 0x07) + 1
            if pos + n > len(buf):
                raise ProtocolError("truncated number")
            mag = int.from_bytes(buf[pos : pos + n], "big")
            pos += n
            decimals = (meta >> 3) & 0x0F
            val: int | float = mag / (10**decimals) if decimals else mag
            out.append(Field(fid, ftype, -val if meta & 0x80 else val))
        elif ftype in (T_BYTES, T_STRING):
            if pos + 2 > len(buf):
                raise ProtocolError("truncated length")
            n = struct.unpack_from(">H", buf, pos)[0]
            pos += 2
            if pos + n > len(buf):
                raise ProtocolError("truncated bytes")
            out.append(Field(fid, ftype, bytes(buf[pos : pos + n])))
            pos += n
        elif ftype == T_STRUCT:
            if pos + 2 > len(buf):
                raise ProtocolError("truncated struct count")
            n = struct.unpack_from(">H", buf, pos)[0]
            pos += 2
            children, pos = _decode(buf, pos, n)
            out.append(Field(fid, ftype, None, children))
        else:
            raise ProtocolError(f"unknown TTLV type {ftype} for id {fid}")
    return out, pos


def fields_to_dict(fields: list[Field]) -> dict[int, Any]:
    """Flatten a field tree into ``{id: value}`` with structs as nested dicts.

    Repeated struct ids (e.g. one per expansion battery) become lists.
    """
    out: dict[int, Any] = {}
    for f in fields:
        if f.type == T_STRUCT:
            val: Any = fields_to_dict(f.children)
        elif f.type in (T_BYTES, T_STRING):
            try:
                val = f.value.decode("utf-8")
            except UnicodeDecodeError:
                val = f.value.hex()
        else:
            val = f.value
        if f.id in out and f.type == T_STRUCT:
            prev = out[f.id]
            out[f.id] = (prev if isinstance(prev, list) else [prev]) + [val]
        else:
            out[f.id] = val
    return out


# ---------------------------------------------------------------------------
# Crypto / login
# ---------------------------------------------------------------------------


def iv_from_random(random_str: str) -> bytes:
    iv = random_str.encode("utf-8")
    return iv[:16].ljust(16, b"\x00")


def aes_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ProtocolError("ciphertext not a multiple of 16 bytes")
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(data) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as err:
        raise ProtocolError("bad padding (wrong auth key?)") from err


def login_payload(auth_key: bytes, random_str: str) -> bytes:
    digest = hashlib.sha256(f"{auth_key.hex()};{random_str}".encode()).hexdigest()
    return encode_bytes(2, digest.encode())


def random_from_frame(frame: Frame) -> str | None:
    """Extract the login random string from a 0x7033 frame."""
    try:
        for f in decode_fields(frame.payload):
            if f.id == 1 and f.type in (T_BYTES, T_STRING):
                return f.value.decode("utf-8")
    except (ProtocolError, UnicodeDecodeError):
        return None
    return None


def result_ok(payload: bytes) -> bool | None:
    """Interpret a 0x7035 / 0x7036 result payload.

    Success is a single number field equal to 0. The field id differs by
    command: 0x7036 uses id 1 (pecron-monitor #112), while the E300LFP
    answers a good login (0x7035) with id 3 = 0. An empty payload is treated
    as "unknown" (None) because some firmware sends a bare 0x7035 on
    successful login.
    """
    if not payload:
        return None
    try:
        fields = decode_fields(payload)
    except ProtocolError:
        return False
    return len(fields) == 1 and fields[0].type == T_NUMBER and fields[0].value == 0
