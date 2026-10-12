# Evidence audit

Contracts: **17** · runs: **85** · cited runs checked: **20**

Errors: **5** · warnings: **6** · notes: **63**

An *error* is a contradiction between a contract and the run it cites. A *warning* is a
claim that cannot be traced. A *note* is evidence that exists but nothing points at, or a
number the contract states that this run never recorded -- often legitimate, since some
results live in side files rather than in the manifest.

## Errors (5)

| kind | contract | run | detail |
|---|---|---|---|
| `unscoped_claim` | gate1-test-spec.yaml | `—` | build_result states user_io_count=1, drc_errors=0, timing_errors=0, max_frequency_mhz=104.943 but names no run, so nothing verifies it |
| `run_record_missing` | gate10-spi-clocked-return.yaml | `20260820T055246Z_gate10_scope_clocked_return_5838ca97` | cited at scope_capture.result: runs/20260820T055246Z_gate10_scope_clocked_return_5838ca97 exists and holds capture.png, summary.json, waveforms.json.gz, but wrote no RunRecord. The evidence is there and no tool pinned it. |
| `primary_run_missing` | gate10-spi-clocked-return.yaml | `20260820T063111Z_gate10_gpio0_3_clocked_return` | cited at gpio_spi_fallback_result, but runs/20260820T063111Z_gate10_gpio0_3_clocked_return is not on disk. The claims in this block have no evidence behind them; only an approved re-run can restore it. |
| `unscoped_claim` | gate11-gpio-spi-code-registers.yaml | `—` | gpio_protocol_result states bit_errors=0 but names no run, so nothing verifies it |
| `pinned_file_drift` | gate9-bbb-host-tooling.yaml | `—` | scripts.spi_clocked_response_verify: BeagleBone-Black/diagnostics/spi_clocked_response_verify.sh on disk hashes to 735c91d33748…, the contract pins 25589ab5151c…. Either the file changed after signing, or the contract records a version that no longer exists. |

## Warnings (6)

| kind | contract | run | detail |
|---|---|---|---|
| `result_without_run` | gate1-test-spec.yaml | `—` | contract records a result block but cites no run (status: blocked_pending_human_review) |
| `evidence_not_registered` | gate12-pru-dac1-plan.yaml | `20260821T012554Z_gate12_pru_dac1_sram` | runs/20260821T012554Z_gate12_pru_dac1_sram/digital_verification.json matches the contract, but the run never recorded it as an artifact -- the digest is pinned by hand, not by the tool |
| `unreadable_run` | — | `20260814T020826Z_probe_comp_check` | no readable manifest |
| `unreadable_run` | — | `20260820T054858Z_gate10_scope_clocked_return_0324a1de` | no readable manifest |
| `unreadable_run` | — | `20260820T054956Z_gate10_scope_clocked_return_2f5ec30c` | no readable manifest |
| `unreadable_run` | — | `20260820T055246Z_gate10_scope_clocked_return_5838ca97` | no readable manifest |

## Notes (2)

| kind | contract | run | detail |
|---|---|---|---|
| `unevidenced_claim` | gate12-pru-dac1-plan.yaml | `20260821T012554Z_gate12_pru_dac1_sram` | digital_execution_result.gpio_spi_full_width_test.bit_errors = 0 is stated in the contract but not recorded in this run |
| `unevidenced_claim` | gate12-pru-dac1-plan.yaml | `20260821T012554Z_gate12_pru_dac1_sram` | digital_execution_result.gpio_spi_safe_run_codes.bit_errors = 0 is stated in the contract but not recorded in this run |

## Uncited runs (61)

Runs that exist and record a verdict, which no contract points at. Most are
superseded builds and are harmless. They are listed because `runs/` is gitignored:
evidence nothing cites is evidence nobody will find, and it is the pool a claim gets
silently attached to later.

| contract the run names | uncited runs |
|---|---|
| gate9-bbb-host-tooling.yaml | 18 |
| gate1-test-spec.yaml | 9 |
| scope-selftest-spec.yaml | 6 |
| gate3-rf1-spi-demo.yaml | 6 |
| gate3-spi-path-diagnostic.yaml | 5 |
| gate2-bbb-cpld-loopback.yaml | 2 |
| gate4-spi-standard-loopback.yaml | 2 |
| gate4-spi-reverse-loopback.yaml | 2 |
| gate8-spi-standard-return-static-high.yaml | 2 |
| gate10-spi-clocked-return.yaml | 2 |
| gate11-gpio-spi-code-registers.yaml | 2 |
| gate12-pru-dac1-plan.yaml | 2 |
| gate5-spi-reverse-ungated-loopback.yaml | 1 |
| gate6-spi-return-static-high.yaml | 1 |
| gate7-spi-standard-ungated-loopback.yaml | 1 |

Full list: `py -3 -m ai_hardware_autolab.orchestrator.audit --json`.
