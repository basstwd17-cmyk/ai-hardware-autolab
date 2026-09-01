# 승인 3 기록 - Gate 1 GPIO measurement

사용자가 2026-08-18 대화에서 아래 물리 연결과 실제 오실로스코프 capture 3회를
명시적으로 승인했다.

## 측정 대상

- 신호: `dbg0`
- CPLD: U2 `LCMXO2-4000HC-4TG144C`
- 핀/site: pin 117 / PT24A
- 회로도 연결: NC, 다른 board net에 연결되지 않은 격리 출력
- 접근 방법: pin 117 임시선
- 인접 pin 116(GND), pin 118(VCCIO0) 단락 검사: PASS (사용자 확인)

이 승인은 DAC1_OUT/DAC2_OUT 또는 SMA RF1/RF2 측정 승인이 아니다. Gate 1
bitstream은 DAC 데이터 버스, D1CLK/D2CLK, BBB 제어 신호를 구동하지 않는다.

## 계측 연결

- RTM3004 CH3
- passive probe 10:1
- probe tip: pin 117 임시선
- probe ground: U4 pin 7
- 입력: DCLimit / 1 Mohm만 허용
- CN1, BBB, HW-USBN-2B `5V Out`: 미연결
- 벤치 전원: +5.0 V, current limit 300 mA

## 허용 범위

- `oscilloscope_capture` 3회
- 각 실행마다 screenshot PNG와 raw waveform CSV 저장
- LOW <= 0.4 V, HIGH >= 2.4 V, HIGH <= 3.6 V
- 유효 LOW/HIGH transition 4개 이상
- OSCH 정확도 미확보이므로 주파수는 판정하지 않음

## 실행 전 사용자 확인

- [x] pin 117 임시선 연결
- [x] pin 116/118 단락 없음
- [x] CH3, probe 10:1, ground U4 pin 7
- [x] CN1/BBB/5V Out 미연결
- [x] 전류 제한 미도달
- [x] 이상 징후 없음
- [x] GPIO 실제 capture 3회 승인

## 실행 결과

실행 후 run 디렉터리, LOW/HIGH, edge 수, voltage violation 수와 최종 판정을
기록한다.
