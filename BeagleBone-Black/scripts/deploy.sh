# deploy.sh - build the PRU0 firmware (clpru) and (re)load it via remoteproc.
# Run from the repo root after editing firmware/main.c and pushing it to the board:
#   py host/bb.py --put firmware/main.c ~/pru/pru0_pulse_io/main.c
#   BB_HOST=beagle.local py host/bb.py scripts/deploy.sh
SR(){ echo "$BB_PW" | sudo -S sh -c "$1" 2>/dev/null; }
export PRU_CGT=/usr/share/ti/cgt-pru
cd ~/pru/pru0_pulse_io || exit 1
echo "## build pru0_pulse_io ##"; make clean >/dev/null 2>&1; make 2>&1 | tail -5
ls gen/pru0_pulse_io.out >/dev/null 2>&1 || { echo "BUILD FAIL"; exit 1; }
SR "cp gen/pru0_pulse_io.out /lib/firmware/am335x-pru0-pulseio.out"
RP=/sys/class/remoteproc/remoteproc1
SR "echo stop > $RP/state"; sleep 0.3
SR "echo -n am335x-pru0-pulseio.out > $RP/firmware"
SR "echo start > $RP/state"; sleep 1.2
SR "chmod 666 /dev/rpmsg0"
python3 - <<'PY'
import os, select
fd=os.open('/dev/rpmsg0', os.O_RDWR)
os.write(fd, bytes([0x03])); r,_,_=select.select([fd],[],[],3)
print("VERSION:", (os.read(fd,512) if r else b'')[1:].decode('ascii','replace'))
os.close(fd)
PY
echo "#### firmware built + loaded ####"
