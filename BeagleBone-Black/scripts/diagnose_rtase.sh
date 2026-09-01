#!/bin/bash
set -u

echo "== identity =="
date -u
cat /proc/device-tree/model 2>/dev/null || true
echo
uptime

echo "== boot overlay =="
grep -E '^(uboot_overlay_pru|uboot_overlay_addr|enable_uboot_overlays)' /boot/uEnv.txt 2>/dev/null || true
for marker in /proc/device-tree/chosen/overlays/*/part-number; do
    [ -e "$marker" ] || continue
    printf '%s: ' "$marker"
    tr -d '\000' < "$marker"
    echo
done

echo "== remoteproc =="
ls -la /sys/class/remoteproc 2>/dev/null || true
for rp in /sys/class/remoteproc/remoteproc*; do
    [ -e "$rp" ] || continue
    printf '%s name=' "$rp"
    cat "$rp/name" 2>/dev/null || true
    printf ' state='
    cat "$rp/state" 2>/dev/null || true
    printf ' firmware='
    cat "$rp/firmware" 2>/dev/null || true
done

echo "== device nodes =="
ls -la /dev/rpmsg* /dev/spidev* 2>/dev/null || true

echo "== modules and drivers =="
lsmod | grep -Ei 'pru|remoteproc|rpmsg|spi' || true
find /sys/bus/platform/drivers -maxdepth 1 -type d \( -iname '*pru*' -o -iname '*remoteproc*' -o -iname '*spi*' \) -print 2>/dev/null || true

echo "== relevant device-tree nodes =="
find /proc/device-tree -maxdepth 10 \( -iname '*pru*' -o -iname '*spi*' -o -iname '*rtase*' \) -print 2>/dev/null | head -200 || true

echo "== kernel log =="
echo "$BB_PW" | sudo -S dmesg | grep -Ei 'remoteproc|pru|rpmsg|spi|overlay|pinmux|failed|error' | tail -200 || true
