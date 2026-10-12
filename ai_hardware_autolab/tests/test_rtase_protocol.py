"""Offline tests for the BBB RTASE two-bit buffer protocol."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "BeagleBone-Black" / "host" / "rtase.py"
PROVISION_SOURCE = SOURCE.with_name("provision_rtase.py")
SPEC = importlib.util.spec_from_file_location("bbb_rtase", SOURCE)
assert SPEC and SPEC.loader
rtase = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rtase)


class FakeRpmsg:
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.total = 0

    def request(self, payload: bytes, timeout_s: float = 5.0) -> bytes:
        del timeout_s
        self.requests.append(payload)
        if payload[0] == rtase.CMD_BUFFER_RESET:
            self.total = 0
            return bytes([payload[0], 1, 0, 0])
        if payload[0] == rtase.CMD_BUFFER_APPEND:
            count = struct.unpack_from("<H", payload, 1)[0]
            assert len(payload) == 3 + (count + 3) // 4
            self.total += count
            return bytes([payload[0], 1]) + struct.pack("<H", self.total)
        if payload[0] == rtase.CMD_RUN:
            shots = struct.unpack_from("<I", payload, 9)[0]
            return bytes([payload[0], 1]) + struct.pack("<I", shots) + bytes([3])
        raise AssertionError(f"unexpected command {payload[0]:#x}")


@pytest.mark.parametrize(
    "symbols",
    ([0], [3, 2, 1, 0], [0, 1, 2, 3, 0], list(range(4)) * 17),
)
def test_two_bit_pack_round_trip(symbols: list[int]) -> None:
    packed = rtase.pack_symbols(symbols)
    assert len(packed) == (len(symbols) + 3) // 4
    assert rtase.unpack_symbols(packed, len(symbols)) == symbols


def test_invalid_symbol_is_rejected() -> None:
    with pytest.raises(ValueError, match="two-bit"):
        rtase.pack_symbols([0, 4])


def test_large_sequence_is_chunked_below_rpmsg_limit() -> None:
    link = FakeRpmsg()
    symbols = [index & 3 for index in range(10_000)]
    assert rtase.load_sequence(link, symbols) == 10_000
    assert link.requests[0] == bytes([rtase.CMD_BUFFER_RESET])
    assert all(len(request) <= 496 for request in link.requests[1:])
    assert len(link.requests) > 2


def test_seeded_random_sequence_is_reproducible() -> None:
    a = rtase.make_sequence("random", 100, 1234)
    b = rtase.make_sequence("random", 100, 1234)
    c = rtase.make_sequence("random", 100, 1235)
    assert a == b
    assert a != c
    assert set(a) <= {0, 1, 2, 3}


def test_delay_requires_five_ns_grid() -> None:
    assert rtase.ns_to_cycles(100) == 20
    with pytest.raises(ValueError, match="multiple of 5"):
        rtase.ns_to_cycles(101)


def test_version_reply_uses_legacy_ascii_format_without_ack_byte() -> None:
    reply = bytes([rtase.CMD_VERSION]) + b"bbb_pulse_io v1.9-rtase"
    assert rtase.validate_reply(bytes([rtase.CMD_VERSION]), reply) == reply


def test_extension_reply_still_requires_success_ack() -> None:
    with pytest.raises(rtase.RtaseError, match="rejected command"):
        rtase.validate_reply(bytes([rtase.CMD_STATUS]), bytes([rtase.CMD_STATUS, 0xEF]))


def test_rtase_provision_releases_all_physically_connected_hdmi_pins() -> None:
    source = PROVISION_SOURCE.read_text(encoding="utf-8")
    assert "disable_uboot_overlay_audio=1" in source
    assert "disable_uboot_overlay_video=1" in source


def test_run_message_layout_and_reply() -> None:
    link = FakeRpmsg()
    result = rtase.run_sequence(
        link, delay_ns=100, pulse_ns=20, gap_us=100_000, shots=25
    )
    request = link.requests[-1]
    assert request[0] == rtase.CMD_RUN
    assert struct.unpack_from("<HHII", request, 1) == (20, 4, 20_000_000, 25)
    assert result["shots_completed"] == 25
    assert result["last_symbol"] == 3
