#!/usr/bin/env python3
"""Slow, scope-friendly GPIO SPI test for the RTASE CPLD return path.

Requires BB-RTASE-A-GPIO-SPI-00A0.  It uses Linux's legacy GPIO sysfs API
only for diagnostic proof, not timing-critical operation.  With the current
``gate8-spi-standard-return-static-high`` SRAM image every received bit must
be one, so rx must be ``ff ff ff ff ff ff``.
"""

from __future__ import annotations

import argparse
import os
import time


GPIO_CS = 5       # P9_17 / GPIO0_5
GPIO_MOSI = 4     # P9_18 / GPIO0_4
GPIO_MISO = 99    # P9_21 / GPIO3_3
GPIO_CLK = 98     # P9_22 / GPIO3_2
GPIO_ROOT = "/sys/class/gpio"


def write(path: str, value: str) -> None:
    with open(path, "w", encoding="ascii") as file:
        file.write(value)


def configure(number: int, direction: str, initial: int | None = None) -> str:
    path = f"{GPIO_ROOT}/gpio{number}"
    if not os.path.isdir(path):
        write(f"{GPIO_ROOT}/export", str(number))
        for _ in range(100):
            if os.path.isdir(path):
                break
            time.sleep(0.01)
        else:
            raise RuntimeError(f"GPIO{number} did not appear")
    if initial is None:
        write(f"{path}/direction", direction)
    else:
        write(f"{path}/direction", f"{direction}{initial}")
    return path


def set_value(path: str, value: int) -> None:
    write(f"{path}/value", str(value))


def get_value(path: str) -> int:
    with open(f"{path}/value", "r", encoding="ascii") as file:
        return int(file.read().strip())


def transfer(tx: bytes, half_period_s: float) -> bytes:
    cs = configure(GPIO_CS, "out", 1)
    mosi = configure(GPIO_MOSI, "out", 0)
    miso = configure(GPIO_MISO, "in")
    clk = configure(GPIO_CLK, "out", 0)
    rx = bytearray()
    set_value(cs, 0)
    for byte in tx:
        received = 0
        for bit in range(7, -1, -1):
            set_value(mosi, (byte >> bit) & 1)
            time.sleep(half_period_s)
            set_value(clk, 1)
            time.sleep(half_period_s)
            received = (received << 1) | get_value(miso)
            set_value(clk, 0)
            time.sleep(half_period_s)
        rx.append(received)
    set_value(cs, 1)
    return bytes(rx)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--half-period-us", type=float, default=100.0)
    parser.add_argument("--tx", default="00 ff aa 55 3c c3")
    args = parser.parse_args()
    if args.half_period_us < 20:
        parser.error("use >=20 us with sysfs GPIO")
    tx = bytes.fromhex(args.tx)
    rx = transfer(tx, args.half_period_us / 1_000_000)
    print(f"tx={tx.hex(' ')}")
    print(f"rx={rx.hex(' ')}")
    return 0 if rx == b"\xff" * len(tx) else 2


if __name__ == "__main__":
    raise SystemExit(main())
