# AI Hardware Autolab

자기 프로젝트의 소스 폴더를 연결하고 시나리오를 입력하면 **Lead → Builder → Verifier**가 조사·설계·구현·검증을 이어가는 개발 플랫폼입니다. 웹 화면에서 역할별 진행 상황과 결과를 확인하고, 축적된 개발 지식을 지식맵에서 탐색합니다.

웹 서비스, 데이터 분석, 연구용 소프트웨어 개발에 같은 흐름을 사용합니다.

## 현재 제공하는 기능

- **프로젝트 연결:** 데몬 PC의 Git 소스 폴더, 목적·참고 자료, 빌드·검증 방법을 등록하고 작업별로 선택합니다.
- **3개 역할:** Lead가 조사·설계, Builder가 구현·실행, Verifier가 요청과 실제 결과를 비교합니다. 부족하면 같은 작업에서 수정합니다.
- **모델 선택:** 공통 모델과 역할별 모델·추론 강도를 선택합니다. 현재는 로컬 Codex CLI가 제공하는 모델을 지원합니다.
- **작업공간:** 대상 프로젝트의 현재 소스를 별도 Git 작업공간에 복사합니다. 원본 브랜치와 소스는 자동으로 덮어쓰지 않습니다.
- **지식맵:** 프로젝트/주제별 가지, 전체 별자리 보기, 본문 검색과 구체적인 기록 상세를 제공합니다.
- **자동 적재:** 역할 결과의 개발 메모, 참고 문서, 구현 파일·해시와 검증 내용을 저장합니다. 재시작 후 놓친 기록을 따라잡습니다.

## 빠른 시작 — 다른 연구원의 프로젝트

필수 환경: Windows, Git, Python 3.11 이상, Node.js 20 이상/npm, 로그인된 Codex CLI. Docker 또는 WSL은 필요하지 않습니다. 모델 사용량은 본인의 Codex 계정에 적용됩니다.

### 1. 설치

플랫폼을 설치할 폴더 이름은 원하는 이름으로 지정합니다. 아래에서 my-agent-platform을 각자의 이름으로 바꾸세요. 설치 스크립트가 현재 설치 위치를 기준으로 Python 환경과 웹 화면을 준비합니다.

~~~powershell
$platformName = 'my-agent-platform'
git clone https://github.com/basstwd17-cmyk/ai-hardware-autolab.git $platformName
Set-Location -LiteralPath $platformName
.\scripts\setup-platform.ps1
~~~

Python 실행 파일을 별도로 지정하려면 setup-platform.ps1에 -PythonCommand 'C:\Python311\python.exe'를 전달합니다. 웹 화면만 다시 준비할 때는 -SkipPython을 사용합니다.

### 2. 개발할 프로젝트 준비

기존 소스 폴더를 그대로 사용합니다. Git 저장소의 최상위 폴더와 첫 커밋이 필요합니다. 소스가 없다면 새 프로젝트 폴더를 만들고 README 등 필요한 파일을 첫 커밋으로 저장하세요. 토큰·실험 원본·대용량 산출물은 대상 프로젝트의 .gitignore에 지정합니다.

플랫폼을 설치한 폴더와 개발 대상은 별개입니다. 예를 들어 플랫폼은 C:/Tools/my-agent-platform, 개발 소스는 C:/Research/my-analysis에 둘 수 있습니다. 개발 대상 폴더 이름과 화면에 표시할 프로젝트 이름도 각자 정합니다.

### 3. 실행 — PowerShell 창 두 개

두 창 모두 플랫폼 저장소 루트에서 시작합니다.

~~~powershell
# 창 1: 개발 대상 프로젝트를 연결한 데몬
$projectRoot = 'C:\Research\my-analysis' # 각자의 개발 소스 경로
.\scripts\run-platform.ps1 -Component daemon -ProjectRoot $projectRoot

# 창 2: 로컬 웹 화면
.\scripts\run-platform.ps1 -Component web
~~~

웹 서버가 출력한 **전체 세션 URL**을 엽니다. 이 실행기는 일반 개발(offline)을 기본으로 사용하며 모델은 그대로 호출합니다. 동일 원장/파이프에 데몬을 중복 실행하지 마세요.

### 4. 화면에서 개발 시작

1. Start → 내 프로젝트 연결에서 이름, 데몬 PC 기준 소스 경로와 개발 안내를 저장합니다. 같은 폴더를 다시 연결하면 안내를 갱신합니다.
2. 이번 작업의 프로젝트에서 선택합니다. 새 프로젝트 선택은 일반 개발 모드로 시작합니다.
3. 모델을 선택하고 구현할 기능과 확인할 결과를 입력합니다.
4. Agents에서 역할별 진행·질문·구현 파일·검증 결과를 확인합니다.
5. 완료 작업의 작업공간에서 변경을 검토한 뒤 원본 프로젝트에 반영합니다. 플랫폼이 자동 merge/push하지 않습니다.

예: CSV 실험 로그에서 결측치를 검사하고 그래프를 그리는 기능을 구현해줘. src/loader.py를 재사용하고 tests/ 회귀 테스트로 확인해줘.

## 지식맵에서 구체적인 정보 읽기

Map → 프로젝트 필터 또는 프로젝트 가지 → 기록 선택으로 들어갑니다.

- 전체 본문과 코드 블록, 설계·구현 결정과 그 이유
- 구현/검증 파일과 저장된 해시
- 참고 문서 링크와 실제 조사 내용
- 저장된 검증 결과, 상태와 적용 범위
- 작업공간 위치, 관련 작업, 원본 기록과 검사 내역

에이전트의 서술은 미검증으로 저장합니다. 개별 기록의 근거 확인이 시나리오 전체 성공을 뜻하지는 않습니다. 과거 기록에 세부 내용이 저장되지 않았다면 이를 새로 만들어 표시하지 않습니다.

새 작업은 선택한 프로젝트의 지식을 조회합니다. 프로젝트를 바꿔도 진행 중 작업의 대상과 설정은 유지됩니다.

## 선택적 원격 배포

Cloudflare 배포는 선택 사항입니다. 각 연구원의 D1/토큰/릴레이 설정을 별도로 준비하세요. 로컬 연구실의 인증 파일이나 기존 토큰을 복사하지 않습니다. 최초 프로젝트 연결은 로컬 웹에서 진행하세요.

## 지원 범위와 제한

- 현재 로컬 런타임은 Codex CLI입니다.
- Git 저장소와 첫 커밋이 필요합니다. Git 서브모듈의 자동 초기화, 모든 언어의 툴체인 설치는 제공하지 않습니다.
- Builder의 일반 명령은 작업공간의 샌드박스에서 실행하며 네트워크 설치가 제한됩니다. 프로젝트의 의존성과 외부 서비스 준비 방법을 안내하세요.
- 소스 변경은 작업공간에 남습니다. 최종 병합·배포는 사람이 검토합니다.
- 프로젝트 구분은 작업/지식의 분류입니다. 하나의 데몬을 여러 사람이 쓰는 조직용 사용자 권한 분리 기능은 아닙니다. 연구원별 독립 설치를 기본으로 안내합니다.

## 저장소 구성

| 위치 | 내용 |
|---|---|
| ai_hardware_autolab/orchestrator/ | 프로젝트 연결, 3개 역할 실행, 원장·데몬·작업공간 |
| ai_hardware_autolab/knowledge/ | 지식 저장·검색·자동 적재 |
| ai_hardware_autolab/webui/ | React 화면, 로컬 웹 서버, 선택적 Cloudflare 연결 |
| scripts/run-platform.ps1 | 자기 프로젝트를 연결하는 공유용 실행기 |
| scripts/setup-platform.ps1 | 설치 폴더 이름과 위치를 자동으로 찾는 설치기 |
| ai_hardware_autolab/tests/ | 장비·유료 모델을 호출하지 않는 회귀 검증 |

이전 계약/Campaign 관련 파일은 과거 구현·호환 자료입니다. 신규 시나리오 실행의 필수 조건이 아닙니다.

## 문서와 검증

- [프로젝트 개발 안내](ai_hardware_autolab/docs/project-development.md)
- [현재 구현 상태와 검증 범위](ai_hardware_autolab/STATUS.md)
- [모델 선택](ai_hardware_autolab/docs/model-selection.md)
- [선택적 원격 배포](ai_hardware_autolab/webui/cloudflare/README.md)

개발자를 위한 내부 검증 명령입니다. 일반 사용자는 위 설치·실행 스크립트만 사용하면 됩니다. ai_hardware_autolab은 플랫폼의 Python 패키지 이름입니다. 설치 폴더와 개발 프로젝트의 이름은 별도로 자유롭게 정합니다.

~~~powershell
.\.venv\Scripts\python.exe -m pytest ai_hardware_autolab/tests/test_projects.py ai_hardware_autolab/tests/test_conversation_service.py ai_hardware_autolab/tests/test_conversation_ingestion.py -q
cd ai_hardware_autolab/webui/frontend
npm.cmd test
npm.cmd run build
~~~

실행 DB, 등록한 개인 프로젝트 경로, 작업공간, 세션 토큰, 계정별 Cloudflare 설정과 신규 실행 결과는 각 PC의 운영 데이터입니다. 저장소에 커밋하거나 일괄 삭제하지 않습니다. 기존 연구실 지식 DB는 소스 clone에 자동으로 포함되지 않습니다.

제3자 에셋의 출처·라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)와 ai_hardware_autolab/webui/assets/MANIFEST.json을 참고하세요.
