"""Capture and decode the Gate 10 clocked SPI response with the RTM3004.

The scope is armed before the BBB transfer is started.  That makes the result
an electrical record of the same transaction whose ``spidev`` return is being
diagnosed, rather than a visual inspection of a stale display.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from QKD_PROJ.hwtools.instruments.rtm3004 import RTM3004
from QKD_PROJ.orchestrator.execution_guard import require_brokered_entrypoint


ROOT = Path(__file__).resolve().parents[1]
BBB_ROOT = ROOT / "BeagleBone-Black"
DEFAULT_SCOPE_HOST = "192.168.50.3"
EXPECTED_MISO = "a55a3cc3f00f"
THRESHOLD_V = 1.65


def digital(values: list[float]) -> list[int]:
    return [int(value > THRESHOLD_V) for value in values]


def edges(levels: list[int], rising: bool) -> list[int]:
    pair = (0, 1) if rising else (1, 0)
    return [i for i in range(1, len(levels)) if (levels[i - 1], levels[i]) == pair]


def bits_as_hex(bits: list[int]) -> str:
    result = bytearray()
    for start in range(0, len(bits) - 7, 8):
        value = 0
        for bit in bits[start : start + 8]:
            value = (value << 1) | bit
        result.append(value)
    return result.hex()


def run_bbb_transfer() -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("BB_HOST", "192.168.50.2")
    env.setdefault("BB_USER", "xilinx")
    env.setdefault("BB_PW", "xilinx")
    # The same tested ioctl path is used, but one transfer is sufficient for a
    # scope acquisition and avoids a later frame overwriting the trigger frame.
    remote_script = "export SPI_REPEATS=1\n" + (
        BBB_ROOT / "diagnostics" / "spi_clocked_response_verify.sh"
    ).read_text(encoding="utf-8")
    return subprocess.run(
        [sys.executable, "host/bb.py"],
        cwd=BBB_ROOT,
        env=env,
        input=remote_script,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


def main() -> int:
    require_brokered_entrypoint(__file__)
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope-host", default=DEFAULT_SCOPE_HOST)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    run_dir = ROOT / "QKD_PROJ" / "runs" / (
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        + "_gate10_scope_clocked_return_"
        + uuid.uuid4().hex[:8]
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    scope = RTM3004(args.scope_host, 5025, timeout_s=15.0)
    try:
        identity = scope.identify()
        if identity.model != "RTM3004":
            raise RuntimeError(f"unexpected scope model: {identity.raw}")
        if "K15" not in {option.upper() for option in identity.options}:
            raise RuntimeError("scope option K15 is required for a single acquisition")

        # A front-panel operation can leave status bits behind.  Start this
        # independent capture with a clean error queue, then wait for each
        # configuration group to complete before interrogating that queue.
        scope.scpi.write("*CLS")

        # Each input is explicitly made high impedance before connecting its
        # capture path.  A 4 mA LVCMOS33 output must never be loaded by 50 ohm.
        for channel in (1, 2, 3, 4):
            scope.scpi.write(f"CHAN{channel}:STAT ON")
            scope.scpi.write(f"CHAN{channel}:COUP DCL")
            scope.scpi.write(f"CHAN{channel}:SCAL 1")
            scope.scpi.write(f"CHAN{channel}:OFFS 0")
            scope.scpi.write(f"CHAN{channel}:BAND FULL")
        scope.scpi.query("*OPC?")
        scope.check_errors("Gate 10 channel configuration")
        scope.assert_high_impedance(1, 2, 3, 4)

        # 1 ms total view contains the complete 48-clock 100 kHz transaction
        # (about 480 us) with margin before and after CS assertion.
        scope.scpi.write("TIM:SCAL 0.0001")
        scope.scpi.write("TIM:POS 0")
        scope.scpi.write("TRIG:A:MODE NORM")
        scope.scpi.write("TRIG:A:TYPE EDGE")
        # CS showed short noise edges in a prior capture.  Clock has a clean,
        # required edge for every actual transfer, so it is the reliable
        # acquisition trigger.  CS is still captured and reported below.
        scope.scpi.write("TRIG:A:SOUR CH4")
        scope.scpi.write("TRIG:A:EDGE:SLOP POS")
        scope.scpi.write("TRIG:A:LEV4 1.65")
        scope.scpi.query("*OPC?")
        scope.check_errors("Gate 10 trigger configuration")

        scope.stop()
        scope.arm_nsingle(1)
        scope.check_errors("Gate 10 arm")
        time.sleep(0.2)
        if not scope.acquisition_state().ready:
            raise RuntimeError("scope did not remain armed for the CS falling edge")

        bbb = run_bbb_transfer()
        state = scope.wait_until_complete(args.timeout, poll_s=0.1)
        scope.stop()
        waveforms = {channel: scope.read_waveform(channel) for channel in (1, 2, 3, 4)}
        screenshot = scope.save_screenshot(run_dir / "capture.png")

        levels = {channel: digital(wave.values_v) for channel, wave in waveforms.items()}
        all_clock_rises = edges(levels[4], rising=True)
        if len(all_clock_rises) < 8:
            raise RuntimeError(f"captured only {len(all_clock_rises)} SCLK rising edges")
        all_periods = [b - a for a, b in zip(all_clock_rises, all_clock_rises[1:])]
        nominal_period = statistics.median(all_periods)
        clock_blocks: list[list[int]] = [[]]
        for edge in all_clock_rises:
            if clock_blocks[-1] and edge - clock_blocks[-1][-1] > 2 * nominal_period:
                clock_blocks.append([])
            clock_blocks[-1].append(edge)
        clock_rises = max(clock_blocks, key=len)
        if len(clock_rises) < 48:
            raise RuntimeError(f"captured only {len(clock_rises)} contiguous SCLK rising edges; need 48")

        mosi = bits_as_hex([levels[2][edge] for edge in clock_rises])
        miso = bits_as_hex([levels[3][edge] for edge in clock_rises])
        step_s = waveforms[1].x_increment_s
        periods = [b - a for a, b in zip(clock_rises, clock_rises[1:])]
        frequency_hz = 1 / (statistics.median(periods) * step_s) if periods else None
        verdict = "PASS" if miso.startswith(EXPECTED_MISO) else "FAIL"

        summary = {
            "test_id": "gate10-spi-clocked-return",
            "verdict": verdict,
            "expected_miso_hex": EXPECTED_MISO,
            "scope_mode0_mosi_hex": mosi,
            "scope_mode0_miso_hex": miso,
            "clock_rising_edges_in_frame": len(clock_rises),
            "sclk_frequency_hz": frequency_hz,
            "cs_falling_edges": len(edges(levels[1], rising=False)),
            "cs_rising_edges": len(edges(levels[1], rising=True)),
            "scope_identity": asdict(identity),
            "front_end": {str(ch): asdict(scope.front_end(ch)) for ch in (1, 2, 3, 4)},
            "acquisition_state": asdict(state),
            "bbb_exit_code": bbb.returncode,
            "bbb_stdout": bbb.stdout,
            "bbb_stderr": bbb.stderr,
            "screenshot": screenshot.name,
            "scpi_commands": scope.scpi.commands(),
        }
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        with gzip.open(run_dir / "waveforms.json.gz", "wt", encoding="utf-8") as out:
            json.dump({str(ch): asdict(wave) for ch, wave in waveforms.items()}, out)

        print(f"run_dir={run_dir}")
        print(f"SCLK edges={len(clock_rises)}; frequency={frequency_hz / 1000:.1f} kHz; "
              f"CS edges={summary['cs_falling_edges']}/{summary['cs_rising_edges']}")
        print(f"scope mode-0 MOSI={mosi}")
        print(f"scope mode-0 MISO={miso}")
        print(f"expected MISO={EXPECTED_MISO}; verdict={verdict}")
        print(bbb.stdout, end="")
        return 0 if verdict == "PASS" else 2
    except Exception as exc:
        (run_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        print(f"run_dir={run_dir}\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            scope.emergency_stop()
        finally:
            scope.close()


if __name__ == "__main__":
    raise SystemExit(main())
