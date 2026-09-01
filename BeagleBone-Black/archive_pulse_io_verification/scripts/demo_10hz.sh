# demo_10hz.sh - 10 Hz, nominal 50% duty square-wave demo on P8_11.
# Probe P8_11 (signal) + P8_1/P8_2 (GND). cmd 0x36 loops forever.
SR(){ echo "$BB_PW" | sudo -S sh -c "$1" 2>/dev/null; }
SR "chmod 666 /dev/rpmsg0"
python3 - <<'PY'
import os, select

fd = os.open('/dev/rpmsg0', os.O_RDWR)
os.write(fd, bytes([0x03]))
r, _, _ = select.select([fd], [], [], 3)
print("VERSION:", (os.read(fd, 512) if r else b'')[1:].decode('ascii', 'replace'))
os.write(fd, bytes([0x36]))
print("FIRING cmd 0x36: 10 Hz square wave, nominal 50% duty on P8_11. Scope P8_11.")
os.close(fd)
PY
echo "#### 10 Hz demo running on P8_11 (stop: reload firmware via deploy.sh) ####"
