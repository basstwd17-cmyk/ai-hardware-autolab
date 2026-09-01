"""Scenario intake chooses reviewed contracts without asking a model."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.agents.schema import load_all  # noqa: E402
from QKD_PROJ.orchestrator.control import ControlError, ControlPlane  # noqa: E402
from QKD_PROJ.orchestrator.jobs import JobLedger  # noqa: E402
from QKD_PROJ.orchestrator.scenario_preflight import (  # noqa: E402
    CONTRACT_GAP,
    HARDWARE,
    OFFLINE,
    PreflightError,
    ScenarioPreflight,
)


def write_contract(
    directory: Path,
    name: str,
    *,
    test_id: str,
    status: str = "ready",
    purpose: list[str] | None = None,
    operations: list[str] | None = None,
    extra: str = "",
) -> Path:
    path = directory / name
    purpose_lines = "\n".join(f"  - {line}" for line in (purpose or [])) or "  - test"
    operation_list = ", ".join(operations or ["diamond_build"])
    path.write_text(
        f"""schema_version: 1
test_id: {test_id}
status: {status}
purpose:
{purpose_lines}
allowed_operations: [{operation_list}]
pass_criteria: {{errors: 0}}
{extra}
""",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def engine(tmp_path: Path) -> ScenarioPreflight:
    write_contract(
        tmp_path,
        "gate12-pru-dac1-plan.yaml",
        test_id="gate12-pru-dac1-plan",
        purpose=["Build and verify the PRU to DAC1 digital control path."],
        operations=["diamond_build", "readback_or_program_status", "sram_fast_program"],
        extra="test_design: {top: gate12_pru_dac1}\n",
    )
    write_contract(
        tmp_path,
        "gate10-spi-clocked-return.yaml",
        test_id="gate10-spi-clocked-return",
        purpose=["Measure an SPI clocked return with the oscilloscope."],
        operations=["diamond_build", "oscilloscope_capture", "sram_fast_program"],
        extra="scope_capture: {instrument: RTM3004}\n",
    )
    write_contract(
        tmp_path,
        "gate4-spi-loopback.yaml",
        test_id="gate4-spi-loopback",
        purpose=["Build and verify an SPI loopback path."],
        operations=["diamond_build", "sram_fast_program"],
    )
    write_contract(
        tmp_path,
        "gate9-bbb-host-tooling.yaml",
        test_id="gate9-bbb-host-tooling",
        purpose=["Run reviewed BeagleBone Black host diagnostics."],
        operations=["bbb_ssh_command", "pru_deploy"],
    )
    return ScenarioPreflight(tmp_path, load_all())


def test_an_explicit_gate_and_offline_boundary_select_one_contract(engine: ScenarioPreflight):
    preview = engine.inspect("Gate12 PRU DAC1 Verilog을 보드 없이 빌드만 해줘")

    assert preview["status"] == "ready"
    assert preview["execution_mode"] == OFFLINE
    assert preview["approval_required"] is False
    assert preview["selected_contract"]["name"] == "gate12-pru-dac1-plan.yaml"
    assert "design-builder" in preview["roles"]
    assert "build-cpld" in preview["skills"]
    assert "bitstream" in preview["expected_artifacts"]


def test_hardware_measurement_is_previewed_but_requires_approval(engine: ScenarioPreflight):
    preview = engine.inspect("Gate10 SPI clocked return을 오실로스코프로 측정해줘")

    assert preview["status"] == "ready"
    assert preview["execution_mode"] == HARDWARE
    assert preview["approval_required"] is True
    assert "measurement" in preview["roles"]
    assert "scope-measure" in preview["skills"]
    assert "scope_capture" in preview["expected_artifacts"]


def test_an_ambiguous_spi_request_is_a_contract_gap(engine: ScenarioPreflight):
    preview = engine.inspect("SPI 경로를 테스트해줘")

    assert preview["status"] == CONTRACT_GAP, [
        item["matched_terms"] for item in preview["candidates"]
    ]
    assert preview["selected_contract"] is None
    assert preview["reason_code"] in ("ambiguous_contract", "no_contract_match")
    assert len(preview["candidates"]) >= 2
    if preview["reason_code"] == "ambiguous_contract":
        assert "derive one scenario-specific contract" in preview["detail"]
        assert "choose a gate" not in preview["detail"]


def test_generic_build_words_cannot_select_an_unrelated_hardware_contract(
    engine: ScenarioPreflight,
):
    preview = engine.inspect(
        "Without hardware, build and verify an STM32G474 HRTIM firmware simulation."
    )

    assert preview["status"] == CONTRACT_GAP, [
        item["matched_terms"] for item in preview["candidates"]
    ]
    assert preview["reason_code"] == "no_contract_match"
    assert preview["selected_contract"] is None


def test_a_gate_without_an_execution_boundary_does_not_guess(engine: ScenarioPreflight):
    preview = engine.inspect("Gate12를 계속 진행해줘")

    assert preview["status"] == CONTRACT_GAP
    assert preview["selected_contract"]["name"] == "gate12-pru-dac1-plan.yaml"
    assert preview["reason_code"] == "execution_scope_unclear"


def test_start_digest_detects_a_contract_changed_after_preview(
    engine: ScenarioPreflight, tmp_path: Path
):
    preview = engine.inspect("Gate12를 보드 없이 빌드만 해줘")
    selected = preview["selected_contract"]
    engine.verify_contract(selected["name"], selected["sha256"])

    path = tmp_path / selected["name"]
    path.write_text(path.read_text(encoding="utf-8") + "\nchanged: true\n", encoding="utf-8")
    with pytest.raises(PreflightError, match="changed after preflight"):
        engine.verify_contract(selected["name"], selected["sha256"])


def test_control_starts_only_the_contract_that_was_previewed(
    engine: ScenarioPreflight, tmp_path: Path
):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine)
    preview = control.preflight("Gate12를 보드 없이 빌드만 해줘")
    selected = preview["selected_contract"]

    answer = control.start(
        selected["name"],
        preview["objective"],
        preview["scenario"],
        selected["sha256"],
        preview["scenario_sha256"],
    )

    job = ledger.job(answer["job"]["job_id"])
    assert job.gate == selected["name"]
    brief = ledger.handoffs(job.job_id, "Brief", 0)[0].payload
    assert brief["resources"] == {
        "selected_contract": selected,
        "execution_mode": preview["execution_mode"],
        "approval_required": preview["approval_required"],
        "roles": preview["roles"],
        "skills": preview["skills"],
        "expected_artifacts": preview["expected_artifacts"],
    }


def test_control_refuses_scenario_text_changed_after_preview(
    engine: ScenarioPreflight, tmp_path: Path
):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine)
    preview = control.preflight("Gate12를 보드 없이 빌드만 해줘")
    selected = preview["selected_contract"]

    with pytest.raises(ControlError, match="scenario changed after preflight"):
        control.start(
            selected["name"],
            preview["objective"],
            preview["scenario"] + " 그리고 보드도 프로그램해줘",
            selected["sha256"],
            preview["scenario_sha256"],
        )
    assert ledger.jobs() == []


def test_a_contract_status_substring_does_not_override_operation_approval(tmp_path: Path):
    write_contract(
        tmp_path,
        "gate1-test-spec.yaml",
        test_id="gate1-test-spec",
        status="blocked_pending_human_review",
        purpose=["Build the Gate1 design."],
    )
    preview = ScenarioPreflight(tmp_path, load_all()).inspect("Gate1을 빌드만 해줘")

    assert preview["status"] == "ready"
    assert preview["selected_contract"]["status"] == "blocked_pending_human_review"


def test_an_exact_filename_beats_sibling_contracts_with_the_same_gate(tmp_path: Path):
    for name in (
        "gate3-rf1-demo.yaml",
        "gate3-rf1-spi-demo.yaml",
        "gate3-spi-path-diagnostic.yaml",
    ):
        write_contract(tmp_path, name, test_id=Path(name).stem)
    preview = ScenarioPreflight(tmp_path, load_all()).inspect(
        "Follow gate3-rf1-demo.yaml exactly and build it offline"
    )

    assert preview["status"] == "ready"
    assert preview["reason_code"] == "selected_explicitly"
    assert preview["selected_contract"]["name"] == "gate3-rf1-demo.yaml"


def test_start_rejects_a_different_contract_even_with_its_valid_digest(
    engine: ScenarioPreflight, tmp_path: Path
):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine)
    preview = control.preflight("Gate12를 보드 없이 빌드만 해줘")
    other = control.preflight("Gate4를 보드 없이 빌드만 해줘")["selected_contract"]

    with pytest.raises(ControlError, match="does not match preflight selection"):
        control.start(
            other["name"],
            preview["objective"],
            preview["scenario"],
            other["sha256"],
            preview["scenario_sha256"],
        )
    assert ledger.jobs() == []


def test_start_rejects_a_scenario_that_preflight_did_not_make_ready(
    engine: ScenarioPreflight, tmp_path: Path
):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine)
    scenario = "SPI 경로를 테스트해줘"
    preview = control.preflight(scenario)
    candidate = preview["candidates"][0]

    with pytest.raises(ControlError, match="scenario preflight is contract_gap"):
        control.start(
            candidate["name"],
            preview["objective"],
            scenario,
            candidate["sha256"],
            preview["scenario_sha256"],
        )
    assert ledger.jobs() == []


def test_start_without_preflight_digests_is_refused_before_job_creation(
    engine: ScenarioPreflight, tmp_path: Path
):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine)

    with pytest.raises(ControlError, match="requires contract_sha256 and scenario_sha256"):
        control.start(
            "nonexistent-contract.yaml",
            "Do it",
            "Gate12를 보드 없이 빌드만 해줘",
        )
    assert ledger.jobs() == []
