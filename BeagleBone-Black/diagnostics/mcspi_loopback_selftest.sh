#!/bin/bash
# Split "the SoC receive datapath is broken" from "the pad input is not
# delivering what the scope sees on the pin".
#
# SPI_LOOP asks the driver for an internal loopback: on omap2-mcspi that is
# MODULCTRL.SYSTEM_TEST, which ties transmit to receive inside the controller
# and ignores the pins entirely. If rx equals tx in that mode, then the ioctl
# path, the driver, the FIFO and the receive register all work, and whatever
# is wrong lives outside the SoC. If rx is still constant there, no amount of
# CPLD or wiring work can fix it.
#
# The same transfer is then repeated with the external path so both results
# come from one run and one board state.
#
# Writes: spidev mode (restored to 0 at the end) and runtime-PM policy
# (restored on exit). No controller register is written by this script, no
# pinmux, no overlay, no firmware.
set -u

SPI_DEV=/sys/bus/platform/devices/48030000.spi
CONTROL="$SPI_DEV/power/control"
SAVED=auto

restore() {
    [ -e "$CONTROL" ] && echo "$BB_PW" | sudo -S sh -c "echo '$SAVED' > '$CONTROL'" 2>/dev/null
    rm -f /tmp/mcspi_selftest.py
    true
}
trap restore EXIT

echo "== identity =="
date -u
uname -r
echo

[ -e "$CONTROL" ] && SAVED=$(cat "$CONTROL" 2>/dev/null || echo auto)
echo "$BB_PW" | sudo -S sh -c "echo on > '$CONTROL'" 2>/dev/null || true
printf 'runtime_status = %s\n' "$(cat "$SPI_DEV/power/runtime_status" 2>/dev/null || echo unknown)"

cat > /tmp/mcspi_selftest.py <<'PY'
"""Internal-loopback vs external transfer, same tx, same session."""
import array
import ctypes
import fcntl
import mmap
import os
import struct
import time

DEVICE = "/dev/spidev0.0"
TX = bytes.fromhex("00ffaa553cc3")
SPEED = 100000
BASE, SIZE = 0x48030000, 0x1000
MODULCTRL, CH0CONF = 0x128, 0x12C

SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_RD_MODE = 0x80016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
SPI_LOOP = 0x10


class Transfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf", ctypes.c_uint64), ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32), ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16), ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8), ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8), ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


MESSAGE = (1 << 30) | (ctypes.sizeof(Transfer) << 16) | (ord("k") << 8) | 0


def set_mode(fd, mode):
    fcntl.ioctl(fd, SPI_IOC_WR_MODE, array.array("B", [mode]))
    read = array.array("B", [0])
    fcntl.ioctl(fd, SPI_IOC_RD_MODE, read, True)
    return read[0]


def transfer(fd):
    tx = ctypes.create_string_buffer(TX, len(TX))
    rx = ctypes.create_string_buffer(len(TX))
    message = Transfer(
        tx_buf=ctypes.addressof(tx), rx_buf=ctypes.addressof(rx),
        len=len(TX), speed_hz=SPEED, bits_per_word=8,
    )
    fcntl.ioctl(fd, MESSAGE, bytes(message))
    return rx.raw


def registers():
    try:
        fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    except OSError as exc:
        return f"(/dev/mem unavailable: {exc})"
    try:
        region = mmap.mmap(fd, SIZE, mmap.MAP_SHARED, mmap.PROT_READ, offset=BASE)
    except OSError as exc:
        os.close(fd)
        return f"(mmap failed: {exc})"
    try:
        modulctrl = struct.unpack_from("<I", region, MODULCTRL)[0]
        ch0conf = struct.unpack_from("<I", region, CH0CONF)[0]
        return (
            f"MODULCTRL=0x{modulctrl:08x} SYSTEM_TEST={(modulctrl >> 3) & 1} "
            f"MS={(modulctrl >> 2) & 1} SINGLE={modulctrl & 1} | "
            f"CH0CONF=0x{ch0conf:08x} IS={(ch0conf >> 18) & 1} "
            f"DPE1={(ch0conf >> 17) & 1} DPE0={(ch0conf >> 16) & 1} "
            f"TRM={(ch0conf >> 12) & 3} POL={(ch0conf >> 1) & 1} PHA={ch0conf & 1}"
        )
    finally:
        region.close()
        os.close(fd)


def run(fd, label, repeats=3):
    print(f"-- {label}")
    for index in range(repeats):
        rx = transfer(fd)
        verdict = "rx == tx" if rx == TX else ("constant " + rx.hex()[:2] if len(set(rx)) == 1 else "mixed")
        print(f"   {index + 1}: tx={TX.hex()} rx={rx.hex()}  [{verdict}]")
        if index == 0:
            print(f"      {registers()}")
        time.sleep(0.05)


def main():
    fd = os.open(DEVICE, os.O_RDWR)
    try:
        fcntl.ioctl(fd, SPI_IOC_WR_BITS_PER_WORD, array.array("B", [8]))
        fcntl.ioctl(fd, SPI_IOC_WR_MAX_SPEED_HZ, array.array("I", [SPEED]))

        print("== external path, mode 0 (baseline for this session) ==")
        print(f"   mode readback = 0x{set_mode(fd, 0):02x}")
        run(fd, "through the pins, CPLD echo")

        print()
        print("== internal loopback, SPI_LOOP ==")
        try:
            effective = set_mode(fd, SPI_LOOP)
        except OSError as exc:
            print(f"   SPI_LOOP rejected by the driver: {exc}")
            print("   -> omap2-mcspi does not expose SPI_LOOP in master->mode_bits.")
            print("      The equivalent is MODULCTRL.SYSTEM_TEST=1, which needs a")
            print("      controller register WRITE. Not done here: this script is")
            print("      read-only with respect to the controller.")
        else:
            print(f"   mode readback = 0x{effective:02x} (SPI_LOOP {'set' if effective & SPI_LOOP else 'NOT set'})")
            if effective & SPI_LOOP:
                run(fd, "transmit tied to receive inside the SoC, pins ignored")
            else:
                print("   driver accepted the ioctl but did not keep the bit; treat as unsupported")

        print()
        print("== restoring mode 0 and repeating the external path ==")
        print(f"   mode readback = 0x{set_mode(fd, 0):02x}")
        run(fd, "through the pins again")
    finally:
        try:
            set_mode(fd, 0)
        except OSError:
            pass
        os.close(fd)
    return 0


raise SystemExit(main())
PY

echo
echo "== spidev self-test =="
echo "$BB_PW" | sudo -S python3 /tmp/mcspi_selftest.py
status=$?

echo
echo "== pad ownership (who claims pin 85 / P9_21) =="
for base in /sys/kernel/debug/pinctrl/*pinmux*; do
    [ -d "$base" ] || continue
    echo "$BB_PW" | sudo -S grep -E "^pin 8[4-7] " "$base/pinmux-pins" 2>/dev/null || true
    echo "$BB_PW" | sudo -S grep -E "^pin 8[4-7] " "$base/pins" 2>/dev/null || true
done

exit "$status"
