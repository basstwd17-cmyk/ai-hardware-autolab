# GPIO-SPI verification record and PRU-to-DAC1 plan

**Date:** 2026-08-20; implementation updated 2026-08-21
**Scope:** RTASE channel A only (BBB -> CPLD -> U6/DAC1 -> U5 -> RF1)  
**Hardware state at end of this record:** Gate12 is loaded in volatile CPLD SRAM. CODE0..3 contain the safe values `[0,1,2,3]`; the finite PRU test has ended and reports outputs inactive. CN1 analog power and RF1/RF2 operation remain outside this execution.

## 1. Verified result

GPIO-based SPI communication between the BBB and the MachXO2 CPLD is proven bidirectional. The actual command and result were:

```text
python host/rtase_spi.py --transport gpio --half-period-us 20 \
  set-verify 0x0001 0x0123 0x2345 0x3ffe

written:  [0x0001, 0x0123, 0x2345, 0x3ffe]
readback: [0x0001, 0x0123, 0x2345, 0x3ffe]
bit_errors: 0
result: PASS
```

The first GPIO result was shifted one bit left. The host transport was fixed to sample MISO immediately **before** raising SCLK, which is SPI mode 0 for this CPLD implementation. This was a host-only correction; no additional CPLD program operation was needed.

Standard BBB McSPI (`/dev/spidev0.0`) is not validated for receive. It showed all-zero or all-one receive data despite an externally correct CPLD MISO waveform. The configured GPIO transport is the current known-good startup path. McSPI may be repaired later for speed, but it is not a dependency for the initial CODE register setup.

## 2. Evidence and exact artifacts

| Item | Result / role | Persistent record |
| --- | --- | --- |
| JTAG target | One MachXO2, IDCODE `0x012BC043` | `contracts/gate11-gpio-spi-code-registers.yaml` |
| Gate10 clocked MISO proof | Scope captured `a55a3cc3f00f` on the physical MISO path while McSPI RX failed | `runs/20260820T055246Z_gate10_scope_clocked_return_5838ca97/` |
| Gate11 build | 0 DRC errors, 0 timing errors, 84.452 MHz reported maximum | `runs/20260820T064141Z_gate11_gpio_spi_code_regs_build/` |
| Gate11 SRAM program | Passed once; bitstream SHA-256 `c09323826a08f203413a29b6696abe7693cdaf9561af0d3bdf2a110d4cf3c029` | `runs/20260820T064639Z_gate11_gpio_spi_code_regs_sram/` |
| Host protocol tests | `8 passed`; pytest cache write warning is non-functional | `tests/test_rtase_spi_protocol.py` |

Gate11 source and candidate files:

- `spi_gpio_code_regs/spi_gpio_code_regs.v` - SPI slave and writable/readable 14-bit `CODE0..CODE3` registers.
- `spi_gpio_code_regs/spi_gpio_code_regs.lpf` - U2 pins 31/32/33/34 only.
- `candidates/gate11-gpio-spi-code-regs/` - immutable programmed candidate.
- `contracts/gate11-gpio-spi-code-registers.yaml` - approval consumption, bitstream hash, JTAG result, and readback result.

BBB-side files used by the verified transport:

- `../BeagleBone-Black/host/rtase_spi.py` - `--transport gpio` implementation; maps P9_17/P9_18/P9_21/P9_22 to GPIO0_5/0_4/0_3/0_2.
- `../BeagleBone-Black/overlay/BB-RTASE-A-GPIO-SPI-00A0.dts` - selected boot overlay; deliberately disables `spi0` and assigns those four pins as GPIO.
- `../BeagleBone-Black/firmware/main.c` - existing PRU protocol, including sequence buffer and run commands. It is not used by Gate11 itself.

## 3. Reviewed analog path and operating implications

Channel A is the only first-output target:

```text
CODE0..3 --SPI--> CPLD
SYM.A[1:0] --PRU P8_11/P8_12--> CPLD --D1B[13:0]--> U6 AD9744
PRU P9_31 --LDASC.A--> CPLD --D1CLK--> U6 CLOCK
U6 IOUTA --DAC1_OUT--> U5 THS3491 --49.9 ohm--> RF1
```

The `LDAC.A` name belongs to the project control net. U6 is an AD9744 and has no LDAC pin; its output data updates on the rising edge of `D1CLK` (U6 pin 28). The AD9744 needs data stable for at least 2 ns before and 1.5 ns after that edge, and has a 1.5 ns minimum clock pulse width. A PRU-generated 20 ns pulse is therefore suitable only when the selection is set and held before the pulse.

The schematic uses U6/U1 AD9744 14-bit current-output DACs, with `2 kohm` full-scale resistors. U5/U7 are THS3491 amplifiers on CN1 `+15 V/GND/-15 V` rails. The channel-A feedback values (576 ohm and 63.4 ohm) give approximately `1 + 576/63.4 = 10.1 V/V`; RF1 has a 49.9 ohm series output resistor and must be characterized into a 50 ohm load.

There is no firmware shutdown boundary for the analog hardware:

- AD9744 `SLEEP` is tied to GND, so it is always awake when 3.3 V is present.
- THS3491 `PD#` is tied to `+15 V`, so it is enabled whenever CN1 is powered.
- Consequently, CN1 disconnected is the only current verified way to keep RF1/RF2 inactive.

## 4. Gate12 implementation and remaining plan

The build-only implementation is complete and pinned at
`candidates/gate12-pru-dac1/`. Build run
`20260821T005712Z_gate12_pru_dac1_build` passed with 22 exact user I/O,
zero DRC/timing errors, no D2 output, and no external signal selected as GSR.
The four startup CODE registers are all zero. The candidate remains **not
approved** for JTAG/SRAM programming.

The combined BBB source is
`../BeagleBone-Black/overlay/BB-RTASE-A-GPIO-SPI-PRU-00A0.dts`. Its preparer
`../BeagleBone-Black/host/prepare_gate12_overlay.py` defaults to staging the
DTBO and runtime files only: no active `uEnv.txt` edit and no reboot. Activation
requires the literal `APPLY_GATE12_OVERLAY` token, creates
`/boot/uEnv.txt.pre-gate12`, and still does not reboot unless `--reboot` is
separately supplied.

On 2026-08-21, with the top PCB physically separated, the combined DTBO and
runtime were installed and the boot entry was activated. A controlled reboot
was observed through SSH going down and returning. The active overlay identity
is `gate12-gpio-spi+pru0-channel-a`; all seven GPIO-SPI/PRU pads matched the
required mux values, the PRU service and `/dev/rpmsg0` were available, VERSION
and PING passed, and the firmware reported `outputs_active: false`. The shared
control lock was present and `/dev/spidev0.0` was absent as intended. The remote
compiled DTBO SHA-256 is
`ee08632d1e824402691ad5e5c7b9325a44ab5fb64e667b165c081628ad77f235`.
No CPLD SRAM program or analog-power operation was performed during that
overlay-activation step.

After the PCB was reconnected and the combined boot was verified again, the
user approved one Gate12 SRAM execution. Run
`20260821T012554Z_gate12_pru_dac1_sram` passed the one-device JTAG scan
(`0x012BC043`) and programmed the exact pinned bitstream
`dfedb3d06846b9c65020873ad26a7a634e5d49b4b6b1ad1e3880ee702f5ce2d4`.
GPIO-SPI then wrote/read `[0x0001,0x0123,0x2345,0x3FFE]` with zero bit errors.
Before the finite PRU run the codes were reduced to `[0,1,2,3]` and again read
back with zero errors. The PRU completed exactly four symbols `0,1,2,3`,
reported last symbol `3`, and returned to `outputs_active: false`.

This is intentionally a scope-limited digital result. It proves BBB GPIO-SPI
register access, PRU finite-run control, and the reviewed CPLD logical mapping
from `SYM.A` to D1B. It does not electrically measure the fourteen D1B traces
or D1CLK, and it does not prove DAC conversion, amplifier behavior, or SMA
output. Those remain end-to-end analog verification items.

`rtase.py` and `rtase_spi.py` now share
`/run/lock/rtase-control.lock`. A PRU buffer/run command and a GPIO-SPI CODE
transaction therefore cannot overlap: whichever starts second waits for the
first to finish. The system service creates this lock with mode `0666` after
the PRU RPMsg device becomes available.

### 4.1 Prepare the BBB before any new CPLD program

1. **Complete:** build and activate one combined boot overlay with the existing GPIO-SPI pins plus PRU0: P8_11 (`R30_15`), P8_12 (`R30_14`), and P9_31 (`R30_0`).
2. **Complete:** preserve `disable_uboot_overlay_audio=1` and `disable_uboot_overlay_video=1`; they release P9_31 and the physically connected channel-B HDMI pins.
3. **Complete:** reboot once and verify SSH, pinmux, PRU VERSION/PING, inactive PRU outputs, and the shared control lock. This reboot occurred before any Gate12 SRAM program.

### 4.2 Implement a channel-A-only CPLD design

The new design must retain Gate11's SPI registers and add only these outputs:

- inputs: `SPI.MOSI`, `SPI.CS`, `SPI.CLK`, `SYM.A0`, `SYM.A1`, `LDASC.A`
- output: `SPI.MISO`
- outputs: `D1B[13:0]` on U2 pins `[56,57,58,59,60,61,62,63,65,67,68,69,70,71]`
- output: `D1CLK` on U2 pin 73

`D2B[13:0]`, `D2CLK`, RF2/channel B, status, and unrelated GPIO outputs must remain unplaced or inputs. The host must complete CODE write/readback before a PRU run and reject a CODE write while a run is active.

Timed PRU order for each symbol:

```text
set SYM.A[1:0] -> wait 100 ns minimum -> pulse P9_31 high for 20 ns ->
hold selection after the pulse
```

The 100 ns margin deliberately exceeds the DAC's few-nanosecond requirement and covers BBB-to-CPLD and CPLD-to-DAC propagation. Gate12 must state this timing assumption explicitly and build with reviewed I/O placement.

### 4.3 Verification gates

| Gate | Preconditions | What is measured | Stop condition |
| --- | --- | --- | --- |
| Digital timing | CN1 disconnected; DAC/amp status recorded | `SYM.A0`, `SYM.A1`, P9_31, D1CLK, and representative D1B bits | Selection not stable before D1CLK, wrong pin placement, or any D2 output |
| DAC code response | Gate12 digital timing passed; CN1 still disconnected | Controlled low-delta code steps and DAC1_OUT behavior | Unexpected DAC1_OUT level/current or board heating |
| RF1 bring-up | Explicit approval; U5 installed; CN1 rails verified; RF1 has 50 ohm load | RF1 amplitude, settling, overshoot, supply current | Current limit, rail droop, abnormal temperature, unexpected RF1 amplitude |
| Automated run | All earlier gates passed | code write/readback, PRU run, scope capture, immutable run record | Any preflight or post-run check fails |

The first analog test must use small, non-calibration code differences chosen only after confirming the U6 MODE state and the measured DAC1_OUT baseline. Do not begin with `0x3FFF` or infer RF voltage directly from a raw DAC code.

## 5. Automation endpoint after Gate12 approval

The intended single host operation is:

```text
preflight
  -> verify BBB boot overlay and PRU PING
  -> verify JTAG ID and approved Gate12 bitstream hash
  -> program volatile SRAM (only with an explicit approval)
  -> GPIO-SPI CODE0..3 write/readback
  -> load PRU symbol sequence
  -> execute PRU run
  -> capture scope data and record configuration, hashes, and observations
```

No command in this document authorizes hardware programming, CN1 power, or RF output. Gate12 requires its own signed/approved candidate contract.

## 6. References

- `RTASE_V1_Schematic1_2026-08-13.pdf`, pages 1--4.
- AD9744 Rev. C data sheet, digital timing table and clock description.
- THS3491 Rev. C data sheet, supply, feedback, and output-drive guidance.
