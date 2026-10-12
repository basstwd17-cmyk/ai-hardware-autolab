# 운영 가이드 - 최초 실행부터 일상 작업까지 (scripts/)

이 문서는 `bbb_pulse_io`를 처음 만지는 사람이 그대로 따라 할 수 있는 실전 운영
가이드다. 프레시 보드를 배선하고 프로비저닝하는 최초 1회 절차, `scripts/`
폴더의 각 셸 스크립트가 하는 일과 실행 방법, 펌웨어 수정 → 재배포 → 재검증의
일상 루프, 그리고 보드 IP가 바뀌었을 때 다시 찾는 법까지 다룬다.

전체 저장소 구조와 검증 결과 요약은 [README.md](../README.md) 참고.

---

## 1. 최초 실행 퀵스타트 (프레시 보드)

### 1.1 배선

BeagleBone Black의 P8 헤더를 다음과 같이 연결한다.

| 용도 | 핀 |
|---|---|
| out0 (5 ns 펄스 / AWG 트리거) | **P8_11** |
| out1 (2번째 채널) | **P8_12** |
| input (루프백 / AWG 에코) | **P8_16** |
| GND | P8_1 / P8_2 |

> ⚠️ **P8_16(input)에 인가하는 전압은 반드시 0–3.3 V 범위 안이어야 한다.**
> 이 범위를 벗어나면(음전압 포함) AM335x 입력 핀이 손상될 수 있다. AWG나
> 외부 신호원을 연결할 때는 출력 레벨을 3.3 V 이하로 맞추고 공통 GND를
> 반드시 함께 연결한다.

### 1.2 파이썬 의존성 설치

호스트(Windows/Linux/macOS 어디든 상관없음)에서 저장소 루트 기준으로:

```bash
pip install -r requirements.txt
```

`requirements.txt`가 요구하는 것은 `paramiko>=3.0` 하나뿐이다 — `host/bb.py`,
`host/provision.py`가 SSH로 보드에 접속할 때 쓴다.

### 1.3 provision.py 실행

```bash
BB_HOST=beagle.local python host/provision.py
# 또는 IP를 직접 지정: python host/provision.py 192.168.0.42
```

로그인 기본값은 `BB_USER=xilinx`, `BB_PW=xilinx`이며 환경변수로 재정의하지
않으면 이 값이 쓰인다. `beagle.local`은 mDNS 호스트이름으로, 보드가 DHCP로
새 IP를 받아도 그대로 살아있다(1.4절/4절 참고).

`provision.py`는 8단계로 진행되며, 각 단계에서 아래 내용을 확인해야 한다.

| 단계 | 하는 일 | 확인 포인트 |
|---|---|---|
| 1/8 | SSH 접속 + 대상이 진짜 AM335x BeagleBone Black인지 확인 | `model: AM335x BeagleBone Black ...` 출력. 다른 문자열이면 즉시 `ABORT` — 같은 LAN에 xilinx/xilinx 로그인을 쓰는 ZynqMP ZCU111도 있으므로 잘못된 보드를 건드리지 않기 위한 안전장치다 |
| 2/8 | PRU 툴체인(`clpru`) 존재 확인, 없으면 설치 필요 | CGT 경로(`/usr/share/ti/cgt-pru`) 출력 |
| 3/8 | `firmware/`, `overlay/BB-PRUOUT-P8-00A0.dts`, `host/awg.py`, `host/run_awg_bbb.py`를 SFTP로 보드에 업로드 | `pushed firmware/ (with vendored pru-support/), overlay, awg host tools` |
| 4/8 | 보드에서 PRU 펌웨어를 오프라인(벤더링된 `pru-support/`) 빌드 | `gen/pru0_pulse_io.out`가 출력에 나타나야 함. 안 나오면 `ABORT: firmware build failed` |
| 5/8 | `dtc`로 핀먹스 오버레이(dtbo) 빌드 후 `/lib/firmware/`에 설치 | `dtc`가 없으면 `ABORT` |
| 6/8 | `/boot/uEnv.txt`에 `uboot_overlay_pru=...`, `enable_uboot_overlays=1` 설정 (기존 uEnv.txt는 `.bak-pulseio`로 백업) | 이미 설정돼 있으면 `overlay already configured`, 새로 설정했으면 재부팅이 필요하다는 메시지 |
| 7/8 | remoteproc으로 펌웨어 (재)로드. 6/8에서 오버레이를 새로 설정했다면 여기서 **자동으로 재부팅**하고 SSH가 끊겼다 돌아올 때까지 대기 후 재접속을 재시도 | 재부팅이 있었다면 콘솔에 그 과정이 그대로 보임 |
| 8/8 | 핀먹스 클레임 여부(P8_11/P8_12/P8_16)와 펌웨어 VERSION 응답 확인 | 마지막 줄이 `PROVISION OK`여야 성공이다 |

**성공 조건**: 콘솔 마지막 줄이 `PROVISION OK`로 시작하고, 그 직전에 찍힌
`firmware VERSION:` 값이 `bbb_pulse_io`로 시작해야 한다. 이 조건이 아니면
`provision.py`가 펌웨어가 PING/VERSION에 응답하지 않는다는 내용의 `ABORT`
메시지와 함께 종료한다.

`provision.py`는 **멱등(idempotent)**하다 — 중간 어디서 실패하든, 원인을
고치고 그냥 다시 실행하면 된다.

> 실패 시 어디를 봐야 하는지, `paramiko` 접속 오류나 SSH 관련 문제는
> [HOST_TOOLS.md](HOST_TOOLS.md)를 먼저 확인한다.

---

## 2. scripts/ 폴더 스크립트 레퍼런스

모든 스크립트는 저장소 루트에서 `host/bb.py`로 실행한다 — `bb.py`가 로컬의
스크립트 파일을 읽어 SSH로 보드에 그대로 흘려보내고(`bash -l`), 표준출력을
그대로 되돌려준다. 접속 정보는 `BB_HOST`/`BB_USER`/`BB_PW` 환경변수로 준다
(기본값은 `xilinx`/`xilinx`이며, 보드 IP가
바뀌었다면 `beagle.local`을 명시하는 편이 안전하다 — 4절 참고).

아래 명령 형태는 bash 계열(예: Git Bash) 기준이다. Windows PowerShell에서는
`VAR=val cmd` 문법이 없으므로 `$env:BB_HOST = 'beagle.local'; py host\bb.py
scripts/deploy.sh` 형태로 나눠 써야 한다.

| 스크립트 | 목적 | 필요 배선 / 전제조건 | 실행 명령 | 기대 출력 |
|---|---|---|---|---|
| `deploy.sh` | PRU0 펌웨어를 clpru로 빌드하고 remoteproc으로 (재)로드 | `firmware/main.c`가 이미 보드에 올라가 있어야 함(아래 1.1절 배선 유지, AWG 불필요) | `BB_HOST=beagle.local py host/bb.py scripts/deploy.sh` | `## build pru0_pulse_io ##` → make 로그 마지막 5줄 → (빌드 실패 시 `BUILD FAIL`로 즉시 종료) → `VERSION: ...` → `#### firmware built + loaded ####` |
| `demo.sh` | cmd `0x32`(5 ns 폭 자동 스윕: 5,10,...,60 ns)를 P8_11에 발사하는 스코프 데모 | 펌웨어가 이미 로드돼 있어야 함(provision/deploy 직후). 스코프를 **P8_11**(신호) + **P8_1/P8_2**(GND)에 프로브 | `BB_HOST=beagle.local py host/bb.py scripts/demo.sh` | `VERSION: ...` → `FIRING cmd 0x32: 5 ns width auto-sweep on P8_11 (5..60 ns, +5 ns/step). Scope P8_11.` → `#### demo running on P8_11 (stop: reload firmware via deploy.sh) ####` |
| `verify_timebase.sh` | 외부 AWG 스테어케이스로 타임베이스 정확도 검증. AWG `TRIG:DEL` tau를 스윕하며 output→input 간격 M(cmd `0x42`, eCAP 타임스탬프)을 측정해 M vs tau를 피팅 | **AWG 필요.** out0(P8_11) → AWG ext-trig, AWG out → P8_16(0–3.3 V), 공통 GND, AWG가 LAN에 있어야 함(스크립트에 하드코딩된 기본 `AWG_IP=192.168.0.7`) | `BB_HOST=beagle.local py host/bb.py scripts/verify_timebase.sh` | `run_awg_bbb.py`가 tau별 측정 결과와 최종 피팅(slope/절편)을 출력한 뒤 `#### DONE ####`로 종료. PASS 기준은 slope ≈ 1.000(실측값은 [README.md](../README.md), [VERIFICATION.md](VERIFICATION.md) 참고) |
| `verify_window.sh` | 외부 AWG 스트로브로 5 ns 출력↔입력 윈도우 제어를 검증. cmd `0x43`으로 PRU가 `out + D` 사이클에 입력 샘플을 위치시키고, D를 스윕해 0→1 전이점 D\*를 찾아 AWG tau와 피팅 | **AWG 필요.** `verify_timebase.sh`와 동일 배선 | `BB_HOST=beagle.local py host/bb.py scripts/verify_window.sh` | `AWG tau(ns) / transition D*(ticks) / window pos(ns)` 표 → `WINDOW vs AWG tau: slope=... intercept=... resid=...` → `PRU-controlled output<->input window tracks the crystal-traceable AWG delay 1:1, in 5 ns eCAP ticks. EXTERNAL proof of 5 ns window control (not self-referential).` → `#### DONE ####` |
| `reboot.sh` | 보드를 재부팅(U-Boot 핀먹스 오버레이를 새로 적용해야 할 때 필요) | 없음 | `BB_HOST=beagle.local py host/bb.py scripts/reboot.sh` | `arming reboot now` → `reboot issued` (직후 SSH 세션이 끊긴다 — 정상) |

> ⚠️ `demo.sh`가 발사하는 스윕은 **펌웨어를 다시 로드할 때까지 PRU에서 계속
> 반복**한다. 멈추려면 `scripts/deploy.sh`로 펌웨어를 재로드해야 한다.

`verify_timebase.sh`는 `AWG_IP=192.168.0.7 TAU_STOP=1000 TAU_STEP=4
N_SAMP=40 REARM_S=0.005`를 스크립트 실행 줄에 직접 박아 넣는다. `bb.py`는
`BB_PW`만 원격 세션에 export하므로, 이 값들을 바꾸려면 로컬의
`scripts/verify_timebase.sh` 파일 자체를 수정해야 한다. `verify_window.sh`
쪽은 파이썬 코드 안에서 `os.environ.get('AWG_IP', '192.168.0.7')`로 읽지만,
`bb.py`가 원격 셸에 `AWG_IP`를 넘겨주지 않으므로 결국 기본값 `192.168.0.7`이
쓰인다 — 이쪽도 IP를 바꾸려면 스크립트 파일을 직접 고치는 편이 확실하다.

---

## 3. 일상 워크플로우

### 3.1 펌웨어 수정 → 재배포 루프

1. 로컬에서 `firmware/main.c`를 수정한다.
2. 보드로 업로드한다.
   ```bash
   py host/bb.py --put firmware/main.c ~/pru/pru0_pulse_io/main.c
   ```
3. 빌드 + remoteproc 재로드.
   ```bash
   BB_HOST=beagle.local py host/bb.py scripts/deploy.sh
   ```
4. 출력 마지막 줄이 `#### firmware built + loaded ####`이고 그 위의
   `VERSION:` 줄이 정상 문자열이면 반영 완료. `BUILD FAIL`이 나오면 make
   로그(출력 마지막 5줄)를 보고 `firmware/main.c`를 고친다.
5. 필요하면 `scripts/demo.sh`로 스코프 확인, 또는 아래 3.2절의 AWG 재검증을
   돌린다.

### 3.2 AWG로 재검증하는 절차

1. 배선 확인: out0(**P8_11**) → AWG ext-trig, AWG out → **P8_16**(0–3.3 V),
   공통 GND.
2. AWG가 LAN에 있고 스크립트가 기대하는 IP(기본 `192.168.0.7`)와 실제 AWG
   IP가 다르면, 2절 마지막 문단대로 로컬 `scripts/verify_timebase.sh` /
   `scripts/verify_window.sh`의 `AWG_IP` 값을 먼저 고친다.
3. 타임베이스 정확도 검증.
   ```bash
   BB_HOST=beagle.local py host/bb.py scripts/verify_timebase.sh
   ```
4. 윈도우 제어 정확도 검증.
   ```bash
   BB_HOST=beagle.local py host/bb.py scripts/verify_window.sh
   ```
5. 두 스크립트 모두 최종 피팅에서 slope ≈ 1.000이 나오면 PASS. 수치 기준과
   과거 실측 결과는 [VERIFICATION.md](VERIFICATION.md) 참고.

### 3.3 보드가 리부트된 뒤 재접속하는 절차

1. `scripts/reboot.sh` 실행 직후(또는 정전·수동 리부트 이후), 보드는 DHCP로
   새 IP를 받을 수 있다 — 이전에 쓰던 `BB_HOST` IP가 더 이상 유효하지 않을
   수 있다.
2. `BB_HOST=beagle.local`로 재접속을 시도한다(mDNS는 DHCP 변경에도 유지된다,
   4절 참고).
3. `provision.py`는 멱등하므로, 재부팅 후 상태 확인 겸 다시 돌려도 안전하다.
   ```bash
   BB_HOST=beagle.local python host/provision.py
   ```
   마지막 줄이 `PROVISION OK`면 정상 복귀한 것이다.

---

## 4. 보드 IP가 바뀌었을 때 찾는 법

보드는 DHCP로 IP를 받으므로 재부팅할 때마다 IP가 달라질 수 있다. 저장소
스크립트들은 전부 `BB_HOST=beagle.local`이라는 mDNS 호스트이름을 기본
권장값으로 쓴다 — DHCP가 바뀌어도 이 이름은 그대로 유지된다.

PowerShell에서 실전 확인 순서:

```powershell
# 1) mDNS 이름으로 먼저 시도 (대부분의 경우 이걸로 충분)
ping beagle.local

# 2) 응답이 없다면 (일부 네트워크/VPN이 멀티캐스트 DNS를 막는 경우) 서브넷을
#    스캔해서 살아있는 호스트를 찾는다
1..254 | ForEach-Object {
    $ip = "192.168.0.$_"
    if (Test-Connection -ComputerName $ip -Count 1 -Quiet) { $ip }
}

# 3) 후보 IP들 중 SSH(22번 포트)가 열려 있는 것만 추린다
Test-NetConnection -ComputerName 192.168.0.42 -Port 22

# 4) MAC 주소로 교차 확인하고 싶다면, 먼저 한 번 접속했을 때 보드에서 자신의
#    MAC을 확인해 기록해 둔다
#      ssh xilinx@<그때의IP> "cat /sys/class/net/eth0/address"
#    그 값을 ARP 캐시와 대조한다
arp -a
```

찾은 IP는 `BB_HOST=<IP>` 형태로 바로 써도 되고(`python host/provision.py
<ip>`처럼 위치인자로 넘겨도 된다), 매번 스캔하기 번거로우면 결국
`beagle.local`이 가장 손이 덜 가는 방법이다.

---

## 5. RPMsg 명령을 스크립트 없이 즉석에서 보내보기

`scripts/*.sh`를 새로 만들지 않고, RPMsg 장치노드를 직접 열어 1바이트 명령을
보내고 응답을 받아보는 최소 흐름은 다음과 같다. 이 코드는 **보드 위에서**
실행되어야 한다(`/dev/rpmsg0`는 보드의 로컬 장치노드). 노드 권한이 아직
666으로 풀리지 않았다면 먼저 `chmod 666 /dev/rpmsg0`(또는 sudo)이 필요하다 —
`deploy.sh`/`demo.sh`는 이미 이 chmod를 포함하고 있다.

```python
import os, select

fd = os.open('/dev/rpmsg0', os.O_RDWR)

# 1바이트 명령 전송 - 0x03 = VERSION. 각 명령 바이트의 정확한 의미와
# 페이로드 형식은 FIRMWARE.md 참고
os.write(fd, bytes([0x03]))

# 최대 3초 대기 후 응답 읽기
r, _, _ = select.select([fd], [], [], 3)
reply = os.read(fd, 512) if r else b''
print(reply)

os.close(fd)
```

이 조각을 보드에서 바로 실행하려면 `python3 -` 뒤에 여기 붙여넣거나, 임시로
`host/bb.py`의 표준입력을 통해 흘려보낸다(`bb.py`는 stdin으로 받은 스크립트를
그대로 원격 `bash -l`에 넣어 실행한다).

---

## 다음으로 읽을 문서

- **[HOST_TOOLS.md](HOST_TOOLS.md)** — `host/bb.py`, `host/provision.py`
  사용법과 접속/빌드 실패 시 원인 파악.
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — 그 밖의 일반적인 문제 해결.
- **[FIRMWARE.md](FIRMWARE.md)** — RPMsg 명령 바이트 프로토콜의 상세 의미.
