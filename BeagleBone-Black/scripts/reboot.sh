# reboot.sh - reboot the board (needed to (re)apply the U-Boot pinmux overlay).
# Reconnect afterwards via BB_HOST=beagle.local (mDNS; survives a DHCP change).
echo "arming reboot now"
echo "$BB_PW" | sudo -S systemctl reboot
echo "reboot issued"
