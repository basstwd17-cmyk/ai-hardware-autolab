# demo.sh - fire a 5 ns scope demo on the (already-loaded) firmware.
# Probe P8_11 (signal) + P8_1/P8_2 (GND). cmd 0x32 = auto-sweep: pulse width
# steps 5,10,15,...,60 ns (each +5 ns) so a scope shows the 5 ns output grid.
SR(){ echo "$BB_PW" | sudo -S sh -c "$1" 2>/dev/null; }
SR "chmod 666 /dev/rpmsg0"
python3 - <<'PY'
import os, select
fd = os.open('/dev/rpmsg0', os.O_RDWR)
os.write(fd, bytes([0x03]))
r,_,_ = select.select([fd],[],[],3)
print("VERSION:", (os.read(fd,512) if r else b'')[1:].decode('ascii','replace'))
os.write(fd, bytes([0x32]))     # 5 ns auto-sweep on P8_11 (loops forever)
print("FIRING cmd 0x32: 5 ns width auto-sweep on P8_11 (5..60 ns, +5 ns/step). Scope P8_11.")
os.close(fd)
PY
echo "#### demo running on P8_11 (stop: reload firmware via deploy.sh) ####"
