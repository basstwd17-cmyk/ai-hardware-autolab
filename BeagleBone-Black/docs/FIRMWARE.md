# PRU0 펌웨어 (firmware/)

`firmware/` 디렉터리는 BeagleBone Black(AM335x) **PRU-ICSS PRU0** 코어에서 실행되는 실제
펌웨어(`bbb_pulse_io`)의 소스 전체다. ARM(Linux) 쪽과는 RPMsg(`rpmsg-raw`, `/dev/rpmsg0`)
1바이트 명령 프로토콜로 통신하며, 모든 정밀 출력·타이밍 측정은 PRU0가 담당한다. 이 문서는
`firmware/` 안의 파일들을 소스 수준에서 다룬다 — 배경/요구사항은
[GOALS.md](GOALS.md), 검증 결과는 [VERIFICATION.md](VERIFICATION.md), 리포 전체 개요와 빠른 시작은
[../README.md](../README.md), 빌드·로드·배포 절차는 [OPERATIONS.md](OPERATIONS.md)를 참고.

---

## 1. 디렉터리 구성

| 파일 | 역할 |
|---|---|
| `main.c` | 펌웨어 로직 전체 — 초기화, RPMsg 명령 디스패치, 각 cmd의 출력/측정 구현 |
| `pru_delay.asm` | 런타임 가변 n-사이클 정확 지연 (`pru_delay(uint32_t n)`), PRU 하드웨어 `loop` 명령 사용 |
| `Makefile` | clpru 빌드 규칙 (컴파일·어셈블·링크) |
| `resource_table.h` | RPMsg용 표준 리소스 테이블 (TI 예제 stock 파일) |
| `intc_map_0.h` | PRU INTC 인터럽트 맵 (TI 예제 stock 파일) |
| `AM335x_PRU_intc_rscTbl.cmd` | 링커 커맨드 파일 — 메모리 맵 정의 (TI 예제 stock 파일) |
| `pru-support/` | 오프라인 빌드를 위해 vendored된 TI PRU 지원 패키지 서브셋 |

---

## 2. 빌드 시스템

`Makefile`이 정의하는 핵심 변수는 다음과 같다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PRU_CGT` | `/usr/share/ti/cgt-pru` | TI PRU 코드젠 툴체인 경로 (`apt: ti-pru-cgt-v2.3`) — env로 override 가능 |
| `SUPPORT` | `./pru-support` | vendored TI 지원 패키지 서브셋 경로 — env로 override 가능 |
| `LINKER_COMMAND_FILE` | `./AM335x_PRU_intc_rscTbl.cmd` | 링커 스크립트 |
| `LIBS` | `--library=$(SUPPORT)/lib/rpmsg_lib.lib` | 프리빌드 RPMsg 라이브러리 |
| `INCLUDE` | `--include_path=$(SUPPORT)/include --include_path=$(SUPPORT)/include/am335x` | 헤더 검색 경로 |
| `STACK_SIZE` / `HEAP_SIZE` | `0x100` / `0x100` | 스택/힙 크기 |
| `GEN_DIR` | `gen` | 빌드 산출물 디렉터리 |
| `PROJ_NAME` | `pru0_pulse_io` | 산출물 베이스 이름 → `gen/pru0_pulse_io.out`, `gen/pru0_pulse_io.map` |

실제 컴파일/링크는 `$(PRU_CGT)/bin/clpru`(clpru 툴체인)를 직접 호출한다. `.c`는
`clpru --include_path=$(PRU_CGT)/include $(INCLUDE) $(CFLAGS) -fe $@ $<` 규칙으로,
`.asm`(`pru_delay.asm`)은 같은 `clpru`를 어셈블러로 호출하는 별도 규칙
(`$(GEN_DIR)/%.object: %.asm`)으로 오브젝트화된다. 링크 단계는
`clpru $(CFLAGS) -z -i$(PRU_CGT)/lib -i$(PRU_CGT)/include $(LFLAGS) -o $(TARGET) $(OBJECTS) -m$(MAP) $(LINKER_COMMAND_FILE) --library=libc.a $(LIBS)`.

**오프라인 빌드가 가능한 이유:** `pru-support/`는 TI PRU Software Support Package의 헤더
(`include/`, `include/am335x/`)와 프리빌드 라이브러리(`lib/rpmsg_lib.lib`)를 그대로
vendored한 서브셋이라(`pru-support/README.md`), 보드에서 28 MB짜리 전체 support 패키지를
새로 clone하지 않고도 `make` 한 번으로 빌드가 끝난다.

**빌드 산출물 → remoteproc 배포:** `make`가 만드는 `gen/pru0_pulse_io.out`은 remoteproc이
기대하는 펌웨어 이미지 이름(`am335x-pru0-pulseio.out`)으로 `/lib/firmware/`에 복사되고,
`/sys/class/remoteproc/remoteproc1/firmware`에 그 이름을 쓴 뒤 `remoteproc1/state`에
`stop`/`start`를 순서대로 써서 PRU0에 로드된다. 이 과정은 리포에서 `scripts/deploy.sh`로
스크립트화되어 있다(세부 절차는 OPERATIONS.md 참고).

---

## 3. 파일별 역할

### 3.1 main.c — 펌웨어 로직

초기화, RPMsg 채널 개설, 명령 디스패치 루프, 각 명령의 실제 출력/측정 구현이 모두 이 한
파일에 있다. 자세한 내용은 아래 4~9절.

### 3.2 pru_delay.asm — 런타임 가변 지연

`__delay_cycles(N)`은 clpru 컴파일러 내장 함수로 **N이 반드시 컴파일타임 상수**여야 한다.
즉 런타임에 호스트가 보낸 값만큼 지연 시간을 정하고 싶을 때는 `__delay_cycles`를 쓸 수
없다. `pru_delay.asm`은 이 문제를 PRU **하드웨어 `LOOP`** 명령으로 해결한다.

```asm
;  clpru ABI: first 32-bit arg in R14 ; leaf return via R3.w2.
	.global	pru_delay
	.sect	".text:pru_delay"
pru_delay:
	qbeq	pd_ret, r14, 0		; n == 0 -> nothing to do
	loop	pd_end, r14		; repeat the body (1 instr) r14 times
	nop				; 1-cycle loop body
pd_end:
pd_ret:
	jmp	r3.w2			; return to caller (leaf)
```

`void pru_delay(uint32_t n)`은 clpru의 **leaf 함수 ABI**를 따른다 — 첫(유일한) 32비트
인자는 `R14`에 실려서 들어오고, 복귀는 `jmp r3.w2`로 이루어진다(스택 프레임이 없는 leaf
함수라 `R3.w2`에 저장된 복귀 주소로 바로 점프). `n==0`이면 `qbeq`로 곧장 `pd_ret`으로
분기해 지연 없이 반환한다. `loop pd_end, r14`는 그 뒤 단일 `nop`(1 사이클)을 정확히 `r14`번
반복시키므로, `pru_delay(D)`는 **정확히 D 사이클(D×5 ns)**을 소모하고 반환한다 — 카운터
읽기·비교 같은 반복당 오버헤드가 전혀 없다. cmd `0x43`이 이 함수를 사용한다.

### 3.3 resource_table.h / intc_map_0.h / AM335x_PRU_intc_rscTbl.cmd — stock TI 설정

세 파일 모두 `pru-support/README.md`에 명시된 대로 TI PRU Software Support Package의
`examples/am335x/PRU_RPMsg_Echo_Interrupt0/` 예제에서 그대로 가져온 **stock(수정 없음)**
파일이다.

- **resource_table.h**: RPMsg용 표준 `struct my_resource_table` — `rpmsg_vdev`(vdev 타입,
  `VIRTIO_ID_RPMSG`, 기능 비트 `RPMSG_PRU_C0_FEATURES`)와 두 개의 vring(`PRU_RPMSG_VQ0_SIZE`
  `= 16`, `PRU_RPMSG_VQ1_SIZE = 16`)을 정의한다. `resourceTable` 심볼은
  `.resource_table` 섹션에 배치되고 `#pragma RETAIN`으로 링커 최적화 제거를 막는다.
- **intc_map_0.h**: `my_irq_rsc` — RemoteProc 드라이버가 초기화 시 참조하는 PRU INTC
  시스템 이벤트 맵. 이 펌웨어는 `{17, 0, 0}` 한 항목만 매핑한다(`{sysevt, channel, host
  interrupt}`, sysevt 17 = `FROM_ARM_HOST`). 헤더 주석대로 이 맵은 **펌웨어 자체에서는
  쓰이지 않고** RemoteProc 초기화 전용이라, `DATA_SECTION`+`RETAIN`이 없으면 컴파일러가
  통째로 제거해 버릴 수 있다.
- **AM335x_PRU_intc_rscTbl.cmd**: 링커 스크립트. `PRU_IMEM : org = 0x00000000 len =
  0x00002000`(8 KB 명령 메모리), `PRU_DMEM_0_1`/`PRU_DMEM_1_0`(각 8 KB 데이터 메모리),
  `PRU_SHAREDMEM`(12 KB), `PRU_CFG`/`PRU_ECAP`/`PRU_IEP`/`PRU_INTC`/`PRU_UART` 등 주변장치
  레지스터 영역, DDR/L3OCMC 등을 정의하고 `.text`→`PRU_IMEM`, `.bss`/`.data`/`.resource_table`
  등을 `PRU_DMEM_0_1`에 배치한다.

---

## 4. 하드웨어 핀 매핑

`main.c` 상단 정의 그대로:

| 신호 | 레지스터 / 비트 | 매크로 | 헤더 핀 |
|---|---|---|---|
| out0 | `R30` bit 15 | `OUT0 (1u << 15)` | **P8_11** |
| out1 | `R30` bit 14 | `OUT1 (1u << 14)` | **P8_12** |
| in0 | `R31` bit 14 | `IN0 (1u << 14)` | **P8_16** |

`R30`(PRU GPO)과 `R31`(PRU GPI / host-int)은 `volatile register uint32_t`로 선언되어 매핑된다.
`OUT1`과 `IN0`이 같은 비트 위치(14)를 쓰지만 서로 다른 레지스터(`R30` vs `R31`)이므로
충돌하지 않는다. 그 외 `HOST_INT = (1u << 30)`은 `R31`에서 호스트 인터럽트 도착 여부를
표시하는 비트다.

---

## 5. 초기화 시퀀스 (main() 상단)

`main()`의 초기화부는 순서대로 다음을 수행한다.

1. **`CT_CFG.SYSCFG_bit.STANDBY_INIT = 0;`** — OCP master port를 연다. 이걸 하지 않으면
   PRU가 주변장치/외부 메모리에 접근할 수 없다.
2. **`__R30 &= ~(OUT0 | OUT1);`** — 두 출력을 idle low로 초기화한다.
3. **`CT_ECAP.ECCTL2 = 0x0010;`** — PRU-ICSS eCAP의 `TSCTR`(타임스탬프 카운터, 200 MHz =
   5 ns)를 free-run 시킨다. 소스 주석에 따르면 `CT_ECAP`은 "near" 주변장치라 읽기가 약
   2 사이클인 반면, `CT_IEP`는 "far"라 약 14 사이클이 걸린다 — 그래서 시간 기준으로 eCAP
   `TSCTR`을 택했다. 이 카운터는 cmd `0x42`/`0x43`의 타임스탬프 소스로 쓰인다.
4. **`CT_INTC.SICR_bit.STS_CLR_IDX = FROM_ARM_HOST;`** — sysevt 17(`FROM_ARM_HOST`)의
   보류 인터럽트 상태를 미리 클리어한다.
5. **`while (!(*status & VIRTIO_CONFIG_S_DRIVER_OK));`** — `resourceTable.rpmsg_vdev.status`를
   폴링해 ARM 쪽 virtio 드라이버가 준비될 때까지 대기한다.
6. **`pru_rpmsg_init(...)`** — `resourceTable.rpmsg_vring0/vring1`과 `TO_ARM_HOST = 16`,
   `FROM_ARM_HOST = 17` 이벤트 번호로 RPMsg 전송 계층을 초기화한다.
7. **`pru_rpmsg_channel(RPMSG_NS_CREATE, &transport, CHAN_NAME, CHAN_PORT)`** — 채널 이름
   `"rpmsg-raw"`, 포트 `30`으로 RPMsg 채널을 생성하고, `PRU_RPMSG_SUCCESS`가 될 때까지
   반복한다. 이 채널 생성 알림을 받아야 Linux 쪽에 `/dev/rpmsgN`이 나타난다.

이후 `while(1)` 메인 루프는 `__R31 & HOST_INT`로 호스트 인터럽트를 감지하고, 감지되면
다시 `CT_INTC.SICR_bit.STS_CLR_IDX = FROM_ARM_HOST;`로 인터럽트 상태를 클리어한 뒤
`pru_rpmsg_receive`가 `PRU_RPMSG_SUCCESS`를 반환하는 동안 메시지를 반복 처리한다.

---

## 6. 공통 인프라

```c
static uint32_t rd32(const uint8_t *p)
{ return (uint32_t)p[0] | ((uint32_t)p[1]<<8) | ((uint32_t)p[2]<<16) | ((uint32_t)p[3]<<24); }
static void wr32(uint8_t *p, uint32_t v)
{ p[0]=(uint8_t)v; p[1]=(uint8_t)(v>>8); p[2]=(uint8_t)(v>>16); p[3]=(uint8_t)(v>>24); }

static void reply(uint16_t n) { pru_rpmsg_send(&transport, dst, src, payload, n); }

#define REQUIRE_LEN(n) if (len < (n)) { payload[1] = 0xEF; reply(2); break; }
```

- **`rd32`/`wr32`**: 4바이트 정수를 **리틀엔디안**으로 payload 버퍼에 직렬화/역직렬화한다
  (`reps`, `n`, `D`, `k` 등 모든 4바이트 인자·결과 필드가 이 포맷).
- **`reply(n)`**: `pru_rpmsg_send(&transport, dst, src, payload, n)`을 감싼 헬퍼. 인자
  순서가 `(transport, src, dst, ...)`인 `pru_rpmsg_send`에 대해, 방금 받은 메시지의
  `dst`(PRU 쪽 채널 주소)를 새 메시지의 `src`로, 받은 메시지의 `src`(원 발신자 주소)를 새
  메시지의 `dst`로 바꿔 넣는다 — 요청을 보낸 호스트에게 그대로 되돌려 보내는 구조다.
- **`REQUIRE_LEN(n)` 매크로**: payload 길이(`len`)가 `n` 바이트에 못 미치면 `[cmd][0xEF]`
  (원래 `payload[0]`에 있던 cmd + NAK 바이트 `0xEF`)를 되돌리고 그 case를 빠져나간다.
  **함수가 아니라 매크로인 이유**: 내부의 `break`가 `main()`의 `switch (cmd)` 문 안에서
  **그 case를 즉시 종료**해야 하기 때문이다. 만약 이걸 함수로 만들면 `break`는 함수 자신의
  스코프(또는 함수 안의 다른 loop/switch)에만 적용되어 바깥 `switch`를 빠져나가지 못한다.
  매크로는 호출 지점에 텍스트 그대로 치환되므로 `break`가 감싸는 `switch (cmd)`에 바로
  적용된다.

---

## 7. RPMsg 명령 프로토콜

`main.c`에 실제로 존재하는 모든 case를 hex 오름차순으로 정리하면 다음과 같다. 목록에 없는
cmd(예: 0x40/0x41/0x50)는 소스에 존재하지 않으며, `default` 분기로 떨어져 `[0xEE][0xEE]`가
반환된다.

| cmd | 이름 | 요청 payload | 응답 payload | 설명 |
|---|---|---|---|---|
| `0x01` | PING | `[0x01]` | `[0x01][0x01]` (2B) | 고정 에코 |
| `0x03` | VERSION | `[0x03]` | `[0x03][ascii...]` (가변, `k+1`B) | 펌웨어 버전 문자열 반환 |
| `0x10` | R1 PATTERN | `[0x10][reps:4]` | `[0x10][1][reps:4]` (6B) | off500/on2000/off600/on500/off500 ns 1사이클을 `reps`회 반복 (0=무한, remoteproc으로만 정지) |
| `0x20` | ≈200 ns 구형파 | `[0x20][reps:4]` | `[0x20][1]` (2B) | 40c/40c 토글, ≈50% duty (0=무한) |
| `0x21` | ≈100 ns 구형파(≈5 MHz) | `[0x21][reps:4]` | `[0x21][1]` (2B) | 20c/20c 토글, ≈50% duty (0=무한) |
| `0x22` | 최소폭 펄스 | `[0x22][reps:4]` | `[0x22][1]` (2B) | ~10 ns(2명령) high, 500 ns 주기 (0→내부적으로 1,000,000회) |
| `0x30` | 스코프 계단 프레임 | `[0x30]` (payload 검사 없음) | **없음** | ~1 µs 마커 + 5 ns씩 늘어나는 펄스 10개, 무한 루프 (remoteproc로만 정지) |
| `0x31` | 단일폭 인터랙티브 트레인 | `[0x31][idx:4]` | **없음** | 선택한 idx의 HIGH 폭으로 무한 펄스 (remoteproc로만 정지) |
| `0x32` | 자동 5 ns 스텝 스윕 | `[0x32]` (payload 검사 없음) | **없음** | 5..60 ns를 자동으로 훑는 무한 루프 (remoteproc로만 정지) |
| `0x34` | out0→out1 지연 램프 | `[0x34]` (payload 검사 없음) | **없음** | 0..100 ns 지연을 자동으로 훑는 무한 루프 (remoteproc로만 정지) |
| `0x35` | out0→out1 고정 지연 | `[0x35][k:4]` | **없음** | 선택한 k의 지연(k×5 ns)으로 무한 루프 (remoteproc로만 정지) |
| `0x36` | 10 Hz 구형파 | `[0x36]` (payload 검사 없음) | **없음** | P8_11에서 50 ms HIGH / 50 ms LOW를 반복하는 무한 루프 (16-bit LOOP 블록 분할, nominal 50% duty, remoteproc로만 정지) |
| `0x42` | AWG 타임베이스 shot | `[0x42][n:4]` | `[0x42][ok][ngood:4][ntmo:4][min:4][max:4][sum:4]` (22B) | 외부 AWG로 트리거→에코 왕복시간 n회 샘플링 |
| `0x43` | AWG 윈도우 스트로브 | `[0x43][D:4]` | `[0x43][ok][bit][D:4]` (7B) | out0 기동 후 정확히 D 사이클 뒤 입력을 1회 샘플 |
| (그 외) | UNKNOWN | 임의 | `[0xEE][0xEE]` (2B) | `default` 분기 |

> ⚠️ `0x30`/`0x31`/`0x32`/`0x34`/`0x35`/`0x36`은 `for(;;)` 무한 루프로 들어가며 **응답을 보내지
> 않는다**. 다시 명령을 받으려면 remoteproc으로 펌웨어를 stop/restart 해야 한다. 또한
> `0x30`/`0x32`/`0x34`는 payload 길이를 `REQUIRE_LEN`으로 검사하지 않는다(고정 동작이라
> 인자가 필요 없기 때문).

### 7.1 스코프 데모 — 출력 5 ns 생성 (0x30, 0x31, 0x32)

- **`0x30` 계단 프레임**: `__R30 |= OUT0; __delay_cycles(200); __R30 &= ~OUT0;`로 약
  1 µs(200×5 ns) 트리거 마커를 만든 뒤, `__delay_cycles(300)`(1500 ns) 간격을 두고 HIGH
  폭이 `__delay_cycles(0)`(생략, 즉시 clear) → `__delay_cycles(1)` → … → `__delay_cycles(9)`로
  1씩 늘어나는 펄스 10개를 낸다(펄스 사이 40 사이클=200 ns 저전위 간격). 각 펄스가 이전보다
  정확히 `__delay_cycles(1)` = 1 PRU 사이클 = **5 ns** 더 긴 것이 핵심 — 스코프에서
  persistence 모드로 계단이 5 ns 간격으로 벌어지는 것을 육안 확인하는 용도다. 프레임 끝에
  `__delay_cycles(1000)`(5 µs) 구분자를 두고 무한 반복한다.
- **`0x31` 단일폭 트레인**: `[idx:4]`로 받은 값을 **컴파일타임 상수만 쓰는 nested
  switch**로 분기해, 그 case 안에서 고정된 `__delay_cycles(N)`으로 HIGH 폭을 만든다.
  지원되는 idx와 코드상의 `__delay_cycles` 인자는 다음과 같다(모두 뒤에 저전위
  `__delay_cycles(10)` = 50 ns 간격).

  | idx | HIGH 구간 `__delay_cycles` 인자 |
  |---|---|
  | 1 | (없음 — set 직후 즉시 clear) |
  | 2 | `1` |
  | 3 | `2` |
  | 4 | `3` |
  | 5 | `4` |
  | 6 | `5` |
  | 7 | `6` |
  | 8 | `7` |
  | 9 | `8` |
  | 10 | `9` |
  | 11 | `10` |
  | 12 | `11` |
  | 16 | `15` |
  | 20 | `19` |
  | 40 | `39` |
  | 그 외 idx | `default`로 떨어져 idx=10과 동일(`9`) |

  idx를 1씩 늘리면 `__delay_cycles` 인자도 1씩 늘어나므로 falling edge가 정확히 5 ns씩
  걸어 나간다(소스 주석: "sweeping idx walks the falling edge by 5 ns"). 목록에 없는 idx
  값은 에러 없이 idx=10과 같은 파형으로 조용히 대체된다.

  > ⚠️ 소스 주석은 이 명령의 저전위 간격을 "low gap ~500 ns"라고 적어 두었지만, 실제 코드는
  > 모든 case에서 `__delay_cycles(10)` = **50 ns**를 쓴다. 주석과 코드 값이 어긋나므로,
  > 실측 파형의 저전위 구간을 신뢰 기준으로 삼을 것.

- **`0x32` 자동 스윕**: payload 인자 없이, `for(;;)` 바깥 루프 안에서 12단계
  (`__delay_cycles(0)`~`__delay_cycles(11)`, 즉 5 ns~60 ns)를 순서대로 각각
  8,000,000회 반복 유지한 뒤 다음 단계로 넘어가는 것을 무한 반복한다. 소스 주석은 각 단계
  유지 시간을 "~0.7 s"로 적고 있다. 호스트 재시작이나 출력 blanking 없이 PRU가 스스로
  스텝을 진행한다.

### 7.2 out0→out1 지연 데모 (0x34, 0x35)

- **`0x34` 지연 램프**: payload 인자 없이, `for(;;)` 무한 루프 안에서 out0(`OUT0`)을 먼저
  세우고 `__delay_cycles(N)` 뒤 out1(`OUT1`)을 세우는 프레임을 16단계 반복한다. 소스의
  인라인 주석 기준 지연값은 `0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 90,
  100`(단위 ns)이며, k=0(둘 다 한 명령으로 동시에 세팅 = 진짜 0 ns)부터 시작한다. 각
  프레임은 두 출력을 세운 뒤 `__delay_cycles(40)`(200 ns) 유지하고 `__delay_cycles(60)`
  (300 ns) 간격을 두고 clear한다.

  > ⚠️ 인라인 주석의 지연 값 나열은 50 ns 다음이 55 ns가 아니라 **60 ns로 건너뛴다**
  > (`__delay_cycles(9)`=50 ns 다음이 `__delay_cycles(11)`=60 ns; `__delay_cycles(10)`
  > =55 ns 단계는 없다). "0..100 ns 지연 램프"라는 이름과 달리 55 ns 스텝은 존재하지
  > 않는다.

- **`0x35` 고정 지연**: `[k:4]`로 받은 값을 nested switch로 분기해, out0→out1 지연을
  `k`값에 대응하는 컴파일타임 상수 `__delay_cycles`로 고정한다. 명시적으로 지원되는 k는
  `0, 1, 2, 3, 4, 5, 6, 8, 10, 20`이며, 각각 `k×5 ns` 지연(예: k=8 → `__delay_cycles(7)`
  = 35 ns 추가 지연 + 5 ns 베이스라인 = 40 ns)에 대응한다.

  > ⚠️ `k = 7, 9, 11..19, 21` 등 목록에 없는 값은 에러를 반환하지 않고 조용히 `default`
  > (소스 주석 그대로 "unlisted k -> 50ns", `__delay_cycles(9)`, k=10과 동일한 지연)로
  > 대체된다. 원하는 지연이 목록에 없으면 실제로는 50 ns가 나간다는 점에 유의.

### 7.3 cmd 0x42 — AWG 타임베이스 검증 shot

외부 Keysight 33521B AWG를 `out0(P8_11)`으로 트리거하고, AWG가 `TRIG:DEL = τ` 뒤 내보내는
에코를 `in(P8_16)`으로 받아 왕복시간을 측정하는 명령이다. `[0x42][n:4]`에서 `n`이 0이거나
2000을 넘으면 100으로 clamp된다. 한 샷(iteration)은:

1. `__R30 &= ~OUT0; __delay_cycles(2000);` — 출력을 내리고 **약 10 µs**(2000×5 ns) 대기
   (저전위 유지 + AWG 재무장 시간).
2. 이전 에코가 다 빠졌는지 `while (__R31 & IN0)`로 확인(최대 60000회 반복 후 포기).
3. `t0 = CT_ECAP.TSCTR;` 로 트리거 순간의 5 ns 카운터 값을 기록한 뒤 `__R30 |= OUT0;`으로
   상승 엣지를 내보내 AWG를 트리거한다.
4. **tight echo poll**: `guard < 16000u` 동안, 매 반복마다 `__R31 & IN0`를 **4회 언롤**해서
   검사한다(에코 상승 엣지를 가능한 한 빨리 잡기 위함).
5. `t1 = CT_ECAP.TSCTR;`을 기록하고, 타임아웃이면 `ntmo++` 후 이 샷은 통계에서 제외
   (`continue`), 아니면 `d = t1 - t0`를 `rmin`/`rmax`/`rsum`에 누적하고 `ngood++`.

응답 `[0x42][ok][ngood:4][ntmo:4][min:4][max:4][sum:4]`(22B)에서 `ok`는
`(ngood>0)?1:0`. 호스트는 `mean = sum/ngood`를 5 ns 배율(`M_ns = mean*5`)로 환산해 τ에
대한 선형 적합을 수행한다(이 값이 [VERIFICATION.md](VERIFICATION.md)의 축 ② 타임베이스 정확도 검증에
쓰인다).

> ⚠️ tight echo poll 위의 소스 주석은 "sample R31 ~every cycle (**8x unrolled**)"라고
> 적혀 있지만, 실제 루프 본문은 `if (__R31 & IN0) { ... break; }`가 **4번** 나열된
> 4x 언롤이다. 주석의 "8x"는 코드와 일치하지 않는다.

### 7.4 cmd 0x43 — AWG 윈도우 스트로브

`0x42`와 같은 배선(out0→AWG ext-trig, AWG 에코→in)을 쓰지만, 목적은 왕복시간 측정이 아니라
**출력↔입력 샘플링 윈도우를 5 ns 단위로 정확히 위치시킬 수 있는지**를 외부 기준으로 증명하는
것이다. `[0x43][D:4]`에서 `D`는 4000을 넘으면 4000으로 clamp된다.

1. `__R30 &= ~OUT0; __delay_cycles(2000);` — 저전위 + AWG 재무장 대기(약 10 µs).
2. 입력이 low인지 드레인 확인(최대 60000회).
3. `__R30 |= OUT0;` — 상승 엣지로 AWG를 트리거.
4. **`pru_delay(D);`** — `pru_delay.asm`의 HW `LOOP`로 **정확히 D 사이클(D×5 ns)**을
   대기한다. 소스 주석은 이 선택 이유를 명시한다: 카운터를 스핀 폴링하는 방식(반복당 read
   + compare)은 약 6사이클 단위의 워블이 남는 반면, HW `LOOP`은 반복당 오버헤드가 0이라
   워블이 없다.
5. `bit = (__R31 & IN0) ? 1u : 0u;` — `pru_delay(D)`가 반환한 그 순간(= out 엣지 +
   D×5 ns)에 입력을 **한 번** 샘플링한다.
6. `__R30 &= ~OUT0;`로 출력을 내린다.

응답은 `[0x43][ok=1][bit][D:4]`(7B). 호스트는 D를 스윕해 `bit`가 0→1로 바뀌는 경계 D*를
찾고, AWG의 `TRIG:DEL = τ`를 스텝시키며 D*의 이동량이 τ와 5 ns 단위로 일치하는지 확인한다
([VERIFICATION.md](VERIFICATION.md)의 축 ③ 런타임 윈도우 포지셔닝 검증).

---

## 8. 타이밍 모델: 컴파일타임 상수 vs 런타임 가변 지연

PRU-ICSS는 200 MHz(1 instruction = 1 cycle = 5 ns)로 동작하고, `__delay_cycles(N)`은
`N` 사이클(=N×5 ns)을 소비하는 clpru 내장 함수다. 다만 **N은 반드시 컴파일타임 상수**여야
한다는 제약이 있다 — 런타임 변수를 그대로 넣을 수 없다.

이 제약 때문에 `0x31`/`0x35`(그리고 `0x30`/`0x32`/`0x34`)는 다음 패턴을 쓴다: 런타임
인자(`idx`, `k`)로 `switch`를 분기시키되, **각 case 내부의 지연은 여전히 컴파일타임
상수**인 `__delay_cycles(리터럴)`을 쓴다. 즉 "런타임 선택 + 컴파일타임 상수 지연"의
조합이며, 지원되지 않는 값은 표에서 본 대로 `default` case로 조용히 대체된다.

반대로 `pru_delay.asm`의 HW `LOOP`은 진짜 **런타임 가변** 지연이다 — `pru_delay(D)`의
`D`는 실행 중에 레지스터(`R14`)로 전달되는 값이며, 반복당 오버헤드 없이 정확히 `D`
사이클을 대기한다. `0x43`이 이 방식을 쓰는 이유가 바로 여기 있다: 호스트가 임의의 `D`
값(최대 4000)을 실시간으로 지정해도 정확한 지연을 얻어야 하기 때문에, 컴파일타임 상수
스위치로는 불가능하고 HW `LOOP`이 필요하다.

---

## 9. IMEM 예산

`AM335x_PRU_intc_rscTbl.cmd`의 `PRU_IMEM : org = 0x00000000 len = 0x00002000`이 정의하는
대로, PRU0의 명령 메모리(IMEM)는 **8 KB(0x2000)**로 하드 제한된다.

> ⚠️ 이전 버전에서 개발/진단용 명령 `0x40`(루프백), `0x41`(자체 staircase), `0x50`(eCAP
> 프로브)과 미사용 `CT_IEP` 초기화를 제거하는 리팩토링을 거치면서, IMEM 사용량이 빌드 시
> 약 `0x1fe8`(8 KB 한도에 거의 가득 참)에서 약 `0x1834`(6196바이트, 약 76%)로 줄었다고
> **빌드 시 확인**되었다. 이 수치는 리포에 커밋되지 않는 빌드 산출물(`gen/` 폴더의
> `.map` 파일)에서 나온 것이라 리포 소스만으로는 재확인할 수 없고, 툴체인 버전·최적화
> 옵션에 따라 달라질 수 있다.

---

## 10. 새 RPMsg 명령을 추가하는 절차

1. `main.c`의 `switch (cmd)` 안에 새 `case 0xNN:` 블록을 추가한다.
2. 요청에 4바이트 이상의 인자가 필요하면 블록 맨 앞에서 `REQUIRE_LEN(n)`으로 길이를
   검증한다(짧으면 자동으로 `[cmd][0xEF]`를 반환하고 case를 빠져나간다).
3. 인자는 `rd32(&payload[1])`로 읽고, 응답은 `payload[]`에 `wr32`/직접 대입으로 채운 뒤
   `reply(n)`으로 되돌린다.
4. 런타임 가변 지연이 필요하면 `pru_delay(D)`(`pru_delay.asm`)를 쓰고, 고정된 값 몇 개
   중 하나만 고르면 되는 경우엔 `0x31`/`0x35`처럼 컴파일타임 상수 `__delay_cycles`
   nested switch 패턴을 쓴다.
5. `case 0x03`(VERSION)의 버전 문자열 리터럴을 갱신해, 배포된 펌웨어가 새 명령을
   포함하는지 `0x03` 조회만으로 구분할 수 있게 한다.
6. `make`로 재빌드하고 `scripts/deploy.sh`로 재로드한다. 자세한 빌드/배포 절차는
   OPERATIONS.md 참고.
