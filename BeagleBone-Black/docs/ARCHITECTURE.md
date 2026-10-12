# 아키텍처 개요

## 1. 프로젝트 요약

`bbb_pulse_io`는 **BeagleBone Black(AM335x)**의 **PRU-ICSS**를 이용해 **5 ns(200 MHz, 1 instruction = 1 cycle)** 분해능의 펄스 출력과 출력→입력 상대 타이밍(윈도우) 제어를 구현하고, 이 5 ns 격자 주장을 자기참조 루프백이 아니라 **외부 크리스털 기준(Keysight 33521B AWG)**으로 독립 검증하는 프로젝트다. 자기참조 루프백은 신뢰할 수 없다고 판단해 폐기했고, 최종 검증은 전부 AWG의 `TRIG:DEL` 격자 대비 선형 적합으로 수행한다. 이 리포는 **BeagleY-AI(AM67A, ECAP 8 ns, PRU 없음)** 단계에서 정의·검증한 목표(R1–R5/G1–G5)를 그대로 이어받아 BBB의 PRU로 재현·이식한 후속 작업이다.

## 2. PRU-ICSS 배경

**PRU-ICSS**(Programmable Realtime Unit - Industrial Communication Subsystem)는 AM335x SoC 안에 Linux가 도는 ARM Cortex-A8 코어와 별도로 존재하는 보조 코어 서브시스템이다. 이 프로젝트가 쓰는 PRU0는 `pruss_ocp_gclk = 200.000 MHz`로 동작하며, **파이프라인 스톨과 캐시가 없다** — 즉 명령 1개를 실행하는 데 걸리는 시간이 항상 정확히 1 cycle = **5.000 ns**로 고정된다. 이 결정론성 덕분에 `__delay_cycles()`(컴파일 타임 상수)로 넣은 지연은 지터 없이 정확히 그 사이클 수만큼만 걸리고, 그 위에 얹은 GPIO 토글은 **cycle-exact 타이밍**이 된다. 실물 확인(2026-06-17): `remoteproc1` = PRU0(`0x4a334000`), `remoteproc2` = PRU1(`0x4a338000`).

Linux 쪽에서 PRU를 다루는 두 가지 개념:

- **remoteproc** — PRU에 펌웨어(`.out`)를 올리고 내리는 커널 프레임워크. `/sys/class/remoteproc/remoteproc1/firmware`에 `/lib/firmware/` 안의 펌웨어 파일명을 쓰고, `/sys/class/remoteproc/remoteproc1/state`에 `stop`/`start`를 써서 PRU 코어를 껐다 켠다. `provision.py`가 이 절차를 자동화한다.
- **RPMsg** — Linux 호스트와 PRU 펌웨어 사이의 제어 채널. PRU가 뜨면 `resourceTable.rpmsg_vdev.status`에 `VIRTIO_CONFIG_S_DRIVER_OK`가 설 때까지 기다렸다가, `pru_rpmsg_init()`으로 vring을 초기화하고 `pru_rpmsg_channel(RPMSG_NS_CREATE, ..., "rpmsg-raw", 30)`으로 채널을 알린다. 이 채널이 붙으면 Linux 쪽에 문자 디바이스 `/dev/rpmsgN`이 생기고, 호스트는 여기에 1바이트 명령을 쓰고 응답을 읽는다.

> ⚠️ AM335x BBB 헤더에는 eCAP **캡처 입력 핀(capin)**이 배선돼 있지 않다. PRU-ICSS 내부의 `CT_ECAP` 자유 카운터(200 MHz, `ECCTL2` `TSCTRSTOP`=1로 free-run) 자체는 정상 동작해 타임스탬프 소스로 쓰이지만, 입력 엣지 검출은 HW 래치가 아니라 **R31 소프트웨어 폴링**으로 대체돼 있다.

## 3. 3자 구성: Host · Board · AWG

```
┌───────────────────────┐                                   ┌───────────────────────────────┐
│  Host (PC, 이 리포)      │   SSH (xilinx@board)              │  Board (BeagleBone Black)      │
│                        │ ────────────────────────────────▶ │  Linux, Debian 13 (trixie)     │
│  host/bb.py            │   원격 셸에서 스크립트 실행,           │                                │
│  host/provision.py     │   /dev/rpmsgN 오픈 → 1B cmd 기록    │  remoteproc1 = PRU0            │
│  host/awg.py           │ ◀──────────────────────────────── │  (PRU-ICSS, 200 MHz = 5 ns/cyc)│
│  host/run_awg_bbb.py   │   stdout / 1B+payload 응답          │  /dev/rpmsgN ↔ pru_rpmsg_*     │
└───────────┬────────────┘  [제어면: RPMsg, 1바이트 cmd]        └────────────────┬───────────────┘
            │                                                                  │
            │ LAN (SCPI, TCP 5025)                          [데이터면: GPIO 핀 직접 토글/폴링]
            │                                                                  │
            ▼                                              R30.15(OUT0) ─▶ P8_11 ──┐ (트리거)
┌───────────────────────┐                                   R30.14(OUT1) ─▶ P8_12  │
│  AWG                   │ ◀──────────────────────────────────────────────────────┘
│  Keysight 33521B       │        P8_11 → AWG Ext-Trig 입력
│  (외부 검증 시에만 필요)   │ ────────────────────────────────────────────────────────▶
└───────────────────────┘        AWG Out(echo) → P8_16 (R31.14, IN0)
```

핵심은 **제어면과 데이터면의 분리**다.

- **제어면**: Host는 PRU와 직접 통신하지 않는다. SSH로 Board에 접속해 원격 셸에서 파이썬 스크립트를 실행하고, 그 스크립트가 Board 안에서 `/dev/rpmsgN`을 열어 1바이트 명령(+필요 시 4바이트 payload)을 쓰고 응답을 읽는다. RPMsg 자체는 보드 로컬 채널이며 네트워크로 노출되지 않는다.
- **데이터면**: 실제 펄스와 타이밍은 RPMsg를 거치지 않는다. PRU0가 `R30`(GPO) 비트를 직접 세팅/클리어해 헤더 핀을 토글하고, `R31`(GPI) 비트를 직접 읽어 입력 핀 상태를 폴링한다. 명령이 하는 일은 "어떤 타이밍 패턴을 실행할지"를 지정하는 것뿐이고, 그 패턴의 실제 실행은 PRU 내부에서 사이클 단위로 끝난다.

| 핀 | 레지스터 비트 | 방향 | 용도 |
|---|---|---|---|
| P8_11 | `R30.15` (`OUT0`) | 출력 | 5 ns 펄스 / AWG 외부트리거 |
| P8_12 | `R30.14` (`OUT1`) | 출력 | 2번째 채널 (out0→out1 지연 데모) |
| P8_16 | `R31.14` (`IN0`) | 입력 | 루프백 / AWG 에코 |

> ⚠️ P8_16 입력은 **0–3.3 V**만 허용한다. AWG 출력 레벨을 이 범위로 맞춰야 한다(README 배선 표 참고).

AWG는 검증(G5, cmd `0x42`)이 필요할 때만 등장하는 외부 장비다. Board의 P8_11이 AWG의 external trigger 입력으로 들어가고, AWG는 `TRIG:DEL`만큼 지연된 echo를 내보내 Board의 P8_16으로 되돌아온다. Board는 이 echo를 PRU 내부에서 `CT_ECAP` 자유 카운터(cmd `0x42`) 또는 `pru_delay`(cmd `0x43`)로 시각화해 AWG의 crystal-traceable 지연 τ와 비교한다.

## 4. 디렉터리 구조

```
BeagleBone-Black/
├── README.md                 (최상위 퀵스타트)
├── requirements.txt          (호스트 파이썬 의존성: paramiko)
├── .gitignore
├── docs/                     (이 문서들 — 경계별 상세 매뉴얼)
├── firmware/                 (PRU0 펌웨어 소스 + vendored TI 지원팩)
│   ├── main.c
│   ├── pru_delay.asm
│   ├── Makefile
│   ├── resource_table.h, intc_map_0.h, AM335x_PRU_intc_rscTbl.cmd
│   └── pru-support/          (vendored TI 헤더 + rpmsg_lib.lib, 오프라인 빌드용)
├── overlay/
│   └── BB-PRUOUT-P8-00A0.dts (U-Boot 핀먹스 오버레이)
├── host/                     (PC에서 실행, SSH로 보드 제어)
│   ├── bb.py                 (SSH 러너)
│   ├── provision.py          (원커맨드 프레시보드 배포)
│   ├── awg.py                (Keysight 33521B SCPI 드라이버)
│   └── run_awg_bbb.py        (AWG 외부검증 스윕)
├── scripts/                  (보드측 작업, host/bb.py로 실행)
│   ├── deploy.sh, verify_timebase.sh, verify_window.sh, demo.sh, reboot.sh
└── results/
    ├── awg_staircase_avg.csv
    └── awg_strobe_avg.csv
```

각 최상위 디렉터리가 담당하는 경계와 상세 문서:

| 디렉터리 | 담당 경계 | 상세 |
|---|---|---|
| `docs/` | 경계별 상세 매뉴얼 모음(이 문서 포함) | 아래 "다음에 읽을 문서" 참고 |
| `firmware/` | PRU0 펌웨어 소스(C/asm)와 오프라인 빌드용 vendored TI 지원팩 | [FIRMWARE.md](FIRMWARE.md) |
| `overlay/` | U-Boot 핀먹스 오버레이(dts) — P8_11/P8_12를 출력, P8_16을 입력으로 PRU R30/R31에 연결 | [OVERLAY.md](OVERLAY.md) |
| `host/` | PC에서 실행하는 SSH 제어·배포 도구 및 AWG SCPI 드라이버 | [HOST_TOOLS.md](HOST_TOOLS.md) |
| `scripts/` | 보드 측에서 실행되는 배포·검증·데모 스크립트(`host/bb.py`로 기동) | [OPERATIONS.md](OPERATIONS.md) |
| `results/` | AWG 외부검증 원시 측정 결과(csv) | [VERIFICATION.md](VERIFICATION.md) |

## 5. BeagleY-AI 대비 비교

| 항목 | BeagleY-AI | BeagleBone Black(이 리포) |
|---|---|---|
| SoC | AM67A (J722S) | AM335x |
| 정밀 타이밍 수단 | ECAP TSCTR 125 MHz (EPWM HRPWM firewall) | PRU-ICSS 200 MHz (PRU0/PRU1) + `CT_ECAP` 자유 카운터(200 MHz) |
| 결정론적 분해능 | **8 ns** | **5 ns** (명령 1개 = 1 cycle = 5 ns) |
| PRU 유무 | 없음 | 있음 (PRU0/PRU1) |

## 6. 다음에 읽을 문서

- 처음 보드를 설치/배포한다 → [OPERATIONS.md](OPERATIONS.md)
- 요구사항(R1–R5)과 달성 현황(G1–G5)을 확인한다 → [GOALS.md](GOALS.md)
- RPMsg 명령 프로토콜과 PRU 펌웨어 내부 동작을 본다 → [FIRMWARE.md](FIRMWARE.md)
- 핀먹스/배선 세부사항을 본다 → [OVERLAY.md](OVERLAY.md)
- 호스트 도구(`bb.py`, `provision.py`, `awg.py`) 사용법을 본다 → [HOST_TOOLS.md](HOST_TOOLS.md)
- AWG 외부검증 방법과 수치를 재현한다 → [VERIFICATION.md](VERIFICATION.md)
- 문제가 생겼을 때 → [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
