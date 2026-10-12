# How the McSPI receive fault was isolated — what was ruled out, and how

**Date:** 2026-08-20
**Companion to:** `2026-08-20-gpio-spi-verification-and-pru-dac-plan.md`, which
records the conclusion and the working GPIO transport. This file records the
*elimination path*: the hypotheses that were killed, the measurement that killed
each one, and the tooling traps found on the way. Without it the next person
re-runs the same eight tests.

## 1. The symptom, and why it was hard to read

BBB `/dev/spidev0.0` returned a constant for every transfer while the CPLD held
a correct pattern on MISO. The constant was **not stable across runs**:

| When | CPLD image | rx |
|---|---|---|
| 2026-08-19 | gate8 static high (pin 33 = 1) | `000000000000` |
| 2026-08-20 early | gate8 static high, after BBB reprovision | `ffffffffffff` |
| 2026-08-20 | gate4 loopback (`miso = cs_n ? 0 : mosi`) | `ffffffffffff`, then `000000000000` |

A driven line cannot answer the same stimulus with two different constants, so
for most of the session the leading hypothesis was that nothing was driving
P9_21 — the CPLD had lost its volatile SRAM, or the return net was open. Both
turned out to be wrong, and both cost time. **The two constants came from the
receiving end, not the transmitting end**, which is exactly what a broken
receive path looks like.

One inference that was available earlier than it was used: with the gate4 image,
`spi_mosi` and `spi_cs_n` are inputs with internal `PULL:DOWN` (see the `.pad`
report). Under every input condition — CS idle high, CS low with a pattern
containing zero bits, or either input open — pin 33 must go low for part of the
frame. `ffffffffffff` was therefore impossible from a connected, configured
CPLD, and that alone pointed at the receiver.

## 2. What was eliminated

| Hypothesis | Killed by | Evidence |
|---|---|---|
| Data lines reversed (D0/D1 swapped) | `CH0CONF` read from the controller | `IS=0` (receive on D0/P9_21), `DPE0=1`, `DPE1=0` (transmit on D1/P9_18) — the standard master configuration |
| Device tree requests the reversed direction | live DT node | `ti,pindir-d0-out-d1-in` absent |
| Pad receiver disabled on MISO | pinctrl-single debugfs | pin 85 (P9_21) conf `0x28` → `RXACTIVE=1`, mux mode 0, pull disabled. P9_18 `0x08`, P9_17/P9_22 outputs |
| Wrong sampling edge / SPI mode | mode 0 and mode 1 compared | both `000000000000`, 0/64 matches. A half-bit misalignment would produce a *shifted* pattern (`007fd52a9e61`), never all zeros |
| Controller never armed / never received | `CH0CTRL`, `CH0STAT` during traffic | `EN=1`, `RXS=1` — the receive register was loaded, with zeros |
| Test programs never passed an rx buffer | source review of both testers | `spi_loopback_native.c` sets `.rx_buf`; `spi_loopback.py` sets `rx_buf=ctypes.addressof(rx)`. Native zero-initialises `rx`, so `ff` could not have come from an untouched buffer |
| MISO net open / CPLD output dead | scope at both ends | U2 pin 33 and BBB P9_21 carry the same waveform |
| Scope 50 Ω input pulling the line down | front-end query | all four channels `DCLimit` = 1 MΩ, 10:1 probes |
| CPLD lost its SRAM configuration | scope, bus idle | MISO sits at **+0.084 V** while CS is idle high, which is what gate4's `miso = cs_n ? 0 : mosi` drives. A tri-stated pin would not |

Idle levels measured on the four SPI signals with no traffic running (5000
points each, 24 ns/sample):

| Channel | Signal | Mean |
|---|---|---|
| CH1 | SPI.CS | **+3.459 V** (idle high) |
| CH2 | SPI.MOSI | +0.048 V |
| CH3 | SPI.MISO | **+0.084 V** (driven low by the CPLD) |
| CH4 | SPI.CLK | +0.104 V (idle low, consistent with mode 0) |

## 3. The measurement that split it

Gate10 put a **fixed clocked response** (`a55a3cc3f00f`) on MISO, so the
returned data no longer depended on MOSI or on the echo path:

- scope on the physical MISO path: `a55a3cc3f00f` present
- GPIO bit-banged read of the same pins: `a55a3cc3f00f` — correct
- `/dev/spidev0.0` on the same board, same wires, same frame: `000000000000`

Same copper, same CPLD image, two receivers, one works. That isolates the fault
to the McSPI receive path or its driver, and it is the reason the project moved
to the GPIO transport rather than continuing to debug the controller.

## 4. Traps found while measuring (each cost time once)

**`/dev/mem` returns `Bus error`, and it is not a permission problem.**
`omap2-mcspi` gates the module's interface clock under runtime PM; reading an
OMAP peripheral register with its clock gated raises an external abort that
reaches user space as SIGBUS. The `mmap` itself succeeds. Pin the device awake
through the kernel's own knob before reading, and restore it afterwards:

```sh
echo on > /sys/bus/platform/devices/48030000.spi/power/control
```

**`CH0CONF` read while the bus is idle can be stale.** The driver rewrites the
channel configuration for each transfer. Read it *during* traffic and report the
distinct values seen, not one sample.

**The scope was in `AUTO` trigger with `TIM:POS = +3.1536 ms`.** A 6-byte frame
at 100 kHz lasts ~480 µs, so the displayed window sat about 3 ms *after* the
trigger — outside the frame entirely — while `AUTO` let it self-fire with no
trigger at all. Any "the waveform looks right" judgement made in that state is
not evidence. Frame captures now use `NORMal`, 50 µs/div, trigger at 8.3 % from
the left, CH1 falling at 1.64 V.

**`/proc/device-tree/ocp/spi@48030000` does not exist on Debian 13.** A fixed
device-tree path prints nothing and looks like an absent property. Resolve
through sysfs instead:

```sh
node=$(readlink -f /sys/class/spi_master/spi0/device/of_node)
```

**A diagnostic's exit code must mean "the diagnostic ran", not "the DUT
passed".** A script returning 1 on `rx != tx` makes a legitimate negative result
land in the run record as a tool failure, and the record's verdict stops meaning
anything. Judgement belongs in stdout; the exit code belongs to the tool.

**The host GPIO transport had a one-bit shift.** Fixed by sampling MISO
immediately *before* raising SCLK, which is mode 0 for this CPLD. Host-side
only — no CPLD reprogram was involved.

## 5. What is still open about McSPI

The fault is isolated, not diagnosed. Nothing here explains *why* the controller
loads zeros while `RXS=1` with a valid signal on an enabled pad. It is no longer
on the critical path — Gate11 proved the GPIO transport carries the CODE
register protocol with 0 bit errors — but if McSPI is ever wanted for speed, the
next steps are the internal-loopback check (`SPI_LOOP` /
`MODULCTRL.SYSTEM_TEST`, see `diagnostics/mcspi_loopback_selftest.sh`) and the
FIFO/`XFERLEVEL` path, neither of which was reached.
