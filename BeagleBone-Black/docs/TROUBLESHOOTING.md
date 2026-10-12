# 트러블슈팅 & 용어집

> 대상: BeagleBone Black (AM335x) PRU 정밀 펄스 I/O (`bbb_pulse_io`)
> 이 문서는 두 부분으로 구성된다: **(A)** 실제로 겪었던 문제를 증상→원인→해결 순으로 정리한 트러블슈팅, **(B)** 리포 맥락에서 쓰이는 용어 정의.
> 더 깊은 배경은 [ARCHITECTURE.md](ARCHITECTURE.md) · [GOALS.md](GOALS.md) · [VERIFICATION.md](VERIFICATION.md)와, 각 항목에서 링크하는 개별 문서([OVERLAY.md](OVERLAY.md) · [HOST_TOOLS.md](HOST_TOOLS.md) · [OPERATIONS.md](OPERATIONS.md) · [FIRMWARE.md](FIRMWARE.md)) 참고.

---

## A. 트러블슈팅

### A-1. 사내망 SSL 중간자 인증서로 git clone / HTTPS 요청 실패

- **증상:** PRU support-package `git clone`이나 `apt` 등 HTTPS 요청이 인증서 오류로 실패한다.
- **원인:** 사내망에 SSL 중간자(intercept) 프록시(SOOSAN INT)가 걸려 있어, 실제 서버 인증서가 사내 프록시가 재서명한 인증서로 치환되어 내려온다.
- **해결:** `-k`/`--insecure` 류로 **TLS 검증을 끄는 우회는 하지 않는다.** 대신 실제 TLS 핸드셰이크에서 사내 프록시가 제시하는 루트 인증서를 추출해 호스트 WSL의 시스템 신뢰 저장소(trust store)에 정식으로 설치하는 방식으로 해결했다. 이 corp CA(SOOSAN INT) 설치는 과거 작업 기록에 "설치·구성 완료"로 남아 있으며, support-package를 받기 전에 반드시 선행되어야 하는 단계였다.

> ⚠️ 인증서 문제를 TLS 검증 비활성화로 "해결"하면 사내 프록시가 아닌 실제 중간자 공격도 통과시키게 된다. 반드시 루트 인증서 설치로 해결한다.

자세한 내용은 [HOST_TOOLS.md](HOST_TOOLS.md) 참고.

---

### A-2. 실행 중에는 Control Module 패드 레지스터가 써지지 않음

- **증상:** 러닝 커널 상태에서 pinmux를 바꾸려고 `/dev/mem`으로 AM335x Control Module 패드 레지스터를 직접 열어 써도 반영되지 않는다. `config-pin` 도구도 설치되어 있지 않다.
- **원인:** 커널이 부팅 시점에 pad 레지스터 소유권을 이미 쥐고 있어, 런타임 `/dev/mem` 직접 쓰기는 **silently ignored** 된다 — 에러 없이 그냥 무시된다.
- **해결:** pinmux는 **반드시 부팅 시점 U-Boot 오버레이로만** 바꿀 수 있다. `/boot/uEnv.txt`의 `uboot_overlay_pru=<dtbo>` 로 지정한 오버레이가 부트 타임에 적용되며, 이 경로가 유일하다.

자세한 내용은 [OVERLAY.md](OVERLAY.md) 참고.

---

### A-3. pinctrl-single이 핀당 3-value 포맷을 요구 — 2-value 포맷은 나머지 핀을 깨뜨림

- **증상:** pinmux 오버레이 dts를 핀당 2-value(`<offset value>`) 포맷으로 작성하면 `dtc` 컴파일은 성공하지만, 실제 적용해 보면 두 번째 핀부터는 아예 무시된다.
- **원인:** 이 커널의 `am33xx_pinmux`는 `#pinctrl-cells=<2>`라서 핀 하나당 **3개의 u32** `<offset conf mux>`를 요구한다(적용 패드값 = `conf|mux`). 2-value 구형 포맷을 쓰면 cell 개수가 어긋나면서 값들이 밀리기 때문에, 두 번째 핀부터는 아예 무시된다.
- **해결:** 핀당 `<offset conf mux>` 3-cell 포맷으로 작성한다. 이 리포의 최종값은 다음과 같다.

| 핀 | 값 (`<offset conf mux>`) | 의미 |
|---|---|---|
| P8_11 (out0) | `0x034 0x0e 0x00` | MODE6 \| NO_PULL, OUTPUT |
| P8_12 (out1) | `0x030 0x0e 0x00` | MODE6 \| NO_PULL, OUTPUT |
| P8_16 (in)  | `0x038 0x2e 0x00` | MODE6 \| INPUT_EN \| NO_PULL, INPUT |

자세한 내용은 [OVERLAY.md](OVERLAY.md) 참고.

---

### A-4. sudo 하위 셸에서 `~`가 root 홈으로 확장됨

- **증상:** `sudo sh -c "cp ~/pru/... /lib/firmware/..."` 같은 명령이 파일을 찾지 못하고 실패한다.
- **원인:** 로그인 셸(`xilinx`)에서 물결표(`~`)는 정상적으로 확장되지만, `sudo sh -c "..."`처럼 sudo로 넘어간 **하위 셸 안의 `~`는 그 하위 셸(root)의 홈 디렉터리로 다시 확장**된다. 즉 실제로는 `/root/pru/...`를 찾게 되어 원하는 파일(`xilinx`의 홈 아래)을 못 찾는다.
- **해결:** `$HOME` 절대경로를 그대로 넘기거나, 원하는 디렉터리로 `cd`한 뒤 상대경로를 쓴다. `host/provision.py`가 실제로 쓰는 방식:

```python
run(c, 'echo "$BB_PW" | sudo -S cp "$HOME/pru/overlay/%s" /lib/firmware/%s' % (DTBO, DTBO))
```

여기서 `$HOME`은 sudo 이전, 로그인 셸이 이미 값을 채운 뒤 문자열로 넘어가므로 `/root`로 잘못 확장될 여지가 없다.

자세한 내용은 [HOST_TOOLS.md](HOST_TOOLS.md) 참고.

---

### A-5. 비밀번호 파이프와 데이터 파이프를 함께 쓰면 데이터가 비밀번호로 먹힘

- **증상:** `echo "$BB_PW" | sudo -S <명령>` 형태로 비밀번호를 파이프로 넘기면서, 같은 파이프로 실제 페이로드(데이터)까지 흘려보내려 하면 명령이 실패하거나 엉뚱하게 동작한다.
- **원인:** `sudo -S`는 stdin에서 비밀번호 한 줄을 읽어 인증에 쓴다. 비밀번호와 실제 데이터를 같은 파이프에 함께 태우면 sudo가 다음에 오는 데이터까지 비밀번호 입력의 일부로 소비하거나, 반대로 명령이 필요로 하는 데이터의 앞부분을 잃어버린다.
- **해결:** **비밀번호 파이프와 데이터 파이프를 분리**한다 — 비밀번호는 인증 전용으로 별도로 소비시키고(예: `echo "$BB_PW" | sudo -S -v` 로 먼저 자격만 얻은 뒤), 실제 데이터는 별도의 파일 업로드(sftp)나 별개의 스트림으로 전달한다.

자세한 내용은 [HOST_TOOLS.md](HOST_TOOLS.md) 참고.

---

### A-6. 재부팅 직후 mDNS(`beagle.local`) 이름이 바로 안 잡힐 수 있음

- **증상:** 보드를 재부팅한 직후에는 평소 쓰던 mDNS 호스트이름(`beagle.local`)으로 SSH 접속이 바로 안 될 수 있다.
- **원인:** 재부팅 후 mDNS(`beagle.local`)이 잠깐 안 잡힐 수 있다.
- **해결:** mDNS 호스트이름(`beagle.local`)으로 접속한다 — `host/provision.py`의 `BB_HOST` 기본값이 바로 이것이다. mDNS가 아직 안 잡히는 경우(재부팅 직후 잠깐 그럴 수 있음)에는 IP `192.168.0.4`로 직접 SSH한다.

> ⚠️ IP를 직접 지정해 재사용할 때는 그 IP가 과거 다른 장비(예: Keysight AWG)에 배정돼 있던 IP는 아닌지 재확인한다 — 이 리포의 `192.168.0.4`가 실제로 그런 이력이 있다.

자세한 내용은 [OPERATIONS.md](OPERATIONS.md) 참고.

---

### A-7. PRU0 명령메모리(IMEM) 8 KB 한계로 빌드가 넘칠 수 있음

- **증상:** RPMsg 명령을 계속 추가하면 PRU0 펌웨어 빌드가 명령 메모리 한계를 넘을 수 있다.
- **원인:** PRU0 명령메모리(IMEM)는 `0x2000`(8 KB) 고정 하드 한계이며, tight-poll 언롤 코드를 포함한 펌웨어 전체가 이 안에 들어가야 한다.
- **해결:** 안 쓰는 명령을 정리한다. 실제로 v1.7에서는 개발기 내부 cross-check/진단용 명령 `0x40`(루프백)·`0x41`(자체 staircase)·`0x50`(eCAP 프로브)과, 이제 안 쓰는 `CT_IEP` 초기화 코드/`pru_iep.h`를 통째로 제거해 **IMEM 약 1.9 KB**를 확보했다.

자세한 내용은 [FIRMWARE.md](FIRMWARE.md) 참고.

---

### A-8. 컴파일타임 지연 함수는 인자가 반드시 상수여야 함

- **증상:** `__delay_cycles(k)` 같은 컴파일타임 지연 함수에 런타임에 바뀌는 변수를 인자로 넘겨서 가변 지연을 만들려고 하면 안 된다.
- **원인:** 이 지연 함수는 인자가 **컴파일타임 상수**일 때만 컴파일러가 그 값만큼의 정적 지연 시퀀스를 생성한다. 런타임 변수는 이 전개 방식과 맞지 않는다.
- **해결:** 런타임 가변 지연이 필요하면(예: cmd `0x43`의 윈도우 지연 `D`) 하드웨어 루프 명령을 쓴다. 이 리포는 `pru_delay.asm`에서 PRU **하드웨어 `loop` 명령**으로 zero-overhead·정확히 1 cycle/count의 가변 지연을 구현해, `__delay_cycles`가 감당 못 하는 런타임 가변 지연 문제를 해결했다.

자세한 내용은 [FIRMWARE.md](FIRMWARE.md) 참고.

---

### A-9. 두 타임스탬프 카운터의 속도차 — 빠른 타임스탬프에는 eCAP를 쓸 것

- **증상:** 같은 200 MHz 카운터인데도 어떤 걸 읽느냐에 따라 타임스탬프 정밀도가 달라진다. IEP 기반 self-staircase는 계단이 뭉개져 실패했다.
- **원인:** `CT_ECAP.TSCTR`은 PRU에 가까운("near") 주변장치라 읽기에 **~2 cycle**이 걸리는 반면, `CT_IEP.TMR_CNT`는 OCP 버스 건너("far") 주변장치라 읽기에 **~14 cycle(~70 ns)**이 걸린다.
- **해결:** 빠른 타임스탬프가 필요하면 `CT_ECAP.TSCTR`을 쓴다 — eCAP이 IEP보다 약 7배 빠르다. 이 차이가 초기 IEP-spin staircase를 ~70 ns 단위로 양자화시켜 실패하게 한 원인이었고, `CT_ECAP.TSCTR`로 교체해 해결했다. v1.7은 `CT_IEP`를 아예 제거하고 `CT_ECAP.TSCTR`만 사용한다(cmd `0x42`/`0x43`).

자세한 내용은 [FIRMWARE.md](FIRMWARE.md) / [VERIFICATION.md](VERIFICATION.md) 참고.

---

## B. 용어집

| 용어 | 정의 |
|---|---|
| **PRU** | Programmable Realtime Unit. AM335x 안의 범용 A8 코어와 별개로 도는 200 MHz 전용 코어(PRU0/PRU1). 파이프라인·캐시가 없어 명령 = 사이클이 결정론적이며, 이 리포에서는 PRU0가 정밀 펄스 출력·입력 타임스탬프를 전담한다. |
| **PRU-ICSS** | PRU-Industrial Communication Subsystem. PRU0/PRU1 코어와 이들이 공유하는 인터럽트 컨트롤러(INTC), IEP 타이머, eCAP 등 주변장치를 묶은 서브시스템 전체를 가리킨다. |
| **remoteproc** | Linux 커널의 원격 프로세서 관리 프레임워크(`pru_rproc`). PRU를 리눅스가 직접 실행하는 스레드가 아니라 별도 프로세서로 취급해, `/sys/class/remoteproc/remoteprocN/firmware`·`state`로 펌웨어 로드/시작/정지를 제어한다. 이 리포는 UIO가 아니라 remoteproc 경로를 쓴다. |
| **RPMsg** | remoteproc Messaging. remoteproc으로 로드된 코어(PRU0)와 리눅스 사이의 메시지 통신 프로토콜로, 리눅스 쪽에 `/dev/rpmsg0` 캐릭터 디바이스를 만든다. 이 리포는 그 위에 1바이트 cmd(+페이로드) 프로토콜을 얹어 PING/VERSION/패턴 출력/AWG 검증 등을 제어한다. |
| **virtio** | RPMsg가 얹혀 있는 리눅스 표준 가상 I/O 전송 계층. remoteproc/RPMsg 스택은 이 virtio 버스를 통해 채널을 등록하고 메시지를 주고받는다. |
| **eCAP** | Enhanced Capture 모듈. PRU-ICSS 내부에 있는 "near" 주변장치로, 자유 실행(free-run) 카운터 `CT_ECAP.TSCTR`을 제공한다. 읽기 비용이 낮아(~2 cycle) 이 리포의 AWG 검증(cmd `0x42`/`0x43`)에 쓰이는 5 ns 타임스탬프의 실제 소스다. |
| **IEP** | Industrial Ethernet Peripheral 타이머. PRU-ICSS의 "far"(OCP 버스 건너) 주변장치로 `CT_IEP.TMR_CNT` 카운터를 제공한다. 같은 200 MHz라도 읽기 비용이 eCAP보다 약 7배 높아(~14 cycle, ~70 ns), 이 리포 v1.7에서는 제거되고 eCAP으로 대체됐다. |
| **TSCTR** | `CT_ECAP.TSCTR` — eCAP 모듈 안의 실제 자유 실행 카운터 레지스터. 200 MHz로 계속 증가하며, 두 시점의 값 차이가 곧 경과 사이클(× 5 ns) 수가 된다. |
| **pinmux / pinctrl-single** | pinmux는 SoC의 물리 핀을 어떤 기능(GPIO, PRU I/O, UART 등)으로 라우팅할지 정하는 일반 개념이고, `pinctrl-single`은 그 설정을 device tree에서 기술하기 위한 리눅스 커널 드라이버/바인딩이다. 이 리포의 커널은 `#pinctrl-cells=<2>`라 핀당 3개 값(`<offset conf mux>`)을 요구한다. |
| **device tree overlay** | 베이스 device tree를 통째로 다시 컴파일하지 않고, 추가/변경분만 담은 `.dts`를 `.dtbo`로 컴파일해 덧씌우는 방식. 이 리포는 `BB-PRUOUT-P8-00A0.dts`로 P8_11/P8_12/P8_16의 pinmux를 정의한다. |
| **U-Boot overlay** | 커널이 뜨기 전, 부트로더(U-Boot) 단계에서 적용되는 device tree overlay. `/boot/uEnv.txt`의 `uboot_overlay_pru=`로 지정하며, 커널이 pad 레지스터 소유권을 쥐기 전에 적용되어야 하는 이 리포의 pinmux는 반드시 이 경로로만 바꿀 수 있다. |
| **cycle-exact** | 매 명령/사이클이 파이프라인 스톨이나 캐시 미스 없이 실제 경과 시간에 결정론적으로 대응함을 뜻한다. PRU는 파이프라인·캐시가 없어 "명령 1개 = 사이클 1개 = 5 ns"가 항상 성립하며, 이 리포의 출력 생성(축 ①)이 이 성질에 기반한다. |
| **SCPI** | Standard Commands for Programmable Instruments. 계측기를 텍스트 명령으로 제어하는 표준 규격으로, 이 리포는 호스트가 LAN으로 Keysight 33521B AWG에 `TRIG:DEL` 등의 SCPI 명령을 보내 외부 지연 기준(τ)을 스윕하는 데 쓴다. |
| **mDNS** | Multicast DNS. 고정 IP 없이 로컬 네트워크에서 `beagle.local` 같은 호스트이름으로 장비를 찾게 해 준다. 이 리포에서 호스트이름으로 접속을 유지하는 수단이며, 재부팅 직후 잠깐 해소되지 않을 수 있다는 함정이 있다. |
| **DHCP** | Dynamic Host Configuration Protocol. 네트워크가 장비에 IP를 동적으로 배정하는 프로토콜이다(A-6 참고). |

---

*관련 문서: [ARCHITECTURE.md](ARCHITECTURE.md) · [GOALS.md](GOALS.md) · [VERIFICATION.md](VERIFICATION.md)*
