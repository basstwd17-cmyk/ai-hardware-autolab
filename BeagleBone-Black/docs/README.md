# 문서 인덱스

`bbb_pulse_io` 리포지토리의 경계별 상세 매뉴얼 모음이다. 각 문서는 그 자체로 완결적으로 읽히지만,
서로 필요한 곳을 링크한다. 최상위 개요와 5분 퀵리드는 [`ARCHITECTURE.md`](ARCHITECTURE.md)에서
시작한다.

| 문서 | 다룬다 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 전체 그림: PRU-ICSS가 뭔지, Host/Board/AWG 3자 구성, 디렉터리 지도, BeagleY-AI 대비 비교 |
| [GOALS.md](GOALS.md) | 요구사항(R1–R5)과 정량 달성목표(G1–G5), 현재 펌웨어(v1.7) 기준 상태, 정직한 한계 |
| [FIRMWARE.md](FIRMWARE.md) | `firmware/` 전체 — 빌드 시스템, 초기화 시퀀스, RPMsg 명령 전체 표, 타이밍 모델, IMEM 예산 |
| [OVERLAY.md](OVERLAY.md) | `overlay/` — 핀먹스 오버레이가 왜 필요한지, 3-value pinctrl 포맷 함정, 빌드/설치 절차 |
| [HOST_TOOLS.md](HOST_TOOLS.md) | `host/` — `bb.py`/`provision.py` 사용법, sudo·파이프 관련 함정, 프로비저닝 8단계 |
| [VERIFICATION.md](VERIFICATION.md) | `host/awg.py`·`host/run_awg_bbb.py`·`results/` — 외부 AWG 검증 방법론과 실측 수치 |
| [OPERATIONS.md](OPERATIONS.md) | `scripts/` — 퀵스타트, 스크립트별 사용법, 일상 워크플로우, 동적 IP 찾는 법 |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | 증상→원인→해결 트러블슈팅 목록과 용어집 |

## 목적별로 바로 가기

- **처음 보드를 설치/배포한다** → [OPERATIONS.md](OPERATIONS.md)
- **뭐가 어떻게 구성되어 있는지 5분 안에 파악한다** → [ARCHITECTURE.md](ARCHITECTURE.md)
- **요구사항/달성 현황을 확인한다** → [GOALS.md](GOALS.md)
- **RPMsg 명령 프로토콜을 찾는다** → [FIRMWARE.md](FIRMWARE.md)
- **핀 배선/핀먹스 문제를 본다** → [OVERLAY.md](OVERLAY.md)
- **`bb.py`/`provision.py` 사용법을 본다** → [HOST_TOOLS.md](HOST_TOOLS.md)
- **5 ns 검증이 어떻게 이뤄졌는지 본다** → [VERIFICATION.md](VERIFICATION.md)
- **뭔가 안 될 때** → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)

리포 최상위 개요·퀵스타트는 [`../README.md`](../README.md), BeagleY-AI(8 ns) 원본 구현은
[`../../BeagleY-AI/BEAGLEY_AI_PULSE_IO_SPEC.md`](../../BeagleY-AI/BEAGLEY_AI_PULSE_IO_SPEC.md) 참고.
