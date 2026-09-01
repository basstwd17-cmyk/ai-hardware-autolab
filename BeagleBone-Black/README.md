# bbb_pulse_io — 5 ns precision pulse I/O on the BeagleBone Black

Cycle-exact pulse generation and output↔input window control on the **BeagleBone
Black** (AM335x) using the **PRU-ICSS at 200 MHz = 5 ns/instruction**. A PRU0
firmware exposes a 1-byte RPMsg command protocol; the host drives it over SSH.
Port of the BeagleY-AI (AM67A, 8 ns) pulse-I/O work — see `..\BeagleY-AI\`.

**Verified (Keysight 33521B AWG, crystal-traceable):**
| axis | result | how |
|---|---|---|
| output generation | **5 ns** cycle-exact (`__delay_cycles`) | scope |
| timebase accuracy | tick = **5.000 ns** ±0.014% (slope 0.99986) | AWG staircase (cmd 0x42) |
| runtime window control | exact 5 ns (HW LOOP, `pru_delay.asm`; slope 1.0000, resid 0.16 ns) | AWG strobe (cmd 0x43) |

Honest limits and the full story: [docs/VERIFICATION.md](docs/VERIFICATION.md),
[docs/GOALS.md](docs/GOALS.md). Full manual index: [docs/README.md](docs/README.md).

---

## Repo layout

```
firmware/      PRU0 firmware (main.c, pru_delay.asm, Makefile) + stock TI config
               + pru-support/  (vendored TI headers + rpmsg_lib.lib -> offline build)
overlay/       BB-PRUOUT-P8-00A0.dts  (U-Boot pinmux overlay: P8_11/P8_12 out, P8_16 in)
host/          bb.py (SSH runner) · provision.py (one-command bring-up)
               awg.py + run_awg_bbb.py (SCPI AWG verification)
scripts/       deploy · verify_timebase · verify_window · demo · reboot   (run via bb.py)
docs/          ARCHITECTURE · GOALS · FIRMWARE · OVERLAY · HOST_TOOLS · VERIFICATION · OPERATIONS · TROUBLESHOOTING
results/       awg_staircase_avg.csv · awg_strobe_avg.csv
```

The repo is **self-contained**: the TI PRU support subset is vendored under
`firmware/pru-support/`, so the board builds offline (no support-package clone).

---

## Quickstart — provision a fresh board

**1. Wire the board** (BBB P8 header):

| purpose | pin |
|---|---|
| out0 (5 ns pulse / AWG trigger) | **P8_11** |
| out1 (2nd channel) | **P8_12** |
| input (loopback / AWG echo, **0–3.3 V only**) | **P8_16** |
| GND | P8_1 / P8_2 |

**2. Install the host dep and provision** (from the repo root):
```
pip install -r requirements.txt
BB_HOST=beagle.local python host/provision.py      # or: python host/provision.py <ip>
```
`provision.py` verifies the target is an AM335x BBB, pushes the firmware, builds
it, installs the pinmux overlay (reboots once), loads it via remoteproc, and
confirms the firmware answers PING/VERSION. Idempotent — safe to re-run.

**3. Confirm** — the provisioner prints `PROVISION OK` and the firmware version.

> Login defaults: `BB_USER=xilinx`, `BB_PW=xilinx`. `beagle.local` is mDNS
> (survives DHCP changes); if it doesn't resolve, pass the board's IP.

---

## Day-to-day

All run from the host as `BB_HOST=<board> py host/bb.py scripts/<name>`:

| command | what |
|---|---|
| edit `firmware/main.c` → `py host/bb.py --put firmware/main.c ~/pru/pru0_pulse_io/main.c` → `scripts/deploy.sh` | rebuild + reload |
| `scripts/demo.sh` | 5 ns output demo on P8_11 (scope it) |
| `scripts/verify_timebase.sh` | AWG staircase — timebase accuracy (needs AWG) |
| `scripts/verify_window.sh` | AWG window strobe — 5 ns window control (needs AWG) |
| `scripts/reboot.sh` | reboot (re-apply overlay) |

AWG verification wiring: `P8_11 → AWG ext-trig`, `AWG out → P8_16` (0–3.3 V),
common GND, AWG on the LAN (`AWG_IP` in the verify scripts, default 192.168.0.7).

### RPMsg command protocol (1 byte + payload → reply)
`0x01` PING · `0x03` VERSION · `0x10` R1 pattern · `0x20/0x21/0x22` fixed pulses ·
`0x30/0x31/0x32` output 5 ns demos · `0x34/0x35` out0→out1 delay · `0x36` 10 Hz square wave ·
`0x42` AWG timebase shot · `0x43` window strobe. Details in
[docs/FIRMWARE.md](docs/FIRMWARE.md).

## RTASE_V1 RF1 end-to-end demo

The RTASE daughterboard uses three PRU0 outputs:

| function | BBB pin | PRU bit | RTASE net |
|---|---|---|---|
| select bit 0 | P8_11 | R30.15 | SYM.A0 |
| select bit 1 | P8_12 | R30.14 | SYM.A1 |
| DAC update | P9_31 | R30.0 | LDAC.A |

Provision once (the first run reboots to apply the P9_31 overlay):

```powershell
$env:BB_HOST='192.168.50.2'
python host\provision_rtase.py 192.168.50.2
```

Then run a slow, easy-to-see four-level RF1 sequence:

```powershell
python host\rtase.py status
python host\rtase.py demo --pattern ramp --shots 100 --delay-ns 100 --pulse-ns 20 --gap-us 100000
```

Use `--pattern random --seed 1234` after the deterministic `0,1,2,3` ramp is
visible.  The host preloads packed two-bit values into PRU local RAM; the timed
loop does not access Linux, SSH, or the network.  `--delay-ns` is quantized to
5 ns but still includes a fixed firmware/CPLD path offset that must be measured
before it is treated as an absolute PD-to-output delay.

---

## Board identity (verified 2026-06-17)

`TI AM335x BeagleBone Black`, Debian 13 (trixie) kernel `6.18.32-bone35`,
`pruss_ocp_gclk = 200.000 MHz → 5.000 ns/cycle`. Login `xilinx`/`xilinx`.

⚠️ **Not the only xilinx/xilinx board on the LAN** — a ZynqMP ZCU111 (`pynq`) is
also present. `provision.py` checks `/proc/device-tree/model` first so it never
provisions the wrong board.

| board | precise timing |
|---|---|
| **BeagleBone Black** (this repo) | PRU 200 MHz = **5 ns** |
| BeagleY-AI (AM67A) | ECAP = 8 ns (no PRU) |
| ZynqMP ZCU111 (RFSoC) | FPGA ~2 ns, RFDC ~150–250 ps |
