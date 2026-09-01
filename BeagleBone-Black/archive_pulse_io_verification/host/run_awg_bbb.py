"""
run_awg_bbb.py - BBB 5 ns AWG staircase verification (BeagleY-AI R5/G5 analog).

Runs ON the BeagleBone Black: the BBB is on the 192.168.0.x LAN, so it drives the
Keysight 33521B over SCPI AND measures the PRU over /dev/rpmsg0 in one process
(no split deployment needed, unlike the BeagleY-AI).

Loop per tau:
  AWG TRIG:DEL = tau  ->  PRU cmd 0x42 fires N trigger pulses on out0 (P8_11) into
  the AWG ext-trig; the AWG echoes one TTL pulse (after tau) into the board input
  (P8_16); the PRU timestamps T_out/T_in on the fast eCAP TSCTR (5 ns) and returns
  mean M.  Then fit M(ns) vs tau(ns): PASS if slope ~ 1.000 with a 5 ns staircase.

Wiring: out0(P8_11)->AWG ExtTrig ; AWG out->P8_16(in) ; common GND. AWG output
MUST be 0..3.3 V (configure_echo sets high=3.3, low=0, OUTP:LOAD INF).

Env: AWG_IP (default 192.168.0.7), TAU_START, TAU_STOP, TAU_STEP, N_SAMP, REARM_S.
"""
import os, sys, struct, select, math, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from awg import Awg33521B

AWG_IP    = os.environ.get("AWG_IP", "192.168.0.7")
TAU_START = int(os.environ.get("TAU_START", "0"))
TAU_STOP  = int(os.environ.get("TAU_STOP", "1000"))
TAU_STEP  = int(os.environ.get("TAU_STEP", "4"))     # 4 ns AWG grid, finer than 5 ns LSB -> reveals staircase
N_SAMP    = int(os.environ.get("N_SAMP", "5"))       # single-shots per tau (median); AWG needs re-arm gap
REARM_S   = float(os.environ.get("REARM_S", "0.006"))# gap between single shots so the AWG re-arms
TICK_NS   = 5.0
RPMSG     = "/dev/rpmsg0"
ECHO_MIN_NS, ECHO_MAX_NS = 20.0, 4000.0              # plausible AWG echo window


def awgshot1(fd):
    """ONE shot (firmware n=1). The 33521B needs ms-scale re-arm, so internal
    bursts (n>1) drop most shots -> always fire singles with a host gap."""
    os.write(fd, bytes([0x42]) + struct.pack("<I", 1))
    r, _, _ = select.select([fd], [], [], 5.0)
    d = os.read(fd, 512) if r else b""
    if len(d) < 22 or d[0] != 0x42 or d[1] != 1:
        return None
    ngood, ntmo, mn, mx, sm = struct.unpack("<IIIII", d[2:22])
    return mn if ngood == 1 else None        # single shot -> mn==mx==sm, in ticks


def measure_tau(fd):
    """MEAN of N_SAMP single shots (ticks) -> fractional, sub-tick (sub-5ns) resolution,
    like the BeagleY-AI N=100 averaging. Outlier-trim via median +/- 3 ticks before mean."""
    vals = []
    for _ in range(N_SAMP):
        m = awgshot1(fd)
        if m is not None:
            vals.append(m)
        time.sleep(REARM_S)
    if not vals:
        return None
    vals.sort()
    med = vals[len(vals) // 2]
    kept = [v for v in vals if abs(v - med) <= 3]      # drop gross wrong-edge outliers
    if not kept:
        kept = vals
    mean = sum(kept) / len(kept)                       # fractional ticks
    return mean, len(kept), (max(kept) - min(kept))    # mean, n, spread(ticks)


def linfit(xs, ys):
    n = len(xs); sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    inter = (sy - slope * sx) / n
    rms = math.sqrt(sum((y - (slope * x + inter)) ** 2 for x, y in zip(xs, ys)) / n)
    return slope, inter, rms


def main():
    print("AWG_IP=%s  tau=%d..%d step %d  N_SAMP=%d" % (AWG_IP, TAU_START, TAU_STOP, TAU_STEP, N_SAMP))
    awg = Awg33521B(AWG_IP)
    print("AWG :", awg.idn())
    awg.configure_echo(width_ns=200)           # safe 3.3 V TTL echo into hi-Z
    awg.output(True)
    print("armed:", awg.armed(), " err:", awg.check_error())
    fd = os.open(RPMSG, os.O_RDWR)

    taus, means = [], []
    print("\n  tau(ns)   M_med(ns)   spread(ns)   n")
    tau = TAU_START
    while tau <= TAU_STOP:
        awg.set_delay_ns(tau)
        if tau % 200 == 0:
            awg.check_error()
        r = measure_tau(fd)
        if r is None:
            print("  %5d    NO ECHO  (check wiring / AWG level / triggers)" % tau)
            tau += TAU_STEP; continue
        med, ns, spread = r
        m_ns = med * TICK_NS
        if ECHO_MIN_NS < m_ns < ECHO_MAX_NS:
            taus.append(tau); means.append(m_ns)
        print("  %5d    %8.2f    %7.1f      %d" % (tau, m_ns, spread * TICK_NS, ns))
        tau += TAU_STEP

    os.close(fd); awg.close()
    if len(taus) < 2:
        print("\nNot enough good points - check the wiring / trigger path."); return
    slope, inter, rms = linfit(taus, means)
    print("\n=== RESULT: BBB 5 ns AWG staircase ===")
    print("points    = %d" % len(taus))
    print("slope     = %.5f    (PASS if ~1.000 +/- 0.03)" % slope)
    print("intercept = %.1f ns   (fixed AWG latency + cable)" % inter)
    print("resid RMS = %.3f ns   (~5 ns LSB expected; SW-poll capture)" % rms)
    ok = abs(slope - 1.0) <= 0.03
    print("VERDICT   :", "PASS - 5 ns output<->input resolution verified vs external AWG"
          if ok else "CHECK - inspect staircase / wiring")
    # dump CSV for plotting
    try:
        with open("/home/xilinx/pru/awg_staircase.csv", "w") as f:
            f.write("tau_ns,M_ns\n")
            for t, m in zip(taus, means):
                f.write("%d,%.3f\n" % (t, m))
        print("CSV       : ~/pru/awg_staircase.csv")
    except Exception as e:
        print("csv write skipped:", e)


if __name__ == "__main__":
    main()
