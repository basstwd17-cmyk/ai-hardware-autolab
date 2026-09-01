#!/usr/bin/env python3
"""Configure and read back RTASE CPLD CODE0..CODE3 over BBB SPI0."""

from __future__ import annotations

import argparse
import array
import ctypes
from contextlib import contextmanager
import json
import os
import shlex
import struct
import sys
import mmap
import time
from typing import Protocol, Sequence


DEFAULT_CODES = (0x0000, 0x1555, 0x2AAA, 0x3FFF)
CONTROL_LOCK = "/run/lock/rtase-control.lock"


class SpiError(RuntimeError):
    pass


@contextmanager
def control_lock():
    """Serialize CODE access with PRU sequence loading and execution."""
    import fcntl

    fd = os.open(CONTROL_LOCK, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Transfer(Protocol):
    def transfer(self, tx: bytes) -> bytes: ...


class SpiIocTransfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf", ctypes.c_uint64),
        ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16),
        ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8),
        ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


def _ioc(direction: int, kind: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (kind << 8) | number


SPI_IOC_WR_MODE = _ioc(1, ord("k"), 1, 1)
SPI_IOC_WR_BITS_PER_WORD = _ioc(1, ord("k"), 3, 1)
SPI_IOC_WR_MAX_SPEED_HZ = _ioc(1, ord("k"), 4, 4)
SPI_IOC_MESSAGE_1 = _ioc(1, ord("k"), 0, ctypes.sizeof(SpiIocTransfer))


class LinuxSpi:
    def __init__(self, device: str = "/dev/spidev0.0", speed_hz: int = 1_000_000) -> None:
        self.device = device
        self.speed_hz = speed_hz

    def transfer(self, tx: bytes) -> bytes:
        import fcntl

        if not tx:
            return b""
        fd = os.open(self.device, os.O_RDWR)
        try:
            fcntl.ioctl(fd, SPI_IOC_WR_MODE, array.array("B", [0]))
            fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, array.array("B", [8]))
            fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, array.array("I", [self.speed_hz]))
            tx_buffer = ctypes.create_string_buffer(tx, len(tx))
            rx_buffer = ctypes.create_string_buffer(len(tx))
            message = SpiIocTransfer(
                tx_buf=ctypes.addressof(tx_buffer),
                rx_buf=ctypes.addressof(rx_buffer),
                len=len(tx),
                speed_hz=self.speed_hz,
                bits_per_word=8,
            )
            fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, bytes(message))
            return bytes(rx_buffer.raw)
        finally:
            os.close(fd)


class GpioSpi:
    """Mode-0 GPIO fallback for the RTASE GPIO-SPI boot overlay.

    This deliberately uses the AM335x GPIO registers rather than sysfs: a
    frame keeps CS asserted across all three bytes and samples MISO immediately
    before the rising edge.  It is not a substitute for the PRU at high rates;
    it is the deterministic, verified path while McSPI RX is unavailable.
    """

    CONTROL_BASE = 0x44E10000
    GPIO0_BASE = 0x44E07000
    PAGE = 0x1000
    GPIO_OE, GPIO_DATAIN, GPIO_DATAOUT = 0x134, 0x138, 0x13C
    GPIO_CLEARDATAOUT, GPIO_SETDATAOUT = 0x190, 0x194
    PADS = {"sclk": 0x950, "miso": 0x954, "mosi": 0x958, "cs": 0x95C}
    GPIO_PADS = {"sclk": 0x07, "miso": 0x27, "mosi": 0x07, "cs": 0x07}
    CS, MOSI, MISO, SCLK = 1 << 5, 1 << 4, 1 << 3, 1 << 2
    OUTPUT_MASK = CS | MOSI | SCLK

    def __init__(self, half_period_us: float = 20.0) -> None:
        if half_period_us < 10.0:
            raise ValueError("GPIO SPI half period must be at least 10 us.")
        self.half_period_s = half_period_us / 1_000_000

    @staticmethod
    def _read32(region: mmap.mmap, offset: int) -> int:
        return struct.unpack_from("<I", region, offset)[0]

    @staticmethod
    def _write32(region: mmap.mmap, offset: int, value: int) -> None:
        struct.pack_into("<I", region, offset, value & 0xFFFFFFFF)

    def _set(self, gpio: mmap.mmap, bits: int) -> None:
        self._write32(gpio, self.GPIO_SETDATAOUT, bits)

    def _clear(self, gpio: mmap.mmap, bits: int) -> None:
        self._write32(gpio, self.GPIO_CLEARDATAOUT, bits)

    def transfer(self, tx: bytes) -> bytes:
        if not tx:
            return b""
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        control = gpio = None
        try:
            control = mmap.mmap(fd, self.PAGE, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE,
                                offset=self.CONTROL_BASE)
            gpio = mmap.mmap(fd, self.PAGE, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE,
                             offset=self.GPIO0_BASE)
            pads = {name: self._read32(control, offset) for name, offset in self.PADS.items()}
            if pads != self.GPIO_PADS:
                raise SpiError(
                    "GPIO transport requires BB-RTASE-A-GPIO-SPI-00A0 "
                    f"(observed padmux {pads})"
                )
            before_oe = self._read32(gpio, self.GPIO_OE)
            before_out = self._read32(gpio, self.GPIO_DATAOUT)
            try:
                # Make every generated signal idle before asserting CS.
                self._set(gpio, self.CS)
                self._clear(gpio, self.MOSI | self.SCLK)
                self._write32(
                    gpio, self.GPIO_OE,
                    (before_oe & ~self.OUTPUT_MASK) | self.MISO,
                )
                time.sleep(self.half_period_s)
                self._clear(gpio, self.CS)
                rx = bytearray()
                for byte in tx:
                    received = 0
                    for bit in range(7, -1, -1):
                        if (byte >> bit) & 1:
                            self._set(gpio, self.MOSI)
                        else:
                            self._clear(gpio, self.MOSI)
                        time.sleep(self.half_period_s)
                        # The slave's nonblocking state advances immediately
                        # after the rising edge.  GPIO software must therefore
                        # sample the bit that was stable *before* that edge;
                        # hardware McSPI performs this at the edge itself.
                        received = (received << 1) | (
                            (self._read32(gpio, self.GPIO_DATAIN) & self.MISO) >> 3
                        )
                        self._set(gpio, self.SCLK)
                        time.sleep(self.half_period_s)
                        self._clear(gpio, self.SCLK)
                        time.sleep(self.half_period_s)
                    rx.append(received)
                return bytes(rx)
            finally:
                # CS is deasserted before restoring every GPIO register.
                self._set(gpio, self.CS)
                self._clear(gpio, self.MOSI | self.SCLK)
                self._set(gpio, before_out & self.OUTPUT_MASK)
                self._clear(gpio, (~before_out) & self.OUTPUT_MASK)
                self._write32(gpio, self.GPIO_OE, before_oe)
        finally:
            if gpio is not None:
                gpio.close()
            if control is not None:
                control.close()
            os.close(fd)


def validate_address(address: int) -> int:
    if address not in range(4):
        raise ValueError("CODE register address must be 0..3.")
    return address


def validate_code(code: int) -> int:
    if not 0 <= code <= 0x3FFF:
        raise ValueError("DAC code must be a 14-bit value (0x0000..0x3fff).")
    return code


def write_frame(address: int, code: int) -> bytes:
    validate_address(address)
    validate_code(code)
    return bytes([address, (code >> 8) & 0x3F, code & 0xFF])


def read_frame(address: int) -> bytes:
    validate_address(address)
    return bytes([0x80 | address, 0, 0])


class CodeRegisters:
    def __init__(self, bus: Transfer) -> None:
        self.bus = bus

    def write(self, address: int, code: int) -> None:
        reply = self.bus.transfer(write_frame(address, code))
        if len(reply) != 3:
            raise SpiError(f"Short SPI write transfer: {reply.hex()}")

    def read(self, address: int) -> int:
        reply = self.bus.transfer(read_frame(address))
        if len(reply) != 3:
            raise SpiError(f"Short SPI read transfer: {reply.hex()}")
        return ((reply[1] & 0x3F) << 8) | reply[2]

    def write_and_verify(self, codes: Sequence[int]) -> list[int]:
        if len(codes) != 4:
            raise ValueError("Exactly four CODE values are required.")
        for address, code in enumerate(codes):
            self.write(address, validate_code(code))
        observed = [self.read(address) for address in range(4)]
        if observed != list(codes):
            raise SpiError(
                "CODE readback mismatch: wrote "
                f"{[hex(v) for v in codes]}, read {[hex(v) for v in observed]}"
            )
        return observed


def direct_command(args: argparse.Namespace) -> dict[str, object]:
    with control_lock():
        if args.transport == "gpio":
            bus: Transfer = GpioSpi(args.half_period_us)
        else:
            bus = LinuxSpi(args.device, args.speed_hz)
        registers = CodeRegisters(bus)
        if args.command == "read":
            addresses = range(4) if args.address is None else (args.address,)
            values = {f"CODE{address}": registers.read(address) for address in addresses}
            return {"ok": True, "codes": values}
        if args.command == "write":
            registers.write(args.address, args.code)
            observed = registers.read(args.address)
            if observed != args.code:
                raise SpiError(f"CODE{args.address} readback {observed:#06x} != {args.code:#06x}")
            return {"ok": True, "address": args.address, "written": args.code, "readback": observed}
        codes = tuple(args.codes) if args.codes else DEFAULT_CODES
        observed = registers.write_and_verify(codes)
        return {"ok": True, "written": list(codes), "readback": observed, "bit_errors": 0}


def remote_command(args: argparse.Namespace, argv: Sequence[str]) -> int:
    try:
        from provision import connect, run
    except ModuleNotFoundError as exc:
        raise SpiError("Install host requirements: python -m pip install -r requirements.txt") from exc
    remote_args = [
        "--local", "--device", args.device, "--speed-hz", str(args.speed_hz),
        "--transport", args.transport, "--half-period-us", str(args.half_period_us), *argv,
    ]
    launch = "python3 ~/pru/rtase_spi.py " + " ".join(map(shlex.quote, remote_args))
    command = ('echo "$BB_PW" | sudo -S -p "" ' + launch) if args.transport == "gpio" else launch
    client = connect(host=args.host)
    try:
        rc, out, err = run(client, command)
    finally:
        client.close()
    if out:
        sys.stdout.write(out)
    if err.strip():
        sys.stderr.write(err)
    return rc


def auto_int(text: str) -> int:
    return int(text, 0)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("BB_HOST", "192.168.50.2"))
    p.add_argument("--device", default="/dev/spidev0.0")
    p.add_argument("--speed-hz", type=int, default=1_000_000)
    p.add_argument("--transport", choices=("spi", "gpio"), default="spi")
    p.add_argument("--half-period-us", type=float, default=20.0,
                   help="GPIO transport half-period (minimum 10 us)")
    p.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True)
    read = sub.add_parser("read")
    read.add_argument("address", nargs="?", type=int)
    write = sub.add_parser("write")
    write.add_argument("address", type=int)
    write.add_argument("code", type=auto_int)
    verify = sub.add_parser("set-verify")
    verify.add_argument("codes", nargs="*", type=auto_int)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(raw)
    try:
        if not args.local:
            return remote_command(args, raw)
        print(json.dumps(direct_command(args), indent=2, sort_keys=True))
        return 0
    except (OSError, SpiError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
