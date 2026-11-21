"""Protocol layer: framing, stuffing, TTLV, crypto."""
import pytest

from custom_components.pecron_ble import protocol as p


@pytest.mark.parametrize(
    "body",
    [b"", b"\xaa", b"\xaa\x55", b"\xaa\xaa", b"\xaa\xaa\xaa", b"\x01\xaa\x55\xaa\x02", bytes(range(256))],
)
def test_stuff_roundtrip(body):
    raw = p.SYNC + body
    stuffed = p.stuff(raw)
    assert p.unstuff(stuffed) == raw
    # Sync word never re-appears inside a stuffed body.
    assert p.SYNC not in stuffed[2:]


def test_known_read_frame_bytes():
    # 0x0011 with pid 1: inner = 00 01 00 11, crc = 0x12, len = 5
    assert p.build_frame(p.CMD_READ_STATUS, b"", 1).hex() == "aaaa00051200010011"


def test_frame_roundtrip_with_stuffing_in_header():
    # pid 0xAAAA forces stuffing inside the header region.
    raw = p.build_frame(0x0014, b"\xaa\x55\x00", 0xAAAA)
    f = p.parse_frame(raw)
    assert (f.cmd, f.packet_id, f.payload, f.crc_ok) == (0x0014, 0xAAAA, b"\xaa\x55\x00", True)


def test_assembler_byte_at_a_time_and_multiple_frames():
    a = p.build_frame(0x7033, p.encode_bytes(1, b"r" * 16), 3)
    b = p.build_frame(0x0014, b"\xaa\xaa\xaa", 0xAA55)
    asm = p.FrameAssembler()
    out = []
    for byte in b"\x00\x13" + a + b:  # leading garbage too
        out += asm.feed(bytes([byte]))
    assert [(f.cmd, f.packet_id) for f in out] == [(0x7033, 3), (0x0014, 0xAA55)]
    assert out[1].payload == b"\xaa\xaa\xaa"


def test_assembler_flags_bad_crc():
    raw = bytearray(p.build_frame(0x0011, b"\x01", 1))
    raw[4] ^= 0xFF  # corrupt CRC byte
    (f,) = p.FrameAssembler().feed(bytes(raw))
    assert not f.crc_ok


@pytest.mark.parametrize(("value", "expected"), [(0, "0000"), (3, "0003"), (25, "0019"), (255, "00ff"), (256, "010100")])
def test_encode_number_matches_issue_112(value, expected):
    assert p.encode_number(value).hex() == expected


def test_encode_int_tag_matches_issue_112():
    assert p.encode_int(51, 3).hex() == "019a0003"


def test_number_decimals_and_sign_roundtrip():
    payload = p.encode_int(2, 120.1, 1) + p.encode_int(3, -0.98, 2) + p.encode_int(4, 70000)
    vals = p.fields_to_dict(p.decode_fields(payload))
    assert vals == {2: 120.1, 3: -0.98, 4: 70000}


def test_bool_and_struct_and_repeated_struct():
    struct = lambda fid, n: ((fid << 3) | 4).to_bytes(2, "big") + n.to_bytes(2, "big")  # noqa: E731
    payload = (
        p.encode_bool(40, True)
        + struct(35, 2) + p.encode_int(1, 90) + p.encode_int(2, 51.2, 1)
        + struct(36, 1) + p.encode_int(2, 80)
        + struct(36, 1) + p.encode_int(2, 70)
        + p.encode_bool(38, False)
    )
    assert p.fields_to_dict(p.decode_fields(payload)) == {
        40: True, 35: {1: 90, 2: 51.2}, 36: [{2: 80}, {2: 70}], 38: False,
    }


def test_decode_rejects_truncated():
    with pytest.raises(p.ProtocolError):
        p.decode_fields(p.encode_int(1, 300)[:-1])


def test_aes_roundtrip_and_wrong_key():
    key, iv = bytes(16), p.iv_from_random("short")
    assert iv == b"short" + bytes(11)
    ct = p.aes_encrypt(key, iv, b"hello")
    assert p.aes_decrypt(key, iv, ct) == b"hello"
    with pytest.raises(p.ProtocolError):
        p.aes_decrypt(b"\x01" * 16, iv, ct)


def test_login_payload_is_sha256_hex_in_field_2():
    payload = p.login_payload(bytes(16), "R")
    (f,) = p.decode_fields(payload)
    assert f.id == 2 and len(f.value) == 64
    assert len(p.build_frame(p.CMD_LOGIN_REQ, payload)) >= 77  # needs long write


@pytest.mark.parametrize(
    ("payload", "ok"),
    [(b"", None), (p.encode_int(1, 0), True), (p.encode_int(1, 1), False), (b"\xff", False)],
)
def test_result_ok(payload, ok):
    assert p.result_ok(payload) is ok
