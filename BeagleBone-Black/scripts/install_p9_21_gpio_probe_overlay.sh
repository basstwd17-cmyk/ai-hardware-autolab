#!/usr/bin/env bash
# Switch only P9_21 to GPIO input for a one-boot receive-path diagnostic.
set -euo pipefail

cd "$HOME/pru/overlay"
dtc -I dts -O dtb -@ \
  -o BB-RTASE-P9-21-GPIO-PROBE-00A0.dtbo \
  BB-RTASE-P9-21-GPIO-PROBE-00A0.dts
echo "$BB_PW" | sudo -S install -m 0644 \
  BB-RTASE-P9-21-GPIO-PROBE-00A0.dtbo \
  /lib/firmware/BB-RTASE-P9-21-GPIO-PROBE-00A0.dtbo
echo "$BB_PW" | sudo -S cp /boot/uEnv.txt /boot/uEnv.txt.before-p9-21-gpio-probe
echo "$BB_PW" | sudo -S sed -i '/^#*uboot_overlay_pru=/d' /boot/uEnv.txt
echo "$BB_PW" | sudo -S sh -c 'echo uboot_overlay_pru=BB-RTASE-P9-21-GPIO-PROBE-00A0.dtbo >> /boot/uEnv.txt'
sync
echo "$BB_PW" | sudo -S systemctl reboot
