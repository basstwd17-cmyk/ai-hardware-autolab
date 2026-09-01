# Gate 3 RF1 SPI-register candidate

This candidate supersedes the fixed-code proof image for the first real
end-to-end run.  It contains four writable/readable 14-bit registers, a
two-bit real-time selector, the DAC1 data bus, and the DAC1 clock-forward path.
DAC2 is not driven.

- run: `20260818T054253Z_gate3_rf1_spi_build`
- Diamond: `3.14.0.75.2`
- DRC: 0 errors, 0 warnings
- timing: 0 errors at 10 MHz SPI constraint (87.382 MHz reported maximum)
- user I/O: 22
- bitstream bytes: `17616`
- raw SHA-256: `9dd18b6d7ca61e84e2cb1587349feb24b55295fbad1e0102ab15fced6121136f`
- design SHA-256: `56f9e9c204a099d5ad03c4589da5aefbb2471cf30d92466f98216c3f3b57adce`

Pinned does not mean approved for SRAM programming or analog power.
