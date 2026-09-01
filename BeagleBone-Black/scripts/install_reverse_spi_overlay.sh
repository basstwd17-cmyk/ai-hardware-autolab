#!/usr/bin/env bash
# Install the separately named, diagnostic-only reverse SPI0 pinmux overlay.
# This does not touch PRU/DAC/SMA outputs; a reboot is required for U-Boot to
# apply its existing uboot_overlay_pru=BB-RTASE-A-SPI-REVERSE-00A0.dtbo line.
set -euo pipefail

cd "$HOME/pru/overlay"
dtc -I dts -O dtb -@ \
  -o BB-RTASE-A-SPI-REVERSE-00A0.dtbo \
  BB-RTASE-A-SPI-REVERSE-00A0.dts

echo "$BB_PW" | sudo -S install -m 0644 \
  BB-RTASE-A-SPI-REVERSE-00A0.dtbo \
  /lib/firmware/BB-RTASE-A-SPI-REVERSE-00A0.dtbo
sha256sum /lib/firmware/BB-RTASE-A-SPI-REVERSE-00A0.dtbo
sync
echo "$BB_PW" | sudo -S systemctl reboot
