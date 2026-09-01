#!/bin/bash
# Read-only snapshot of everything the kernel already knows about McSPI0.
#
# No writes, no /dev/mem, no SPI transfer, no reconfiguration. Answers two
# questions the register map would otherwise be needed for:
#   which data line does the driver transmit on / receive from
#   which clock polarity and phase is the device configured with
#
# set -u only. One missing path must not truncate the rest of the dump.
set -u

echo "== identity =="
date -u
tr -d '\000' </proc/device-tree/model 2>/dev/null || true
echo
uname -r

echo
echo "== live overlays =="
for marker in /proc/device-tree/chosen/overlays/*; do
    [ -e "$marker" ] || continue
    printf '%s: ' "$(basename "$marker")"
    tr -d '\000' <"$marker" 2>/dev/null || true
    echo
done

echo
echo "== spi0 device-tree node (as applied) =="
# Resolve through sysfs rather than guessing a /proc/device-tree path: the
# live node moved under Debian 13's kernel (ocp/ is no longer the parent), and
# a fixed path silently prints nothing instead of failing.
resolved=$(readlink -f /sys/class/spi_master/spi0/device/of_node 2>/dev/null || true)
[ -n "$resolved" ] || resolved=$(readlink -f /sys/bus/platform/devices/48030000.spi/of_node 2>/dev/null || true)
echo "   resolved via sysfs: ${resolved:-<not found>}"
for node in "$resolved" /proc/device-tree/ocp/spi@48030000 /proc/device-tree/*/spi@48030000; do
    [ -n "$node" ] && [ -d "$node" ] || continue
    echo "-- $node"
    for property in "$node"/*; do
        [ -f "$property" ] || continue
        printf '   %-28s ' "$(basename "$property")"
        tr -d '\000' <"$property" 2>/dev/null | tr -c '[:print:]' ' '
        echo
    done
    # The one property that decides the data-line direction. Absent means the
    # omap2-mcspi default: D0 = input (MISO), D1 = output (MOSI).
    if [ -e "$node/ti,pindir-d0-out-d1-in" ]; then
        echo "   >> ti,pindir-d0-out-d1-in PRESENT  -> D0 = OUTPUT, D1 = INPUT (reversed)"
    else
        echo "   >> ti,pindir-d0-out-d1-in absent   -> D0 = input, D1 = output (default)"
    fi
    for child in "$node"/*/; do
        [ -d "$child" ] || continue
        echo "   -- child $(basename "$child")"
        for property in "$child"*; do
            [ -f "$property" ] || continue
            printf '      %-25s ' "$(basename "$property")"
            tr -d '\000' <"$property" 2>/dev/null | tr -c '[:print:]' ' '
            echo
        done
    done
    break
done

echo
echo "== spi bus as the kernel enumerated it =="
ls -la /sys/class/spi_master/ 2>/dev/null || true
for device in /sys/bus/spi/devices/*; do
    [ -e "$device" ] || continue
    echo "-- $device"
    for field in modalias driver_override of_node/compatible of_node/spi-max-frequency; do
        [ -e "$device/$field" ] || continue
        printf '   %-24s ' "$field"
        tr -d '\000' <"$device/$field" 2>/dev/null | tr -c '[:print:]' ' '
        echo
    done
done
ls -la /dev/spidev* 2>/dev/null || true

echo
echo "== spidev configuration (ioctl read-back, no transfer) =="
python3 - <<'PY' 2>&1 || true
import array
import fcntl
import os

SPI_IOC_RD_MODE = 0x80016B01
SPI_IOC_RD_LSB_FIRST = 0x80016B02
SPI_IOC_RD_BITS_PER_WORD = 0x80016B03
SPI_IOC_RD_MAX_SPEED_HZ = 0x80046B04
SPI_IOC_RD_MODE32 = 0x80046B05

MODE_BITS = [
    (0x01, "CPHA"),
    (0x02, "CPOL"),
    (0x04, "CS_HIGH"),
    (0x08, "3WIRE"),
    (0x10, "LOOP"),
    (0x20, "NO_CS"),
    (0x40, "READY"),
]

for path in ("/dev/spidev0.0", "/dev/spidev0.1", "/dev/spidev1.0"):
    if not os.path.exists(path):
        continue
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError as exc:
        print(f"{path}: cannot open ({exc})")
        continue
    try:
        def read(request, typecode, size):
            buf = array.array(typecode, [0] * size)
            fcntl.ioctl(fd, request, buf, True)
            return buf[0]

        mode = read(SPI_IOC_RD_MODE, "B", 1)
        mode32 = read(SPI_IOC_RD_MODE32, "I", 1)
        lsb = read(SPI_IOC_RD_LSB_FIRST, "B", 1)
        bits = read(SPI_IOC_RD_BITS_PER_WORD, "B", 1)
        speed = read(SPI_IOC_RD_MAX_SPEED_HZ, "I", 1)
        flags = " ".join(name for mask, name in MODE_BITS if mode32 & mask) or "-"
        print(f"{path}")
        print(f"   mode         = 0x{mode:02x}  (SPI mode {mode & 3})")
        print(f"   mode32       = 0x{mode32:08x}  [{flags}]")
        print(f"   CPOL={(mode >> 1) & 1} CPHA={mode & 1}")
        print(f"   bits/word    = {bits}")
        print(f"   lsb_first    = {lsb}")
        print(f"   max_speed_hz = {speed}")
    except OSError as exc:
        print(f"{path}: ioctl failed ({exc})")
    finally:
        os.close(fd)
PY

echo
echo "== pad configuration for SPI0 (pinctrl-single) =="
echo "   pin 84 = P9_22 SCLK, 85 = P9_21 D0, 86 = P9_18 D1, 87 = P9_17 CS0"
echo "   conf bits: 6 SLEWCTRL | 5 RXACTIVE | 4 PULLTYPESEL(1=up) | 3 PULLUDEN(1=disabled) | 2:0 mux"
for base in /sys/kernel/debug/pinctrl/*pinmux*; do
    [ -d "$base" ] || continue
    echo "-- $base"
    echo "$BB_PW" | sudo -S grep -E "^pin 8[4-7] " "$base/pins" 2>/dev/null || true
    echo "$BB_PW" | sudo -S grep -E "^pin 8[4-7] " "$base/pinmux-pins" 2>/dev/null || true
done

echo
echo "== driver binding and clocks =="
ls -la /sys/bus/platform/drivers/omap2_mcspi/ 2>/dev/null || true
for state in /sys/bus/platform/devices/48030000.spi/power/runtime_status \
             /sys/bus/platform/devices/48030000.spi/power/control; do
    [ -e "$state" ] || continue
    printf '%s = ' "$state"
    cat "$state" 2>/dev/null || true
done

echo
echo "== kernel log, spi only =="
echo "$BB_PW" | sudo -S dmesg | grep -Ei 'mcspi|spidev|spi0|spi master' | tail -60 || true
