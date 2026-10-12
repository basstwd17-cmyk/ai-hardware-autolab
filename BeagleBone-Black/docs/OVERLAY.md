# 핀먹스 오버레이 (overlay/)

`overlay/` 디렉터리에는 디바이스트리 핀먹스 오버레이 파일이 정확히 1개 있다 — **`BB-PRUOUT-P8-00A0.dts`**. 이 문서는 이 파일이 왜 필요하고, 무엇을 하며, 어떻게 빌드·설치·검증하는지를 다룬다.

상세 구현 배경과 프로젝트 전체 맥락은 [FIRMWARE.md](FIRMWARE.md)·[VERIFICATION.md](VERIFICATION.md)·[GOALS.md](GOALS.md)를 참고.

---

## 1. 왜 오버레이가 필요한가

PRU-ICSS가 GPIO 헤더 핀을 **R30(출력)** / **R31(입력)** 레지스터로 토글·읽으려면, 그 핀의 **pinmux**가 부팅 시점에 PRU 모드로 설정되어 있어야 한다. 이 프로젝트가 쓰는 3개 핀(P8_11 / P8_12 / P8_16)은 기본적으로 PRU 기능이 아니라 다른 용도(파일 주석 기준 `gpmc_ad13` / `gpmc_ad12` / `gpmc_ad14`)로 잡혀 있다 — 즉 부팅 직후 상태 그대로는 R30/R31로 동작하지 않는다.

이 핀먹스는 **런타임에 바꿀 수 없다.** AM335x의 Control Module pad 레지스터(`pinctrl-single`, 베이스 주소 `44e10800`)는 커널이 부팅 후 장악하고 있어서, 사용자 공간에서 `/dev/mem`을 열어 해당 오프셋에 직접 쓰기를 시도해도 조용히 무시된다. C에서 `mmap(..., MAP_SHARED, ...)`로 `/dev/mem`을 매핑해 값을 써도 결과는 같다 — 커널이 이미 소유한 레지스터에 대한 사용자 공간 쓰기는 반영되지 않는다. 이 보드에는 `config-pin` 유틸리티도 설치되어 있지 않아 그 경로도 없다.

> ⚠️ 결과적으로 **부팅 시점에 적용되는 U-Boot 디바이스트리 오버레이가 유일한 pinmux 변경 경로**다. 오버레이를 바꾸거나 새로 적용하려면 반드시 **재부팅**해야 하며, 실행 중인 시스템에 재적용할 방법은 없다.

---

## 2. 이 오버레이가 실제로 하는 일

파일은 `/dts-v1/; /plugin/;` 로 시작하는 표준 디바이스트리 오버레이이며, 세 부분으로 구성된다.

### 2.1 `chosen` 노드 — 오버레이 식별자 등록

```dts
&{/chosen} {
	overlays {
		BB-PRUOUT-P8-00A0.kernel = "v2-3cell-P8_11+P8_12-out-P8_16-in";
	};
};
```

`/chosen/overlays` 아래에 이 오버레이의 식별 문자열(`v2-3cell-P8_11+P8_12-out-P8_16-in`)을 등록한다. 이 값 자체는 pinmux 로직에 관여하지 않고, U-Boot 오버레이 로더가 "어떤 오버레이가 적용됐는지"를 추적할 수 있게 하는 메타데이터다.

### 2.2 `am33xx_pinmux` 아래 핀그룹 정의

```dts
&am33xx_pinmux {
	pruio_pins: pruio_pins {
		pinctrl-single,pins = <
			0x034 0x0e 0x00		/* P8_11 -> R30.15 OUTPUT (out0, pad=0x0e) */
			0x030 0x0e 0x00		/* P8_12 -> R30.14 OUTPUT (out1, pad=0x0e) */
			0x038 0x2e 0x00		/* P8_16 -> R31.14 INPUT  (pad=0x2e) */
		>;
	};
};
```

`am33xx_pinmux` 노드 아래에 `pruio_pins`라는 이름의 핀그룹을 새로 정의한다. 각 행이 핀 하나에 대응하며, 값의 의미는 §4에서 자세히 다룬다.

### 2.3 `pruss` 노드 — 그룹을 기본 pinctrl 상태로 소비

```dts
&pruss {
	status = "okay";
	pinctrl-names = "default";
	pinctrl-0 = <&pruio_pins>;
};
```

`pruss` 노드를 활성화(`status = "okay"`)하고, `pinctrl-0`(즉 `pinctrl-names = "default"`에 대응하는 상태)로 `pruio_pins` 그룹을 지정한다. 이로써 부팅 시 `pruss` 드라이버가 이 3개 핀의 pinmux를 요청·점유(claim)하게 된다.

---

## 3. 핀 매핑 표

파일 주석과 `pinctrl-single,pins` 값을 그대로 옮기면 다음과 같다.

| 헤더 핀 | 베이스 신호 (주석 기준) | 오프셋 | pad 값 | 값 구성 | PRU 신호 | 방향 |
|---|---|---|---|---|---|---|
| **P8_11** | `gpmc_ad13` | `0x034` | `0x0e` | MODE6(`0x06`) \| NO_PULL(`0x08`) | `pr1_pru0_pru_r30_15` | OUTPUT (out0) |
| **P8_12** | `gpmc_ad12` | `0x030` | `0x0e` | MODE6(`0x06`) \| NO_PULL(`0x08`) | `pr1_pru0_pru_r30_14` | OUTPUT (out1) |
| **P8_16** | `gpmc_ad14` | `0x038` | `0x2e` | MODE6(`0x06`) \| INPUT_EN(`0x20`) \| NO_PULL(`0x08`) | `pr1_pru0_pru_r31_14` | INPUT |

> ⚠️ out1(P8_12, R30 bit14)과 in(P8_16, R31 bit14)은 **같은 비트 번호(14)지만 서로 다른 레지스터·다른 핀**이다. R30은 PRU 출력(GPO), R31은 PRU 입력(GPI)이므로 혼동하지 않도록 주의.

---

## 4. 3-value 포맷 함정 (가장 중요한 부분)

### 4.1 문제의 근원

파일 상단 주석에 명시된 대로, 이 커널의 `am33xx_pinmux`는 `#pinctrl-cells = <2>`로 선언되어 있다. `pinctrl-single` 드라이버에서 `#pinctrl-cells=<2>`는 핀 하나당 엔트리가 **3개의 32비트 값**으로 구성됨을 뜻한다:

```
<offset  conf  mux>
```

그리고 실제로 하드웨어에 적용되는 최종 패드 값은 `(conf | mux)`다. 이 오버레이에서는 세 번째 값(`mux`)을 항상 `0x00`으로 두고, MODE·PULL·INPUT_EN을 전부 두 번째 값(`conf`, 예: `0x0e`, `0x2e`)에 담는 방식을 쓴다.

이와 달리 **2-값 포맷**(`<offset value>`만 나열하는 방식, 오래된 커널이나 일부 오버레이 프레임워크 매크로가 쓰는 형태)으로 이 파일을 작성하면 어떻게 되는지가 핵심 함정이다. `pinctrl-single,pins` 프로퍼티는 단순히 32비트 값의 연속 배열(flat array)이고, 파서는 이를 `#address-cells + #pinctrl-cells` = `1 + 2` = 3개씩 끊어 읽는다. 2-값 포맷으로 값을 나열하면 dtc 컴파일 자체는 **에러 없이 성공**한다 — dtc는 배열의 개수가 3의 배수인지까지는 몰라도(구조적으로는 그냥 u32 배열이므로), 의도한 "핀 개수"만큼 3-값 단위로 안 끊기는 문제까지는 잡아내지 못한다. 결과적으로:

- **첫 번째 핀 엔트리만 우연히 유효한 것처럼 보이지만**, 사실은 `<offset1 val1 offset2>`로 읽혀 두 번째 핀의 오프셋이 첫 번째 핀의 `mux` 셀 자리에 끼어들어 `conf|mux` 연산에 OR되어 들어간다 — 즉 첫 핀에 적용되는 값 자체가 의도와 다르게 깨진다.
- **이후 핀들은 오프셋 정렬이 통째로 밀려서** 파서가 기대하는 위치에 유효한 오프셋이 오지 않고, 결과적으로 두 번째 핀부터는 아예 mux되지 않는다(pinmux가 적용되지 않은 채 기본 상태로 남는다).

> ⚠️ 즉 2-값 포맷은 **컴파일 타임에는 아무 경고도 없이 통과**하고, 실제 핀 점유 결과를 확인하기 전까지는 문제를 알아채기 어렵다. 이 커널(`6.18.32-bone35`, `#pinctrl-cells=<2>`) 대상으로는 반드시 3-값 포맷을 써야 한다.

### 4.2 어떻게 알아냈는가 — 베이스 DTB 디컴파일

이 파일 헤더 주석에 남아있는 검증 근거가 그대로 발견 과정이다:

```
(Verified against base-DTB groups, e.g. i2c0 <0x188 0x30 0x00>, leds <0x54 0x00 0x07>.)
```

즉, 부팅 시 U-Boot가 로드하는 **베이스 DTB**(오버레이 적용 전, 커널이 실제로 쓰는 기본 디바이스트리)를 `dtc`로 디컴파일해서, 이미 배포되어 정상 동작 중인 기존 pinctrl 그룹들(`i2c0`, `leds` 등)의 `pinctrl-single,pins` 값을 직접 확인했다. 그 결과 `i2c0`은 `<0x188 0x30 0x00>`, `leds`는 `<0x54 0x00 0x07>` 처럼 **핀 하나당 항상 3개의 값**이 나열되어 있음을 확인했고, 이를 근거로 이 커널의 `am33xx_pinmux`가 3-값(`<offset conf mux>`) 포맷을 요구한다는 것을 확정했다.

### 4.3 어떻게 검증했는가

1. **디컴파일 재확인:** 작성한 오버레이를 `dtc -I dtb -O dts`로 다시 디컴파일해, `pruio_pins`의 `pinctrl-single,pins` 값이 의도한 3개 항목(`0x034 0x0e 0x00` / `0x030 0x0e 0x00` / `0x038 0x2e 0x00`)으로 정확히 나오는지 눈으로 확인한다.
2. **리부트 후 실기 확인:** 오버레이를 설치하고 재부팅한 뒤, pinctrl 디버그 파일에서 각 핀이 `pruss`에 정상 점유(claim)됐는지 확인한다.

```bash
sudo grep -E 'pin 12 |pin 13 |pin 14 ' \
  /sys/kernel/debug/pinctrl/44e10800.pinmux-pinctrl-single/pins
```

여기서 `pin 12` / `pin 13` / `pin 14`는 오프셋을 4로 나눈 핀 인덱스에 대응한다 — `0x030 / 4 = 12`(P8_12), `0x034 / 4 = 13`(P8_11), `0x038 / 4 = 14`(P8_16). 이 grep 결과에 세 핀이 모두 나타나고 소유자가 `pruss` 관련 항목이면 pinmux가 정상 적용된 것이고, 특정 핀이 목록에서 빠져 있으면 §4.1의 2-값 포맷 함정(또는 오버레이 미적용)을 의심해야 한다.

---

## 5. 빌드 / 설치 절차

**1. dtc로 dts → dtbo(오버레이 바이너리) 컴파일:**

```bash
# device-tree-compiler 미설치 시:
#   sudo apt-get install device-tree-compiler
dtc -I dts -O dtb -@ -o BB-PRUOUT-P8-00A0.dtbo BB-PRUOUT-P8-00A0.dts
```

`-@` 플래그는 오버레이(`/plugin/;`)가 참조하는 phandle 심볼을 유지하기 위해 필요하다.

**2. `/lib/firmware/`에 설치:**

```bash
sudo cp BB-PRUOUT-P8-00A0.dtbo /lib/firmware/
```

**3. `/boot/uEnv.txt`에 `uboot_overlay_pru` 항목으로 지정:**

```
uboot_overlay_pru=/lib/firmware/BB-PRUOUT-P8-00A0.dtbo
```

기존에 다른 `uboot_overlay_pru=`(주석 처리된 것 포함) 줄이 남아있으면 U-Boot가 마지막 활성 줄만 적용하는 것이 아니라 예기치 않게 동작할 수 있으므로, 기존 줄을 정리하고 위 한 줄만 남기는 것이 안전하다.

**4. 재부팅:**

```bash
sudo systemctl reboot
```

> ⚠️ **런타임 재적용은 불가능하다.** `remoteproc`의 `firmware`/`state` 파일을 통해 PRU 펌웨어 자체는 재부팅 없이 다시 로드할 수 있지만(펌웨어 교체와 핀먹스 오버레이 적용은 완전히 별개 메커니즘이다), pinmux 오버레이는 U-Boot가 Linux 커널을 시작하기 **이전**에 적용하는 것이라 커널이 이미 부팅된 이후에는 다시 트리거할 방법이 없다. 오버레이를 바꿀 때마다 반드시 재부팅해야 한다.

재부팅 후에는 §4.3의 pinctrl 디버그 파일 확인으로 정상 적용 여부를 검증한다.

---

## 6. 새 핀을 오버레이에 추가하는 절차 (이 파일의 패턴 기준 일반화)

1. **오프셋 확인:** 추가하려는 헤더 핀에 대응하는 Control Module pad 오프셋을 확인한다. 이 파일의 예처럼 오프셋은 핀별 pad control 레지스터 위치이며(`P8_11 → 0x034`, `P8_12 → 0x030`, `P8_16 → 0x038`), 필요하면 베이스 DTB를 디컴파일해 인접한 기존 핀 정의에서 패턴을 재확인한다(§4.2와 동일한 방법).
2. **pad 값 계산:** 원하는 기능에 맞춰 MODE 비트와 필요한 플래그를 조합한다. 이 파일에서 확인된 조합은 출력용 `0x0e`(MODE6 `0x06` \| NO_PULL `0x08`), 입력용 `0x2e`(MODE6 `0x06` \| INPUT_EN `0x20` \| NO_PULL `0x08`)이다.
3. **3-값 항목 추가:** `pruio_pins`의 `pinctrl-single,pins` 목록에 `<offset  value  0x00>` 형태로 **반드시 3개 값을 전부 채워서** 한 줄을 추가한다. §4.1에서 설명한 2-값 포맷은 절대 쓰지 않는다 — 컴파일은 통과해도 이후 핀들이 조용히 mux되지 않는다.
4. **재컴파일·재설치:** §5의 `dtc` 명령으로 다시 컴파일하고 `/lib/firmware/`에 덮어쓴 뒤 재부팅한다(`uEnv.txt`의 `uboot_overlay_pru` 값은 파일명이 같다면 바꿀 필요 없다).
5. **검증:** 재부팅 후 §4.3의 방법대로 오버레이를 디컴파일해 값이 의도대로 나왔는지, 그리고 pinctrl 디버그 파일에서 새 핀 인덱스(`offset / 4`)가 `pruss`에 정상 점유됐는지 확인한다.
6. `pruss`가 이미 `pinctrl-names = "default"` 하나만 쓰고 있으므로, 새 핀도 같은 `pruio_pins` 그룹에 추가하면 별도의 `pinctrl-names`/`pinctrl-N` 확장 없이 동일한 기본 상태로 함께 적용된다.
