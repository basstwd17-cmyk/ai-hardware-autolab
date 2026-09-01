# Gate 3 RF1 end-to-end CPLD candidate

Signal path:

```text
BBB PRU0 R30.15/R30.14 -> P8_11/P8_12 -> SYM.A[1:0]
BBB PRU0 R30.0         -> P9_31       -> LDAC.A
                                      -> CPLD CODE0..CODE3 mux
                                      -> D1B[13:0] + D1CLK
                                      -> AD9744 U6 -> THS3491 U5 -> RF1
```

The candidate drives DAC1 only.  D2B and D2CLK do not exist as HDL ports and
remain unused CPLD inputs.  The four raw codes are `0x0000`, `0x1555`,
`0x2AAA`, and `0x3FFF`.

## Build result

- run: `20260818T051959Z_gate3_rf1_demo_build`
- Diamond: `3.14.0.75.2`
- DRC: 0 errors, 0 warnings
- timing errors: 0
- user I/O: 18 (3 inputs, 15 outputs)
- bitstream bytes: `16127`
- raw SHA-256: `10d33603ce5d92b653713136d5ba97185815d1dcf9ca75d0c5a39d5e0ca5495c`
- design SHA-256: `7ffface184ca22dfc82eba77cd4a38094b7f746008a00dc8e1459524a1fea2b5`

This directory pins the build output but does not itself approve SRAM
programming or CN1 analog power.
