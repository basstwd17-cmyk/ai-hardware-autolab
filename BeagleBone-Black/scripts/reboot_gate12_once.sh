#!/bin/sh
set -eu

grep -Fqx 'uboot_overlay_pru=BB-RTASE-A-GPIO-SPI-PRU-00A0.dtbo' /boot/uEnv.txt
grep -Fqx 'disable_uboot_overlay_audio=1' /boot/uEnv.txt
grep -Fqx 'disable_uboot_overlay_video=1' /boot/uEnv.txt
test -s /boot/uEnv.txt.pre-gate12

echo "GATE12_REBOOTING"
echo "$BB_PW" | sudo -S systemctl reboot

