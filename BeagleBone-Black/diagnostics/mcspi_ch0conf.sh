#!/bin/bash
# Read McSPI0 channel-0 registers, decoded. Read-only with respect to the
# controller: no register is written, no pinmux is touched, no overlay or
# firmware changes.
#
# Why user-space /dev/mem gives "Bus error" here: omap2-mcspi uses runtime PM
# and gates the module's interface clock when the device is idle. Reading an
# OMAP peripheral register with its clock gated raises an external abort,
# which reaches user space as SIGBUS. It is not a permission problem -- the
# mmap succeeded. So this script pins the device awake through the kernel's
# own runtime-PM knob first, and restores the previous policy at the end.
#
# CH0CONF is also rewritten by the driver for each transfer, so a value read
# while the bus is idle can be stale. The script therefore samples twice:
#   phase A  idle, device forced resumed
#   phase B  during a transfer burst identical to the existing loopback test
#            (same 6-byte pattern at 100 kHz on /dev/spidev0.0)
set -u

SPI_DEV=/sys/bus/platform/devices/48030000.spi
CONTROL="$SPI_DEV/power/control"
SAVED=auto

restore() {
    if [ -w "$CONTROL" ] || [ -e "$CONTROL" ]; then
        echo "$BB_PW" | sudo -S sh -c "echo '$SAVED' > '$CONTROL'" 2>/dev/null || true
        printf 'restored power/control = '
        cat "$CONTROL" 2>/dev/null || echo "?"
    fi
    rm -f /tmp/mcspi_ch0conf.py
}
trap restore EXIT

echo "== identity =="
date -u
uname -r
tr -d '\000' </proc/device-tree/model 2>/dev/null || true
echo

echo "== runtime PM before =="
for field in power/control power/runtime_status power/runtime_active_time; do
    [ -e "$SPI_DEV/$field" ] || continue
    printf '%-28s = ' "$field"
    cat "$SPI_DEV/$field" 2>/dev/null || true
done
if [ -e "$CONTROL" ]; then
    SAVED=$(cat "$CONTROL" 2>/dev/null || echo auto)
fi

echo
echo "== forcing the module clock on (runtime PM only, reverted on exit) =="
echo "$BB_PW" | sudo -S sh -c "echo on > '$CONTROL'" || true
for _ in $(seq 1 20); do
    state=$(cat "$SPI_DEV/power/runtime_status" 2>/dev/null || echo unknown)
    [ "$state" = active ] && break
    sleep 0.1
done
printf 'runtime_status = %s\n' "$(cat "$SPI_DEV/power/runtime_status" 2>/dev/null || echo unknown)"

cat > /tmp/mcspi_ch0conf.py <<'PY'
"""Decode McSPI0 CH0 registers, idle and under traffic. Reads only."""
import array
import ctypes
import fcntl
import mmap
import os
import struct
import threading
import time

BASE = 0x48030000
SIZE = 0x1000
REGS = {
    "MCSPI_SYSCONFIG": 0x110,
    "MCSPI_SYSSTATUS": 0x114,
    "MCSPI_MODULCTRL": 0x128,
    "MCSPI_CH0CONF": 0x12C,
    "MCSPI_CH0STAT": 0x130,
    "MCSPI_CH0CTRL": 0x134,
    "MCSPI_XFERLEVEL": 0x17C,
}
TRM = {0: "0 transmit+receive", 1: "1 receive only", 2: "2 transmit only", 3: "3 reserved"}


def bit(value, index):
    return (value >> index) & 1


def field(value, high, low):
    return (value >> low) & ((1 << (high - low + 1)) - 1)


def decode_ch0conf(value):
    is_bit, dpe1, dpe0 = bit(value, 18), bit(value, 17), bit(value, 16)
    rx_line = "D1 (P9_18, the MOSI pad)" if is_bit else "D0 (P9_21, the MISO pad)"
    if dpe0 == 0 and dpe1 == 1:
        tx_line = "D0 (P9_21, the MISO pad)"
    elif dpe0 == 1 and dpe1 == 0:
        tx_line = "D1 (P9_18, the MOSI pad)"
    elif dpe0 == 0 and dpe1 == 0:
        tx_line = "BOTH D0 and D1"
    else:
        tx_line = "NEITHER line enabled for transmission"
    lines = [
        f"   PHA={bit(value, 0)} POL={bit(value, 1)}  -> SPI mode {bit(value, 1) * 2 + bit(value, 0)}",
        f"   CLKD={field(value, 5, 2)} EPOL={bit(value, 6)} (1 = CS active low)",
        f"   WL={field(value, 11, 7)}  -> {field(value, 11, 7) + 1} bits per word",
        f"   TRM={TRM[field(value, 13, 12)]}",
        f"   DPE0={dpe0} DPE1={dpe1}  -> transmits on {tx_line}",
        f"   IS={is_bit}          -> receives from {rx_line}",
        f"   TURBO={bit(value, 19)} FORCE={bit(value, 20)} SBE={bit(value, 23)}",
        f"   FFEW={bit(value, 27)} FFER={bit(value, 28)} CLKG={bit(value, 29)}",
    ]
    verdict = []
    if is_bit:
        verdict.append("IS=1: reception is taken from D1, not from the MISO pad")
    if dpe0 != 1 or dpe1 != 0:
        verdict.append("DPE pair is not the standard D1-transmit setting")
    if field(value, 13, 12) == 2:
        verdict.append("TRM=transmit only: the receive register is never loaded")
    if field(value, 11, 7) != 7:
        verdict.append("word length is not 8 bits")
    if bit(value, 1) or bit(value, 0):
        verdict.append("clock polarity/phase is not mode 0")
    return lines, verdict


def decode_modulctrl(value):
    return [
        f"   SINGLE={bit(value, 0)} (1 = single-channel master)",
        f"   PIN34={bit(value, 1)} (1 = 3-pin mode, CS not used)",
        f"   MS={bit(value, 2)} (0 = master)",
        f"   SYSTEM_TEST={bit(value, 3)} (1 = loopback test mode inside the SoC)",
    ]


def decode_ch0stat(value):
    names = ["RXS", "TXS", "EOT", "TXFFE", "TXFFF", "RXFFE", "RXFFF"]
    return ["   " + " ".join(f"{name}={bit(value, i)}" for i, name in enumerate(names))]


class Traffic(threading.Thread):
    """The existing loopback transfer, repeated so CH0CONF holds live values."""

    TX = bytes.fromhex("00ffaa553cc3")

    def __init__(self, device="/dev/spidev0.0", speed_hz=100000, seconds=2.0):
        super().__init__(daemon=True)
        self.device, self.speed_hz, self.seconds = device, speed_hz, seconds
        self.results, self.error, self.count = [], None, 0

    def run(self):
        class Transfer(ctypes.Structure):
            _fields_ = [
                ("tx_buf", ctypes.c_uint64), ("rx_buf", ctypes.c_uint64),
                ("len", ctypes.c_uint32), ("speed_hz", ctypes.c_uint32),
                ("delay_usecs", ctypes.c_uint16), ("bits_per_word", ctypes.c_uint8),
                ("cs_change", ctypes.c_uint8), ("tx_nbits", ctypes.c_uint8),
                ("rx_nbits", ctypes.c_uint8), ("word_delay_usecs", ctypes.c_uint8),
                ("pad", ctypes.c_uint8),
            ]

        request = (1 << 30) | (ctypes.sizeof(Transfer) << 16) | (ord("k") << 8) | 0
        try:
            fd = os.open(self.device, os.O_RDWR)
        except OSError as exc:
            self.error = f"open {self.device}: {exc}"
            return
        try:
            fcntl.ioctl(fd, 0x40016B01, array.array("B", [0]))          # WR_MODE 0
            fcntl.ioctl(fd, 0x40016B03, array.array("B", [8]))          # WR_BITS
            fcntl.ioctl(fd, 0x40046B04, array.array("I", [self.speed_hz]))  # WR_SPEED
            deadline = time.monotonic() + self.seconds
            while time.monotonic() < deadline:
                tx = ctypes.create_string_buffer(self.TX, len(self.TX))
                rx = ctypes.create_string_buffer(len(self.TX))
                message = Transfer(
                    tx_buf=ctypes.addressof(tx), rx_buf=ctypes.addressof(rx),
                    len=len(self.TX), speed_hz=self.speed_hz, bits_per_word=8,
                )
                fcntl.ioctl(fd, request, bytes(message))
                self.count += 1
                if len(self.results) < 4:
                    self.results.append(rx.raw.hex())
                time.sleep(0.002)
        except OSError as exc:
            self.error = f"transfer: {exc}"
        finally:
            os.close(fd)


def read_all(region):
    return {name: struct.unpack_from("<I", region, offset)[0] for name, offset in REGS.items()}


def report(title, values):
    print(f"-- {title}")
    for name, value in values.items():
        print(f"   {name:<16} = 0x{value:08x}")
    print("   MODULCTRL:")
    for line in decode_modulctrl(values["MCSPI_MODULCTRL"]):
        print("   " + line)
    print("   CH0CONF:")
    lines, verdict = decode_ch0conf(values["MCSPI_CH0CONF"])
    for line in lines:
        print("   " + line)
    print("   CH0STAT:")
    for line in decode_ch0stat(values["MCSPI_CH0STAT"]):
        print("   " + line)
    print(f"   CH0CTRL EN={values['MCSPI_CH0CTRL'] & 1}")
    if verdict:
        for item in verdict:
            print(f"   !! {item}")
    else:
        print("   -> matches the standard master configuration for this board")


def main():
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        region = mmap.mmap(fd, SIZE, mmap.MAP_SHARED, mmap.PROT_READ, offset=BASE)
    except OSError as exc:
        print(f"mmap /dev/mem at 0x{BASE:08x} failed: {exc}")
        os.close(fd)
        return 2
    try:
        try:
            idle = read_all(region)
        except Exception as exc:  # SIGBUS surfaces here only if it is trappable
            print(f"register read failed even with the clock forced on: {exc}")
            return 2
        print("== phase A: bus idle ==")
        report("registers with no transfer running", idle)

        print()
        print("== phase B: during a transfer burst (00ffaa553cc3 @ 100 kHz) ==")
        traffic = Traffic()
        traffic.start()
        time.sleep(0.05)
        samples = {}
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            values = read_all(region)
            key = (values["MCSPI_CH0CONF"], values["MCSPI_CH0CTRL"] & 1)
            samples.setdefault(key, [0, values])
            samples[key][0] += 1
            time.sleep(0.005)
        traffic.join(timeout=5)
        print(f"   transfers issued : {traffic.count}")
        print(f"   rx observed      : {', '.join(traffic.results) or '(none)'}")
        if traffic.error:
            print(f"   transfer error   : {traffic.error}")
        print(f"   distinct CH0CONF values while running: {len(samples)}")
        for (conf, enabled), (count, values) in sorted(
            samples.items(), key=lambda item: -item[1][0]
        ):
            print()
            report(f"CH0CONF=0x{conf:08x} CH0CTRL.EN={enabled} seen {count}x", values)
        return 0
    finally:
        region.close()
        os.close(fd)


raise SystemExit(main())
PY

echo
echo "== McSPI0 registers =="
echo "$BB_PW" | sudo -S python3 /tmp/mcspi_ch0conf.py
status=$?

echo
echo "== pad configuration re-read (for correlation) =="
for base in /sys/kernel/debug/pinctrl/*pinmux*; do
    [ -d "$base" ] || continue
    echo "$BB_PW" | sudo -S grep -E "^pin 8[4-7] " "$base/pins" 2>/dev/null || true
done

exit "$status"
