#!/usr/bin/env python3
"""Set only the five reviewed RTASE diagnostic return pins to GPIO input."""

from __future__ import annotations

import json
import mmap
import os
import struct


CONTROL_BASE = 0x44E10000
CONTROL_SIZE = 0x1000
GPIO_INPUT_MODE7 = 0x27
PINS = {
    "P8_13": 0x824,
    "P8_14": 0x828,
    "P8_15": 0x83C,
    "P8_16": 0x838,
    "P8_18": 0x88C,
}
ALLOWED_CURRENT_VALUES = {0x30, GPIO_INPUT_MODE7}


def read32(memory: mmap.mmap, offset: int) -> int:
    return struct.unpack_from("<I", memory, offset)[0]


def write32(memory: mmap.mmap, offset: int, value: int) -> None:
    struct.pack_into("<I", memory, offset, value)


def main() -> int:
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        memory = mmap.mmap(
            fd,
            CONTROL_SIZE,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
            offset=CONTROL_BASE,
        )
        try:
            before = {name: read32(memory, offset) for name, offset in PINS.items()}
            unexpected = {
                name: value
                for name, value in before.items()
                if value not in ALLOWED_CURRENT_VALUES
            }
            if unexpected:
                raise RuntimeError(f"refusing unexpected pinmux values: {unexpected}")
            for offset in PINS.values():
                write32(memory, offset, GPIO_INPUT_MODE7)
            after = {name: read32(memory, offset) for name, offset in PINS.items()}
            if set(after.values()) != {GPIO_INPUT_MODE7}:
                raise RuntimeError(f"pinmux readback mismatch: {after}")
        finally:
            memory.close()
    finally:
        os.close(fd)
    print(json.dumps({"before": before, "after": after}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
