# Gate 2 BBB-to-CPLD loopback candidate

목적은 최종 DAC 동작 전에 가장 짧은 경로로 BBB 신호가 CPLD까지 도달하는지
확인하는 것이다.

```
BBB PRU0 R30.15
  -> P8_11
  -> RTASE_V1 SYM.A0
  -> CPLD pin 85 / PR13B (input)
  -> combinational assign
  -> CPLD pin 117 / PT24A (output)
  -> temporary wire
  -> RTM3004 CH3
```

이 설계에는 DAC 데이터, D1CLK/D2CLK, SPI.CLK, LDASC.A 포트가 없다.

## 빌드 결과

- run: `20260818T050620Z_gate2_bbb_cpld_loopback_build`
- Diamond: `3.14.0.75.2`
- DRC: 0 errors, 0 warnings
- timing errors: 0
- user I/O: 2개
- input: pin 85, bank 1, PR13B, LVCMOS33_IN
- output: pin 117, bank 0, PT24A, LVCMOS33_OUT, 4 mA, SLOW

## 승인 대상

- file: `bbb_cpld_loopback_impl1.bit`
- bytes: `16029`
- raw SHA-256: `e468354719bd52ee5347399482d208c854141d0bf248dbb1d0d04843c0ae0768`
- design SHA-256: `6c27678de684cbdb5c881d428b5c5bb87d37f9bb9adc9ceb0c899aff417f038d`

현재 상태는 `pinned_not_approved`다. 실제 SRAM 프로그램은 BBB 장착과 단일 5 V
전원 경로를 확인한 뒤 별도로 승인한다.
