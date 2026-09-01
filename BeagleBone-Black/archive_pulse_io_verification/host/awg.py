"""
awg.py - Keysight/Agilent 33521B (33500B-series) driver for the
output<->input 8 ns timing-resolution verification.

ROLE OF THE AWG: a TRIGGERED, PROGRAMMABLE-DELAY ECHO.

    board output0 --(rising edge)--> AWG rear "Ext Trig" input
    AWG waits TRIG:DEL (tau)      --> emits ONE clean TTL pulse
    AWG main output --(TTL)------->  board input channel

The host sweeps TRIG:DEL (4 ns grid, crystal-accurate, traceable). The board
measures the output->input interval and must reproduce tau with slope 1.000 in
8 ns steps. See BOARD_AGENT_SPEC.md for the full protocol.

No external deps (pyvisa NOT required) - raw SCPI over TCP 5025.
Verified against unit: Agilent 33521B, S/N MY59003298, FW 5.02, @ 192.168.0.4.
"""
import socket
import time


class Awg33521B:
    def __init__(self, ip="192.168.0.4", port=5025, timeout=3.0):
        self.sock = socket.create_connection((ip, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""

    # ---------------- low level ----------------
    def _tx(self, cmd):
        self.sock.sendall((cmd + "\n").encode("ascii"))

    def _read_line(self):
        while b"\n" not in self._buf:
            chunk = self.sock.recv(256)
            if not chunk:
                raise IOError("AWG socket closed")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("ascii").strip()

    def query(self, cmd):
        self._tx(cmd)
        return self._read_line()

    def check_error(self):
        """Raise if the AWG SCPI error queue is non-empty."""
        resp = self.query("SYST:ERR?")
        if int(resp.split(",")[0]) != 0:
            raise RuntimeError("AWG SCPI error: %s" % resp)
        return resp

    # ---------------- high level ----------------
    def idn(self):
        return self.query("*IDN?")

    def configure_echo(self, width_ns=200, high_v=3.3, low_v=0.0, slope="POS"):
        """Set up a triggered single-pulse TTL echo on the external trigger.

        width_ns : echo pulse width. ONLY the rising edge matters for timing;
                   width just has to be wide enough for the board input to
                   register cleanly (default 200 ns -> flat-top TTL).
        high/low : echo levels into the board's hi-Z input (OUTP:LOAD INF).
        """
        cmds = [
            "*CLS",
            "FUNC PULS",
            "OUTP:LOAD INF",              # board input is hi-Z -> displayed == actual
            "VOLT:HIGH %g" % high_v,
            "VOLT:LOW %g" % low_v,
            "FUNC:PULS:TRAN:BOTH MIN",    # fastest edges (~8.4 ns)
            "FUNC:PULS:WIDT %.10f" % (width_ns * 1e-9),
            "FREQ 1000",                  # carrier period; NCYC 1 -> 1 pulse / trigger
            "BURS:MODE TRIG",
            "BURS:NCYC 1",
            "TRIG:SOUR EXT",              # the board drives the trigger
            "TRIG:SLOP %s" % slope,
            "TRIG:DEL 0",
            "BURS:STAT ON",
        ]
        for c in cmds:
            self._tx(c)
            time.sleep(0.02)
        self._tx("*WAI")
        time.sleep(0.1)
        self.check_error()

    def set_delay_ns(self, tau_ns):
        """Trigger->echo delay. 33521B grid = 4 ns; keep tau_ns a multiple of 4.
        (Per-step SYST:ERR? skipped for speed; call check_error() periodically.)"""
        self._tx("TRIG:DEL %.10f" % (tau_ns * 1e-9))
        self._tx("*WAI")
        time.sleep(0.005)

    def output(self, on=True):
        self._tx("OUTP %d" % (1 if on else 0))
        time.sleep(0.02)
        self.check_error()

    def armed(self):
        """True if the trigger system is armed and waiting (Operation Status bit 5)."""
        return bool(int(self.query("STAT:OPER:COND?")) & 32)

    def triggers_arriving(self, secs=3.0):
        """Sanity check: are external triggers actually being received?
        Watches the Operation-Status 'Waiting for Trigger' (bit 5) condition and
        its transition latch. Drive the board output during this window."""
        self._tx("STAT:OPER:PTR 65535")
        self._tx("STAT:OPER:NTR 65535")
        self._tx("*CLS")
        time.sleep(0.05)
        self.query("STAT:OPER:EVEN?")  # clear latch
        fired = total = 0
        t0 = time.time()
        while time.time() - t0 < secs:
            if not (int(self.query("STAT:OPER:COND?")) & 32):
                fired += 1
            total += 1
        latched = int(self.query("STAT:OPER:EVEN?"))
        return {
            "fired_samples": fired,
            "total_samples": total,
            "latched_event": latched,
            "trigger_seen": bool(latched & 32) or fired > 0,
        }

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    awg = Awg33521B()
    print("IDN :", awg.idn())
    awg.configure_echo(width_ns=200)
    awg.output(True)
    print("armed:", awg.armed())
    print("err  :", awg.check_error())
    print("Now drive board output0 -> Ext Trig; sampling 3 s ...")
    print(awg.triggers_arriving(3.0))
    awg.close()
