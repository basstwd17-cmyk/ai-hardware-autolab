# 과거 README — 2026-09-04 이전 안내 (현재 상태 아님)

이력 보존용 문서입니다. 상태·명령·승인 설명은 현재 플랫폼의 기준이 아닙니다.
원래 상대 경로의 기준은 ai_hardware_autolab/였습니다. 현재 안내는 ../../README.md를 읽으세요.

RTASE_V1 (Lattice MachXO2 CPLD) · BeagleBone Black PRU · RTM3004 오실로스코프를
하나의 명령으로 시험하는 자동화 시스템.

## 설계 원칙

**AI는 교체 가능하고, Tool API는 자산이다.**

AI 모델은 몇 달마다 바뀐다. 하지만 `program_cpld()`가 안정적으로 동작하면 그 위에는
어떤 AI든 얹을 수 있다. 그래서 개발 순서는 Agent가 아니라 Tool Layer가 먼저다.

**AI는 실시간 파형을 직접 만들지 않는다.**

AI는 계획·코드·판단을 담당하고, 실시간 동작은 BBB PRU / CPLD / DAC가 수행한다.
5 ns 타이밍 경로에 Python, 네트워크, 모델 호출을 넣지 않는다.

## 3계층

```
1계층  AI Agent          무엇을 할지 결정
       요구사항 해석 · 데이터시트 검색 · 핀/타이밍 확인 · Verilog/C 생성
       테스트 계획 작성 · 컴파일 에러 분석 · 측정 결과 해석 · 다음 실험 결정
                              ↓  계획 (JSON)
2계층  Orchestrator      그 순서를 실제로 실행
       orchestrator/     평범한 Python. AI 없음. 상태 전이와 호출 순서만.
                              ↓  함수 호출
3계층  Hardware Tools    장비를 실제로 제어
       hwtools/          AI 없음. 결정론적. 같은 입력 → 같은 ToolResult.
```

### 계층 경계에서 지켜야 하는 것

**안전 게이트는 3계층 안에 있다.** 2계층에 두면 새 Orchestrator나 직접 호출이
우회한다. 모든 Tool은 `SafetyGate`를 인자로 받아 하드웨어를 만지기 전에 통과한다.

**판정과 해석을 나눈다.**
- 3계층 = 결정론적 판정. test spec의 임계값 대비 PASS / FAIL / WARNING.
- 1계층 = 해석과 다음 결정. 왜 실패했는지, 무엇을 바꿀지.

AI가 임계값 판정을 하면 같은 측정이 두 번 다르게 나온다. 하지 않는다.

**모든 Tool은 `ToolResult`를 반환한다.** 콘솔 출력이 아니다. 반환값에
실행된 명령줄, 아티팩트 SHA-256, 장비 `*IDN?`, 측정값이 들어 있어야
나중에 재현할 수 있다.

## hwtools 구조 — 고정 / 가변

측정 종류가 달라지면 스코프 설정도 달라진다. 그래서 3계층을 둘로 나눈다.

**고정 (거의 안 바뀜)**

| 파일 | 역할 |
|---|---|
| `result.py` | `ToolResult` / `Artifact` — 반환 계약 |
| `safety.py` | `SafetyGate` — 계약 강제. 하드웨어 앞을 막는다 |
| `scpi.py` | `ScpiTransport` — 기록형 SCPI, mock 지원 |
| `ssh.py` | `SshTransport` — 기록형 SSH, client 주입 가능, BB_PW 자동 마스킹 |
| `record.py` | `RunRecord` — 실행 디렉터리, manifest, 해시 |

**가변 (실험마다 새로 작성)**

`instruments/` 장비별 고정부 · `recipes/` 측정 설정 (타임베이스, 트리거, 표본 수)

## 구현된 Tool

| Tool | 계층 | 상태 |
|---|---|---|
| `measure_scope(recipe, ...)` | 3 | 동작. RTM3004 실장비 검증 완료 |
| `build_cpld(project, gate)` | 3 | 동작. Diamond 3.14 실빌드 검증 완료 |
| `scan_jtag()` | 3 | 동작. 승인 1 완료 |
| `program_cpld_sram()` | 3 | 동작. 승인 2 완료, 1회 실행 소진 (계약 `execution_run` 기록) |
| `measure_gate1_gpio()` | 3 | 작업 중 (승인 3) |
| `run_bbb(script)` | 3 | 동작. 계약 allowlist(내용 해시) 안의 script만. fake SSH 검증 |
| `deploy_pru()` | 3 | 동작. upload→build→install→service→VERSION/PING. fake SSH 검증 |
| `Gate1Sequence` | 2 | 동작. bundle 검증·소비 + scan → program → measure 상태 머신 |

## MCP 서버 (런타임 경계)

```
mcp_server/    같은 3계층 도구를 Claude Code · Codex 등 MCP 클라이언트에 노출
```

**AI가 교체 가능하다는 원칙의 실행판이다.** 도구·계약·RunRecord는 그대로 두고 위에
얹는 런타임만 바꾼다. 의존성이 없고 API 키도 필요 없다 — MCP stdio는 줄 단위
JSON-RPC라 `protocol.py`에 직접 구현했다.

**도구 목록 자체가 첫 번째 잠금장치다.** `--allow-hardware` 없이는 보드를 만지는
도구가 등록되지 않으므로 어떤 프롬프트로도 부를 수 없다. `SafetyGate`는 그 아래에서
그대로 동작한다. 그 플래그는 실행 인자이고, 그게 이 아키텍처가 원래 그 결정을 두는
자리다 — 사람이, 세션당 한 번.

도구는 **경로가 아니라 계약 이름**을 받는다. 모델이 임의 경로를 넘길 수 있으면
계약이 무슨 일이 있었는지 설명하기를 그만둔다.

## Orchestrator (2계층)

```
orchestrator/
  bundle.py     ApprovalBundle    사람이 한 번 서명하는 세션 단위 승인
  ledger.py     ConsumptionLedger 그 서명이 얼마나 소진됐는지
  preflight.py  첫 명령 전에 할 수 있는 모든 거부
  sequence.py   Gate1Sequence     scan -> program -> measure 상태 머신
```

**preflight은 파일만으로 판정 가능한 모든 것을 한 번에 보고한다.** 첫 실패에서
멈추지 않는다 — 하드웨어 세션은 보드에 전원이 들어가 있고 사람이 서 있는 상태라,
3단계에서 "3단계는 애초에 못 돈다"를 알게 되면 1·2단계가 이미 소진한 예산까지
같이 버린다. 심각도는 둘이다. `blocking`은 시작 자체가 불가, `advisory`는 시작은
되지만 뒤 step이 안 돌아 도중에 멈춘다는 뜻이다. measure 도구가 없는데 scan을
도는 건 정당한 사용이므로 advisory이지, 오류가 아니다. preflight은 하드웨어를
건드리지도, 예산을 쓰지도, 상태를 바꾸지도 않는다.

**resume은 `BLOCKED`만 허용한다.** BLOCKED는 아무것도 건드리지 않은 상태이므로
막은 원인이 사라진 뒤 이어받아도 이미 일어난 일이 달라지지 않는다. `COMPLETE`는
이어받을 게 없고, `ABORTED`는 step이 돌다 실패한 것이라 이어받으면 아무도 안 본
실패를 건너뛰게 된다 — 그래서 편하게 만들지 않고 거부한다. resume은 새 `RunRecord`를
쓰고 이전 디렉터리를 이름으로 가리킨다. 어느 쪽도 다른 쪽 이야기를 하려고
덮어쓰이지 않는다.

**계약과 bundle은 다른 질문에 답한다.** 계약은 "이 보드에서 이 작업이 애초에
허용되는가"이고 오래 간다. bundle은 "지금, 이 파일들에 대해, 몇 번까지, 누가
승인했는가"이고 짧게 간다. Orchestrator가 계약을 고쳐 스스로 권한을 만들 수 있으면
계약은 사람이 검토하는 문서이기를 그만두므로, bundle은 별도 파일이고 2계층은
**읽기만** 한다.

bundle은 아티팩트를 **경로가 아니라 SHA-256으로** 지정한다. 서명과 실행 사이에
경로는 다른 파일을 가리키게 만들 수 있지만 해시는 그럴 수 없다.

소진 기록(ledger)은 계약이 아니라 기계 소유 파일에 쌓인다. 시도는 도구를 부르기
**전에** 기록한다 — 나중에 기록하면 프로세스를 죽이는 step이 예산을 공짜로 돌려받는다.

ledger는 **1회 승인과 2회 실행 사이를 막는 유일한 파일**이라 그에 맞게 쓴다.
모든 쓰기는 temp + fsync + `os.replace`로 원자적이고, read-modify-write는 배타 락
안에서 돌며 락 안에서 디스크를 다시 읽는다. 깨진 JSON은 빈 ledger로 취급하지 않고
거부한다 — 그렇게 하면 기록된 모든 실행을 되돌려주기 때문이다. 예약됐는데 결과가
안 적힌 항목(`dangling`)은 "도구가 불렸고 결과를 아무도 모른다"는 뜻이므로 preflight이
다음 실행을 막는다. 보드를 건드렸는지 여기서는 알 수 없고, 안 건드렸다고 가정하면 안 된다.

상태: `INIT -> READY -> SCANNED -> PROGRAMMED -> MEASURED -> COMPLETE`.
종료 실패는 둘로 나뉜다. **`ABORTED`** 는 step이 돌다가 실패한 것 — 보드를 건드렸다.
**`BLOCKED`** 는 step을 정당하게 시작할 수 없던 것(bundle 미승인, 예산 소진,
도구 미구현) — 아무것도 건드리지 않았다.

2계층은 보드 전원을 내릴 수 없다. `safe_off()`는 내린 척하지 않고 사람이 해야 할
동작을 기록한다.

승인 bundle 양식: `contracts/bundles/gate1-bundle.template.yaml` (서명 불가 템플릿).

## Skill

`.claude/skills/` — `scope-measure`, `build-cpld` 작성 완료.
`scan-jtag`, `program-cpld-sram`, `measure-gate1-gpio`, `datasheet-research`,
`pcb-validation`, `report` 미작성.

`cpld-build-flash`라는 이름은 쓰지 않는다. Gate 1은 nonvolatile Flash가
금지돼 있고 SRAM Program만 허용되므로, 이름 하나가 그 구분을 무너뜨린다.

지터 측정과 지연 측정은 스코프 설정이 다르다. 이 부분을 고정 API로 얼리면
새 측정을 할 때마다 맞지 않는 옵션과 싸우게 된다. 대신 매번 새로 짜되,
**고정 계층을 거치므로 안전 한계와 명령 기록은 빠뜨릴 수 없다.**

단, "적당한 양의 데이터"는 판단해도 되지만 **결과에는 숫자로 남아야 한다.**
N, 샘플레이트, 측정 방식이 `measurements`에 없으면 같은 값을 두 번 못 낸다.

## 계약

| 파일 | 내용 |
|---|---|
| `contracts/board-map.yaml` | 보드의 실제 부품·핀·전압과 그 근거 |
| `contracts/gate1-test-spec.yaml` | 허용/금지 작업, 통과 조건, 중지 조건 |

미확정 값(`UNVERIFIED`, `null`)이 있으면 그 값에 의존하는 Tool이 **스스로 거부한다.**
모델이 추측하지 않는다. TQFP144 핀을 잘못 찌르는 것보다 멈추는 게 싸다.

## 현재 상태 (2026-08-14)

**검증 완료**

- 스코프 자체점검 6회 연속 PASS (N=200 3회, N=50 3회). 요청 count와 실제 count 정확히 일치.
  주파수 평균 999.84 Hz, 계약 기준 1000 Hz ±1%.
- Gate 1 CPLD 빌드 3회 연속 PASS. `dbg0` → pin 117, LVCMOS33, 4 mA, SLOW.
  사용자 I/O 1개, DRC 0, timing 0, design hash 3회 동일.
- 테스트 71개 통과 (하드웨어 불필요)

**보드는 아직 만지지 않았다**

`allow_hardware=False`가 기본값이고, `GATE1_REVIEW.md` 체크리스트 10개가 전부 미확인이다.
JTAG scan, SRAM program, GPIO 구동은 코드가 막고 있다.

**미해결**

- OSCH 정확도 — MachXO2 데이터시트 필요. 해결 전까지 주파수 허용오차를 정하지 않는다.
  Gate 1은 "반복 토글"만 판정한다 (`waveform_must_toggle`).
- pin 117 probing 접근 방법 미확정

## 실행

```powershell
py -3 -m pytest ai_hardware_autolab/tests -q
```

`runs/`는 gitignore 대상이다. 아티팩트 원본은 남기지 않고 manifest의 해시로 추적한다.
