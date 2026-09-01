#!/usr/bin/env python3
"""Full-duplex SPI loopback check for a temporary CPLD echo image."""

from __future__ import annotations

import argparse
import array
import ctypes
import fcntl
import json
import os


class SpiIocTransfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf", ctypes.c_uint64), ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32), ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16), ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8), ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8), ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


def ioc(direction: int, kind: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (kind << 8) | number


SPI_IOC_WR_MODE = ioc(1, ord("k"), 1, 1)
SPI_IOC_WR_BITS_PER_WORD = ioc(1, ord("k"), 3, 1)
SPI_IOC_WR_MAX_SPEED_HZ = ioc(1, ord("k"), 4, 4)
SPI_IOC_MESSAGE_1 = ioc(1, ord("k"), 0, ctypes.sizeof(SpiIocTransfer))


def transfer(device: str, speed_hz: int, payload: bytes, mode: int = 0) -> bytes:
    fd = os.open(device, os.O_RDWR)
    try:
        fcntl.ioctl(fd, SPI_IOC_WR_MODE, array.array("B", [mode]))
        fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, array.array("B", [8]))
        fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, array.array("I", [speed_hz]))
        tx = ctypes.create_string_buffer(payload, len(payload))
        rx = ctypes.create_string_buffer(len(payload))
        message = SpiIocTransfer(
            tx_buf=ctypes.addressof(tx), rx_buf=ctypes.addressof(rx),
            len=len(payload), speed_hz=speed_hz, bits_per_word=8,
        )
        fcntl.ioctl(fd, SPI_IOC_MESSAGE_1, bytes(message))
        return bytes(rx.raw)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/spidev0.0")
    parser.add_argument("--speed-hz", type=int, default=100_000)
    parser.add_argument("--mode", type=int, choices=range(4), default=0)
    args = parser.parse_args()
    tx = bytes.fromhex("00 ff aa 55 3c c3")
    rx = transfer(args.device, args.speed_hz, tx, args.mode)
    report = {
        "tx": tx.hex(), "rx": rx.hex(), "speed_hz": args.speed_hz,
        "mode": args.mode, "ok": rx == tx,
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
