"""Offline tests for the RTASE CPLD SPI register protocol."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "BeagleBone-Black" / "host" / "rtase_spi.py"
SPEC = importlib.util.spec_from_file_location("bbb_rtase_spi", SOURCE)
assert SPEC and SPEC.loader
spi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spi)


class FakeBus:
    def __init__(self) -> None:
        self.codes = list(spi.DEFAULT_CODES)
        self.frames: list[bytes] = []

    def transfer(self, tx: bytes) -> bytes:
        self.frames.append(tx)
        address = tx[0] & 3
        if tx[0] & 0x80:
            code = self.codes[address]
            return bytes([0, (code >> 8) & 0x3F, code & 0xFF])
        self.codes[address] = ((tx[1] & 0x3F) << 8) | tx[2]
        return bytes(3)


def test_frame_layout() -> None:
    assert spi.write_frame(2, 0x2AAA) == bytes([0x02, 0x2A, 0xAA])
    assert spi.read_frame(2) == bytes([0x82, 0, 0])


@pytest.mark.parametrize("bad", (-1, 4))
def test_register_address_is_two_bits(bad: int) -> None:
    with pytest.raises(ValueError, match="0..3"):
        spi.read_frame(bad)


@pytest.mark.parametrize("bad", (-1, 0x4000))
def test_code_is_fourteen_bits(bad: int) -> None:
    with pytest.raises(ValueError, match="14-bit"):
        spi.write_frame(0, bad)


def test_all_four_registers_write_and_read_back() -> None:
    bus = FakeBus()
    registers = spi.CodeRegisters(bus)
    codes = [0x0001, 0x0123, 0x2345, 0x3FFE]
    assert registers.write_and_verify(codes) == codes
    assert bus.codes == codes
    assert bus.frames == [
        spi.write_frame(0, codes[0]),
        spi.write_frame(1, codes[1]),
        spi.write_frame(2, codes[2]),
        spi.write_frame(3, codes[3]),
        spi.read_frame(0),
        spi.read_frame(1),
        spi.read_frame(2),
        spi.read_frame(3),
    ]


def test_readback_mismatch_fails() -> None:
    class BrokenBus(FakeBus):
        def transfer(self, tx: bytes) -> bytes:
            reply = super().transfer(tx)
            return bytes([0, 0, 0]) if tx[0] & 0x80 else reply

    with pytest.raises(spi.SpiError, match="mismatch"):
        spi.CodeRegisters(BrokenBus()).write_and_verify([1, 2, 3, 4])


def test_gpio_transport_refuses_unreviewed_timing() -> None:
    with pytest.raises(ValueError, match="at least 10 us"):
        spi.GpioSpi(9.9)
