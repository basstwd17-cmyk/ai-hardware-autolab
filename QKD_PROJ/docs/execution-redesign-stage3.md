# 실행부 교체 — 3단계: Codex 개발 런타임 연결

2026-09-10. 별도 `conversation-prototype-v1` 경로만 대상이다.
**웹의 기본 실행부·운영 job·장비 권한은 변경하지 않았다. 하드웨어 실행 완료 단계가 아니다.**

## 달라진 연결

`ConversationFlow → ConversationCodexRuntime → StdioAppServer → Codex 기본 개발 도구`

기존 JSON-RPC 전송 구현만 재사용한다. 기존 CodexAgentRunner의 Plan/StepResult,
prepared_plan, 계약 컴파일, 도구별 필수 인자 생성은 호출하지 않는다.

- Lead: 원래 목표와 이전 결과를 읽고 일반 문장으로 접근 방법 전달.
- Builder: 격리된 작업공간의 코드 작성·명령 실행·일반 개발 오류 수정.
- Verifier: 읽기 전용으로 결과를 검토하고 완료 또는 구체적인 수정 요청.
- Codex 한 작업 턴 안에서는 기본 도구 루프가 계속 진행된다. 명령 exit code가 0이
  아니라는 이유만으로 플랫폼이 턴을 종료하지 않는다. 모델이 결과를 보고 수정할 수 있다.
- 턴 사이에는 원장의 역할별 최신 메시지·사용자 피드백·산출물 참조를 다시 전달한다.
  역할마다 장기 대화를 영구 유지하는 기능은 이번 범위가 아니다.

구현 방법·보드 종류별 분기·필수 테스트 횟수는 추가하지 않았다.
최종 응답에는 status/summary/artifacts/target만 사용하며 중첩 JSON 문자열은 없다.
성공 여부는 원래 시나리오와 실제 결과를 검토해야 하며 파일 존재만으로 증명되지 않는다.

## 오류와 권한 처리

| 상황 | 처리 |
| --- | --- |
| 작업공간 코드·일반 명령 오류 | 같은 Codex 작업 턴에서 원인 확인/수정 가능 |
| 최종 응답 JSON/필드/파일 참조 오류 | 최대 한 번, 도구 비활성화된 별도 교정 턴으로 응답만 수정 |
| 프로세스 단절·타임아웃 | 실행 전체를 자동 재시도하지 않음. 원장에 미완료 기록 |
| 사용자 취소 | 취소를 원장에 먼저 기록한 뒤 실행 중 턴 인터럽트 요청 |
| 취소 후 늦게 도착한 응답/오류 | 취소 유지, 완료/재시작으로 덮어쓰지 않음 |

교정 턴에는 원래 최종 메시지와 오류만 전달한다. shell/unified_exec/apply_patch를
비활성화하고 readOnly sandbox를 사용한다. 실제 작업 턴을 재호출하지 않는다.
작업 턴 1회 + 필요 시 교정 1회가 하나의 advance에 해당하므로, 모델 호출 수는 최대 2회다.
2단계의 'advance당 runtime 한 번'은 유지되지만 runtime 내부 모델 턴과는 구분한다.

일반 Builder 명령에는 기존 오프라인 명령 분류기와 native workspaceWrite sandbox를 쓴다.
관측된 fileChange의 모든 대상/이동 경로가 작업공간 안일 때만 해당 패치를 승인한다.
영구 명령 규칙 제안은 채택하지 않고 이번 명령만 승인한다. 추가 파일/네트워크 권한은
거부한다. Lead/Verifier와 응답 교정은 파일 쓰기 승인을 받지 않는다.

장비 MCP·외부 앱·플러그인은 사용자 설정 파일을 바꾸지 않고 자식 App Server의
실행 설정에서 비활성화하고, effective config를 확인한다. 지원하지 않는 설정이면
권한을 넓히는 대체 실행은 하지 않는다. 사용자 로그인은 설치된 Codex가 관리하며
계정/토큰 파일을 복사하거나 코드에 저장하지 않는다.

**명령 문자열 분류는 OS 격리의 대체물이 아니며 USB/JTAG 등 모든 직접 장치 경로를
차단했다고 주장하지 않는다.** 장비를 허용하는 런타임과 접근 직전의 공통 권한 연결은
4단계에서 별도로 검증해야 한다. 현재 런타임은 hardware 모드를 받지 않는다.
Native Web/지식 서비스의 새 실행부 연결도 아직 없으며 이번 단계는 오프라인 개발이다.

## 명시적 개발용 실행

운영 데몬/웹의 명령을 바꾸지 않는다. 다음 CLI는 별도 원장과 빈 작업공간을 만든다.
기존 저장소를 복제하거나 원본에 결과를 병합하는 명령이 아니다.

```powershell
py -3 -m QKD_PROJ.orchestrator.conversation_cli create --ledger QKD_PROJ/runs/conversation-dev/jobs.db --workspaces QKD_PROJ/runs/conversation-dev/workspaces --objective "오프라인으로 작은 덧셈 함수를 구현하고 결과를 저장해줘"
```

생성 출력의 job_id를 아래 `<job_id>`에 사용한다. create/status는 모델을 호출하지 않는다.
advance만 실제 모델 사용량을 소비하며 한 역할씩 진행한다. 기본 모델은 사용자 설정을
따르고, 원하는 경우 advance에 `--model`/`--effort`를 명시할 수 있다.

```powershell
py -3 -m QKD_PROJ.orchestrator.conversation_cli status --ledger QKD_PROJ/runs/conversation-dev/jobs.db --workspaces QKD_PROJ/runs/conversation-dev/workspaces --job <job_id>
py -3 -m QKD_PROJ.orchestrator.conversation_cli advance --ledger QKD_PROJ/runs/conversation-dev/jobs.db --workspaces QKD_PROJ/runs/conversation-dev/workspaces --job <job_id>
```

같은 원장의 CLI는 기존 OS 락으로 직렬화된다. CLI 프로세스가 강제 종료되어 시작만
남은 시도는 다음 호출이 임의 재실행하지 않는다. 웹 버튼 연결은 아직 없다.

runtime 이벤트는 job/role/attempt에 호스트가 연결한다. 델타는 제외하고 큰 payload는
잘렸다는 표시와 함께 제한한다. 이 기록은 활동 관측이며 실제 장비 run의 대체물이 아니다.

## 검증 범위

최종 결과: 기존 기준선 포함 **199 passed, 2 skipped**. 별도 opt-in 실제 Codex
Builder smoke **1 passed (12.45초)**: addition.py 생성 → 실행 → 실제 stdout 5를
result.txt에 저장 → 파일 참조 반환. 실제 모델의 세 역할 전체 시나리오 통과는 아니다.

- 임시 SQLite를 쓰는 역할 전환·피드백·취소·재시작 회귀.
- 가짜 응답으로 직접 봉투, 도구 없는 교정, 교정 상한, 단절 시 재시도 금지 검증.
- 별도 Python App Server 테스트 프로세스가 실제 작은 코드를 생성하고 실행:
  실패(exit 1) → 수정 → 성공(exit 0) → 결과 파일 → Verifier 전달.
  이 프로세스의 판단은 결정론적 fixture이며 실제 모델의 사고를 검증한 것은 아니다.
- 설치된 Codex 0.153.4로 initialize/config 조회와 교정 도구 비활성화 확인.
- 라이브 모델 smoke는 기본 테스트에서 제외한다. `QKD_CODEX_LIVE_SMOKE=1`을
  명시해야 작은 Builder 작업 한 번을 실행하며 90초 인터럽트 타이머를 둔다.

라이브 검사에서 발견하고 수정한 연결 오류:

- CLI dotted config 키에 따옴표를 포함해 새 잘못된 MCP 항목을 만들던 오류를 수정.
- untrusted 정책은 작업공간 내부 파일 패치도 승인을 요청할 수 있다. item/started의
  실제 변경 경로를 확인해 단발 승인하도록 수정.
- 영구 실행 규칙 제안이 함께 왔다는 이유로 일반 명령까지 거부하던 오류를 수정.
  제안은 채택하지 않고 검토한 현재 명령만 accept한다.
- pytest 임시 폴더의 OWNER RIGHTS 중심 DACL은 Windows 제한 토큰에서 읽을 수 없었다.
  기존 ACL·sandbox를 바꾸지 않고, 호스트가 만든 일반 상속 권한의 전용 폴더에서
  먼저 모델 없는 command/exec로 동일 sandbox 실행을 확인한 뒤 실제 모델 smoke를 통과했다.

Windows 라이브 smoke에는 `QKD_CODEX_LIVE_ROOT`를 일반 상속 권한의 전용 테스트
디렉터리로 지정한다. 테스트마다 새 하위 폴더를 만들며 결과를 보존한다.
이번 보존 위치: `runs/stage3-live-23a0b0fcff934fb2adf1be2e3143e632/`.
실패한 pytest ACL 재현 폴더도 `runs/stage3-smoke-da56ec703bc44db3a30d7c05ef140601/`에
보존했다. 기존 운영 job/run과는 별개이며 자동 삭제하지 않았다.

실물 BBB/CPLD/스코프, Native Web, 전체 세 역할의 라이브 모델 E2E는 아직 미검증이다.
새 흐름에 장비/run/지식 연결을 마친 뒤 첫 실물 시나리오를 검증하고 기본 라우팅을 전환한다.

공식 API 기준: [App Server — turn, sandbox, approvals](https://learn.chatgpt.com/docs/app-server),
[configuration — shell tools and integrations](https://learn.chatgpt.com/docs/config-file/config-reference).
OpenAI Docs 확인 결과를 반영해 직접 outputSchema와 역할별 sandbox를 적용했다.
