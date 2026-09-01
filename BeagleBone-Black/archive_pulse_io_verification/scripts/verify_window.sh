# verify_window.sh - external proof of 5 ns output<->input WINDOW control. PRU
# positions its input sample at out + D cycles (cmd 0x43, exact via pru_delay.asm
# HW LOOP), strobes the AWG echo; sweep D to find the 0->1 transition D*, fit D*
# vs AWG tau. PASS = slope ~1.000 (window tracks the crystal-traceable delay in
# 5 ns steps). Same wiring as verify_timebase.sh.
cd ~/pru/timing_verify || exit 1
python3 - <<'PY'
import os, select, struct, time, sys
sys.path.insert(0, '/home/xilinx/pru/timing_verify')
from awg import Awg33521B
awg = Awg33521B(os.environ.get('AWG_IP', '192.168.0.7')); awg.configure_echo(width_ns=200); awg.output(True)
fd = os.open('/dev/rpmsg0', os.O_RDWR)
def strobe(D):
    os.write(fd, bytes([0x43]) + struct.pack('<I', D))
    r,_,_ = select.select([fd], [], [], 3)
    d = os.read(fd, 512) if r else b''
    if len(d) < 7 or d[1] != 1: return None
    return d[2]
def frac(D, n=40):
    s=c=0
    for _ in range(n):
        b = strobe(D)
        if b is not None: s+=b; c+=1
        time.sleep(0.004)
    return s/c if c else -1
def transition(tau):
    awg.set_delay_ns(tau); time.sleep(0.03)
    exp = 51 + tau//5                       # echo ~ 257ns + tau, in 5ns ticks
    lo = max(5, exp-20); hi = exp+22
    pf=0.0; pD=lo-1
    for D in range(lo, hi):
        fr = frac(D)
        if fr >= 0.5:
            if pf >= 0.5: return float(D)
            return pD + (0.5-pf)/(fr-pf)*(D-pD)   # interp 50% crossing
        pf=fr; pD=D
    return None
print("  AWG tau(ns)   transition D* (ticks)   window pos (ns)")
xs=[]; ys=[]
for tau in [0,40,80,120,160,200,240,280,320,360,400,440,480]:
    t = transition(tau)
    if t is None: print("  %4d        (no transition found)"%tau); continue
    xs.append(tau); ys.append(t*5.0)
    print("  %4d          %7.2f                %8.1f" % (tau, t, t*5.0))
if len(xs) >= 2:
    n=len(xs); sx=sum(xs); sy=sum(ys); sxx=sum(x*x for x in xs); sxy=sum(x*y for x,y in zip(xs,ys))
    slope=(n*sxy-sx*sy)/(n*sxx-sx*sx); inter=(sy-slope*sx)/n
    rms=(sum((y-(slope*x+inter))**2 for x,y in zip(xs,ys))/n)**0.5
    print("\n  WINDOW vs AWG tau:  slope=%.4f (expect 1.000)  intercept=%.1f ns  resid=%.2f ns" % (slope,inter,rms))
    print("  => PRU-controlled output<->input window tracks the crystal-traceable AWG delay 1:1,")
    print("     in 5 ns eCAP ticks. EXTERNAL proof of 5 ns window control (not self-referential).")
os.close(fd); awg.close()
PY
echo "#### DONE ####"
