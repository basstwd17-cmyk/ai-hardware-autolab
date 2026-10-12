# 과거 STATUS — 2026-09-04 이전 누적 메모 (현재 재개 지점 아님)

과거 관측과 후속 메모를 보존합니다. 현재 보드 상태·승인·작업 지시가 아닙니다.
원래 상대 경로의 기준은 ai_hardware_autolab/였습니다. 현재 안내는 ../../STATUS.md를 읽으세요.

다음 세션은 여기서부터 시작한다. 상세 근거는 `docs/`, `contracts/`, `../GATE1_REVIEW.md`에 있다.

## 한 줄 요약

**Gate 12의 디지털 제어 경로가 뚫렸다 — 다만 범위 한정이다.** BBB 통합 오버레이 활성화·재부팅
검증, CPLD SRAM 프로그램, GPIO-SPI CODE0..3 왕복(bit_errors 0), PRU 0·1·2·3 시퀀스 4샷 완료까지
기록으로 남아 있다. **증명되지 않은 것: D1B/D1CLK의 전기적 무결성, DAC 변환, 앰프·SMA 출력.**
승인된 SRAM 실행 1회는 소진됐고, 계약의 `allowed_operations`에서 `jtag_scan`·`sram_fast_program`이
빠졌다 — 재적재하려면 새 승인이 필요하다. 에이전트 플랫폼은 P0(MCP 서버)·P1(에이전트 정의 6개와
검증 루프)까지 올라갔고 P2(잡 러너)는 미착수.

## 지금 보드 상태

기록이 말하는 마지막 상태다 (2026-08-21 01:26 UTC, run `20260821T012554Z_gate12_pru_dac1_sram`).
그 이후 전원을 껐는지는 **기록에 없다.** 껐다면 CPLD 구성은 사라졌다.

- **Gate 12 구성**이 휘발성 SRAM에 올라갔다 (`gate12_pru_dac1`, bitstream `dfedb3d0…`).
  Gate 11 구성이 아니다 — 덮어썼다.
- BBB는 **통합 오버레이** `BB-RTASE-A-GPIO-SPI-PRU-00A0`로 부팅돼 있다. `spi0` 비활성
  (`spidev0_0_present: false`), pinmux 검증됨, PRU 서비스 active, `/dev/rpmsg0` 존재,
  `pru_ping_ok`. uEnv 백업은 `/boot/uEnv.txt.pre-gate12`.
- **상단 PCB가 분리된 상태**로 실행됐다 (`physical_state: top_PCB_separated`).
- PRU 출력은 유한 시퀀스 후 비활성으로 복귀했다 (`pru_outputs_active: false`).
- DAC 데이터 버스는 이제 CPLD가 **구동한다** (D1B 14비트 + D1CLK가 배치됨). 앰프 전원·RF1/RF2는
  여전히 비활성이고, **CN1 미연결**이 아날로그 출력을 막는 유일한 검증된 방법이다
  (AD9744 `SLEEP`은 GND 고정, THS3491 `PD#`는 +15 V 고정).

## Gate 진행

| Gate | 내용 | 결과 |
|---|---|---|
| 1 | JTAG scan → SRAM program → pin 117 GPIO | scan·program PASS. **GPIO 측정은 승인만 있고 실행 0회** |
| 2 | BBB → CPLD loopback | 빌드만, `pinned_not_approved` |
| 3 | RF1 데모 / SPI 레지스터 / 경로 진단 | `cs_seen=1`, `sclk_seen=1`, **`write_done=0`** |
| 4–8 | SPI loopback·정적 HIGH 변형 | rx가 `000000` 또는 `ffffffff` 상수 |
| **10** | 고정 응답 `a55a3cc3f00f` 클럭드 리턴 | **스코프·GPIO는 정확히 수신, McSPI만 `000000`** → 결함 격리. ⚠ GPIO 근거 run이 디스크에 없다 (아래 미해결) |
| **11** | GPIO-SPI CODE0..3 레지스터 | **PASS. `[0x0001,0x0123,0x2345,0x3FFE]` 왕복, bit_errors 0** |
| **12** | PRU → CPLD → DAC1 채널 A | **디지털 구간 PASS (범위 한정).** 빌드 0/0 · 오버레이 부팅 검증 · SRAM program · CODE 왕복 0 · PRU 4샷. **아날로그 미착수** |

`spidev0.0`(McSPI)는 수신이 검증되지 않았다. 속도가 필요해지면 나중에 고칠 수 있지만
초기 CODE 설정 경로의 의존성은 아니다.

## 다음에 할 일

1. **Gate 12 다음 단계는 `digital_timing`** — 계약 `verification_sequence`의 첫 항목.
   SYM_A0·SYM_A1·P9_31·D1CLK·대표 D1B 비트를 스코프로 관측한다. 전제조건은 CN1 분리.
   지금까지 증명된 것은 **호스트가 보낸 명령이 완료됐다**는 것이지, 그 신호가 DAC 핀에
   제대로 도착했다는 것이 아니다. 그 간극이 이 단계다.
   재적재가 필요하면 **새 승인부터** — 승인된 1회는 이미 소진됐고 계약이
   `jtag_scan`·`sram_fast_program`을 다시 금지 목록에 넣었다.
2. ~~allowlist 해시 불일치 해소~~ — **2026-08-27 해결.** 드리프트가 아니라 계약의
   전사 오류였다: 스크립트는 `a2e47df` 이후 한 번도 바뀌지 않았고 blob은 늘
   `735c91d3…`였다. gate9 한 줄만 정정. `run_bbb`가 다시 실행한다.
3. **Gate 1 GPIO 측정 3회** — 승인은 있고 실행 0회. Gate 1이 공식적으로 안 닫힌 유일한 이유.
4. **에이전트 플랫폼** — P2 원장(`bee8e43`에서 고정)과 루프가 올라갔다. App Server
   스파이크는 팀원이 완료(Codex 0.148.0에서 commandExecution·fileChange·mcpToolCall·
   approval·subAgentActivity 관측, `CodexAppServerAdapter` 구현). 다음은 어댑터 위의
   `AgentRunner` 구현 → seam에서 봉투 검증 → `run_bbb` effect 분리.
   `event_id`/`seq` 계약과 UI replay bridge는 팀원 담당.
   상세: `docs/2026-08-21-agent-platform-p2-loop.md` §9.
5. **Gate 12 산출물 커밋.** 에이전트 플랫폼(P1)과 증거 감사는 커밋됐다. 아직 밖에 있는 것:
   `contracts/gate12-*`, `candidates/gate12-pru-dac1/`, `gate12_pru_dac1/`, BBB host 도구·오버레이.
   `runs/`는 gitignore라 candidates/contracts가 유일한 증거 사슬인데 그게 추적되지 않고 있다.

## 미해결

| 항목 | 막고 있는 것 | 필요한 것 |
|---|---|---|
| McSPI RX 원인 | 속도 (기능은 GPIO로 우회됨) | `SPI_LOOP` 내부 루프백, FIFO/`XFERLEVEL` 경로 |
| pin 117 GPIO 측정 | Gate 1 종료 | 실행만 |
| OSCH 정확도 | GPIO 주파수 판정 | MachXO2 데이터시트 |
| U6 MODE 상태·DAC1_OUT 기준값 | Gate 12 아날로그 단계 | 첫 코드 스텝 전에 실측 |
| D1B·D1CLK 전기적 무결성 | Gate 12 `dac_code_response` | `digital_timing` 스코프 관측 |
| Gate 12 SRAM 재적재 | 구성이 날아갔을 경우 전부 | 새 승인 (1회분 소진) |
| 전원 차단 여부 | 지금 보드 상태 확정 | 기록에 없음 — 확인만 |
| **gate10 `gpio_spi_fallback_result` run 부재** (`primary_run_missing`) | GPIO 읽기 주장의 1차 증거 | **승인된 재실행만이 복구한다.** gate11 run으로 교체 불가 — 아래 참조 |
| gate10 스코프 캡처가 RunRecord 없음 | 도구가 다이제스트를 고정 못함 | 캡처 경로가 RunRecord를 안 남긴다. 증거 파일 자체는 있고 읽힌다 |
| **gate11 `gpio_protocol_result`가 run을 안 가리킴** (`unscoped_claim`) | `bit_errors: 0`을 검증할 방법 | 어떤 run에도 붙어 있지 않다 |
| gate1 `build_result`가 run을 안 가리킴 (`unscoped_claim`) | 같음 | 같음 |
| `digital_verification.json`이 artifact 미등록 | 해시가 도구가 아니라 손으로 고정됨 | PRU 검증 경로에서 artifact 등록 |
| gate9 스크립트 7개 미분류 (`effect_class` 없음) | `--role measurement`가 BBB 스크립트를 하나도 못 돌림 | 제안 patch 검토 후 서명 |
| ~~gate9 `spi_clocked_response_verify.sh`~~ | — | **2026-08-27 해결.** `pinned_file_drift`가 아니었다: 스크립트 커밋은 `a2e47df` 하나뿐이고 git blob과 디스크가 동일하다(`735c91d3…`, 줄바꿈 변환도 아님). 같은 커밋이 쓴 계약 digest가 처음부터 틀렸던 것 — 나중 커밋 `f301a03`의 gate10은 같은 파일에 올바른 값을 적었다. gate9 한 줄 정정 |

## 구현 상태

**3계층 `hwtools/`** — `measure_scope` · `build_cpld` · `scan_jtag` ·
`program_cpld_sram` · `measure_gate1_gpio` · `run_bbb` · `deploy_pru` 전부 동작.
고정부에 `ssh.py`(기록형 SSH, BB_PW 마스킹) 추가.

**2계층 `orchestrator/`** — `ApprovalBundle` · `ConsumptionLedger` · `preflight` ·
`Gate1Sequence` 구현·테스트 완료. **다만 실제 실행 경로에서는 여전히 우회 중**이다.
Gate 3~12의 프로그래밍은 각 설계 폴더의 `program.py`가 hwtools를 직접 부른다.

**MCP 서버 `mcp_server/`** — 같은 도구를 Claude Code·Codex 어디서든 호출.
의존성 0, API 키 불필요. 기본 프로파일은 오프라인 도구만, `--allow-hardware`를 붙여야
보드 도구가 **목록에 등록된다.** `--role <에이전트명>`으로 한 행까지 좁힌다 (`ROLE_TOOLS`·
`ROLE_EFFECTS` 매트릭스가 유일한 출처). `measurement`와 `board-operator`가 `run_bbb`를
공유하므로 계약 스크립트마다 `effect_class: read|mutate`를 보고 거른다 — **분류가 없으면
`mutate`로 친다.** 설정을 바꾸고 복원하는 건 read가 아니다. 복원은 첫 변경의 부재가 아니라
두 번째 변경이고, 그 사이에 프로세스가 죽으면 보드는 바뀐 채로 남는다.
상세: `docs/2026-08-21-agent-platform-p0-mcp-server.md`, `mcp_server/README.md`

**에이전트 정의 `agents/`** (신규, P1) — 6개 에이전트와 하나의 루프.
`gate-architect`(계획) → `design-builder`·`board-operator`·`measurement`(실행)
→ `verifier`(증거만으로 판정) → 불만족이면 재계획, 아니면 `reporter`가 문서화.
계획자와 검증자는 **쓰기 도구가 없다.** 판정은 도구가 내린 값을 복사할 뿐이고,
`met: true`인데 근거 run이 없으면 스키마가 거부한다. 같은 실패가 3회 반복되면
`exhausted`로 종료한다 — 무한 루프를 막는 유일한 장치.
정의는 frontmatter+markdown이라 벤더 중립이고, `export.py`가 Claude Code·Codex용으로
렌더한다. MCP 서버는 `agent-<name>` 프롬프트로도 배포하며, 잠긴 서버에서는 보드
에이전트를 도구와 똑같이 감춘다. 상세:
`docs/2026-08-21-agent-platform-p1-agent-definitions.md`

**증거 감사 `orchestrator/evidence.py` · `audit.py`** (신규) — 조건 판정을 모델에서 회수했다.
`EvidenceReducer`가 run 기록에서 필드를 꺼내 계약의 연산자로 비교하고, artifact 해시를 다시
계산하고, `failure_signature`와 3회 규칙으로 `exhausted`를 결정한다. 기록되지 않은 필드는
`evidence_missing`, 여러 스텝이 다른 값을 남겼으면 `ambiguous` — **둘 다 추측으로 메우지 않는다.**
`audit.py`는 같은 원리를 전수 적용해 계약의 주장을 그 주장이 인용한 run에만 대조한다.

**작업 원장 `orchestrator/jobs.py`** (신규, P2) — 데몬의 기억. `jobs`·`events`·`handoffs`·
`approvals` 4개 테이블. `iteration`과 실패 서명·strike를 **에이전트 밖**에 두고(프롬프트 안에
있으면 rate-limit 창을 넘기며 사라진다), `events.event_id`가 UI 재접속 커서다 — 런타임의
커서가 아니라. 상태 변경과 그걸 설명하는 이벤트는 **한 트랜잭션**에서 커밋된다.
승인은 한 번만 결정되고, `state='pending'` 가드 + rowcount 확인으로 두 번 클릭이 경합해도
하나의 결정이 된다. `exhausted` 판정은 여기서만 내려진다 — reducer는 한 iteration만 보고,
같은 실패 3연속은 원장만 볼 수 있다. 상세:
`docs/2026-08-21-agent-platform-p2-job-ledger.md`

**루프 `orchestrator/loop.py`** (신규, P2) — plan→execute→reduce→record→decide.
`advance()`는 **한 걸음만 하고 반환한다.** 단계는 저장하지 않고 원장에서 파생하므로
(iteration에 Plan 없음→plan, StepResult 없는 step→execute, Verdict 없음→reduce, 있음→decide)
프로세스가 죽어도 `Loop`를 새로 만들면 그대로 이어진다. 하드웨어 스텝은 사람 앞에서 멈추고,
**거부는 재요청하지 않는다.** 런타임 실패는 blocked이지 조건 실패가 아니다 — 죽은 프로세스는
보드에 대해 아무것도 말하지 않는다. 정지 규칙 2개: 같은 실패 3회(`exhausted`)와
iteration 상한(매번 다르게 실패하면 strike가 안 쌓여서 첫 규칙이 못 본다).
**자체 이벤트 종류를 만들지 않는다** — 런타임 어휘는 어댑터 것이다. 상세:
`docs/2026-08-21-agent-platform-p2-loop.md`

**데몬 `orchestrator/daemon.py`** (신규) — 어느 job이 다음에 움직이는지 결정한다.
`tick()`은 `advance()`를 **정확히 한 번** 부른다. 재시도 일정을 저장하지 않고 로그에서
파생한다 (연속된 `rate_limited:` 차단 수 → 지수 백오프). 정지 이유 3종을 구분한다 —
승인 대기(사람), rate limit(시계), 런타임 실패(사람). 오분류가 비대칭이라 명시적 예외가
메시지 휴리스틱을 이기고, 휴리스틱은 **횟수 제한**이 있다. 6번 연속 "잠깐 스로틀"은
스로틀이 아니다.

`HardwareApprovalGate`가 `codex_runner`의 `hardware_approval` 콜백을 채운다.
**승인 결정과 아티팩트 해시가 둘 다 맞아야만** 통과다 — 사람이 이 job·이 계약·이 step에
대해 granted했고, 그 아티팩트가 **지금 디스크에서 서명 당시 해시와 일치해야** 한다.
아무것도 안 묶인 승인은 거부다: 설명에 한 서명은 비트스트림 두 개를 구별하지 못한다.
상세: `docs/2026-08-21-agent-platform-p2-daemon.md`

**서비스 `orchestrator/service.py` · `control.py` · `runtime_events.py`** (신규) —
데몬을 호스팅하는 프로세스. 단일 인스턴스는 **OS 파일 락**이다 (pid 파일은 자기를 낡게 만든
크래시보다 오래 산다). 타이머는 고정 간격이 아니라 **스케줄러가 아는 마감에서 sleep을 파생**하고
30초 상한을 둔다 — 파이프로 승인이 들어오면 남의 재시도 시계를 안 기다려야 하니까.
제어 채널은 **named pipe** (`\\.\pipe\rtase-agent-daemon`). 고정 localhost
포트는 브라우저 아무 페이지나 POST할 수 있다 — Connect AI에서 안 가져올 부분.
`approve`는 **아무것도 인가하지 않는다.** 사람이 말한 걸 기록할 뿐이고 게이트가 다시 해시한다.
`cancel`은 닫기 전에 미결 요청을 철회한다. `steer`는 로그에 남고 **다음 handoff가 전달 증거**다.
Codex 알림은 `runtime.<method>`로 기록한다 — 이름은 런타임 것 그대로, 네임스페이스는 충돌 방지용.
**증거가 아니라 관측이므로** 버퍼링한다: 크래시는 UI 애니메이션 꼬리를 잃지 보드에 대한 주장을
잃지 않는다. 상세: `docs/2026-08-21-agent-platform-p2-service.md`

**UI 배선 `orchestrator/wire.py`** (신규) — 오피스 UI 계약
(`agent-platform/docs/platform-integration.md`) 구현. subscribe → ledger.event 재생 →
ready → 같은 연결로 계속 push. `ready.cursor`는 **마지막으로 보낸 event_id와 항상 같다**
(tip을 재생 *뒤에* 읽고 움직였으면 다시 비운다). `approval.decide`/`job.steer`/`job.cancel`은
`commandId`를 달고 `command.result`로 답한다. 결정은 `accepted`/`declined` 단어이고
**`ControlPlane.approve`는 진짜 JSON boolean만 받는다** — `bool("false")`가 True라서
`"granted":"false"`가 승인으로 기록될 수 있었다.

파이프 보안을 명시했다: 현재 logon SID DACL `D:P(A;;GA;;;<SID>)`, `PIPE_REJECT_REMOTE_CLIENTS`,
`FILE_FLAG_FIRST_PIPE_INSTANCE`. 첫 인스턴스를 `open()`에서 만들어 이름/권한 오류가
**시작 시점에** 실패한다. 전송 계층 버그 하나 더: 동기 파이프 핸들은 I/O를 직렬화해서
reader가 `ReadFile`에 블록돼 있으면 pusher의 `WriteFile`이 뒤에 줄 섰다 — `PeekNamedPipe`로
읽기를 논블로킹으로 바꿔 해결. TCP에선 안 나타나던 문제다.

steer 전달 경계 수정: reducer가 쓰는 `handoff.Verdict`가 **아무도 읽지 않은 steer를 소비**하고
있었다. 이제 `handoff.Plan`/`handoff.StepResult`만 전달 증거다. reporter는 steering을
지시로 받지 않고 `undelivered_steering`을 기록용 맥락으로 받는다.

`orchestrator/pagination.py` (신규) — `limit=10_000`이 **가장 오래된** 1만 개를 읽고 있었다.
런타임 이벤트가 곧 그 경계를 넘길 참이었다. 실행: `py -3 -m ai_hardware_autolab.orchestrator`.
상세: `docs/2026-08-21-agent-platform-p2-service.md`

외부 Node 브리지가 accept 루프를 죽인 보고(error 5)는 **여기서 재현되지 않았다** — Node 재연결
8회·동시 4개·CLI 동시 접속 모두 통과. 그래도 그걸 가능하게 하던 구조는 없앴다:
`FILE_FLAG_FIRST_PIPE_INSTANCE`를 가변 상태가 아니라 **파라미터**로 바꾸고, 생성 실패가 accept
루프를 죽이지 않게 하고(죽으면 파이프 이름이 사라져 ENOENT가 영구화된다), **다음 인스턴스를
넘겨주기 전에 미리 만들어** 아무것도 listening하지 않는 창을 없앴다. `tests/test_ui_bridge.py`가
실제 `node` 프로세스로 회귀를 잡는다 — 같은 인터프리터 클라이언트는 GIL을 공유해서 이 계열
버그를 통과시킨다.

`willBeDelivered`는 이제 현재 iteration의 **Verdict outcome을 읽는다.** terminal이면 reporter만
남고 reporter는 지시를 받지 않으므로 false다. iteration 상한도 배달을 끝낸다.

`job.result`는 ready **뒤에** 보낸다 — 재생은 커서당 한 프레임이어야 하고 result는 커서가 아니다.
내용은 하나도 저작되지 않는다: 카운트는 reducer 조건에 대한 산술, 요약은 outcome과 실패 필드명
인용, **Verdict 원문과 그 해시**가 같이 간다. `artifact.registered`는 경로를 별도 프레임으로
나르고, 존재·프로젝트 루트 내부를 확인한 것만 제공한다. 같은 id에 다른 경로는 거부한다.

**BBB 진단** — gate9 계약의 allowlist 7개, 경로가 아니라 **내용 해시**로 고정.

## 문서

| 파일 | 내용 |
|---|---|
| `docs/2026-08-20-gpio-spi-verification-and-pru-dac-plan.md` | GPIO-SPI 검증 결과 + Gate 12 계획 + 아날로그 경로 |
| `docs/2026-08-20-mcspi-rx-fault-elimination.md` | 무엇을 어떻게 배제했는가, 계측 함정 |
| `docs/2026-08-21-agent-platform-p0-mcp-server.md` | 에이전트 플랫폼 P0~P4, MCP 서버 |
| `docs/2026-08-21-agent-platform-p1-agent-definitions.md` | P1 루프, 4개 경계, P2가 채워야 할 구멍 |
| `mcp_server/README.md` | 등록 방법, 도구 추가 방법 |
| `agents/README.md` | 6개 에이전트, 종료 조건, 에이전트 추가 방법 |
| `docs/2026-08-21-evidence-audit.md` | 계약 주장 ↔ run 기록 전수 대조 (자동 생성) |
| `docs/2026-08-21-agent-platform-p2-job-ledger.md` | P2 원장 4테이블, 이벤트 종류, DB가 강제하는 불변조건 |
| `docs/2026-08-21-agent-platform-p2-loop.md` | 루프 5단계, 정지 조건, 런타임 seam |
| `docs/2026-08-21-agent-platform-p2-daemon.md` | 스케줄러, 정지 이유 3종, 하드웨어 승인 게이트 |
| `docs/2026-08-21-agent-platform-p2-service.md` | 프로세스 수명, named pipe, 제어 명령, 런타임 이벤트 기록 |
| `docs/evidence/gate10-*.json` | Gate 10 조건 명세와 그 판정 (`primary_run_missing`) |
| `contracts/proposals/…gate10-evidence-gap.patch` | **적용하지 않은** 제안 diff. 사람이 서명한다 |
| `contracts/proposals/…gate9-effect-class.patch` | 같음. 스크립트 7개의 read/mutate 분류 |
| `../GATE1_REVIEW.md` | Gate 1 체크리스트와 승인 기록 |

## 함정 (반복해서 물릴 것들)

- `prj_project new`가 자기 LPF를 만들고 나중에 추가한 LPF를 `excluded="TRUE"`로 만든다.
  → `build_cpld()`가 2단계로 회피 중.
- `.twr`이 두 개고 `_lse.twr`은 가짜 200 MHz 기준이다 (422 errors vs 실제 0).
- RTM3004은 Nx Single 무장 중에도 `ACQ:STAT?`에 `STOP`을 반환한다.
- RTM3004 커플링 `DC`는 **50 Ω**, `DCLimit`가 1 MΩ다. 3.3 V 로직에 50 Ω를 걸면
  없는 Low가 만들어진다.
- 프로브 감쇠비가 스스로 바뀐다 (프로브 꽂으면 1:1 → 10:1).
- 스코프가 `AUTO` 트리거면 트리거 없이도 촬영한다. `TIM:POS`가 프레임 밖이면 파형이
  그럴듯하게 틀린다. 프레임 캡처는 `NORMal` + 프레임 크기 창으로.
- `/dev/mem` `Bus error`는 권한이 아니라 **runtime PM 클럭 게이팅**이다.
  `power/control=on` 후 읽고 원복.
- Debian 13에서 `/proc/device-tree/ocp/spi@...` 고정 경로는 조용히 빈 출력을 낸다.
  `readlink -f /sys/class/spi_master/spi0/device/of_node`로 해결.
- 진단 스크립트의 종료 코드는 **"진단이 돌았는가"**여야 한다. DUT 판정을 종료 코드에
  넣으면 정당한 음성 결과가 RunRecord에 도구 실패로 기록된다.
- 보드 전원이 끊기면 CPLD SRAM 구성이 사라진다. BBB 재부팅 순서에 특히 주의.
- `runs/`는 gitignore 대상. manifest의 해시로만 추적한다.

## 검증 명령

```powershell
py -3 -m pytest ai_hardware_autolab/tests -q          # 625 passed, 1 failed (해시 불일치 가드)
py -3 ai_hardware_autolab/mcp_server/server.py --list-tools
py -3 -m ai_hardware_autolab.agents.export --check-only   # ok: 6 definitions, no problems
py -3 -m ai_hardware_autolab.orchestrator.audit           # 계약 17 × run 85 대조
```

수치: 계약 17개 · 테스트 파일 28개 · 에이전트 6개 · run 기록 85개.
감사 결과: **error 5 · warning 6 · note 63** (`docs/2026-08-21-evidence-audit.md`).
