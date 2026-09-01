#!/usr/bin/env bash
# Set the BBB's wired interface to the direct-link address used by RTASE.
# Requires BB_PW, supplied automatically by host/bb.py.
set -euo pipefail

network_file=/etc/systemd/network/eth0.network
tmp_file=$(mktemp)
trap 'rm -f "$tmp_file"' EXIT

cat >"$tmp_file" <<'EOF'
[Match]
Name=eth0
Type=ether

[Link]
RequiredForOnline=yes

[Network]
Address=192.168.50.2/24
EOF

echo "$BB_PW" | sudo -S cp "$network_file" "$network_file.bak-dhcp"
echo "$BB_PW" | sudo -S install -m 0644 "$tmp_file" "$network_file"
echo "$BB_PW" | sudo -S systemctl restart systemd-networkd
sleep 3
networkctl status eth0 --no-pager
