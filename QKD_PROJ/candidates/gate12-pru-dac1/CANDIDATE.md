# Gate12 PRU-to-DAC1 candidate

This directory pins the output of build run
`20260821T005712Z_gate12_pru_dac1_build`.

It combines the physically verified GPIO-SPI CODE0..CODE3 protocol with the
PRU channel-A selection/update path and places only D1B[13:0], D1CLK, and
SPI.MISO as outputs. All four startup CODE values are zero. D2/RF2 is absent.

This candidate is **not approved for JTAG/SRAM programming**. CN1 analog power
and RF1 operation are also not approved.

