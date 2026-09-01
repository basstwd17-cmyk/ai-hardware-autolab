---
name: build-cpld
description: Build a Lattice MachXO2 design with Diamond and check the result against the contract - HDL/LPF authoring, pin placement, DRC, timing, bitstream hashing. Use before writing or changing any Verilog or LPF for the RTASE_V1 CPLD, and before running build_cpld(). Runs with no board attached; programming is a separate skill.
---

# Building the RTASE_V1 CPLD

Scope: source to bitstream. **No board, no JTAG cable, no power.** Putting a
bitstream into the chip is `program-cpld-sram`, a separate tool with separate
approval. Do not combine the two -- Gate 1 forbids nonvolatile flash entirely,
and a skill named "build-flash" invites exactly that mistake.

## The target

```
LCMXO2-4000HC-4TG144C   TQFP144, speed grade 4, VCCIO 3.3 V
Diamond 3.14.0.75.2 at C:\lscc\diamond\3.14   (not on PATH)
Synthesis: LSE
```

Gate 1 project: `QKD_PROJ/gate1_cpld/`. Keep it separate from
`C:\KIST\Lattice`, which is kept only for comparison. That older design takes
`sel0`/`sel1` on pins 35 and 34, which the schematic shows as `LDASC.A` and
`SPI.CLK` -- board nets, not spare debug pins. Do not merge the two.

## The check that matters

Not "did it compile". A design can compile cleanly, pass DRC with zero errors,
and still place an output on a board net. The pad report is parsed and compared
against the contract, and an unexpected port fails the build.

Gate 1's whole safety argument is: **one electrically isolated pin is driven and
nothing else.** If a port is added to the top module, `gate1-test-spec.yaml` and
the LPF must change in the same commit.

## Traps

### 1. `prj_project new` silently disables your LPF

It writes its own `<name>.lpf` and makes that the active logic preference. An
LPF added afterwards is accepted and then marked `excluded="TRUE"`:

```
WARNING - There already exists a 'Logic Preference'.
          The added resource 'gate1_gpio_toggle.lpf' is disabled.
```

The build then **succeeds** with every pin placed by tool defaults. Observed:
`dbg0` on pin 86 as `LVCMOS25_OUT` at 8 mA instead of pin 117 as `LVCMOS33_OUT`
at 4 mA. Exit code 0, DRC 0 errors.

`prj_src remove` does not dislodge it. `build_cpld()` works around this by
creating the project first, writing the constraints into the file Diamond
insists on, and then running the build as a second phase. It verifies the write
landed. Do not "simplify" that back into one phase.

### 2. There are two timing reports and one of them is fiction

| File | What it is |
|---|---|
| `<name>_impl1.twr` | post-place-and-route TRACE report -- **the real one** |
| `<name>_lse.twr` | synthesis stage, scored against a default 200 MHz LSE invents when no SDC is given |

On a 2.08 MHz design the LSE report showed **422 timing errors** while the real
report showed **0**, with 104.943 MHz maximum frequency. Score only reports
containing `Lattice TRACE Report`. The log line `Applying 200.000000 MHz
constraint to all clocks` / `1 constraints not met` comes from the same place
and is not a sign-off result.

`WARNING - synthesis: No user .sdc file` is expected: constraints are in the LPF.

### 3. Unstated properties become tool defaults

The earlier project left `DRIVE` and `SLEWRATE` out of the LPF and Diamond chose
8 mA, while the contract calls for 4 mA. Nothing warned. State every property
the contract names:

```
IOBUF PORT "dbg0" IO_TYPE=LVCMOS33 DRIVE=4 SLEWRATE=SLOW;
```

4 mA is also the figure behind the 1 Mohm probe requirement in `board-map.yaml`,
so it is not cosmetic.

### 4. The bitstream is not byte-reproducible

Diamond stamps the build time into the header:
`Date: Fri Aug 14 14:06:10 2026`. Two builds from identical sources differ by
exactly 2 bytes out of 16271, with all 971880 bits of logic identical.

So two hashes get recorded, answering different questions:

| Field | Question |
|---|---|
| `bitstream_sha256` | is this the exact file that was approved? |
| `bitstream_design_sha256` | did a rebuild produce the same design? |

Programming approval must pin a **specific built artifact**. "Rebuild and
compare the hash" does not work.

### 5. OSCH accuracy is not established

`OSCH` `NOM_FREQ` is a fixed list (2.08 default, up to 133.0 MHz). The accuracy
is **not** in the Diamond libraries or webhelp, and the MachXO2 datasheet is not
present locally.

Do not turn the nominal into a scope pass criterion. Gate 1 needs to show that a
safe output toggles repeatably, not that it is precisely 1 Hz. See
`gate1-test-spec.yaml`, `osch_accuracy`.

## Procedure

1. Read `contracts/gate1-test-spec.yaml` for the allowed top, pin, I/O type,
   drive and slew. Never take them from an older project.
2. Write or edit the HDL and LPF in `QKD_PROJ/gate1_cpld/`. State every
   contract-named property explicitly.
3. Run `build_cpld()` with an `IoExpectation` built from the contract.
4. Read the result, not the console: `user_io`, `user_io_count`, `drc_errors`,
   `timing_errors`, `timing_preferences`, `max_frequency_mhz`.
5. Confirm the resource report agrees. A correct Gate 1 build shows
   `Number of PIO sites used: 1 + 4(JTAG)`.
6. Record `bitstream_design_sha256` before proposing anything for programming.

## Do not

- Add a port "just for debugging" without changing the contract first.
- Accept a build because the exit code was 0.
- Use `.jed`. Gate 1 is SRAM, and the artifact is `.bit`.
- Set a frequency threshold from the OSCH nominal.

## Reference

- Tool: `QKD_PROJ/hwtools/cpld.py`
- Sources: `QKD_PROJ/gate1_cpld/gate1_gpio_toggle.{v,lpf}`
- Contract: `QKD_PROJ/contracts/gate1-test-spec.yaml`
- Human review still outstanding: `GATE1_REVIEW.md`
- Kept for comparison only, do not merge: `C:\KIST\Lattice`
