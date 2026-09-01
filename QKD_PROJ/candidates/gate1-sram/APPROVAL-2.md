# 승인 2 기록 — SRAM Fast Program (승인됨)

`GATE1_REVIEW.md`의 3단계 승인 중 2단계. 사용자가 2026-08-18 대화에서
아래 고정 artifact에 대한 `SRAM Fast Program` 1회 실행을 명시적으로 승인했다.

---

## A. 선행 조건 (전부 체크되어야 서명 가능)

- [x] **승인 1이 완료됐다.** JTAG scan 결과 장치 1개, IDCODE `0x012BC043`.
      run 디렉터리: `QKD_PROJ/runs/20260818T021017Z_gate1_jtag_scan`
- [x] 후보 비트스트림의 raw sha256을 **프로그래밍 직전에 재계산**해 아래 값과 대조한다.
- [x] `.v` / `.lpf` 해시가 `CANDIDATE.md`의 값과 여전히 일치하는지 도구가 강제한다.
- [x] **CN1 미연결**을 확인했다. (아날로그 출력을 끄는 유일한 수단)
- [x] BBB 미연결, 벤치 전원 +5.0 V / 300 mA 제한.
- [x] U4 6선 배선이 그대로이고 `5V Out`은 미연결이다.

## B. 승인 대상 — 정확히 이 파일 하나

```
경로     QKD_PROJ/candidates/gate1-sram/gate1_gpio_toggle_impl1.bit
크기     16271 byte
raw      c656c41ab34df655fb1d638d4af25439ff45fd3f70a73692fad3519085463910
```

**raw 해시가 한 글자라도 다르면 다른 파일이다.** 재빌드본은 설계가 같아도 이 승인의
대상이 아니다. 대상을 바꾸려면 이 문서를 새로 발급한다.

참고값 (승인 대상 아님): `design 71c6be3a3b01b00b0d94f1af329529dff83f4711545597cba777722e2c961866`

## C. 허용 범위

| 항목 | 값 |
|---|---|
| operation | `SRAM Fast Program` **단일** |
| access mode | `SRAM` |
| 케이블 / 포트 | HW-USBN-2B (FTDI) / `FTUSB-0` |
| TCK divider | `10` (실주파수 미확정 — 아래 참조) |
| 실행 횟수 | 1회 |

**금지**: Flash / XFLASH 계열 전부, `nonvolatile_flash`, Feature Row, USERCODE 기록,
`OperationOverride` 사용, 승인 대상 외 `.bit` 로드.

TCK divider 10의 실제 주파수는 **미확정**이다. HW-USBN-2B의 FTDI 칩이 2232H이면
2.7 MHz, 2232D이면 0.5 MHz다. CLI 도움말의 주파수 표는 GUI 도움말의 공식과
d≥6에서 어긋나므로 신뢰하지 않는다. divider 값만 기록하고 주파수는 칩 식별 후 채운다.

## D. 실행 후 기록 (실행한 사람이 채운다)

| 항목 | 값 |
|---|---|
| 실행 일시 (UTC) | `2026-08-18T02:28:58Z` |
| run 디렉터리 | `QKD_PROJ/runs/20260818T022845Z_gate1_sram_program` |
| `programmed_bitstream_sha256` | `c656c41ab34df655fb1d638d4af25439ff45fd3f70a73692fad3519085463910` |
| `approved_bitstream_sha256`와 일치 | ☒ 예 ☐ 아니오 |
| pgrcmd returncode (signed) | `0` |
| program 로그 파일 | `sram_program.pgrcmd.log` |
| verify 결과 | JTAG chain PASS, `Operation: successful` |
| 프로그래밍 중 소비 전류 | 300 mA 전류 제한에 도달하지 않음 (사용자 확인) |
| 이상 징후 | 없음 (연기·발열·이상 LED 등 사용자 확인) |

**불일치 시**: 프로그래밍 중단, 전원 차단, 이 문서에 사유 기록. 재시도 금지.

## E. 중단 조건 (하나라도 해당하면 즉시 중단)

- raw 해시 불일치
- pgrcmd returncode ≠ 0
- 전류가 300 mA 제한에 도달
- DAC1_OUT / DAC2_OUT 또는 RF1 / RF2에서 예상 밖 신호 관측
- D1CLK / D2CLK / SPI.CLK / LDASC.A 토글 관측

## F. 서명

| | |
|---|---|
| 승인자 | 사용자 |
| 승인 일자 | 2026-08-18 |
| 허용 범위 | `SRAM Fast Program`, 위 raw 해시 파일 한정 |
| 유효 조건 | `.v` / `.lpf` / 후보 `.bit` 해시 불변. 하나라도 바뀌면 이 승인은 무효 |

승인 후 `GATE1_REVIEW.md`의 "승인 기록"에 한 줄 추가하고 이 문서를 가리킬 것.

## G. 전원 차단 후 추가 1회 승인

pin 117 임시선을 안전하게 연결하고 인접 pin 116/118 단락을 확인하기 위해 전원을
차단했으며, 그 결과 휘발성 SRAM 구성이 소실됐다. 사용자는 2026-08-18 대화에서
위와 동일한 raw SHA-256 artifact에 대한 `SRAM Fast Program` 추가 1회를 승인했다.

- 누적 허용 횟수: 2회
- 기존 사용 횟수: 1회
- 이번 추가 허용: 1회
- 목적: 승인 3의 pin 117 GPIO 측정
- Flash/Feature Row/USERCODE: 계속 금지
- 실행 결과: PASS, 2026-08-18T04:58:29Z 시작, 14.891 s
- 실행 경로: 승인 계약을 다시 연 뒤 실행한 pytest preflight가 mock 없이
  `program_cpld_sram()`을 호출함
- 기록 상태: pgrcmd 성공 결과는 확인했으나 RunRecord 없음. fixture scan 결과가 전달됐으므로
  실제 JTAG scan으로 live chain을 다시 확인한 뒤 측정할 것
- 누적 소비: 2/2, 추가 프로그램 금지
