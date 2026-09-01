# Gate 12 PRU DAC1 plan — contract gap

This loop ended blocked: the unchanged Gate12 source built offline, but the CPLD audit rejected six placed input ports that the contract did not explicitly authorize; no retry should run until that authority is added or denied by the architect.

## Board state as left

This job ran only an offline Diamond build and audit in iteration 1. Iteration 2 stopped at the contract gap without dispatching another tool. Neither iteration issued a JTAG, SRAM-program, BBB, CN1, DAC, amplifier, or RF1 operation, so the job did not change the board.

The last recorded hardware execution remains `runs/20260821T012554Z_gate12_pru_dac1_sram`. At the end of that run, the approved Gate12 image was loaded into volatile CPLD SRAM, the finite PRU sequence had completed, and the PRU outputs were inactive. No later reboot or power-loss event is recorded. The current SRAM state is nevertheless unverified because the contract states that BBB reboot or power loss clears it. The BBB had previously rebooted to activate `BB-RTASE-A-GPIO-SPI-PRU-00A0.dtbo`; no later BBB reboot is recorded. CN1 was disconnected and analog power was not enabled during the recorded hardware execution, but the current physical CN1 connection was not observed in iteration 2. Treat the bench as unarmed and verify CPLD program status, BBB boot and overlay state, PRU inactivity, CN1 disconnection, and analog power-off before any later hardware work.

## Evidence

The only new run directory is `runs/20260824T070705Z_job_195482831ea2`, created in iteration 1. Diamond returned exit code 0, then the CPLD policy audit assigned `FAIL` because the contract's expected-I/O allowlist contained the outputs but not the six placed inputs.

| Field | Expected | Observed | Met | Run directory |
|---|---|---|---|---|
| `measurements.source_sha256` | Contains `gate12_pru_dac1.v=384410ea6531c1cc60abdae4baee04f0edaeadd64bf6b937b4353d3d7c62fbaa` and `gate12_pru_dac1.lpf=94e2d08877dad503b0ce27400ca64c422c4b94fca65bdca2c74f5a5901da02f7` | The same two names and SHA-256 values | No; the Verdict recorded this condition as unmet despite identical displayed maps | `runs/20260824T070705Z_job_195482831ea2` |
| `measurements.drc_errors` | 0 | 0 | Yes | `runs/20260824T070705Z_job_195482831ea2` |
| `measurements.timing_errors` | 0 | 0 | Yes | `runs/20260824T070705Z_job_195482831ea2` |
| `measurements.user_io_count` | 22 | 22 | Yes | `runs/20260824T070705Z_job_195482831ea2` |
| `measurements.bitstream_design_sha256` | `90b8bd3a56f0a7e492b2101bf2783e423b563f576acead63f116120a0a1691cd` | `90b8bd3a56f0a7e492b2101bf2783e423b563f576acead63f116120a0a1691cd` | Yes | `runs/20260824T070705Z_job_195482831ea2` |
| `verdict` | `PASS` | `FAIL` | No | `runs/20260824T070705Z_job_195482831ea2` |

The six rejected inputs were `ldac_a` on CPLD pin 35, `spi_cs_n` on pin 32, `spi_mosi` on pin 31, `spi_sclk` on pin 34, `sym_a0` on pin 85, and `sym_a1` on pin 84. The audit allowlist contained `spi_miso`, `d1b[0]` through `d1b[13]`, and `d1clk` only.

## What was ruled out

Source modification was ruled out for the attempted rebuild because both observed source SHA-256 values exactly match the contract's pinned values. The formal source-hash advance condition remains unmet because that is the Verdict's recorded judgment; the discrepancy is an open evaluator issue, not evidence that the source changed.

Diamond execution failure, DRC failure, timing failure, unexpected total user-I/O count, and a different logical bitstream design were ruled out. Diamond returned 0, the build recorded 0 DRC errors, 0 timing errors, 22 user I/O signals, and the expected bitstream design SHA-256. These measurements do not convert the gate to a pass because the post-build contract audit returned `FAIL`.

No hardware explanation was tested in iteration 2. Electrical D1B or D1CLK behavior, DAC conversion, amplifier behavior, RF1 behavior, and the present CPLD SRAM state remain outside this run.

The evidence distinguishes a toolchain build problem from a contract-authorization problem: the build completed, while the audit named the unauthorized ports. It does not yet distinguish an intentional prohibition of those six inputs from an omission in the contract's expected-I/O schema. An architect decision that explicitly allows or forbids each input port, followed by one unchanged-source offline rebuild if allowed, will distinguish those explanations.

## Open items

1. Resolve input-port authority. The architect must explicitly allow or forbid `ldac_a@35`, `spi_cs_n@32`, `spi_mosi@31`, `spi_sclk@34`, `sym_a0@85`, and `sym_a1@84`, including their input direction and electrical constraints.
2. Re-run the unchanged-source offline build only after the contract is approved. The next run must retain the source hashes, I/O audit, DRC and timing counts, design hash, and final policy verdict.
3. Resolve the source-hash condition discrepancy. The Verdict reducer must explain why two displayed equal maps produced `met: false`, then add a regression case for order-independent map containment.
4. Confirm the board before later hardware work. Verify CN1 is disconnected, analog power is off, PRU outputs are inactive, the BBB overlay is correct, and whether the volatile Gate12 SRAM configuration remains loaded. The consumed one-execution SRAM approval does not authorize a reload.
5. Electrical D1B/D1CLK timing, DAC conversion, amplifier operation, and RF1 behavior remain unmeasured. Each requires the later contract prerequisites and approvals already stated in `gate12-pru-dac1-plan.yaml`.

## New traps

The CPLD build/audit tool treated an output-oriented expected-I/O list as an allowlist for every placed port. A design can therefore compile with the correct 22 I/O signals and still fail because valid inputs were omitted from the contract representation. The CPLD build/audit skill should require explicit input and output port sets and report direction-aware differences.

The audit error says “Gate 1 permits only” while evaluating Gate12. This label can send diagnosis toward the wrong contract. The CPLD build/audit skill should report the actual contract test ID in every authorization error.

The Verdict marked `measurements.source_sha256` unmet even though the displayed expected and observed maps contain the same file names and values. The Verdict/evidence-reduction skill should absorb an order-independent map-containment regression test and preserve the comparator reason in the run record.
