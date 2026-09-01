#!/bin/sh
set -eu

echo "MODEL"
tr -d '\000' </proc/device-tree/model
echo

echo "BOOT"
grep -E '^(enable_uboot_overlays|uboot_overlay_pru|disable_uboot_overlay_(audio|video))=' /boot/uEnv.txt
test -s /boot/uEnv.txt.pre-gate12
echo "RECOVERY_OK"

echo "CHOSEN_OVERLAY"
for marker in /proc/device-tree/chosen/overlays/*; do
    [ -f "$marker" ] || continue
    printf '%s=' "$(basename "$marker")"
    tr -d '\000' <"$marker"
    echo
done

echo "PINMUX"
pinfile=/sys/kernel/debug/pinctrl/44e10800.pinmux-pinctrl-single/pins
for number in 12 13 84 85 86 87 100; do
    echo "$BB_PW" | sudo -S grep -F "pin $number " "$pinfile"
done

echo "SERVICE"
systemctl is-active rtase-pru.service
test -e /dev/rpmsg0
test ! -e /dev/spidev0.0
ls -l /dev/rpmsg0 /run/lock/rtase-control.lock

echo "PRU_STATUS"
python3 "$HOME/pru/rtase.py" --local status

echo "GATE12_BOOT_VERIFY_OK"

