#!/bin/bash
# Temporarily bypass McSPI to test the BBB's P9_21 GPIO input buffer.
#
# The currently loaded non-DAC gate4 design implements MISO = MOSI while CS
# is low.  This script therefore drives P9_18 high and P9_17 low as GPIOs,
# reads P9_21 as GPIO0_3, then restores every changed GPIO register, padmux
# value, and runtime-PM policy.  It never drives SCLK, PRU, DAC, JTAG, or any
# analog-output pin; no bootloader/overlay/CPLD configuration is changed.
set -euo pipefail

TMP=/tmp/p9_21_gpio_loopback_probe.py
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

cat > "$TMP" <<'PY'
import json
import mmap
import os
import struct
import time

CONTROL_BASE, CONTROL_SIZE = 0x44E10000, 0x1000
GPIO0_BASE, GPIO_SIZE = 0x44E07000, 0x1000
GPIO_OE, GPIO_DATAIN, GPIO_DATAOUT = 0x134, 0x138, 0x13C
GPIO_CLEARDATAOUT, GPIO_SETDATAOUT = 0x190, 0x194

# The overlay's pinctrl offsets are relative to AM33xx pinmux at 0x44e10800;
# this process maps the larger 0x44e10000 control-module page.
PADS = {"cs": 0x95C, "mosi": 0x958, "miso": 0x954}
EXPECTED = {"cs": 0x08, "mosi": 0x08, "miso": 0x28}
GPIO_MODE_OUTPUT, GPIO_MODE_INPUT = 0x07, 0x27
CS_BIT, MOSI_BIT, MISO_BIT = 1 << 5, 1 << 4, 1 << 3
HOLD_S = 2.0
PM_PATHS = ("/sys/bus/platform/devices/44e07000.gpio/power/control",)


def read32(region, offset):
    return struct.unpack_from("<I", region, offset)[0]


def write32(region, offset, value):
    struct.pack_into("<I", region, offset, value & 0xFFFFFFFF)


def set_bits(region, mask):
    write32(region, GPIO_SETDATAOUT, mask)


def clear_bits(region, mask):
    write32(region, GPIO_CLEARDATAOUT, mask)


def restore_output_bits(region, before, mask):
    set_bits(region, before & mask)
    clear_bits(region, (~before) & mask)


def force_pm_on():
    saved = {}
    for path in PM_PATHS:
        if not os.path.exists(path):
            continue
        with open(path, encoding="ascii") as stream:
            saved[path] = stream.read().strip()
        with open(path, "w", encoding="ascii") as stream:
            stream.write("on\n")
    return saved


def restore_pm(saved):
    for path, value in saved.items():
        try:
            with open(path, "w", encoding="ascii") as stream:
                stream.write(value + "\n")
        except OSError:
            pass


def main():
    pm_saved = force_pm_on()
    fd = None
    control = gpio0 = None
    changed = False
    before = {}
    try:
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        control = mmap.mmap(fd, CONTROL_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=CONTROL_BASE)
        gpio0 = mmap.mmap(fd, GPIO_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=GPIO0_BASE)

        before["pads"] = {name: read32(control, offset) for name, offset in PADS.items()}
        if before["pads"] != EXPECTED:
            raise RuntimeError(f"refusing unexpected SPI padmux: {before['pads']}")
        before["gpio0_oe"] = read32(gpio0, GPIO_OE)
        before["gpio0_out"] = read32(gpio0, GPIO_DATAOUT)
        # Pre-read the input register while every pad is still unchanged.  If
        # this faults, no multiplexing or output direction has been altered.
        before["gpio0_datain"] = read32(gpio0, GPIO_DATAIN)

        # Preload GPIO output latches to a harmless SPI-idle state before the
        # pads are switched.  They are not connected to the pins yet.
        set_bits(gpio0, CS_BIT)
        clear_bits(gpio0, MOSI_BIT)
        write32(gpio0, GPIO_OE, before["gpio0_oe"] & ~(CS_BIT | MOSI_BIT))

        # Keep CS inactive while changing modes; only then assert the safe
        # diagnostic combination CS=0, MOSI=1, which makes gate4 MISO=1.
        write32(control, PADS["miso"], GPIO_MODE_INPUT)
        write32(control, PADS["mosi"], GPIO_MODE_OUTPUT)
        write32(control, PADS["cs"], GPIO_MODE_OUTPUT)
        changed = True
        expected_gpio_pads = {
            "cs": GPIO_MODE_OUTPUT,
            "mosi": GPIO_MODE_OUTPUT,
            "miso": GPIO_MODE_INPUT,
        }
        applied_pads = {name: read32(control, offset) for name, offset in PADS.items()}
        if applied_pads != expected_gpio_pads:
            raise RuntimeError(
                "kernel pinctrl retained the SPI padmux; refusing to drive GPIO "
                f"until it is cleanly detached: {applied_pads}"
            )
        set_bits(gpio0, MOSI_BIT)
        clear_bits(gpio0, CS_BIT)
        drive_state = {
            "pads_after": applied_pads,
            "gpio0_oe_after": read32(gpio0, GPIO_OE),
            "gpio0_dataout_after": read32(gpio0, GPIO_DATAOUT),
            "hold_s": HOLD_S,
        }
        time.sleep(HOLD_S)
        samples = [(read32(gpio0, GPIO_DATAIN) & MISO_BIT) >> 3 for _ in range(32)]

        print(json.dumps({
            "ok": True,
            "test": "gate4 CS=0 MOSI=1 -> expect P9_21 GPIO0_3=1",
            "drive_state": drive_state,
            "samples": samples,
            "high_samples": sum(samples),
            "total_samples": len(samples),
            "gpio_input_verdict": "HIGH" if all(samples) else "LOW_OR_UNSTABLE",
        }, sort_keys=True))
    finally:
        if changed:
            # Deassert before restoring the McSPI function, then restore exact
            # GPIO latch, direction, and pinmux snapshots.
            set_bits(gpio0, CS_BIT)
            clear_bits(gpio0, MOSI_BIT)
            restore_output_bits(gpio0, before["gpio0_out"], CS_BIT | MOSI_BIT)
            write32(gpio0, GPIO_OE, before["gpio0_oe"])
            for name, offset in PADS.items():
                write32(control, offset, before["pads"][name])
        for region in (gpio0, control):
            if region is not None:
                region.close()
        if fd is not None:
            os.close(fd)
        restore_pm(pm_saved)


main()
PY

echo "$BB_PW" | sudo -S -p '' python3 "$TMP"
