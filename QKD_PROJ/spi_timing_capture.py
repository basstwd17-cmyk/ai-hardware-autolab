"""Read one armed RTM3004 acquisition and decode the SPI frame from the waveforms.

    py -3 QKD_PROJ/spi_timing_capture.py [--arm]

Channel map (as wired for this session):
    CH1 = SPI.CS   (P9_17 / U2 pin 32)
    CH2 = SPI.MOSI (P9_18 / U2 pin 31)
    CH3 = SPI.MISO (P9_21 / U2 pin 33)
    CH4 = SPI.CLK  (P9_22 / U2 pin 34)

This decodes what a mode-0 master *should* have latched, straight from the
captured levels, so the answer does not depend on reading a screen. If the
decoded MISO byte stream is the transmitted pattern while McSPI reported
zeros, the fault is inside the controller. If the decoded MISO stream is zero
too, the scope and McSPI agree and the CPLD echo is not present during the
clocked window.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from QKD_PROJ.hwtools.instruments.rtm3004 import RTM3004  # noqa: E402

HOST = "192.168.50.3"
THRESHOLD_V = 1.65
NAMES = {1: "CS", 2: "MOSI", 3: "MISO", 4: "SCLK"}
EXPECTED_TX = "00ffaa553cc3"


def digital(values: list[float]) -> list[int]:
    return [1 if value > THRESHOLD_V else 0 for value in values]


def edges(levels: list[int], rising: bool) -> list[int]:
    want = (0, 1) if rising else (1, 0)
    return [
        index
        for index in range(1, len(levels))
        if (levels[index - 1], levels[index]) == want
    ]


def to_bytes(bits: list[int]) -> str:
    out = bytearray()
    for start in range(0, len(bits) - 7, 8):
        byte = 0
        for bit in bits[start : start + 8]:
            byte = (byte << 1) | bit
        out.append(byte)
    return out.hex()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", action="store_true", help="arm a fresh single acquisition and exit")
    parser.add_argument("--wait", type=float, default=0.0,
                        help="arm, then block up to N seconds for a trigger, then decode")
    parser.add_argument("--host", default=HOST)
    args = parser.parse_args()

    scope = RTM3004(args.host, 5025, timeout_s=30.0)
    transport = scope.scpi
    try:
        if args.arm or args.wait:
            scope.stop()
            scope.arm_nsingle(1)
            if args.arm:
                print("armed one single acquisition; run the SPI traffic now")
                return 0
            print(f"armed; waiting up to {args.wait:g}s for a CS falling edge. Run the traffic now.",
                  flush=True)
            # ACQ:STAT? reports STOP while armed, so readiness goes through
            # AcquisitionState rather than that query.
            state = scope.wait_until_complete(args.wait, poll_s=0.5)
            print(f"triggered: state={state.state}", flush=True)

        waveforms = {channel: scope.read_waveform(channel) for channel in (1, 2, 3, 4)}
        step = waveforms[1].x_increment_s
        print(f"record: {waveforms[1].record_length} points, {step * 1e9:.0f} ns/sample, "
              f"{(waveforms[1].x_stop_s - waveforms[1].x_start_s) * 1e6:.0f} us window")

        levels = {channel: digital(wave.values_v) for channel, wave in waveforms.items()}
        for channel, wave in waveforms.items():
            values = wave.values_v
            high = [v for v in values if v > THRESHOLD_V]
            low = [v for v in values if v <= THRESHOLD_V]
            print(
                f"  CH{channel} {NAMES[channel]:<4}: "
                f"low={statistics.fmean(low):+.2f}V " if low else f"  CH{channel} {NAMES[channel]:<4}: low=- ",
                end="",
            )
            print(f"high={statistics.fmean(high):+.2f}V " if high else "high=- ", end="")
            print(f"duty_high={100 * sum(levels[channel]) / len(values):.1f}% "
                  f"transitions={len(edges(levels[channel], True)) + len(edges(levels[channel], False))}")

        cs_falls = edges(levels[1], rising=False)
        cs_rises = edges(levels[1], rising=True)
        if not cs_falls:
            print("\nNo CS falling edge in this record: nothing was captured, or CS never asserted.")
            return 1
        start = cs_falls[0]
        end = next((index for index in cs_rises if index > start), len(levels[1]) - 1)
        print(f"\nframe: CS low for {(end - start) * step * 1e6:.1f} us")

        clk_rise = [index for index in edges(levels[4], True) if start < index < end]
        clk_fall = [index for index in edges(levels[4], False) if start < index < end]
        print(f"SCLK inside CS low: {len(clk_rise)} rising, {len(clk_fall)} falling edges")
        if len(clk_rise) >= 2:
            periods = [(b - a) * step for a, b in zip(clk_rise, clk_rise[1:])]
            print(f"SCLK period: {statistics.median(periods) * 1e6:.2f} us "
                  f"-> {1 / statistics.median(periods) / 1000:.1f} kHz")
        if not clk_rise:
            print("No clock inside the CS-low window; nothing to decode.")
            return 1

        print()
        print("decoded, sampling on the SCLK RISING edge (what mode 0 latches):")
        print(f"  MOSI = {to_bytes([levels[2][i] for i in clk_rise])}")
        print(f"  MISO = {to_bytes([levels[3][i] for i in clk_rise])}")
        print("decoded, sampling on the SCLK FALLING edge (what mode 1 latches):")
        print(f"  MOSI = {to_bytes([levels[2][i] for i in clk_fall])}")
        print(f"  MISO = {to_bytes([levels[3][i] for i in clk_fall])}")
        print(f"  expected tx pattern = {EXPECTED_TX}")

        # How much setup time does a rising-edge sampler actually get?
        mosi_changes = sorted(edges(levels[2], True) + edges(levels[2], False))
        deltas = []
        for change in mosi_changes:
            following = next((index for index in clk_rise if index > change), None)
            if following is not None:
                deltas.append((following - change) * step)
        if deltas:
            print()
            print(f"MOSI transition -> next SCLK rising edge: "
                  f"min={min(deltas) * 1e6:.2f} us median={statistics.median(deltas) * 1e6:.2f} us "
                  f"(half period = {statistics.median(periods) * 5e5:.2f} us)" if len(clk_rise) >= 2 else "")
        miso_changes = sorted(edges(levels[3], True) + edges(levels[3], False))
        print(f"MOSI transitions in record: {len(mosi_changes)}, MISO transitions: {len(miso_changes)}")
        return 0
    finally:
        transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
