# 호스트 도구 (host/)

`host/` 디렉터리는 **PC(호스트)에서 실행되는** 파이썬 도구 모음이다. BeagleBone Black(BBB) 보드에는
파이썬 SDK나 GUI 없이 SSH/SFTP로만 접근하므로, 이 디렉터리의 두 파일이 사실상 보드와의 유일한
인터페이스다.

| 파일 | 역할 |
|---|---|
| `bb.py` | 재사용 가능한 SSH 러너 — 원격 명령 실행, 스크립트 전송, sftp 업/다운로드 |
| `provision.py` | 신규 보드 1회성 완전 자동 프로비저닝 (8단계) |
| `awg.py`, `run_awg_bbb.py` | `provision.py`가 보드로 함께 푸시하는 AWG 검증용 도구 (본 문서 범위 밖) |

두 파일 모두 `paramiko`에 의존하며, 접속 정보는 동일한 3개 환경변수(`BB_HOST`/`BB_USER`/`BB_PW`)로
제어된다.

---

## bb.py — 재사용 가능 SSH 러너

### 설계 목적

`bb.py`는 매 호출마다 새로 SSH 연결을 맺고, 명령(또는 스크립트)을 원격에서 실행한 뒤 stdout/stderr를
그대로 되돌려주는 **얇은 SSH 실행기**다. 상태를 유지하지 않으므로(연결마다 `connect()`를 새로 호출)
`scripts/*.sh`를 하나씩 실행하는 일상적인 작업 흐름(README의 "Day-to-day" 절)의 기반이 된다.

### 접속 정보 (환경변수)

| 환경변수 | 기본값 (코드 그대로) |
|---|---|
| `BB_HOST` | `192.168.0.4` |
| `BB_USER` | `xilinx` |
| `BB_PW` | `xilinx` |

```python
HOST = os.environ.get("BB_HOST", "192.168.0.4")
USER = os.environ.get("BB_USER", "xilinx")
PW   = os.environ.get("BB_PW",   "xilinx")
```

### 호출 형태

`bb.py`는 `sys.argv[1:]`를 다음 순서로 해석한다(먼저 매치되는 조건이 우선).

```python
a = sys.argv[1:]
if a and a[0] == "--put":
    sftp_put(a[1], a[2]); sys.exit(0)
if a and a[0] == "--get":
    sftp_get(a[1], a[2]); sys.exit(0)
if not a:
    sys.exit(run(sys.stdin.read()))
if os.path.exists(a[0]):
    sys.exit(run(open(a[0], "r", encoding="utf-8").read()))
sys.exit(run(" ".join(a)))
```

1. **명령 문자열**: `py bb.py "remote one-liner"` — 인자를 공백으로 이어붙여 그대로 원격에서 실행.
2. **스크립트 파일 경로**: `py bb.py path\to\script.sh` — 첫 인자가 **로컬**에 실제로 존재하는 파일이면
   그 파일 내용을 읽어 원격 `bash -l`에 흘려보낸다. `run()`이 `\r\n`을 `\n`으로 치환하므로(CRLF-safe),
   Windows에서 작성한 스크립트를 그대로 넘겨도 안전하다.
3. **stdin으로 스크립트 전달**: `echo "cmd" | py bb.py` — 인자가 하나도 없으면 표준입력 전체를 스크립트로
   읽어 실행한다.
4. **`--put` / `--get`으로 sftp 전송**: `py bb.py --put LOCAL REMOTE`, `py bb.py --get REMOTE LOCAL`.

### run()의 동작과 원격 sudo 메커니즘

```python
def run(script, timeout=180):
    script = script.replace("\r\n", "\n")
    c = connect()
    full = "export BB_PW=%s\n%s" % (shlex.quote(PW), script)
    stdin, out, err = c.exec_command("bash -l", get_pty=False, timeout=timeout)
    stdin.write(full + "\nexit\n")
    stdin.flush()
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    c.close()
    ...
```

- `bash -l`(로그인 셸)을 `get_pty=False`로 실행한다. **PTY(가상 터미널)가 없으므로** `sudo`가 기본으로
  쓰는 터미널 비밀번호 프롬프트를 사용할 수 없다.
- 그래서 `run()`은 스크립트 본문 앞에 `export BB_PW=<비밀번호>` 한 줄을 붙여 **원격 bash 세션의 환경변수로
  비밀번호를 먼저 심어둔다.** 비밀번호는 `shlex.quote()`로 안전하게 이스케이프된다.
- 이 덕분에 원격에서 실행되는 스크립트는 언제든 `echo "$BB_PW" | sudo -S <cmd>` 형태로 비밀번호 없는
  대화형 프롬프트 없이 `sudo`를 호출할 수 있다. 이 패턴은 `bb.py`의 모듈 docstring에도 그대로 명시돼
  있다: `Remote sudo: scripts can use: echo "$BB_PW" | sudo -S <cmd> (PW exported on remote)`.
- 실행 후 stdout/stderr를 각각 끝까지 읽고(`out.read()`, `err.read()`), `out.channel.recv_exit_status()`로
  종료 코드를 받아온 뒤 연결을 닫는다. stderr에 내용이 있으면 `--- stderr ---` 구분선과 함께 별도 출력한다.

### sftp_put / sftp_get

```python
def sftp_put(local, remote):
    c = connect(); s = c.open_sftp(); s.put(local, remote); s.close(); c.close()

def sftp_get(remote, local):
    c = connect(); s = c.open_sftp(); s.get(remote, local); s.close(); c.close()
```

각각 별도의 SSH 연결을 새로 맺고 `paramiko`의 SFTP 클라이언트로 파일 하나를 올리거나 받은 뒤 연결을
정리한다. 여러 파일/디렉터리를 재귀적으로 옮기는 로직은 `bb.py`에는 없다(그 역할은 `provision.py`의
`put_tree()`가 담당한다).

> ⚠️ `connect()`는 `paramiko.AutoAddPolicy()`를 사용한다. 즉 알려지지 않은 호스트 키를 확인 없이 자동으로
> 신뢰하고 접속한다 — 랩 내부망 전용 도구이므로 편의를 택한 것이지만, 호스트 키 검증이 없다는 점은
> 명확히 알고 써야 한다.

> ⚠️ `BB_PW`는 원격 셸의 환경변수로 평문 그대로 export되고, 스크립트 안에서 `echo "$BB_PW"`로 다시
> 출력된다. 같은 보드에 다른 사용자가 로그인해 있다면 `ps`로 프로세스 인자나 환경을 들여다볼 때 노출될
> 수 있는 값이므로, 랩 내부망 이외의 환경에서는 이 방식을 그대로 재사용하지 않는 것이 좋다.

---

## sudo/파이프 관련 두 가지 함정

`bb.py`와 `provision.py`는 모두 `echo "$BB_PW" | sudo -S <cmd>` 패턴으로 비밀번호 없는 `sudo`를
구현한다. 이 패턴을 응용할 때 실제로 부딪히는 두 가지 함정이 있다.

### 함정 1 — 비밀번호 파이프와 데이터 파이프를 같은 스트림에 섞으면 안 된다

`sudo -S`는 표준입력에서 비밀번호 한 줄을 읽어들인다. 그런데 그 비밀번호를 넘기는 파이프를, `sudo`로
실행되는 명령이 실제로 처리해야 할 데이터를 전달하는 용도로 **동시에** 쓰려고 하면 문제가 생긴다.
예를 들어 파일 내용을 원격에서 특권으로 써넣으려고 비밀번호와 데이터를 한 파이프에 함께 흘려보내
`sudo -S`로 실행되는 `tee` 같은 명령에 전달하면, `sudo -S`가 표준입력으로 들어오는 내용을 전부
비밀번호로 소비해버려서 뒤쪽 명령에는 실제 데이터가 하나도 도달하지 않는다.

올바른 패턴은 **비밀번호만** 파이프로 `sudo -S`에 주고, 그 안에서 실행되는 셸이 데이터 자체를
처리하게 하는 것이다. `provision.py`의 모든 `sudo` 호출이 실제로 이 원칙을 지키고 있다.

- 데이터를 아예 표준입력으로 넘기지 않는 명령만 `sudo`로 실행한다: `cp`, `chmod`, `apt-get`,
  `systemctl reboot` 등.
- 파일에 텍스트 한 줄을 추가해야 할 때는 데이터를 파이프가 아니라 **명령줄 인자**로 넘긴다.

```
echo "$BB_PW" | sudo -S sh -c "echo uboot_overlay_pru=BB-PRUOUT-P8-00A0.dtbo >> /boot/uEnv.txt"
```

이렇게 하면 파이프에는 비밀번호 한 줄만 흐르고, 실제로 파일에 쓰일 내용은 `sh -c`에 넘겨진 명령
문자열 안에 이미 들어 있어 별도의 데이터 스트림이 필요 없다. 큰 파일(빌드된 오버레이 `.dtbo`, PRU
펌웨어 `.out`)을 옮길 때도 마찬가지로, `provision.py`는 이 파일들을 sftp로 올리는 대신 **비특권
사용자**(`xilinx`)의 홈 디렉터리 안에서 직접 빌드해둔 뒤(`.dtbo`는 5/8 단계의 `dtc`, `.out`은 4/8
단계의 `make`), `sudo -S cp <홈 디렉터리 경로> /lib/firmware/...` 로 마지막 복사만 특권으로
수행한다(아래 5/8, 7/8 단계). `cp`는 표준입력 데이터가 필요 없으므로 비밀번호 파이프와 절대
충돌하지 않는다.

### 함정 2 — sudo 하위 셸에서 물결표(~)는 root의 홈으로 확장된다

`provision.py`의 5/8 단계 주석이 이 문제를 정확히 짚는다.

```python
# $HOME is expanded by the (xilinx) login shell BEFORE sudo; a bare ~ inside
# `sudo sh -c` would expand to /root and the copy would silently fail.
run(c, 'echo "$BB_PW" | sudo -S cp "$HOME/pru/overlay/%s" /lib/firmware/%s' % (DTBO, DTBO))
```

`~`는 셸이 **명령을 파싱하는 시점**에 그 명령을 실행하는 사용자의 홈으로 치환된다. `sudo -S cp ~/... `
처럼 물결표를 `sudo`에 넘어가는 인자 안에 그대로 쓰면, 치환이 `sudo`로 새로 시작되는 셸(즉 `root`)
기준으로 일어나 `/root/...`가 되어버리고, 로그인 사용자(`xilinx`)의 홈에 있는 실제 파일은 찾지 못한
채 조용히 실패한다(오류 메시지 없이 원하는 파일을 못 찾을 뿐이므로 알아채기 어렵다).

해결책은 물결표 대신 **로그인 셸에서 `sudo` 호출 전에 이미 확장돼 있는 `$HOME` 환경변수**를 쓰는
것이다. `bash -l`이 `sudo` 명령을 실행하기 전에 이미 `$HOME`을 `xilinx`의 홈으로 채워 넣은 상태이므로,
그 문자열이 `sudo`가 실행하는 `cp`의 인자로 그대로 전달된다. `provision.py`는 오버레이 `.dtbo`
복사(5/8)와 PRU 펌웨어 `.out` 복사(7/8) 두 곳 모두에서 이 방식을 쓴다.

```
sudo -S cp "$HOME/pru/pru0_pulse_io/gen/pru0_pulse_io.out" /lib/firmware/am335x-pru0-pulseio.out
```

(다른 해결책으로 `cd`로 원하는 디렉터리에 먼저 이동한 뒤 상대경로를 쓰는 방법도 있다 — 실제로 5/8
단계의 `dtc` 빌드 명령은 `sudo` 없이 `cd ~/pru/overlay && dtc ...` 형태로 물결표를 쓰는데, 이때는
`sudo` 하위 셸이 아니라 로그인 셸 자신이 명령을 실행하므로 `~`가 정상적으로 `xilinx`의 홈으로
확장된다.)

---

## provision.py — 8단계 프로비저닝 흐름

`provision.py`는 신규(또는 초기화된) BeagleBone Black 한 대를 완전히 자동으로 서비스 가능한 상태로
만든다. 실행은 저장소 루트에서:

```bash
python host/provision.py                # BB_HOST 환경변수 또는 기본값 beagle.local 사용
python host/provision.py 192.168.0.42   # 대상 IP를 인자로 직접 지정
```

호스트는 인자 → `BB_HOST` 환경변수 → 기본값 `beagle.local` 순으로 결정되고, 계정 정보는 `BB_USER`
(기본 `xilinx`), `BB_PW`(기본 `xilinx`)로 제어된다. 각 단계는 `step()`으로 표시되고, 중단 조건을
만족하면 `die()`가 메시지를 출력하고 `sys.exit(1)`로 즉시 종료한다.

### 왜 대상 검증이 언제나 1단계인가

`provision.py`의 첫 동작은 파일을 옮기거나 무언가를 빌드하기 전에, 접속한 대상이 진짜
**AM335x BeagleBone Black**인지부터 확인하는 것이다. 이는 실제로 겪을 수 있는 사고를 막기 위한
안전장치다: 같은 랩 네트워크에는 `xilinx`/`xilinx`라는 **동일한 로그인 계정**을 쓰는 다른 보드도
함께 있다. `BB_HOST`를 잘못 지정하면 SSH 인증 자체는 그대로 성공해버리므로, 모델 문자열을 먼저
확인하지 않으면 엉뚱한 보드에 펌웨어를 밀어넣고 U-Boot 설정까지 바꿔버리는 사고로 이어질 수 있다.
`provision.py`의 모듈 docstring도 "important because the lab has other xilinx/xilinx boards on the
same subnet"이라고 이 이유를 명시하고 있다.

### 1/8 — 연결 + 대상이 BeagleBone Black인지 검증

```python
rc, model, _ = run(c, "cat /proc/device-tree/model 2>/dev/null | tr -d '\\000'")
if "AM335x BeagleBone Black" not in model:
    die(...)
```

디바이스트리의 `model` 노드를 읽어 `"AM335x BeagleBone Black"` 문자열이 포함돼 있는지만 확인한다.
포함돼 있지 않으면 그 즉시 `die()`로 중단 — 이 시점에는 아직 아무 파일도 옮기지 않았고 아무 설정도
바꾸지 않았으므로 몇 번을 실행해도 안전한(멱등적인) 읽기 전용 단계다.

### 2/8 — PRU 툴체인(clpru) 확인

```python
probe = ("command -v clpru 2>/dev/null || "
         "{ test -x /usr/share/ti/cgt-pru/bin/clpru && echo /usr/share/ti/cgt-pru/bin/clpru; } "
         "|| echo MISSING")
```

`clpru`가 `PATH`(`command -v clpru`)에 있거나, TI CGT 설치 경로(`/usr/share/ti/cgt-pru/bin/clpru`)에
실행 가능한 형태로 있으면 통과한다. 둘 다 없으면 `MISSING`으로 판정하고, `apt-get update` +
`apt-get -y install ti-pru-cgt-v2.3`을 `sudo -S`로 시도한 뒤 다시 검사한다. 그래도 여전히
`MISSING`이면 `die()`로 중단(수동 설치를 안내). 이미 `clpru`가 있으면 설치 시도 자체를 건너뛰므로
재실행해도 안전하다.

### 3/8 — 펌웨어 + 오버레이 + host 도구 sftp 푸시

```python
run(c, "mkdir -p ~/pru/pru0_pulse_io ~/pru/overlay ~/pru/timing_verify")
sftp = c.open_sftp()
home = sftp.normalize(".")
put_tree(sftp, os.path.join(REPO, "firmware"), home + "/pru/pru0_pulse_io")
put_tree(sftp, os.path.join(REPO, "overlay", "BB-PRUOUT-P8-00A0.dts"),
         home + "/pru/overlay/BB-PRUOUT-P8-00A0.dts")
for f in ("awg.py", "run_awg_bbb.py"):
    sftp.put(os.path.join(REPO, "host", f), home + "/pru/timing_verify/" + f)
```

`mkdir -p`로 대상 디렉터리 3개(`~/pru/pru0_pulse_io`, `~/pru/overlay`, `~/pru/timing_verify`)를
준비한 뒤, `put_tree()`(로컬이 디렉터리면 재귀적으로 `sftp.mkdir` + 하위 항목을 순회, 파일이면
`sftp.put`)로 저장소의 `firmware/`(벤더링된 `pru-support/` 포함) 전체와 오버레이 소스
`BB-PRUOUT-P8-00A0.dts`를 옮기고, `host/awg.py`·`host/run_awg_bbb.py`는 개별 `sftp.put`으로
`~/pru/timing_verify/`에 놓는다. 이 단계 자체에는 전송 결과를 검증하는 `die()` 조건이 없다 —
빌드가 실패하면 다음 단계(4/8)에서 걸러진다. `sftp.mkdir`이 이미 존재하는 디렉터리에 대해 낸
`IOError`는 `put_tree()`가 무시하고, 파일 전송은 항상 덮어쓰기이므로 재실행해도 안전하다.

### 4/8 — PRU 펌웨어 오프라인 빌드

```python
rc, out, err = run(c, "cd ~/pru/pru0_pulse_io && make clean >/dev/null 2>&1; "
                      "PRU_CGT=/usr/share/ti/cgt-pru make 2>&1 | tail -4; "
                      "ls gen/pru0_pulse_io.out")
if "pru0_pulse_io.out" not in out:
    die("firmware build failed:\n" + err)
```

`make clean`으로 이전 빌드 산출물을 지운 뒤, `PRU_CGT` 환경변수를 TI CGT 설치 경로로 명시적으로
고정해 Makefile이 `PATH`와 무관하게 항상 같은 컴파일러를 쓰도록 하고 다시 빌드한다(`firmware/`
아래 벤더링된 `pru-support/` 덕분에 인터넷 연결 없이 오프라인으로 빌드된다). 빌드 산출물
`gen/pru0_pulse_io.out`이 `ls` 출력에 나타나지 않으면 캡처해둔 stderr와 함께 `die()`로 중단.
`make clean`을 매번 실행하므로 재실행해도 항상 처음부터 다시 빌드되어 안전하다.

### 5/8 — 핀먹스 오버레이(.dtbo) 빌드 + 설치 + 검증

```python
rc, dtc_out, _ = run(c, "command -v dtc || echo MISSING")
if "MISSING" in dtc_out:
    die("dtc not found - run: sudo apt-get install device-tree-compiler")
run(c, "cd ~/pru/overlay && dtc -I dts -O dtb -@ -o %s BB-PRUOUT-P8-00A0.dts 2>&1" % DTBO)
run(c, 'echo "$BB_PW" | sudo -S cp "$HOME/pru/overlay/%s" /lib/firmware/%s' % (DTBO, DTBO))
rc, out, _ = run(c, "ls -l /lib/firmware/%s 2>/dev/null || echo MISSING" % DTBO)
if "MISSING" in out:
    die("overlay dtbo not installed to /lib/firmware (dtc build or cp failed).")
```

`dtc`(device-tree-compiler)가 없으면 `clpru`와 달리 자동 설치를 시도하지 않고 바로 `die()`로
중단해 수동 설치를 안내한다. `dtc`가 있으면 `~/pru/overlay`에서 `.dts`를 `.dtbo`로 컴파일하고,
"함정 2"에서 설명한 `$HOME` 기반 경로로 `/lib/firmware/`에 `sudo` 복사한다. 복사 후
`/lib/firmware/<DTBO>`가 실제로 존재하는지 `ls -l`로 재확인하고, 없으면 `die()`. `dtc` 빌드와
복사를 매번 새로 수행하고 결과가 항상 동일한 파일로 덮어써지므로 재실행해도 안전하다.

### 6/8 — /boot/uEnv.txt에 U-Boot 오버레이 설정 (멱등적으로 반영)

```python
rc, cur, _ = run(c, "grep -c '^uboot_overlay_pru=%s$' /boot/uEnv.txt 2>/dev/null || true" % DTBO)
rc2, dupe, _ = run(c, "grep -c '^uboot_overlay_pru=' /boot/uEnv.txt 2>/dev/null || true")
needs_reboot = not (cur.strip() == "1" and dupe.strip() == "1")
```

먼저 두 가지를 센다 — ① 우리가 원하는 정확한 값(`uboot_overlay_pru=BB-PRUOUT-P8-00A0.dtbo`)의
**활성** 줄이 정확히 1개인지, ② `uboot_overlay_pru=`로 시작하는 활성 줄이 전체에서 정확히 1개뿐인지.
둘 다 참이면(즉 이미 원하는 값 하나만 정확히 설정돼 있으면) `needs_reboot`는 `False`가 되고, 아래
수정 로직 전체를 건너뛴 채 "overlay already configured"만 출력한다. 이것이 이 단계의 멱등성을
만드는 핵심 조건이다.

`needs_reboot`가 `True`인 경우에만:

1. `/boot/uEnv.txt`를 `/boot/uEnv.txt.bak-pulseio`로 백업(`sudo cp`).
2. `sed -i "/^#*uboot_overlay_pru=/d"`로 **주석 처리된 것까지 포함해** 기존 `uboot_overlay_pru=` 줄을
   모두 삭제 — 예시로 남아있는 `#uboot_overlay_pru=...` 줄이 몇 개든 상관없이 깨끗하게 지운다.
3. `sudo sh -c "echo uboot_overlay_pru=<DTBO> >> /boot/uEnv.txt"`로 원하는 활성 줄 하나만 새로 추가.
4. `enable_uboot_overlays=1`이 이미 있는지 `grep -q`로 확인하고, 없을 때만 추가 — 이 하위 확인은
   `needs_reboot` 분기 안에서도 별도로 멱등적으로 처리된다.

이 단계 자체에는 `die()` 조건이 없다(적용 여부는 7/8·8/8에서 리부트 후 실질적으로 확인된다). 전체
로직이 "정확히 원하는 상태인지"부터 확인하고 필요한 만큼만 고치므로, 이미 설정된 보드에 재실행해도
파일을 건드리지 않는다.

### 7/8 — 펌웨어 배포 + remoteproc로 로드 (+ 필요 시 리부트 대기)

```python
run(c, 'echo "$BB_PW" | sudo -S cp '
       '"$HOME/pru/pru0_pulse_io/gen/pru0_pulse_io.out" /lib/firmware/%s' % FW_NAME)
rc, chk, _ = run(c, "test -f /lib/firmware/%s && echo OK || echo MISSING" % FW_NAME)
if "OK" not in chk:
    die("firmware .out not installed to /lib/firmware (cp failed).")
```

빌드된 `.out`을 (5/8과 동일하게 `$HOME` 경로로) `/lib/firmware/am335x-pru0-pulseio.out`에 복사하고,
파일이 실제로 존재하는지 `test -f`로 확인 — 실패 시 `die()`.

`needs_reboot`가 `True`(6/8에서 오버레이 설정을 바꾼 경우)일 때만 리부트 절차가 들어간다:

- `sudo -S systemctl reboot`를 짧은 타임아웃(15초)으로 실행 — 리부트가 SSH 채널 자체를 끊어버리는
  것이 정상이므로 예외를 그냥 흡수한다.
- `wait_ssh(HOST, up=False, deadline_s=60)`로 TCP 22번 포트가 실제로 닫히는지 최대 60초 폴링. 이
  단계는 실패해도(60초 안에 안 내려가도) 경고만 출력하고 계속 진행한다.
- `wait_ssh(HOST, up=True, deadline_s=180)`로 포트가 다시 열릴 때까지 최대 180초 폴링. 여기서
  실패하면 `die()` — "board did not come back after reboot"로 중단하고 재연결·재실행을 안내한다.
- 포트가 열려도 `sshd`가 즉시 인증 가능한 상태는 아닐 수 있으므로(콜드 부팅 직후), 4초 간격으로 최대
  6회 `connect()`를 재시도한다. 6회 모두 실패하면 `die()`.

리부트 여부와 무관하게 항상 실행되는 remoteproc 재로드 시퀀스(`RP = /sys/class/remoteproc/remoteproc1`):

```
echo stop > $RP/state        (2>/dev/null로 이미 정지 상태인 경우의 오류 억제)
sleep 0.3
echo -n <FW_NAME> > $RP/firmware   (-n: 개행 없이 정확한 파일명만 기록)
echo start > $RP/state
(최대 25회 * 0.2초 = 5초 동안 /dev/rpmsg0 노드 생성 대기)
sudo chmod 666 /dev/rpmsg0   (이후 비-root로도 캐릭터 디바이스를 열 수 있도록)
```

이 시퀀스 직후에는 별도의 `die()` 검사가 없다 — 실제 동작 확인은 8/8에서 이뤄진다. `stop`→`firmware`
교체→`start` 순서 자체가 이미 실행 중인 상태에서도 안전하게 반복 가능하도록 설계돼 있고, 리부트
분기는 `needs_reboot`가 참일 때만 타므로 재실행해도 안전하다.

### 8/8 — 핀먹스 + 펌웨어 최종 검증

```python
rc, pins, _ = run(c, "echo \"$BB_PW\" | sudo -S grep -E 'pin 12 |pin 13 |pin 14 ' "
                     "/sys/kernel/debug/pinctrl/44e10800.pinmux-pinctrl-single/pins 2>/dev/null")
```

디버그 인터페이스(`pinctrl-single/pins`)에서 `pin 12 |pin 13 |pin 14 ` 문자열과 일치하는 핀먹스
항목을 `grep`으로 조회해 출력한다. 이 "pin N" 표기는 `pinctrl-single` 디버그 인터페이스 자체가
쓰는 내부 순번 표기일 뿐이며, 이 번호가 BeagleBone의 물리적 P8 커넥터 핀 번호와 대응한다는 근거는
소스 어디에도 없다. 비어 있으면 "(pins not claimed - overlay may need a reboot)"라는 안내만 남기고,
이 자체는 `die()` 조건이 아니라 참고 정보다.

```python
rc, ver, _ = run(c, "python3 - <<'PY'\n"
                    "import os,select\n"
                    "fd=os.open('/dev/rpmsg0',os.O_RDWR)\n"
                    "os.write(fd,bytes([0x03]))\n"
                    "r,_,_=select.select([fd],[],[],3)\n"
                    "print((os.read(fd,512) if r else b'')[1:].decode('ascii','replace'))\n"
                    "os.close(fd)\nPY")
```

원격에서 짧은 `python3` 스크립트를 즉석 실행해 `/dev/rpmsg0`를 열고 `VERSION` 명령 바이트
(`0x03`)를 한 바이트 쓴 뒤, `select()`로 최대 3초 응답을 기다려 최대 512바이트를 읽는다(응답의
첫 바이트는 잘라내고 나머지를 ASCII로 디코딩). 최종적으로 응답 문자열이 `bbb_pulse_io`로 시작하면
`"PROVISION OK"`를 출력하고 다음에 쓸 명령(`BB_HOST=<host> py host/bb.py scripts/demo.sh`)을
안내한다. 그렇지 않으면(응답이 없거나 다른 문자열이면) `die("firmware did not respond to
PING/VERSION - check dmesg / remoteproc state.")`로 중단한다. 이 단계는 상태를 바꾸지 않는 조회이므로
몇 번을 반복해도 안전하다.

### 단계별 요약

| 단계 | 내용 | 중단(die) 조건 | 재실행 안전성 |
|---|---|---|---|
| 1/8 | 디바이스트리 model 문자열 확인 | `"AM335x BeagleBone Black"` 미포함 | 안전 (읽기 전용) |
| 2/8 | `clpru` 존재 확인, 없으면 apt 설치 시도 | 설치 후에도 여전히 없음 | 안전 (이미 있으면 설치 생략) |
| 3/8 | firmware/overlay/host 도구 sftp 푸시 | (없음, 4/8에서 간접 검증) | 안전 (항상 덮어쓰기) |
| 4/8 | `make clean` 후 오프라인 빌드 | `gen/pru0_pulse_io.out` 미생성 | 안전 (매번 완전 재빌드) |
| 5/8 | `dtc`로 `.dtbo` 빌드 후 `/lib/firmware`에 설치 | `dtc` 없음 / 설치 후 파일 미확인 | 안전 (매번 재빌드+덮어쓰기) |
| 6/8 | `uEnv.txt`에 오버레이 설정 반영 | (없음) | 안전 (이미 정확히 설정돼 있으면 무수정) |
| 7/8 | 펌웨어 설치 + 필요 시 리부트 + remoteproc 로드 | `.out` 설치 실패 / 리부트 후 SSH 미복귀 | 안전 (리부트는 `needs_reboot`일 때만) |
| 8/8 | 핀먹스 조회 + PING/VERSION 확인 | 펌웨어가 `bbb_pulse_io`로 응답 안 함 | 안전 (조회만 수행) |

---

## 참고

- RPMsg 1바이트 명령 프로토콜(`0x01` PING, `0x03` VERSION 등)의 세부 내용은 [FIRMWARE.md](FIRMWARE.md) 참고.
- 실측 타이밍 결과와 한계는 [VERIFICATION.md](VERIFICATION.md) 참고.
