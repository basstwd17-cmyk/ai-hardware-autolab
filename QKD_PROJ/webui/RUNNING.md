# Windows 실행 안내

다른 연구원이 자신의 프로젝트로 시작하려면 [프로젝트 개발 안내](../docs/project-development.md)와
저장소 루트 README를 먼저 참고하세요. 공유용 scripts/run-platform.ps1은 소스 위치를 별도로
받고 일반 개발을 기본으로 실행합니다. 아래는 기존 연구실 하드웨어 운영 명령입니다.

작업 폴더는 `C:\KIST\QKD_PROJ`입니다. `QKD\_PROJ`가 아닙니다.
아래 명령은 PowerShell용이며, 복사 오류를 줄이기 위해 한 줄로 작성했습니다.

## 1. 프론트엔드 빌드

최초 실행, `dist` 삭제, 프론트엔드 소스·정적 자산·의존성·빌드 설정 변경 시 실행합니다.
Python 코드만 바뀌었으면 웹 빌드는 필요하지 않습니다.

```powershell
cd C:\KIST\QKD_PROJ\webui\frontend
npm.cmd run build
```

`node_modules`가 없는 새 설치에서는 먼저 `npm.cmd ci`를 실행합니다.
빌드 후 로컬 브라우저를 새로고침합니다. 프론트엔드만 바뀌면 데몬 재시작은 필요하지 않습니다.

## 2. 데몬 — PowerShell 창 1

Docker Desktop과 WSL은 필요하지 않습니다. 에이전트의 일반 개발 명령은 job별 작업공간의
Codex 샌드박스에서 실행되고, 장비 동작은 호스트 하드웨어 어댑터를 통해 별도로 실행됩니다.
Docker 서비스가 꺼져 있어도 아래 명령으로 플랫폼을 사용할 수 있습니다.

```powershell
cd C:\KIST
py -3 -m QKD_PROJ.orchestrator --ledger QKD_PROJ\webui\state\web-jobs.db --runs QKD_PROJ\runs --knowledge-db QKD_PROJ\runs\knowledge.db --knowledge-project qkd-proj --knowledge-hardware rtase-v1 --knowledge-revision v1.0 --profile hardware --pipe '\\.\pipe\qkd-agent-web'
```

장비 접근을 끄려면 `--profile hardware`만 `--profile offline`으로 바꿉니다.
예전 명령의 `--contracts QKD_PROJ\contracts`는 이제 필요 없습니다. 남아 있어도
호환 안내를 출력하고 무시하며 현재의 시나리오 기반 데몬을 실행합니다.
Offline은 모델을 전혀 호출하지 않는다는 의미가 아닙니다.
기본 구성은 `boards/rtase-v1/v1.0/hardware.yaml`입니다. 시작 화면에서 다른 구성을
등록·선택하면 해당 job에 적용되고, 데몬 재시작은 필요하지 않습니다. 기본값 자체를 바꾸려면
`--hardware-board <등록된 ID> --hardware-revision <등록된 리비전>`을 사용합니다.
`--knowledge-hardware`와 `--knowledge-revision`은 장비를 선택하는 옵션이 아닙니다.
제출한 시나리오의 장비 호출은 자동 실행됩니다. Builder가 job 작업공간에서 만든 BBB
스크립트와 CPLD SRAM XCF/bitstream은 프로필 등록이나 Inbox 확인 없이 사용할 수 있습니다.
실제 실행한 파일 해시와 결과는 run에 기록됩니다.

새 PC에서는 Python 의존성을 `py -3 -m pip install -r QKD_PROJ\requirements.txt`로 설치합니다.
USB/COM 탐색·시리얼 통신에는 포함된 `pyserial`이 필요합니다. Windows USB 목록은
PowerShell의 읽기 전용 PnP 조회를 사용합니다. 연결이 발견되지 않아도 소스 개발은 가능합니다.
보드 등록 절차와 현재 지원 범위는 [다른 보드 지원](../docs/board-independent-development.md)을 참고하세요.

기존 웹 작업 이력을 이어 쓰려면 `--ledger`를 유지합니다.
생략 시 기본 원장은 `QKD_PROJ\runs\jobs.db`로 달라집니다.
파이프 기본값도 `qkd-agent-platform`이므로 이 안내에서는 세 프로세스 모두
`qkd-agent-web`을 명시합니다. 같은 원장/파이프에 데몬을 중복 실행하지 않습니다.

## 3. 로컬 웹 서버 — PowerShell 창 2

```powershell
cd C:\KIST
py -3 -m QKD_PROJ.webui.backend.app --daemon '\\.\pipe\qkd-agent-web' --static QKD_PROJ\webui\frontend\dist
```

출력되는 `open: http://127.0.0.1:8787/?token=...`에서 `http`부터 끝까지 엽니다.
`긴문자열`을 직접 입력하는 것이 아니라 실제 출력된 토큰을 사용합니다.
웹 프로세스를 재시작하면 새 URL을 사용해야 합니다.
이 주소는 실행 중인 PC용입니다. 휴대폰에서 `127.0.0.1`은 휴대폰 자신입니다.

PowerShell의 파이프 표기는 `'\\.\pipe\qkd-agent-web'`입니다.
Python/JSON 문자열의 이스케이프를 추가하지 않습니다. 여러 줄 명령을 쓰는 경우
백틱은 줄 끝 마지막 문자여야 하며 `&#x20;`, `\--`, `wrangler\@latest`는 넣지 않습니다.

## 4. 휴대폰 화면 배포 — 변경 시

Cloudflare 프로젝트·D1·operator/device 토큰의 최초 설정은
[cloudflare/README.md](cloudflare/README.md)를 참고합니다.
설정이 끝났으면 다음 명령으로 빌드 결과와 Pages Functions를 배포합니다.

```powershell
cd C:\KIST\QKD_PROJ\webui\cloudflare
npx.cmd --yes wrangler@latest pages deploy ../frontend/dist --project-name rtase-agent-platform
```

`cloudflare` 폴더에서 실행해야 `functions/`와 `wrangler.jsonc`를 함께 사용합니다.
프론트엔드 변경은 빌드 후 재배포하며, Pages Functions 변경도 재배포가 필요합니다.
운영 주소에 반영됐는지는 Wrangler 출력의 배포 환경/주소를 확인합니다.
미리보기 브랜치 배포는 기존 운영 주소를 갱신하지 않을 수 있습니다.
명령 형식: [Cloudflare Wrangler Pages 문서](https://developers.cloudflare.com/workers/wrangler/commands/pages/).

## 5. 휴대폰 연결 릴레이 — PowerShell 창 3

배포된 화면만으로 로컬 데몬과 연결되지는 않습니다. 데몬을 실행한 PC에서
다음 프로세스도 계속 실행해야 합니다. 기존에 발급한 raw **device** 토큰을 입력합니다.

```powershell
cd C:\KIST
$relaySecret = Read-Host 'Cloudflare device token' -AsSecureString
$env:QKD_RELAY_DEVICE_TOKEN = [System.Net.NetworkCredential]::new('', $relaySecret).Password
Remove-Variable relaySecret
py -3 -m QKD_PROJ.orchestrator.cloud_relay --url 'https://rtase-agent-platform.pages.dev' --device-id 'lab-pc' --daemon '\\.\pipe\qkd-agent-web'
```

휴대폰에서 <https://rtase-agent-platform.pages.dev>를 열고 `lab-pc`와 기존 raw
**operator** 토큰을 입력합니다. 로컬 웹 세션 토큰, operator 토큰, device 토큰은 서로 다릅니다.
휴대폰 경로는 `휴대폰 → Pages/D1 → cloud_relay → 데몬`이며 PC와 릴레이가 켜져 있어야 합니다.
이 경로 자체는 로컬 웹 서버 프로세스를 필요로 하지 않습니다.

## 변경 후 적용 및 정리

### 지식맵 자동 적재·운영 동기화 (2026-10-08)

이번 버전은 위 명령으로 데몬과 릴레이를 모두 새로 실행해야 적용됩니다.
데몬의 `--knowledge-db QKD_PROJ\runs\knowledge.db`를 유지하세요.
데몬이 새 결과와 과거 미처리 대화형 결과를 적재하고 릴레이가 운영 지식맵으로 복제합니다.
모델이나 장비를 과거 결과 수집 때문에 다시 실행하지 않습니다.

운영 첫 화면에서 **지식맵 열기**, 또는
<https://rtase-agent-platform.pages.dev/knowledge.html>로 접속합니다.
기존 operator token과 `lab-pc`를 사용합니다. 상단 `동기화 X/Y`가 같아지면 당시의
지식 이벤트를 모두 받은 상태입니다. 첫 실행은 기존 지식도 받으므로 잠시 걸릴 수 있습니다.
PC가 꺼지면 마지막 동기화 자료를 표시하며 실시간 관측으로 간주하지 않습니다.
`knowledge sync deferred`가 반복되면 릴레이·데몬의 버전과 지식 DB 설정을 확인합니다.

### 기존 적용 안내

- 데몬/호스트 Python 변경: 해당 데몬을 재시작합니다. 실행 중 작업 상태를 먼저 확인합니다.
- 웹 백엔드 Python 변경: 웹 서버를 재시작하고 새 세션 URL을 엽니다.
- 릴레이 Python 변경: 릴레이를 재시작합니다.
- 프론트엔드 변경: 빌드 → 로컬 새로고침, 휴대폰에는 추가 배포합니다.
- `__pycache__`, `.pytest_cache`는 재생성 가능한 캐시입니다.
- `runs`, `boards`, `references`, `candidates`, `workspaces`, `webui/state`, SQLite 파일,
  Cloudflare 설정·인증 파일은 운영 데이터/근거이므로 일괄 삭제하지 않습니다.
- `dist`는 현재 화면을 제공하는 결과물이며 `node_modules`는 빌드 의존성입니다.

현재 구현 범위와 실물 검증 여부는 [STATUS.md](../STATUS.md)를 참고하세요.
