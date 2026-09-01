#!/bin/sh
set -u

echo "TIME_UPTIME"
date -u
uptime
cat /proc/uptime

echo "MOUNTS"
findmnt / || true
findmnt /boot || true
lsblk -o NAME,FSTYPE,LABEL,UUID,MOUNTPOINTS || true

echo "BOOT_FILES"
ls -l /boot/uEnv.txt /boot/uEnv.txt.pre-gate12 2>/dev/null || true
grep -nE '^(enable_uboot_overlays|uboot_overlay_pru|disable_uboot_overlay_(audio|video))=' /boot/uEnv.txt 2>/dev/null || true
for candidate in /boot/firmware/uEnv.txt /boot/uboot/uEnv.txt /uEnv.txt; do
    if [ -f "$candidate" ]; then
        echo "ALT_UENV=$candidate"
        grep -nE '^(enable_uboot_overlays|uboot_overlay_pru|disable_uboot_overlay_(audio|video))=' "$candidate" || true
    fi
done

echo "DTBO"
sha256sum /lib/firmware/BB-RTASE-A-GPIO-SPI-PRU-00A0.dtbo 2>/dev/null || true
strings /lib/firmware/BB-RTASE-A-GPIO-SPI-PRU-00A0.dtbo 2>/dev/null | grep -E 'gate12|GPIO-SPI-PRU|rtase-gpio' || true

echo "SERVICE_FILES"
systemctl show rtase-pru.service -p ActiveEnterTimestamp -p ExecMainStartTimestamp -p FragmentPath || true
sha256sum /usr/local/sbin/rtase-pru-service "$HOME/pru/rtase-pru-service.sh" 2>/dev/null || true
grep -n rtase-control /usr/local/sbin/rtase-pru-service "$HOME/pru/rtase-pru-service.sh" 2>/dev/null || true

echo "BOOT_LOG"
echo "$BB_PW" | sudo -S dmesg | grep -Ei 'overlay|uboot|pru|pinmux|failed|error' | tail -100 || true

