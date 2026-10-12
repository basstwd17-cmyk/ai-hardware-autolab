"""Offline safety and integration checks for Gate12."""

from __future__ import annotations

import re
import hashlib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
BBB = ROOT.parent / "BeagleBone-Black"
SPEC = ROOT / "contracts" / "gate12-pru-dac1-plan.yaml"
HDL = ROOT / "gate12_pru_dac1" / "gate12_pru_dac1.v"
LPF = ROOT / "gate12_pru_dac1" / "gate12_pru_dac1.lpf"
OVERLAY = BBB / "overlay" / "BB-RTASE-A-GPIO-SPI-PRU-00A0.dts"
PREPARER = BBB / "host" / "prepare_gate12_overlay.py"
PRU = BBB / "firmware" / "main.c"
RTASE_HOST = BBB / "host" / "rtase.py"
SPI_HOST = BBB / "host" / "rtase_spi.py"
PRU_SERVICE = BBB / "scripts" / "rtase-pru-service.sh"


EXPECTED_PINS = {
    "spi_mosi": 31,
    "spi_cs_n": 32,
    "spi_miso": 33,
    "spi_sclk": 34,
    "ldac_a": 35,
    **{f"d1b[{bit}]": pin for bit, pin in enumerate(
        (56, 57, 58, 59, 60, 61, 62, 63, 65, 67, 68, 69, 70, 71)
    )},
    "d1clk": 73,
    "sym_a1": 84,
    "sym_a0": 85,
}


def locate_map(text: str) -> dict[str, int]:
    return {
        name: int(pin)
        for name, pin in re.findall(
            r'LOCATE\s+COMP\s+"([^"]+)"\s+SITE\s+"(\d+)"', text
        )
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gate12_contract_cannot_program_or_power_hardware() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert spec["status"] == "digital_control_path_verified_scope_limited"
    assert spec["allowed_operations"] == ["diamond_build", "readback_or_program_status"]
    assert "sram_fast_program" in spec["forbidden_operations"]
    assert "enable_analog_output" in spec["forbidden_operations"]
    candidate = spec["sram_program_candidate"]
    assert candidate["status"] == "executed"
    assert candidate["approved"] is False
    assert candidate["approval_executions_used"] == candidate["approval_max_executions"] == 1


def test_pinned_gate12_candidate_matches_every_recorded_hash() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    candidate = spec["sram_program_candidate"]
    directory = ROOT.parent / candidate["directory"]
    assert sha256(directory / candidate["bitstream"]) == candidate["bitstream_sha256"]
    assert (directory / candidate["bitstream"]).stat().st_size == candidate["bitstream_bytes"]
    for name, digest in candidate["source_sha256"].items():
        assert sha256(ROOT / "gate12_pru_dac1" / name) == digest
    for name, digest in candidate["companion_sha256"].items():
        assert sha256(directory / name) == digest
    log = (directory / "build.log").read_text(encoding="utf-8", errors="replace")
    assert "No signal is selected as Global Set/Reset." in log
    assert "GSR instance connected to net" not in log


def test_combined_overlay_has_exact_gpio_spi_and_pru_pinmux() -> None:
    text = OVERLAY.read_text(encoding="utf-8")
    for row in (
        "0x15c 0x07 0x00",  # P9_17 GPIO0_5 CS
        "0x158 0x07 0x00",  # P9_18 GPIO0_4 MOSI
        "0x154 0x27 0x00",  # P9_21 GPIO0_3 MISO input
        "0x150 0x07 0x00",  # P9_22 GPIO0_2 SCLK
        "0x034 0x0e 0x00",  # P8_11 PRU R30.15
        "0x030 0x0e 0x00",  # P8_12 PRU R30.14
        "0x190 0x0d 0x00",  # P9_31 PRU R30.0
    ):
        assert text.count(row) == 1
    assert '&spi0 {\n    status = "disabled";' in text
    assert "&gpio0 {" in text
    assert "&pruss {" in text


def test_overlay_preparer_is_nonactivating_by_default() -> None:
    text = PREPARER.read_text(encoding="utf-8")
    assert 'if not args.activate:' in text
    assert 'print("NO BOOT FILE CHANGE; NO REBOOT")' in text
    assert 'CONFIRM_TOKEN = "APPLY_GATE12_OVERLAY"' in text
    assert "/boot/uEnv.txt.pre-gate12" in text
    assert "if args.reboot and not args.activate:" in text


def test_gate12_has_only_reviewed_channel_a_pins() -> None:
    assert locate_map(LPF.read_text(encoding="utf-8")) == EXPECTED_PINS
    hdl = HDL.read_text(encoding="utf-8").lower()
    assert "d2b" not in hdl
    assert "d2clk" not in hdl
    assert "rf2" not in hdl


def test_gate12_starts_all_codes_at_zero_and_preserves_gate11_spi_shape() -> None:
    text = HDL.read_text(encoding="utf-8")
    for index in range(4):
        assert f"code{index} = 14'h0000;" in text
    assert "always @(posedge spi_sclk)" in text
    assert "posedge spi_sclk or posedge spi_cs_n" not in text
    assert "rx_shift <= {rx_shift[22:0], spi_mosi};" in text
    assert "tx_word[23 - bit_count]" in text
    assert "d1clk = ldac_a" in text


def test_pru_finite_run_orders_select_settle_update_and_gap() -> None:
    text = PRU.read_text(encoding="utf-8")
    run = text[text.index("case 0x52:"):text.index("case 0x53:")]
    positions = [
        run.index("selected = rtase_symbol"),
        run.index("__R30 = (__R30 & ~RTASE_OUTPUTS)"),
        run.index("pru_delay(settle_cycles)"),
        run.index("__R30 |= RTASE_UPDATE"),
        run.index("pru_delay(pulse_cycles)"),
        run.index("__R30 &= ~RTASE_UPDATE"),
        run.index("rtase_delay_long(gap_cycles)"),
    ]
    assert positions == sorted(positions)


def test_pru_and_spi_commands_share_one_control_lock() -> None:
    for source in (RTASE_HOST, SPI_HOST):
        text = source.read_text(encoding="utf-8")
        assert 'CONTROL_LOCK = "/run/lock/rtase-control.lock"' in text
        assert "with control_lock():" in text
        assert "fcntl.LOCK_EX" in text
    service = PRU_SERVICE.read_text(encoding="utf-8")
    assert ": >/run/lock/rtase-control.lock" in service
    assert "chmod 0666 /run/lock/rtase-control.lock" in service
