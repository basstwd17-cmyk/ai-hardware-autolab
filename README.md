# AI Hardware Autolab

로컬 Codex 런타임, 결정론적 계약/증거 판정, SQLite 작업 원장, 하드웨어 승인 경계와 웹 UI를 묶은 개인용 하드웨어 개발 오케스트레이션 플랫폼입니다.

시나리오를 받으면 기존 지식과 계약을 조회하고, 필요한 조사·설계·빌드·시뮬레이션·측정 단계를 계획합니다. 각 단계의 상태는 에이전트 메모리가 아니라 append-only 원장에 남으며, 하드웨어 접촉은 서명된 campaign 범위와 아티팩트 해시를 사용 시점에 다시 확인합니다.

## 저장소 구성

- `QKD_PROJ/orchestrator/` — 작업 원장, 한 tick 한 단계 루프, 데몬, named pipe 제어 채널
- `QKD_PROJ/agents/` — 역할과 도구 프로파일
- `QKD_PROJ/contracts/` — gate 계약과 결정론적 pass criteria
- `QKD_PROJ/boards/`, `campaigns/`, `capabilities/` — 보드·장비·허용 범위 모델
- `QKD_PROJ/research/`, `knowledge/` — 자료 조사와 누적 지식 그래프
- `QKD_PROJ/hwtools/` — 빌드·시뮬레이션·배포·측정용 원시 도구 어댑터
- `QKD_PROJ/webui/` — Pixel Office, agent feed, campaign 제어, Obsidian형 knowledge map, Cloudflare relay
- `.claude/agents/`, `.claude/skills/` — 재사용할 에이전트 및 실행 스킬
- `BeagleBone-Black/` — BBB/PRU 호스트·펌웨어·오버레이 소스

`STM32Cube/stm32g474-hrtim-delay` 구현은 별도 저장소로 관리합니다: <https://github.com/basstwd17-cmyk/stm32g474-hrtim-delay>

## 로컬 실행

Windows PowerShell에서 프런트엔드 소스를 바꾼 경우에만 먼저 빌드합니다.

```powershell
cd C:\KIST\QKD_PROJ\webui\frontend
npm.cmd ci
npm.cmd run build
```

터미널 1에서 데몬을 실행합니다.

```powershell
cd C:\KIST
py -3 -m QKD_PROJ.orchestrator `
  --ledger QKD_PROJ\webui\state\web-jobs.db `
  --runs QKD_PROJ\runs `
  --contracts QKD_PROJ\contracts `
  --knowledge-db QKD_PROJ\runs\knowledge.db `
  --knowledge-project qkd-proj `
  --knowledge-hardware rtase-v1 `
  --knowledge-revision v1.0 `
  --profile offline `
  --pipe "\\.\pipe\qkd-agent-web"
```

터미널 2에서 웹 서버를 실행하고 출력되는 전체 token URL을 엽니다.

```powershell
cd C:\KIST
py -3 -m QKD_PROJ.webui.backend.app `
  --daemon "\\.\pipe\qkd-agent-web" `
  --static QKD_PROJ\webui\frontend\dist
```

처음에는 `--profile offline`으로 검증하십시오. Hardware campaign은 별도 승인 범위, 연결된 장비, fail-safe 출력 상태와 watchdog이 준비된 뒤 사용합니다.

## 검증

```powershell
py -3 -m pytest QKD_PROJ\tests -q
cd QKD_PROJ\webui\frontend
npm.cmd ci
npm.cmd test -- --run
npm.cmd run build
```

## 저장하지 않는 데이터

실행 DB, run 증거, 세션 토큰, Cloudflare 장치/operator token, 로컬 `wrangler.jsonc`, 비트스트림 및 빌드 출력은 의도적으로 Git에서 제외합니다. 이들은 각 실험 장비와 사용자 환경의 로컬 상태이며 소스 코드와 분리해야 합니다.

Cloudflare relay를 새 환경에 배포할 때는 `QKD_PROJ/webui/cloudflare/wrangler.example.jsonc`를 개인 `wrangler.jsonc`로 복사하고 자신의 D1 database ID를 넣으십시오. 토큰은 파일에 커밋하지 말고 환경변수 또는 Cloudflare secret으로만 전달합니다.

## 제3자 에셋

Pixel Office 에셋의 원본, 고정 커밋, 파일별 SHA-256과 라이선스는 `QKD_PROJ/webui/assets/MANIFEST.json` 및 `LICENSE-pixel-agents`에 기록되어 있습니다. 자세한 내용은 `THIRD_PARTY_NOTICES.md`를 확인하십시오.

