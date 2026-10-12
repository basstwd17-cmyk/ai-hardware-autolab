# 에이전트 실행부 교체 — 1단계 기준선

작성일: 2026-09-10. 범위: 변경 분리·기존 경로 보존·완료 기준 고정.
**새 실행부는 아직 구현하거나 활성화하지 않았다. 실물 검증 완료 문서가 아니다.**

## 이번에 정리한 것

| 구분 | 처리 |
| --- | --- |
| PowerShell 래퍼·복합 개발 명령 오탐 수정 | 유지. 실제 거부 명령과 승인 함수 회귀 테스트 포함 |
| 짧은 Lead 계획 → Builder 준비, 후보 참조, 재사용 카탈로그 부분 로딩 | 기존 경로 그대로 유지 |
| 실험 중 `device_setup.py` 및 `__scenario__` 자동 계약 변환 | 초안 보관 후 실행 경로에서 분리 |
| 실험 중 시나리오 금지문 추출·복합 동작 권한 검사 연결 | 초안 보관. 부분 구현을 완료된 보호장치로 취급하지 않음 |
| 실험 중 SSH 전기적 검사 예외·GPIO-SPI 전용 검사 | 초안 보관 후 원래 검사로 복원 |
| 실험용 device_setup을 쓰라는 Lead/Builder 지침 | 제거하고 기존 지침 유지 |
| 웹, 원장 스키마, 장비 코드, job 작업공간, 기존 run | 이번 단계에서 변경하지 않음 |

저장소에 광범위한 기존 미커밋 변경이 있어 Git HEAD로 되돌리지 않았다.
이 대화에서 추가한 미완성 변경만 정확한 패치 단위로 분리했다.
커밋·브랜치 전환·운영 프로세스 재시작·job 취소/재개·장비 접속은 하지 않았다.

### 초안 복구 자료

`history/execution-redesign-20260910/unverified-device-setup-before-separation.zip`

SHA-256: `d4aee52b7c5da3e0f010ccf9c648d837874e036e3cdc8dc165e1734b4288696d`

ZIP은 분리 직전의 여섯 파일 전체 사본이며 경로 매핑은 아래와 같다.
다른 기존 변경도 포함하므로 **나중에 통째로 덮어쓰지 말고 필요한 변경만 비교·적용한다.**

| ZIP 항목 | 원래 경로(ai_hardware_autolab 기준) |
| --- | --- |
| device_setup.py | orchestrator/device_setup.py |
| development.py | orchestrator/development.py |
| contract_compiler.py | orchestrator/contract_compiler.py |
| scenario_access.py | orchestrator/scenario_access.py |
| safety.py | hwtools/safety.py |
| builder.md | agents/simple/builder.md |

ZIP CRC 검사 정상, 6개 항목 확인. device_setup.py는 실행 소스에서 제거했으나 ZIP에서 복구 가능하다.

## 보존하는 플랫폼과 교체 경계

현재 연결:

`웹/ControlPlane → JobLedger → Daemon.tick → WorkflowRouter → simple Loop → CodexAgentRunner`

- 보존: 웹 UI/이벤트 전송, JobLedger, 스케줄러, WorkspaceManager, run/아티팩트 저장,
  지식 조회, 실제 장비 드라이버, 취소·승인·접근 경계.
- 교체 대상: simple Loop의 계획 해석, 필수 계약 컴파일, 준비 iteration,
  에이전트 응답/실패 전달과 역할 전환. 런타임 호출부도 필요한 연결만 분리한다.
- 이후 연결점: `orchestrator/workflows.py`의 `advance(job_id)`/`phase(job_id)`.
  현재는 `__main__.py`가 구성한 기존 simple Loop를 그대로 사용한다.
- 새 경로는 별도 명시적 workflow 식별자로 선택한다. 기존 Brief를 수정하거나
  기존/취소 job을 새 실행부로 자동 전환하지 않는다. 아직 라우터 코드는 변경하지 않았다.
- 새 실행부도 재시작 시 이미 장비에 적용됐는지 불명확한 동작을 자동 재실행하지 않는다.
- 장비 권한과 전기적 제한은 기능 성공 판정과 분리한다. Verifier의 사후 판정은
  장비 안전 검사를 대체하지 않는다. 반대로 안전 문서 작성이 역할 전달의 필수조건이 되지 않는다.

## 첫 시나리오 고정

BBB–RTASE V1 CPLD GPIO-SPI CODE0~CODE3 쓰기·읽기와 스코프 실제 파형 기록.
장비 주소·배선은 사용자가 제공한 검증 시나리오 기준이며 현재 연결 성공을 의미하지 않는다.

- BBB: 192.168.50.2:22, 사용자 xilinx. 비밀번호는 로컬 자격증명만 사용.
- 스코프: RTM3004, 192.168.50.3:5025.
- BBB/RTASE는 P8/P9 헤더 결합, PCB 전원부 공급. 전원 설정 변경 없음.
- CH1=P9_17/CS, CH2=P9_22/CLK, CH3=P9_18/MOSI, CH4=P9_21/MISO.
- 공통 GND, 프로브 ×10, 채널 ×10, 1MΩ, DC coupling.
- CODE0=0x0001, CODE1=0x0123, CODE2=0x2345, CODE3=0x3FFE.
- 기존 코드/타이밍/현재 CPLD 구성 우선. 스코프 준비 후 대표 쓰기·읽기 포착.
- SRAM은 꼭 필요한 근거와 검증된 후보가 있을 때만 허용. Flash, 전원 변경,
  PRU·DAC·RF 출력은 금지. 장비 상태 불명확 시 추측 재실행 금지.

완료 증거:

1. 네 레지스터의 쓴 값/읽은 값/일치 여부.
2. CS/CLK/MOSI/MISO 실제 파형과 실측 클럭 주기.
3. 파형 CSV와 화면 캡처가 실제 파일로 존재.
4. 실행 코드/수정 코드 위치와 식별 정보. CPLD 구성은 확인된 범위만 보고.
5. 각 장비 시도에 구분되는 run, Verifier의 요구사항별 근거·미확인 사항.

준비 테스트 PASS, 스코프 식별 성공, Plan/StepResult 생성만으로 이 시나리오를 완료 처리하지 않는다.
전체 테스트·새 SPI 구현은 기존 방법으로 해결되지 않을 때만 한다.

## 반드시 재현할 실패

- `job_af81239f2769`: pytest·해시 명령이 장비 직접 실행으로 오인됨.
  이번 기준선은 해당 명령 분류와 승인 함수 수정이 유지된 상태다.
- `job_76bb2c18632e`: 준비 테스트 2개 통과 후 실제 스코프 식별은 성공했지만,
  `generated hardware contract has no electrical limits`로 거래/파형 수집 전 실패.
  근거: `../runs/20260910T011220Z_mission_gpio_spi_current_config/manifest.json`.
  이 오류는 **1단계에서 해결하지 않았다**. 새 경로에서 같은 불일치가 발생하지 않아야 한다.
- 복합 스코프 레시피가 BBB를 조작하는 경우, 스코프 권한만으로 BBB 접근까지
  허용되어서는 안 된다. 새 경로의 장비 실행 연결에서 별도 회귀 확인이 필요하다.

## 검증 및 다음 단계

장비 없는 회귀: **157 passed, 1 skipped**.

```powershell
py -3 -m pytest ai_hardware_autolab/tests/test_execution_guard.py ai_hardware_autolab/tests/test_codex_runner.py ai_hardware_autolab/tests/test_builder_preparation.py ai_hardware_autolab/tests/test_execution_simplification.py ai_hardware_autolab/tests/test_handoff_preparation.py ai_hardware_autolab/tests/test_scenario_access.py -q --tb=short -p no:cacheprovider
```

Windows 임시 폴더 권한이 있는 환경에서 실행. 이 결과는 전체 저장소/라이브 모델/
하드웨어 검증을 뜻하지 않는다. 프런트엔드 변경·빌드·배포 없음.
운영 데몬이 이미 로드한 코드는 이번 파일 정리로 자동 갱신되지 않는다.
이 기준선만으로 사용자에게 같은 하드웨어 job을 다시 돌리도록 권하지 않는다.

다음은 2단계: 별도 최소 실행부에서 목표/맥락/산출물 전달과
Lead → Builder → Verifier → 수정 요청 흐름을 장비 없이 검증한다.
기존 Loop를 먼저 고치거나 기본 라우팅을 전환하지 않는다.
UI·모델 대화 유지·새 보드 확장은 첫 실물 시나리오 완료 뒤로 미룬다.

### 기준선 소스 SHA-256

| ai_hardware_autolab 기준 파일 | SHA-256 |
| --- | --- |
| orchestrator/execution_guard.py | 0c8cfa58562ff964b4aa0150bfc0ad0f92e823690cd0140f7329c665214641dd |
| orchestrator/development.py | a889c04da93ba2cee52b3d57bffd1a6b9ac435ebc93e83de127a7914a0100ad9 |
| orchestrator/contract_compiler.py | d88d0abae79ea68fa7208b88dcb8e075b1d829391894196b8ec3d3f1b2d44386 |
| orchestrator/scenario_access.py | 93627bd5e93654371aae1abb0790f4ec423ea3f3e145f9f6ec9001b70101a26d |
| hwtools/safety.py | ffb5dc2c4244a68680099bd9856b92388f5e413dc2b5806133189001970cf5e9 |
| agents/simple/builder.md | c96a45a1f5dc8a730495ea0806a482f2370cd270cb063a6350af863862bbd03a |
| orchestrator/workflows.py | a52d36a7572703ba4d0c1cc67f5f08a8bbc563279abc2250c3f40f63720127d7 |
