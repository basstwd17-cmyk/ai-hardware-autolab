"""Tests for layer 2: the bundle, the ledger and the Gate 1 state machine.

No Diamond, no cable, no instrument. The layer 3 tools are injected as fakes,
which is the whole reason StepTools exists -- the ordering and permission logic
has to be testable without a board, because it is the part that decides whether
the board gets touched at all.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.hwtools import (  # noqa: E402
    Contracts,
    FAIL,
    PASS,
    RunRecord,
    SafetyGate,
    ToolResult,
)
from ai_hardware_autolab.orchestrator import (  # noqa: E402
    BundleError,
    ConsumptionLedger,
    Gate1Sequence,
    Gate1State,
    LedgerError,
    SequenceError,
    StepTools,
    check_against_contract,
    load_bundle,
    migrate_json_ledger,
    resume_sequence,
)

TODAY = date(2026, 8, 18)
PINNED_BITSTREAM = "c656c41ab34df655fb1d638d4af25439ff45fd3f70a73692fad3519085463910"


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def unapproved_gate() -> SafetyGate:
    """The contract exactly as it stands: Approval 2 not signed."""
    return SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=True)


@pytest.fixture
def gate() -> SafetyGate:
    """A gate with hardware enabled and Approval 2 signed.

    Signed in memory, never in the file -- the real contract must stay
    unapproved until a human signs it. Most of these tests are about ordering
    and budget, which only become reachable once approval exists.
    """
    contracts = Contracts.load("gate1-test-spec.yaml")
    candidate = contracts.test_spec["sram_program_candidate"]
    # Set every approval field explicitly rather than patching whichever ones
    # currently differ. The real contract moves as the gates are worked
    # through, and these tests are about the state machine, not about what the
    # board happens to have been approved for today.
    candidate["approved"] = True
    candidate["status"] = "approved"
    candidate["approved_by"] = "tester"
    candidate["approved_on"] = "2026-08-18"
    candidate["approval_max_executions"] = 1
    candidate["approval_executions_used"] = 0
    candidate.pop("execution_run", None)
    return SafetyGate(contracts, allow_hardware=True)


@pytest.fixture
def xcf_files(tmp_path: Path) -> dict[str, Path]:
    scan = tmp_path / "scan-only.xcf"
    scan.write_text("<ispXCF>scan</ispXCF>", encoding="utf-8")
    program = tmp_path / "program.xcf"
    program.write_text("<ispXCF>program</ispXCF>", encoding="utf-8")
    return {"scan": scan, "program": program}


def digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_doc(
    xcf_files: dict[str, Path],
    *,
    steps: tuple[str, ...] = ("scan",),
    max_executions: int = 3,
    bitstream: str = PINNED_BITSTREAM,
) -> dict:
    blocks = {
        "scan": {
            "step": "scan",
            "operation": "jtag_scan",
            "max_executions": max_executions,
            "artifacts": {"scan_xcf_sha256": digest(xcf_files["scan"])},
            "params": {"files": {"scan_xcf": str(xcf_files["scan"])}},
        },
        "program": {
            "step": "program",
            "operation": "sram_fast_program",
            "max_executions": max_executions,
            "artifacts": {
                "program_xcf_sha256": digest(xcf_files["program"]),
                "bitstream_sha256": bitstream,
            },
            "params": {"files": {"program_xcf": str(xcf_files["program"])}},
        },
        "measure": {
            "step": "measure",
            "operation": "oscilloscope_capture",
            "max_executions": max_executions,
            "artifacts": {},
            "params": {"channel": "CH3"},
        },
    }
    return {
        "schema_version": 1,
        "bundle_id": "gate1-test",
        "test_spec": "gate1-test-spec.yaml",
        "approved_by": "tester",
        "issued_on": "2026-08-18",
        "expires_on": "2026-08-25",
        "steps": [blocks[s] for s in steps],
    }


def write_bundle(tmp_path: Path, doc: dict, name: str = "bundle.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def fake_result(action: str, verdict: str = PASS, ok: bool = True) -> ToolResult:
    result = ToolResult(tool="cpld", action=action, ok=ok)
    result.verdict = verdict
    if not ok:
        result.error = f"{action} failed"
    return result


def passing_tools() -> StepTools:
    """Fakes that record themselves, exactly as every hwtools tool does.

    Layer 2 deliberately does not call record.add(); each tool's own _finish()
    already does. Fakes that skipped it would leave the orchestrator's manifest
    empty here and full in production, which is the wrong way round for a test.
    """
    return StepTools(
        scan=lambda gate, record, **kw: record.add(fake_result("jtag_scan")),
        program=lambda gate, scan_result, **kw: kw["record"].add(
            fake_result("program_sram")
        ),
        measure=lambda gate, record, **kw: record.add(fake_result("gate1_gpio")),
    )


def build(
    tmp_path: Path,
    gate: SafetyGate,
    bundle_path: Path,
    tools: StepTools,
    *,
    run: str = "a",
) -> Gate1Sequence:
    # `run` separates the run directories while the ledger stays shared, which
    # is what makes "a second sequence under the same bundle" testable.
    return Gate1Sequence(
        gate=gate,
        bundle=load_bundle(bundle_path, today=TODAY),
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / run),
        tools=tools,
    )


# -- bundle loading ---------------------------------------------------------


def test_valid_bundle_loads(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    bundle = load_bundle(path, today=TODAY)
    assert bundle.bundle_id == "gate1-test"
    assert bundle.authorises("scan")
    assert not bundle.authorises("program")
    assert len(bundle.sha256) == 64


def test_expired_bundle_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    with pytest.raises(BundleError, match="expired"):
        load_bundle(path, today=date(2026, 9, 1))


def test_bundle_not_yet_valid_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    with pytest.raises(BundleError, match="not valid until"):
        load_bundle(path, today=date(2026, 8, 1))


def test_unknown_schema_version_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["schema_version"] = 2
    with pytest.raises(BundleError, match="schema_version"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_unsigned_bundle_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["approved_by"] = ""
    with pytest.raises(BundleError, match="approved_by"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_program_without_scan_cannot_be_authorised(
    tmp_path: Path, xcf_files: dict[str, Path]
) -> None:
    """A signature must not be able to grant a shortcut past the scan."""
    doc = bundle_doc(xcf_files, steps=("program",))
    with pytest.raises(BundleError, match="out of order or skip"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_steps_out_of_order_are_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    doc["steps"].reverse()
    with pytest.raises(BundleError, match="out of order"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_duplicate_step_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["steps"].append(dict(doc["steps"][0]))
    with pytest.raises(BundleError, match="more than once"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_step_cannot_rename_its_operation(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["steps"][0]["operation"] = "diamond_build"
    with pytest.raises(BundleError, match="always"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_zero_executions_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files, max_executions=0)
    with pytest.raises(BundleError, match="max_executions"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_missing_required_digest_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["steps"][0]["artifacts"] = {}
    with pytest.raises(BundleError, match="must pin"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_malformed_digest_is_refused(tmp_path: Path, xcf_files: dict[str, Path]) -> None:
    doc = bundle_doc(xcf_files)
    doc["steps"][0]["artifacts"]["scan_xcf_sha256"] = "not-a-hash"
    with pytest.raises(BundleError, match="64-hex"):
        load_bundle(write_bundle(tmp_path, doc), today=TODAY)


def test_shipped_template_does_not_load() -> None:
    """A template must never be usable as an approval."""
    template = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "bundles"
        / "gate1-bundle.template.yaml"
    )
    assert template.is_file()
    with pytest.raises(BundleError):
        load_bundle(template, today=TODAY)


# -- bundle vs contract -----------------------------------------------------


def test_bundle_matching_the_contract_has_no_problems(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    bundle = load_bundle(
        write_bundle(tmp_path, bundle_doc(xcf_files, steps=("scan", "program"))),
        today=TODAY,
    )
    assert check_against_contract(bundle, gate) == []


def test_bundle_naming_a_different_bitstream_is_caught(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"), bitstream="a" * 64)
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY)
    problems = check_against_contract(bundle, gate)
    assert any("pins" in p for p in problems)


def test_program_step_needs_approval_2_before_anything_runs(
    tmp_path: Path, unapproved_gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Caught at verify time, not after a scan has already been spent.

    program_cpld_sram() refuses this too, and its refusal is the one that
    counts. Repeating it here only moves the refusal earlier.
    """
    candidate = unapproved_gate.contracts.test_spec["sram_program_candidate"]
    candidate["approved"] = False
    candidate["status"] = "pinned_not_approved"
    candidate["approval_max_executions"] = 1
    candidate["approval_executions_used"] = 0
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY)
    problems = check_against_contract(bundle, unapproved_gate)
    assert any("not approved" in p for p in problems)

    seq = build(tmp_path, unapproved_gate, write_bundle(tmp_path, doc), passing_tools())
    assert seq.run() is Gate1State.BLOCKED
    assert seq.ledger.spent(seq.bundle.sha256, "scan") == 0
    assert "scan" not in seq.results


def test_spent_approval_says_spent_not_unsigned(
    tmp_path: Path, unapproved_gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A consumed approval and an unsigned one both leave approved=false.

    Only one of them is fixed by signing, so they must not read the same.
    """
    candidate = unapproved_gate.contracts.test_spec["sram_program_candidate"]
    candidate["status"] = "executed"
    candidate["approval_max_executions"] = 1
    candidate["approval_executions_used"] = 1
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY)
    problems = check_against_contract(bundle, unapproved_gate)
    assert any("spent" in p for p in problems)
    assert not any("Sign Approval 2 first" in p for p in problems)


def test_unsigned_approval_says_unsigned(
    tmp_path: Path, unapproved_gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    candidate = unapproved_gate.contracts.test_spec["sram_program_candidate"]
    candidate["status"] = "pinned_not_approved"
    candidate["approved"] = False
    candidate["approval_max_executions"] = 1
    candidate["approval_executions_used"] = 0
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY)
    assert any(
        "Sign Approval 2 first" in p
        for p in check_against_contract(bundle, unapproved_gate)
    )


def test_scan_only_bundle_is_fine_without_approval_2(
    tmp_path: Path, unapproved_gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Approval 1 does not wait on Approval 2."""
    bundle = load_bundle(write_bundle(tmp_path, bundle_doc(xcf_files)), today=TODAY)
    assert check_against_contract(bundle, unapproved_gate) == []


def test_bundle_for_another_test_spec_is_caught(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files)
    doc["test_spec"] = "gate2-test-spec.yaml"
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY)
    assert any("written against" in p for p in check_against_contract(bundle, gate))


# -- state machine ----------------------------------------------------------


def test_documents_only_gate_blocks_everything(
    tmp_path: Path, xcf_files: dict[str, Path]
) -> None:
    gate = SafetyGate(Contracts.load("gate1-test-spec.yaml"), allow_hardware=False)
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    assert seq.verify() is Gate1State.BLOCKED
    assert "documents-only" in seq.blockers[0]


def test_verify_rejects_a_substituted_file(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """The digest, not the path, is what the signature covers."""
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    xcf_files["scan"].write_text("<ispXCF>tampered</ispXCF>", encoding="utf-8")
    seq = build(tmp_path, gate, path, passing_tools())
    assert seq.verify() is Gate1State.BLOCKED
    assert "different file" in seq.blockers[0]


def test_scan_only_bundle_completes_after_scan(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A bundle that stops after scan is not a failure. It ends where it ends."""
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    assert seq.run() is Gate1State.COMPLETE
    assert "scan" in seq.results
    assert "program" not in seq.results
    assert not seq.safe_off_required


def test_full_bundle_runs_all_three_steps(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), passing_tools())
    assert seq.run() is Gate1State.COMPLETE
    assert set(seq.results) == {"scan", "program", "measure"}
    assert [t.to for t in seq.transitions][:4] == [
        Gate1State.READY,
        Gate1State.SCANNED,
        Gate1State.PROGRAMMED,
        Gate1State.MEASURED,
    ]


def test_failing_scan_aborts_and_never_programs(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    tools = passing_tools()
    tools.scan = lambda gate, record, **kw: fake_result("jtag_scan", FAIL, ok=False)
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    assert seq.run() is Gate1State.ABORTED
    assert "program" not in seq.results
    assert seq.safe_off_required


def test_a_raising_tool_still_spends_its_attempt(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    def explode(gate, record, **kw):
        raise RuntimeError("cable fell out")

    tools = passing_tools()
    tools.scan = explode
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), tools)
    assert seq.run() is Gate1State.ABORTED
    assert seq.ledger.spent(seq.bundle.sha256, "scan") == 1
    assert "cable fell out" in seq.transitions[-1].reason


def test_program_cannot_start_before_scan(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), passing_tools())
    seq.verify()
    with pytest.raises(SequenceError, match="only start from SCANNED"):
        seq.program()


def test_program_receives_the_scan_result(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Layer 3 re-checks the scan itself; layer 2 must actually hand it over."""
    seen: dict[str, object] = {}

    def program(gate, scan_result, **kw):
        seen["scan_result"] = scan_result
        seen["program_xcf"] = kw["program_xcf"]
        return fake_result("program_sram")

    tools = passing_tools()
    tools.program = program
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    seq.run()
    assert seen["scan_result"] is seq.results["scan"]
    assert Path(seen["program_xcf"]) == xcf_files["program"]


def test_missing_measure_tool_blocks_rather_than_aborts(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Nothing was touched, so this is BLOCKED, not ABORTED."""
    tools = passing_tools()
    tools.measure = None
    doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    assert seq.run() is Gate1State.BLOCKED
    assert "not implemented" in seq.blockers[-1]
    assert not seq.safe_off_required


def test_exhausted_budget_blocks_the_step(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, max_executions=1)
    path = write_bundle(tmp_path, doc)
    first = build(tmp_path, gate, path, passing_tools(), run="first")
    assert first.run() is Gate1State.COMPLETE

    second = build(tmp_path, gate, path, passing_tools(), run="second")
    assert second.run() is Gate1State.BLOCKED
    # Caught in preflight, before the step is even attempted.
    assert "authorised execution" in second.blockers[-1]
    assert not second.preflight_report.ok
    assert [c.name for c in second.preflight_report.blocking] == ["budget_scan"]
    assert "scan" not in second.results


def test_safe_off_reports_manual_actions_only(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Layer 2 has no power control, so it must not claim to have used any."""
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    actions = seq.safe_off()
    assert any("bench supply" in a for a in actions)
    assert any("CN1" in a for a in actions)
    assert seq.summary()["safe_off_required"] is True


def test_sequence_json_is_written_on_every_transition(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    seq.run()
    written = json.loads((seq.record.dir / "sequence.json").read_text(encoding="utf-8"))
    assert written["state"] == "COMPLETE"
    assert written["bundle"]["bundle_sha256"] == seq.bundle.sha256
    assert written["ledger"]["steps"]["scan"]["attempts"] == 1
    assert [t["to"] for t in written["transitions"]] == ["READY", "SCANNED", "COMPLETE"]


def test_each_step_is_recorded_exactly_once(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Layer 3 tools record themselves; layer 2 must not record them again.

    Every hwtools _finish() calls record.add(). A second add() here would put
    the same ToolResult in manifest.steps twice, which makes a run look like it
    ran each step twice.
    """
    doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), passing_tools())
    assert seq.run() is Gate1State.COMPLETE

    manifest = json.loads((seq.record.dir / "manifest.json").read_text(encoding="utf-8"))
    actions = [s["action"] for s in manifest["steps"]]
    assert actions == ["jtag_scan", "program_sram", "gate1_gpio"]


# -- preflight --------------------------------------------------------------


def test_preflight_reports_everything_at_once(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """One pass should surface every fixable problem, not just the first."""
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    doc["test_spec"] = "gate2-test-spec.yaml"
    path = write_bundle(tmp_path, doc)
    xcf_files["scan"].write_text("<ispXCF>tampered</ispXCF>", encoding="utf-8")

    seq = build(tmp_path, gate, path, passing_tools())
    report = seq.preflight()
    failed = {c.name for c in report.blocking}
    assert {"bundle_matches_contract", "artifacts_match_digests"} <= failed
    assert not report.ok
    assert seq.state is Gate1State.INIT  # preflight changes nothing


def test_preflight_does_not_spend_the_budget(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    seq.preflight()
    seq.preflight()
    assert seq.ledger.spent(seq.bundle.sha256, "scan") == 0


def test_missing_later_tool_blocks_the_whole_sequence(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Nothing runs when a later step cannot.

    `program` carries a one-execution approval. Starting a run whose
    measurement cannot happen would spend that approval and leave the board
    programmed with nothing measured, and the approval does not come back.
    """
    tools = passing_tools()
    tools.measure = None
    doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    report = seq.preflight()
    assert not report.ok
    assert [c.name for c in report.blocking] == ["tool_measure"]

    assert seq.run() is Gate1State.BLOCKED
    assert seq.results == {}
    assert seq.ledger.spent(seq.bundle.sha256, "scan") == 0
    assert seq.ledger.spent(seq.bundle.sha256, "program") == 0


def test_later_step_budget_exhaustion_also_blocks(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    path = write_bundle(tmp_path, doc)
    seq = build(tmp_path, gate, path, passing_tools())
    seq.ledger.reserve(seq.bundle.sha256, "program", 3)
    seq.ledger.reserve(seq.bundle.sha256, "program", 3)
    seq.ledger.reserve(seq.bundle.sha256, "program", 3)
    for entry in seq.ledger.dangling(seq.bundle.sha256):
        seq.ledger._record_outcome(entry["entry_id"], verdict=PASS, run="earlier")

    second = build(tmp_path, gate, path, passing_tools(), run="second")
    report = second.preflight()
    assert not report.ok
    assert [c.name for c in report.blocking] == ["budget_program"]
    assert second.run() is Gate1State.BLOCKED
    assert second.ledger.spent(second.bundle.sha256, "scan") == 0


def test_missing_first_tool_blocks(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    tools = passing_tools()
    tools.scan = None
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), tools)
    report = seq.preflight()
    assert not report.ok
    assert "tool_scan" in {c.name for c in report.blocking}


def test_preflight_surfaces_contract_stop_conditions(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    stops = [c for c in seq.preflight().checks if c.name == "stop_conditions"]
    assert stops and "IDCODE" in stops[0].detail


def test_dangling_attempt_blocks_the_next_run(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """An attempt that was never resolved means a tool ran and nobody knows how."""
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    seq = build(tmp_path, gate, path, passing_tools())
    seq.ledger.reserve(seq.bundle.sha256, "scan", 3)  # reserved, never completed

    second = build(tmp_path, gate, path, passing_tools(), run="second")
    assert second.run() is Gate1State.BLOCKED
    assert "no_dangling_attempts" in {c.name for c in second.preflight_report.blocking}
    assert "unknown from here" in second.blockers[0]


def test_the_bundle_text_is_preserved_in_the_run(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A digest proves a bundle changed; only the text says what it authorised."""
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    seq = build(tmp_path, gate, path, passing_tools())
    seq.run()

    copied = seq.record.dir / seq.bundle_copy
    assert copied.is_file()
    assert copied.read_bytes() == path.read_bytes()

    # The run stays readable after the original is edited out of existence.
    path.unlink()
    written = json.loads((seq.record.dir / "sequence.json").read_text(encoding="utf-8"))
    assert written["bundle_copy"] == copied.name
    assert written["bundle"]["bundle_sha256"] == seq.bundle.sha256
    assert yaml.safe_load(copied.read_text(encoding="utf-8"))["bundle_id"] == "gate1-test"


# -- signatures -------------------------------------------------------------


class _StubVerifier:
    """Stands in for a real scheme. Signature is valid when it equals the payload."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.payloads: list[bytes] = []

    def verify(self, payload: bytes, signature: dict) -> bool:
        self.payloads.append(payload)
        return self.accept


def test_unsigned_bundle_is_advisory_when_attended(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A person at the bench is the control, so this is reported, not refused."""
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    report = seq.preflight()
    assert report.ok
    signature = [c for c in report.checks if c.name == "bundle_signature"][0]
    assert not signature.ok
    assert "name typed into a file" in signature.detail


def test_unsigned_bundle_blocks_unattended_operation(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    report = seq.preflight(require_signature=True)
    assert not report.ok
    assert "bundle_signature" in {c.name for c in report.blocking}


def test_load_bundle_can_require_a_signature(
    tmp_path: Path, xcf_files: dict[str, Path]
) -> None:
    path = write_bundle(tmp_path, bundle_doc(xcf_files))
    with pytest.raises(BundleError, match="not cryptographically signed"):
        load_bundle(path, today=TODAY, require_signature=True)


def test_a_signature_that_does_not_verify_is_refused(
    tmp_path: Path, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files)
    doc["signature"] = {"algorithm": "stub", "key_id": "k1", "value": "nope"}
    with pytest.raises(BundleError, match="does not verify"):
        load_bundle(
            write_bundle(tmp_path, doc), today=TODAY, verifier=_StubVerifier(accept=False)
        )


def test_signature_covers_the_bundle_but_not_itself(
    tmp_path: Path, xcf_files: dict[str, Path]
) -> None:
    """A signature cannot sign itself, and must not depend on YAML formatting."""
    doc = bundle_doc(xcf_files)
    doc["signature"] = {"algorithm": "stub", "key_id": "k1", "value": "x"}
    verifier = _StubVerifier()
    bundle = load_bundle(write_bundle(tmp_path, doc), today=TODAY, verifier=verifier)
    assert bundle.signature_verified
    payload = verifier.payloads[0]
    signed = json.loads(payload)
    assert "signature" not in signed
    assert signed["bundle_id"] == "gate1-test"
    assert signed["steps"][0]["step"] == "scan"

    # Same meaning, different YAML formatting -> same signed bytes.
    reordered = {k: doc[k] for k in reversed(list(doc))}
    other = tmp_path / "reordered.yaml"
    other.write_text(yaml.safe_dump(reordered, sort_keys=True), encoding="utf-8")
    again = _StubVerifier()
    load_bundle(other, today=TODAY, verifier=again)
    assert again.payloads[0] == payload


def test_verified_signature_satisfies_unattended_preflight(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files)
    doc["signature"] = {"algorithm": "stub", "key_id": "k1", "value": "x"}
    path = write_bundle(tmp_path, doc)
    seq = Gate1Sequence(
        gate=gate,
        bundle=load_bundle(path, today=TODAY, verifier=_StubVerifier()),
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "signed"),
        tools=passing_tools(),
    )
    assert seq.preflight(require_signature=True).ok
    assert seq.summary()["bundle"]["signature_verified"] is True


# -- ledger transactions ----------------------------------------------------


def test_ledger_spends_before_the_step_runs_survives_reopen(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(tmp_path / "ledger.db")
    reservation = ledger.reserve("a" * 64, "scan", 2)
    # Committed before the tool is called, so a crash here still spent it.
    assert ConsumptionLedger(tmp_path / "ledger.db").spent("a" * 64, "scan") == 1
    reservation.complete(verdict=PASS, run="run-1")
    reopened = ConsumptionLedger(tmp_path / "ledger.db")
    assert reopened.entries("a" * 64, "scan")[0]["verdict"] == PASS
    assert reopened.dangling("a" * 64) == []


def test_second_connection_cannot_overspend(tmp_path: Path) -> None:
    """A count read in one process must not go stale against another's write."""
    first = ConsumptionLedger(tmp_path / "ledger.db")
    second = ConsumptionLedger(tmp_path / "ledger.db")
    first.reserve("a" * 64, "scan", 1)
    with pytest.raises(LedgerError, match="authorised executions"):
        second.reserve("a" * 64, "scan", 1)


def test_concurrent_reservers_produce_exactly_one_winner(tmp_path: Path) -> None:
    """The property the whole store exists for, exercised for real.

    Eight threads race for a single authorised execution. BEGIN IMMEDIATE plus
    UNIQUE (bundle_sha256, step, attempt) has to leave exactly one.
    """
    import threading

    path = tmp_path / "ledger.db"
    ConsumptionLedger(path)  # create the schema before the race
    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        ledger = ConsumptionLedger(path)
        barrier.wait()
        try:
            winners.append(ledger.reserve("a" * 64, "program", 1).entry_id)
        except LedgerError as exc:
            losers.append(str(exc))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"{len(winners)} threads reserved the same execution"
    assert len(losers) == 7
    assert ConsumptionLedger(path).spent("a" * 64, "program") == 1


def test_table_invariant_refuses_an_attempt_past_the_budget(tmp_path: Path) -> None:
    """The budget is enforced by the schema, not only by the count check."""
    import sqlite3

    path = tmp_path / "ledger.db"
    ConsumptionLedger(path)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO attempts (entry_id, bundle_sha256, step, attempt, "
                "of_max, reserved_at, reserved_by_pid) VALUES "
                "('x', 'a', 'program', 2, 1, 'now', 0)"
            )
    finally:
        conn.close()


def test_duplicate_attempt_number_is_refused_by_the_index(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "ledger.db"
    ledger = ConsumptionLedger(path)
    ledger.reserve("a" * 64, "scan", 3)
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO attempts (entry_id, bundle_sha256, step, attempt, "
                "of_max, reserved_at, reserved_by_pid) VALUES "
                "(?, ?, 'scan', 1, 3, 'now', 0)",
                ("different-id", "a" * 64),
            )
    finally:
        conn.close()


def test_failed_reservation_leaves_no_row(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(tmp_path / "ledger.db")
    ledger.reserve("a" * 64, "scan", 1)
    with pytest.raises(LedgerError):
        ledger.reserve("a" * 64, "scan", 1)
    assert ledger.spent("a" * 64, "scan") == 1


def test_budget_is_per_bundle_digest(tmp_path: Path) -> None:
    """A re-issued bundle is a new approval, so it gets a fresh budget."""
    ledger = ConsumptionLedger(tmp_path / "ledger.db")
    ledger.reserve("a" * 64, "scan", 1)
    ledger.reserve("b" * 64, "scan", 1)
    assert ledger.remaining("b" * 64, "scan", 1) == 0
    assert ledger.spent("a" * 64, "scan") == 1


def test_legacy_json_ledger_is_refused_not_reinterpreted(tmp_path: Path) -> None:
    """Reading it as an empty database would return every spent execution."""
    path = tmp_path / "old.json"
    path.write_text(
        json.dumps({"ledger_version": 1, "entries": []}), encoding="utf-8"
    )
    with pytest.raises(LedgerError, match="migrate_json_ledger"):
        ConsumptionLedger(path)


def test_json_ledger_migrates_without_losing_spent_executions(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps(
            {
                "ledger_version": 1,
                "entries": [
                    {
                        "entry_id": "abc:scan:1",
                        "bundle_sha256": "a" * 64,
                        "step": "scan",
                        "attempt": 1,
                        "of": 2,
                        "reserved_at": "2026-08-18T00:00:00+00:00",
                        "completed_at": "2026-08-18T00:00:01+00:00",
                        "verdict": PASS,
                        "run": "earlier",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    new = tmp_path / "ledger.db"
    assert migrate_json_ledger(old, new) == 1
    ledger = ConsumptionLedger(new)
    assert ledger.spent("a" * 64, "scan") == 1
    assert ledger.entries("a" * 64, "scan")[0]["verdict"] == PASS
    assert old.is_file()  # the old evidence is left exactly where it was


def test_foreign_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    path.write_text("not a ledger at all", encoding="utf-8")
    with pytest.raises(LedgerError, match="neither a SQLite database"):
        ConsumptionLedger(path)


# -- resume -----------------------------------------------------------------


def interrupted_after_program(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path], run: str = "first"
) -> Gate1Sequence:
    """A process that scanned and programmed, then ended before measuring.

    Driven step by step rather than through run(), which is exactly what an
    interrupted process leaves behind: PROGRAMMED, non-terminal, nothing
    dangling in the ledger.
    """
    doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), passing_tools(), run=run)
    assert seq.verify() is Gate1State.READY
    assert seq.scan() is Gate1State.SCANNED
    assert seq.program() is Gate1State.PROGRAMMED
    return seq


def test_resume_runs_only_measure_from_the_programmed_checkpoint(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """The reason resume exists: a program approval is granted for one run."""
    first = interrupted_after_program(tmp_path, gate, xcf_files)

    resumed = resume_sequence(
        gate=gate,
        bundle=first.bundle,
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "resumed"),
        resume_from=first.record.dir,
        tools=passing_tools(),
        power_maintained=True,
    )
    assert resumed.progress is Gate1State.PROGRAMMED
    assert set(resumed.results) == {"scan", "program"}
    assert resumed.run() is Gate1State.COMPLETE

    # The steps that already ran are not run again, so they cost nothing more.
    assert resumed.ledger.spent(first.bundle.sha256, "scan") == 1
    assert resumed.ledger.spent(first.bundle.sha256, "program") == 1
    assert resumed.ledger.spent(first.bundle.sha256, "measure") == 1


def test_resume_across_program_requires_power_to_be_stated(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """SRAM is volatile, so a cycled supply leaves an unconfigured chip.

    Measuring one produces a confidently wrong number rather than an error,
    which is why this is not allowed to default.
    """
    first = interrupted_after_program(tmp_path, gate, xcf_files)
    with pytest.raises(SequenceError, match="volatile"):
        resume_sequence(
            gate=gate,
            bundle=first.bundle,
            ledger=ConsumptionLedger(tmp_path / "ledger.db"),
            record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "r"),
            resume_from=first.record.dir,
            tools=passing_tools(),
        )


def test_resume_before_program_needs_no_power_statement(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Nothing volatile has been loaded yet, so there is nothing to lose."""
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    first = build(tmp_path, gate, write_bundle(tmp_path, doc), passing_tools())
    first.verify()
    assert first.scan() is Gate1State.SCANNED

    resumed = resume_sequence(
        gate=gate,
        bundle=first.bundle,
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "resumed"),
        resume_from=first.record.dir,
        tools=passing_tools(),
    )
    assert resumed.progress is Gate1State.SCANNED
    assert resumed.run() is Gate1State.COMPLETE
    assert resumed.ledger.spent(first.bundle.sha256, "scan") == 1


def test_resume_refuses_while_an_attempt_is_unresolved(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A reserved-but-unresolved attempt means a tool ran and nobody knows how."""
    first = interrupted_after_program(tmp_path, gate, xcf_files)
    first.ledger.reserve(first.bundle.sha256, "measure", 3)  # killed mid-step

    with pytest.raises(SequenceError, match="unresolved attempt"):
        resume_sequence(
            gate=gate,
            bundle=first.bundle,
            ledger=ConsumptionLedger(tmp_path / "ledger.db"),
            record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "r"),
            resume_from=first.record.dir,
            tools=passing_tools(),
            power_maintained=True,
        )


def test_resume_restores_the_scan_result_downstream_tools_check(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """program_cpld_sram() re-checks the scan, so it must survive the round trip."""
    scan = fake_result("jtag_scan")
    scan.measurements = {
        "device_count": 1,
        "idcode": "0x012BC043",
        "chain_verified": True,
    }
    tools = passing_tools()
    tools.scan = lambda gate, record, **kw: record.add(scan)
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    first = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    first.verify()
    assert first.scan() is Gate1State.SCANNED

    resumed = resume_sequence(
        gate=gate,
        bundle=first.bundle,
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "resumed"),
        resume_from=first.record.dir,
        tools=passing_tools(),
    )
    restored = resumed.results["scan"]
    assert restored.action == "jtag_scan"
    assert restored.verdict == PASS
    assert restored.measurements["idcode"] == "0x012BC043"
    assert restored.measurements["device_count"] == 1


def test_resume_refuses_an_aborted_sequence(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Resuming past a failure nobody looked at is exactly what must not happen."""
    tools = passing_tools()
    tools.scan = lambda gate, record, **kw: fake_result("jtag_scan", FAIL, ok=False)
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), tools)
    assert seq.run() is Gate1State.ABORTED
    with pytest.raises(SequenceError, match="ran and failed"):
        resume_sequence(
            gate=gate,
            bundle=seq.bundle,
            ledger=seq.ledger,
            record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "r"),
            resume_from=seq.record.dir,
            tools=passing_tools(),
        )


def test_resume_refuses_a_completed_sequence(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    seq = build(tmp_path, gate, write_bundle(tmp_path, bundle_doc(xcf_files)), passing_tools())
    assert seq.run() is Gate1State.COMPLETE
    with pytest.raises(SequenceError, match="nothing to continue"):
        resume_sequence(
            gate=gate,
            bundle=seq.bundle,
            ledger=seq.ledger,
            record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "r"),
            resume_from=seq.record.dir,
            tools=passing_tools(),
        )


def test_resume_refuses_a_different_bundle(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """A different bundle is a different approval, so it is a new run."""
    first = interrupted_after_program(tmp_path, gate, xcf_files)
    other_doc = bundle_doc(xcf_files, steps=("scan", "program", "measure"))
    other_doc["bundle_id"] = "gate1-other"
    other = load_bundle(write_bundle(tmp_path, other_doc, "other.yaml"), today=TODAY)
    with pytest.raises(SequenceError, match="different approval"):
        resume_sequence(
            gate=gate,
            bundle=other,
            ledger=first.ledger,
            record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "r"),
            resume_from=first.record.dir,
            tools=passing_tools(),
            power_maintained=True,
        )


def test_resume_links_both_run_directories(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    """Neither record is rewritten to tell the other's story."""
    first = interrupted_after_program(tmp_path, gate, xcf_files)
    resumed = resume_sequence(
        gate=gate,
        bundle=first.bundle,
        ledger=ConsumptionLedger(tmp_path / "ledger.db"),
        record=RunRecord("orch_test", gate, runs_dir=tmp_path / "runs" / "resumed"),
        resume_from=first.record.dir,
        tools=passing_tools(),
        power_maintained=True,
    )
    resumed.run()
    written = json.loads((resumed.record.dir / "sequence.json").read_text(encoding="utf-8"))
    assert written["resumed_from"] == first.record.dir.name
    assert resumed.record.dir != first.record.dir
    untouched = json.loads(
        (first.record.dir / "sequence.json").read_text(encoding="utf-8")
    )
    assert untouched["state"] == "PROGRAMMED"
    assert untouched["resumed_from"] is None


def test_terminal_state_refuses_further_steps(
    tmp_path: Path, gate: SafetyGate, xcf_files: dict[str, Path]
) -> None:
    doc = bundle_doc(xcf_files, steps=("scan", "program"))
    tools = passing_tools()
    tools.scan = lambda gate, record, **kw: fake_result("jtag_scan", FAIL, ok=False)
    seq = build(tmp_path, gate, write_bundle(tmp_path, doc), tools)
    seq.run()
    assert seq.state is Gate1State.ABORTED
    assert seq.program() is Gate1State.ABORTED
    assert "program" not in seq.results
