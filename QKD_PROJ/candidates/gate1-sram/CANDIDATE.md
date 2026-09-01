# 승인 2 후보 비트스트림 — Gate 1 SRAM

**이것은 승인이 아니다.** 승인 2에서 지목할 **대상 하나를 고정**한 기록이다.
프로그래밍 허가는 `../../../GATE1_REVIEW.md`의 승인 기록에만 있다.

- 고정일: 2026-08-18
- 대상 도구: `program_cpld_sram()` (미구현)
- 허용 operation: `SRAM Fast Program` 단일. Flash 계열 전부 금지.

## 왜 이 파일인가

최종 PASS 빌드 3개는 **설계가 동일하다**. `bitstream_design_sha256`이 셋 다
`71c6be3a…`로 같고, `.v`/`.lpf` 해시도 같고, 배치된 I/O·DRC·timing 결과도 같다.
서로 다른 것은 헤더의 빌드 타임스탬프뿐이다 (offset 244–245, 최대 2 byte).

| run | raw sha256 (앞 12) | 헤더 Date |
|---|---|---|
| `20260814T052115Z_gate1_build_final_1` | `3dd7c09302b5` | Fri Aug 14 14:21:21 2026 |
| `20260814T052123Z_gate1_build_final_2` | `3f0ff80ae744` | Fri Aug 14 14:21:32 2026 |
| **`20260814T052133Z_gate1_build_final_3`** | **`c656c41ab34d`** | Fri Aug 14 14:21:41 2026 |

셋 다 기술적으로 동등하므로 선택 근거는 기술적일 수 없다. **final_3을 고른 이유는
`gate1-test-spec.yaml`의 `build_result.runs`에 이미 정확히 적혀 있는 유일한 run이기
때문이다.** 나머지 둘은 계약에 적힌 이름과 실제 디렉터리 이름이 달랐다(아래 참조).
후보를 계약이 이미 지목하고 있던 것으로 맞추면 증거 사슬에 새 불일치가 생기지 않는다.

## 고정된 신원

```
파일        gate1_gpio_toggle_impl1.bit
크기        16271 byte
raw         c656c41ab34df655fb1d638d4af25439ff45fd3f70a73692fad3519085463910
design      71c6be3a3b01b00b0d94f1af329529dff83f4711545597cba777722e2c961866
```

`raw`가 승인 2가 지목하는 값이다. 프로그래밍 직전에 이 값을 다시 계산해 일치를
확인하지 않으면 프로그래밍하지 않는다.

`design`은 재빌드해도 유지되는 값이라 "같은 설계인가"에만 답한다. **승인 대상을
지목하는 데 써서는 안 된다** — 다른 시각에 빌드된 다른 파일도 같은 값을 갖는다.

## 출처 사슬

```
gate1_gpio_toggle.v    80f744afedc6ef78ada9993b938cee5385fb3bde940d1e269f430f627e18dbf1
gate1_gpio_toggle.lpf  3d157ed8897fcfe1954ac70cc7c7d1aa98c0de0d53742f11a2d602e8f0fb0b45
        │
        └─ Diamond 3.14.0.75.2 (C:\lscc\diamond\3.14)
                │
                └─ gate1_gpio_toggle_impl1.bit   c656c41ab34d…
```

두 소스 해시는 2026-08-18 현재 작업 트리의 파일과 **일치함을 재확인했다.**
`.v`/`.lpf`가 바뀌면 이 후보는 자동으로 무효다.

빌드 결과: `dbg0` → pin 117 / PT24A / LVCMOS33_OUT / 4 mA / SLOW, 사용자 I/O 1개,
DRC 0, timing 0, max 104.943 MHz (요구 2.08 MHz).

## 동봉 파일

| 파일 | sha256 |
|---|---|
| `gate1_gpio_toggle_impl1.bit` | `c656c41ab34df655fb1d638d4af25439ff45fd3f70a73692fad3519085463910` |
| `gate1_gpio_toggle_impl1.pad` | `4f2ad93ce7865dee67ee73059dd314682423fb0966cd5a8ca5e49dd81efd6026` |
| `build.log` | `6a29797d789f51cf3f92a440b71734bf0d2bd420dd7d258df6f502a02249488a` |
| `manifest.json` | `f9ef4f7fe76bd18ff1e6ac74f8d9d1d9c8c24fa378bc89ee0196db5ef7b0e86f` |

## 불변 규칙

`runs/`는 `.gitignore:35`로 제외된다. 원본 run 디렉터리의 `.bit`은 **git이 추적하지
않으므로** `git clean -xdf` 한 번에 사라진다. 승인 대상이 그런 자리에 있으면 안 되므로
이 디렉터리로 복사했고, 여기는 추적된다.

- 이 디렉터리의 파일은 **재빌드 결과로 덮어쓰지 않는다.**
- 설계가 바뀌면 이 디렉터리를 수정하지 말고 **새 후보 디렉터리를 만든다.**
- `tests/test_gate1_candidate.py`가 매 테스트 실행마다 해시 일치를 강제한다.
  변조되거나 재빌드본으로 교체되면 테스트가 깨진다.

## 부수적으로 고친 것

`gate1-test-spec.yaml`의 `build_result.runs`가 실제 디렉터리와 달랐다.

```
계약에 적혀 있던 것          실제 디렉터리
20260814T052120Z_…_final_1   20260814T052115Z_…_final_1
20260814T052126Z_…_final_2   20260814T052123Z_…_final_2
20260814T052133Z_…_final_3   20260814T052133Z_…_final_3   (일치)
```

셋 중 둘이 존재하지 않는 디렉터리를 가리키고 있었다. 계약을 실제 이름으로 고쳤다.

## 아직 안 한 것

- 승인 2는 **받지 않았다.** `APPROVAL-2.md`는 빈 양식이다.
- `program_cpld_sram()`은 **미구현**이다.
- 승인 1(JTAG scan)이 선행이다. 그 전에는 이 후보를 쓸 일이 없다.
