#!/usr/bin/env python3
"""Temporarily detach McSPI0 and read Gate 10 MISO through GPIO0_3.

This is an intentionally bounded diagnostic/fallback: it never reboots or
changes uEnv, it sends only one 48-bit frame, and its finally block restores
the exact pad/GPIO snapshots before rebinding omap2_mcspi.
"""

from __future__ import annotations

import json
import mmap
import os
import struct
import time


CONTROL_BASE = 0x44E10000
GPIO0_BASE = 0x44E07000
PAGE = 0x1000
GPIO_OE, GPIO_DATAIN, GPIO_DATAOUT = 0x134, 0x138, 0x13C
GPIO_CLEARDATAOUT, GPIO_SETDATAOUT = 0x190, 0x194
PADS = {"sclk": 0x950, "miso": 0x954, "mosi": 0x958, "cs": 0x95C}
STANDARD_PADS = {"sclk": 0x08, "miso": 0x28, "mosi": 0x08, "cs": 0x08}
GPIO_PADS = {"sclk": 0x07, "miso": 0x27, "mosi": 0x07, "cs": 0x07}
CS, MOSI, MISO, SCLK = 1 << 5, 1 << 4, 1 << 3, 1 << 2
MASK_OUTPUT = CS | MOSI | SCLK
EXPECTED = bytes.fromhex("a55a3cc3f00f")
DRIVER = "/sys/bus/platform/drivers/omap2_mcspi"
DEVICE = "48030000.spi"


def read32(region: mmap.mmap, offset: int) -> int:
    return struct.unpack_from("<I", region, offset)[0]


def write32(region: mmap.mmap, offset: int, value: int) -> None:
    struct.pack_into("<I", region, offset, value & 0xFFFFFFFF)


def set_bits(region: mmap.mmap, mask: int) -> None:
    write32(region, GPIO_SETDATAOUT, mask)


def clear_bits(region: mmap.mmap, mask: int) -> None:
    write32(region, GPIO_CLEARDATAOUT, mask)


def restore_output(region: mmap.mmap, before: int, mask: int) -> None:
    set_bits(region, before & mask)
    clear_bits(region, (~before) & mask)


def write_sysfs(name: str, value: str) -> None:
    with open(name, "w", encoding="ascii") as stream:
        stream.write(value)


def wait_for(path: str, present: bool, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if os.path.exists(path) == present:
            return
        time.sleep(0.02)
    raise RuntimeError(f"timed out waiting for {path} present={present}")


def main() -> int:
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    control = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                        offset=CONTROL_BASE)
    gpio = mmap.mmap(fd, PAGE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                     offset=GPIO0_BASE)
    before_pads = {name: read32(control, offset) for name, offset in PADS.items()}
    before_oe = read32(gpio, GPIO_OE)
    before_out = read32(gpio, GPIO_DATAOUT)
    unbound = False
    restored = False
    result: dict[str, object] = {
        "test": "gate10_gpio0_3_clocked_return",
        "expected_hex": EXPECTED.hex(),
        "before_pads": before_pads,
    }
    try:
        boot_gpio_mode = before_pads == GPIO_PADS
        if before_pads not in (STANDARD_PADS, GPIO_PADS):
            raise RuntimeError(f"refusing unexpected starting pad state: {before_pads}")
        if not boot_gpio_mode and not os.path.exists(f"{DRIVER}/{DEVICE}"):
            raise RuntimeError("omap2_mcspi is not bound; refusing ambiguous restore state")

        # Boot-GPIO mode is the durable fallback: its DT overlay already owns
        # these pads.  Standard mode retains the bounded detach attempt for
        # diagnosis; it is not a replacement for the boot overlay.
        if not boot_gpio_mode:
            write_sysfs(f"{DRIVER}/unbind", DEVICE)
            unbound = True
            wait_for("/dev/spidev0.0", False)
        set_bits(gpio, CS)
        clear_bits(gpio, MOSI | SCLK)
        write32(gpio, GPIO_OE, (before_oe & ~MASK_OUTPUT) | MISO)
        if not boot_gpio_mode:
            for name, offset in PADS.items():
                write32(control, offset, GPIO_PADS[name])
        applied = {name: read32(control, offset) for name, offset in PADS.items()}
        if applied != GPIO_PADS:
            raise RuntimeError(f"GPIO padmux write did not stick: {applied}")

        # CS high preloads the fixed response; each falling SCLK edge advances
        # the CPLD shift register after the mode-0 rising-edge sample.
        time.sleep(0.001)
        clear_bits(gpio, CS)
        bits: list[int] = []
        for _ in range(48):
            set_bits(gpio, SCLK)
            time.sleep(0.00001)
            bits.append((read32(gpio, GPIO_DATAIN) & MISO) >> 3)
            clear_bits(gpio, SCLK)
            time.sleep(0.00001)
        set_bits(gpio, CS)
        received = bytes(
            sum(bit << (7 - index) for index, bit in enumerate(bits[start:start + 8]))
            for start in range(0, 48, 8)
        )
        result.update({
            "mode": "boot_gpio_overlay" if boot_gpio_mode else "runtime_detach",
            "applied_gpio_pads": applied,
            "gpio0_3_rx_hex": received.hex(),
            "match": received == EXPECTED,
        })
    finally:
        # Make GPIO outputs idle before returning the pads to McSPI ownership.
        try:
            set_bits(gpio, CS)
            clear_bits(gpio, MOSI | SCLK)
            restore_output(gpio, before_out, MASK_OUTPUT)
            write32(gpio, GPIO_OE, before_oe)
            for name, offset in PADS.items():
                write32(control, offset, before_pads[name])
            restored_pads = {name: read32(control, offset) for name, offset in PADS.items()}
            if restored_pads != before_pads:
                raise RuntimeError(f"padmux restore mismatch: {restored_pads}")
            restored = True
        finally:
            if unbound:
                write_sysfs(f"{DRIVER}/bind", DEVICE)
                wait_for("/dev/spidev0.0", True)
            gpio.close()
            control.close()
            os.close(fd)
            result["restored"] = restored
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("match") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
