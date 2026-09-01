---
name: scope-measure
description: Measure a signal on the RTM3004 oscilloscope - jitter, delay, levels, timing, waveform capture. Use whenever a measurement recipe has to be written or changed, before writing any SCPI. Carries the instrument's known traps, which silently produce plausible-but-wrong numbers if ignored.
---

# RTM3004 measurement

Read this before writing SCPI. Every trap below has already cost a session
once. The numbers an oscilloscope returns are always plausible, so a wrong
setup does not look like an error -- it looks like a result.

## The instrument

```
Rohde&Schwarz RTM3004   S/N 1335.8794k04/106125   FW 01.700
TCPIP::192.168.50.3::5025::SOCKET   (PC side 192.168.50.1/24)
Options: K1 K2 K3 K5 B1 K31 K6 K7 K15 B6 B245 K36 K37
```

Relevant options: **B1** 16 logic channels (needed for buses wider than 4),
**B6** internal generator, **K15** history and segmented memory, **K37** spectrum.

## What you write, and what you must not rewrite

`hwtools/instruments/rtm3004.py` is the fixed part: identify, error queue,
acquisition state, termination check, data retrieval, screenshot, safe state.
**Do not add configuration to it.**

You write a *recipe*: channels, scale, timebase, trigger, which measurement,
how many samples, what counts as a pass. Recipes are per experiment and are
expected to be thrown away. That split is deliberate -- the previous driver
froze one delay measurement into a 200-line `configure()` and it could not be
reused for anything else.

## Traps

### 1. Coupling selects termination, and 50 ohm loads the DUT

There is no `CHANnel<m>:IMPedance` command. Termination comes from coupling:

| `CHANnel<m>:COUPling` | Termination |
|---|---|
| `DC` | **50 ohm** |
| `DCLimit` | 1 Mohm |
| `ACLimit` | 1 Mohm, AC |

CH1 and CH2 were found on `DC` (50 ohm). A 4 mA LVCMOS33 output into 50 ohm is
about 66 mA at 3.3 V: the level reads low and the pin is stressed. It looks
like a failing design.

Call `scope.assert_high_impedance(*channels)` in every recipe that probes a
logic output. Never skip it because "the channel was fine last time" -- the
front panel changes it.

### 2. `ACQ:STAT?` alone cannot tell you the scope is armed

While an Nx Single acquisition is armed and waiting, this instrument reports
`STOP`. Reading only `ACQ:STAT?` makes a healthy measurement look dead.

Always pair it with `STAT:OPER:COND?` bit 3 (waiting for trigger).
`AcquisitionState.ready` does this. Meanings: `RUN` or WTRigger = ready,
`COMPlete` = done, `BREak` = aborted.

### 3. A rejected command returns nothing at all

The instrument answers an unknown header with silence and a `-113` in the error
queue. Two consequences:

- The socket read times out. `ScpiTransport` recovers via `resync()`; do not
  reintroduce `socket.makefile()`, which is permanently dead after one timeout.
- **Call `check_errors()` after every configuration group.** A rejected setup
  command leaves the *previous* setting in place and the run continues,
  measuring the wrong thing and reporting success.

### 4. `9.91E37` is "no value", not a number

Returned when no measurement has been made yet. Averaging it into a result
poisons the whole series. Anything above `1e36` must be discarded, not used.

### 5. Three ways to get jitter, three different answers

| Path | Command | What it is |
|---|---|---|
| Instrument statistics | `MEAS<m>:STAT:VAL:ALL?` | scope's own per-acquisition measurement |
| Raw waveform | `CHAN<n>:DATA?` + header | your own computation from samples |
| History segments | `CHAN<n>:HIST:...` (K15) | per-segment values from segmented memory |

Pick one deliberately and **record which one in `measurements`**. A jitter
number without its path is not reproducible.

### 6. Free-running RUN does not give you the N you asked for

`RUN` keeps accumulating between polls, so statistics cover an unknown count.
A run that asked for 50 covered 218 and then 227. The mean was still valid, but
quoting it as "N=50" would have been false.

Use `ACQuire:NSINgle:COUNt <n>` then `SINGle` (`scope.arm_nsingle(n)`), wait via
`scope.wait_until_complete()`, and **verify the count actually reached** with
`MEAS<m>:RES:WFMC?` on every measurement place, not just the first. Verified:
requesting 200 gives exactly 200, requesting 50 gives exactly 50.

Nx Single needs the **K15** option. Without it the command is accepted and
ignored, and N silently free-runs -- worse than an error, because the wrong N
still gets quoted. `arm_nsingle()` checks for K15 first.

### 7. Fast segmentation drops waveforms from statistics

With fast segmentation on, not every acquisition reaches the measurement
statistics. If the recipe's claim is "N waveforms were measured", leave it off
and verify the count with `MEAS<m>:RES:WFMC?`.

### 8. Read the waveform header with the waveform

`CHAN<n>:DATA:HEAD?` gives XStart, XStop, RecordLength, ValuesPerSample. Without
it the sample array has no time axis, and a later timebase change silently
reinterprets an old capture. `read_waveform()` always fetches both.

Note `RecordLength` here is the *transferred* length, which follows
`CHAN<n>:DATA:POINts` (display data vs acquisition memory) and will not match
`ACQ:POIN?`. Observed: `ACQ:POIN?` 5000, transferred 1200.

### 9. Screenshots must come from the live screen

`HCOP:LANG PNG` then `HCOP:DATA?`. Never read back a file from the instrument's
internal storage -- it may be from an earlier run. Validate the PNG magic bytes;
a truncated transfer produces an unopenable artifact that nobody notices until
the run is being reviewed.

### 10. External AWG re-arm is milliseconds

For the Keysight 33521B used as an external stimulus: trigger re-arm takes
milliseconds, so a burst with n>1 mostly gets missed. Fire single shots from the
host at roughly 6 ms spacing and average over repetitions instead.

### 11. The front panel changes under you

Two settings were observed changing with no command from the automation:

- **Probe attenuation.** CH3 went 1:1 to 10:1 the moment a passive probe was
  plugged in -- the instrument detects R&S probes and rescales itself.
  Attenuation multiplies every voltage, so a reading without it is not
  comparable to one from another day.
- **Coupling.** CH1 was found on `DC` (50 ohm) in one session and `DCLimit`
  (1 Mohm) in the next.

Consequence: never trust the contract's record of front-end state, and never
trust the previous run's. `measure_scope()` reads `front_end()` for every
probed channel and records it, so this cannot be lost by rewriting a recipe.
Do not move that into a recipe.

### 12. SCPI forms that look right and are not

| Wrong | Right |
|---|---|
| `CHANnel<m>:IMPedance?` | `PROBe<m>:SETup:IMPedance?` |
| `PROBe<m>:SETup:ATTenuation:MODE?` | `PROBe<m>:SETup:MODE?` |
| `CHANnel:HISTory:STATe?` | per channel: `CHANnel<m>:HISTory:...` |

When unsure, grep the manual rather than guessing:
`tmp/pdfs/RTM3000_UserManual_en_11.txt`.

## Writing a recipe

1. `gate.require("oscilloscope_capture")` and `require_verified("scope.model", ...)`.
2. `scope.identify()` into `ToolResult.versions`.
3. `scope.assert_high_impedance(...)` for every probed logic output.
4. Configure, then `check_errors("<what you just set>")` after each group.
5. Acquire. Judge readiness with `AcquisitionState.ready`, never `ACQ:STAT?` alone.
6. Verify the count actually reached with `MEAS<m>:RES:WFMC?`.
7. Put N, sample rate, measurement type and retrieval path into `measurements`.
8. Judge against `gate.pass_criteria()` only. Thresholds come from the test
   spec, never from a model's opinion.
9. On any failure path, `scope.emergency_stop()`.

## Do not

- Enable a generator output without an explicit gate check.
- Judge PASS/FAIL from anything but the contract thresholds.
- Leave `n` out of the recorded measurements because "it was obvious".

## Reference

- Fixed driver: `QKD_PROJ/hwtools/instruments/rtm3004.py`
- Contract: `QKD_PROJ/contracts/board-map.yaml` (`scope:` section)
- Manual text: `tmp/pdfs/RTM3000_UserManual_en_11.txt`
- Prior working driver, for behaviour not yet captured here:
  `STM32Cube/delay_generator_g474ceu6/tools/automation/common/rtm3004.py`
