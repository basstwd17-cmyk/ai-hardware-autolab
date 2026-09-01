#!/usr/bin/env bash
# Restore the standard SPI0 roles: P9_18/D1 output (MOSI), P9_21/D0 input
# (MISO).  U-Boot applies the selected overlay on the requested reboot.
set -euo pipefail

cd "$HOME/pru/overlay"
dtc -I dts -O dtb -@ -o BB-RTASE-A-00A0.dtbo BB-RTASE-A-00A0.dts
echo "$BB_PW" | sudo -S install -m 0644 BB-RTASE-A-00A0.dtbo /lib/firmware/BB-RTASE-A-00A0.dtbo
echo "$BB_PW" | sudo -S cp /boot/uEnv.txt /boot/uEnv.txt.before-standard-spi
echo "$BB_PW" | sudo -S sed -i '/^#*uboot_overlay_pru=/d' /boot/uEnv.txt
echo "$BB_PW" | sudo -S sh -c 'echo uboot_overlay_pru=BB-RTASE-A-00A0.dtbo >> /boot/uEnv.txt'
sync
echo "$BB_PW" | sudo -S systemctl reboot
