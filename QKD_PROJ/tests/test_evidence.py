"""Offline tests for the evidence reducer and the corpus audit.

Two kinds of test here. Most build a synthetic run directory and assert that a
condition resolves the way it must -- including the cases where the honest
answer is "this cannot be decided", which are the ones a model gets wrong by
producing a confident number instead.

A few run against the real corpus. Those assert shape and invariants rather
than counts, because the corpus grows: a test that asserts "two errors" fails
the day someone records a third run, which teaches people to delete tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import audit, evidence  # noqa: E402
from QKD_PROJ.orchestrator.evidence import (  # noqa: E402
    AMBIGUOUS,
    EVIDENCE_MISSING,
    EXHAUSTED,
    FROM_SIDE_FILE,
    MET,
    OPEN,
    PRIMARY_RUN_MISSING,
    RUN_ABSENT,
    RUN_NO_RECORD,
    SATISFIED,
    UNMET,
    Condition,
    EvidenceError,
    reduce_gate,
)

QKD_ROOT = Path(__file__).resolve().parents[1]


# -- fixtures ----------------------------------------------------------------


def make_run(root: Path, name: str, manifest: dict, files: dict[str, str] | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    for filename, text in (files or {}).items():
        (directory / filename).write_text(text, encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


@pytest.fixture()
def runs(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_build",
        {
            "run": "build",
            "verdict": "PASS",
            "steps": [
                {
                    "tool": "cpld",
                    "action": "build",
                    "verdict": "PASS",
                    "measurements": {"drc_errors": 0, "timing_errors": 0, "freq_mhz": 84.4},
                    "artifacts": [],
                }
            ],
        },
    )
    return root


# -- resolving a field -------------------------------------------------------


def test_a_recorded_field_is_read_from_the_run(runs):
    result = evidence.evaluate(
        Condition("measurements.drc_errors", 0, "contract", "20260101T000000Z_build"), runs
    )
    assert result.status == MET
    assert result.observed == 0
    assert result.evidence == ["20260101T000000Z_build"]
    assert result.tool == "cpld"


def test_a_value_that_disagrees_is_unmet_not_missing(runs):
    result = evidence.evaluate(
        Condition("measurements.freq_mhz", 100, "datasheet", "20260101T000000Z_build"), runs
    )
    assert result.status == UNMET
    assert result.observed == 84.4


def test_a_field_the_run_never_recorded_is_evidence_missing(runs):
    """Different from a failure: it says something about the process, not the board."""
    result = evidence.evaluate(
        Condition("measurements.bit_errors", 0, "contract", "20260101T000000Z_build"), runs
    )
    assert result.status == EVIDENCE_MISSING
    assert result.observed is None
    assert not result.met


def test_a_condition_naming_no_run_cannot_be_checked(runs):
    result = evidence.evaluate(Condition("measurements.drc_errors", 0, "contract", None), runs)
    assert result.status == EVIDENCE_MISSING
    assert "names no run" in result.detail


def test_a_run_that_does_not_exist_is_evidence_missing(runs):
    result = evidence.evaluate(
        Condition("verdict", "PASS", "contract", "20261231T000000Z_imaginary"), runs
    )
    assert result.status == EVIDENCE_MISSING


def test_two_steps_disagreeing_is_ambiguous_not_a_guess(tmp_path):
    """The condition did not say which step it meant. Picking one would look measured."""
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_two",
        {
            "verdict": "PASS",
            "steps": [
                {"tool": "bbb", "action": "write", "measurements": {"bit_errors": 0}},
                {"tool": "bbb", "action": "read", "measurements": {"bit_errors": 3}},
            ],
        },
    )
    result = evidence.evaluate(
        Condition("measurements.bit_errors", 0, "contract", "20260101T000000Z_two"), root
    )
    assert result.status == AMBIGUOUS
    assert not result.met
    assert "does not say which step" in result.detail


def test_naming_the_step_resolves_the_ambiguity(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_two",
        {
            "verdict": "PASS",
            "steps": [
                {"tool": "bbb", "action": "write", "measurements": {"bit_errors": 0}},
                {"tool": "bbb", "action": "read", "measurements": {"bit_errors": 3}},
            ],
        },
    )
    result = evidence.evaluate(
        Condition("steps.read.measurements.bit_errors", 0, "contract", "20260101T000000Z_two"), root
    )
    assert result.status == UNMET
    assert result.observed == 3


# -- operators ---------------------------------------------------------------


@pytest.mark.parametrize(
    "expect,observed,met",
    [
        (0, 0, True),
        ("<= 10", 9, True),
        ("<= 10", 11, False),
        (">= 0", 0, True),
        ("!= 0", 3, True),
        ("> 80", 84.4, True),
        ({"op": "in", "value": [1, 2, 3]}, 2, True),
        ({"op": "in", "value": [1, 2, 3]}, 9, False),
        ([1, 2], [1, 2], True),
    ],
)
def test_operators(tmp_path, expect, observed, met):
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_x",
        {"verdict": "PASS", "steps": [{"tool": "t", "action": "a", "measurements": {"v": observed}}]},
    )
    result = evidence.evaluate(Condition("measurements.v", expect, "s", "20260101T000000Z_x"), root)
    assert result.met is met


def test_an_unknown_operator_is_refused():
    with pytest.raises(EvidenceError, match="unknown operator"):
        Condition("f", {"op": "approximately", "value": 1}).operator()


def test_comparing_a_string_to_a_number_is_unmet_not_a_crash(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_x",
        {"verdict": "PASS", "steps": [{"tool": "t", "action": "a", "measurements": {"v": "n/a"}}]},
    )
    result = evidence.evaluate(Condition("measurements.v", "<= 10", "s", "20260101T000000Z_x"), root)
    assert result.status == UNMET


# -- artifact hashes ---------------------------------------------------------


def test_a_changed_artifact_is_caught(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    directory = make_run(
        root,
        "20260101T000000Z_h",
        {
            "verdict": "PASS",
            "steps": [],
            "artifacts": [
                {
                    "path": "out.bit",
                    "kind": "bitstream",
                    "sha256": "0" * 64,
                    "size_bytes": 3,
                }
            ],
        },
        files={"out.bit": "abc"},
    )
    assert directory.is_dir()
    mismatches = evidence.verify_artifacts("20260101T000000Z_h", root)
    assert len(mismatches) == 1
    assert mismatches[0]["problem"] == "hash changed"


def test_a_gate_with_correct_conditions_but_a_broken_artifact_is_not_satisfied(tmp_path):
    """Every condition holds and the gate still does not close. That is the point."""
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_h",
        {
            "verdict": "PASS",
            "steps": [{"tool": "t", "action": "a", "measurements": {"drc_errors": 0}}],
            "artifacts": [{"path": "out.bit", "kind": "b", "sha256": "0" * 64, "size_bytes": 3}],
        },
        files={"out.bit": "abc"},
    )
    reduction = reduce_gate(
        "g.yaml",
        [{"field": "measurements.drc_errors", "expect": 0, "source": "c", "run": "20260101T000000Z_h"}],
        runs_dir=root,
    )
    assert reduction.outcome == OPEN
    assert reduction.hash_mismatches
    assert not reduction.satisfied


# -- reducing a gate ---------------------------------------------------------


def test_all_conditions_met_closes_the_gate(runs):
    reduction = reduce_gate(
        "g.yaml",
        [
            {"field": "verdict", "expect": "PASS", "source": "c", "run": "20260101T000000Z_build"},
            {
                "field": "measurements.timing_errors",
                "expect": 0,
                "source": "c",
                "run": "20260101T000000Z_build",
            },
        ],
        runs_dir=runs,
    )
    assert reduction.outcome == SATISFIED
    assert reduction.failure_signature is None
    assert all(c.met for c in reduction.conditions)


def test_a_gate_with_no_conditions_cannot_be_decided(runs):
    with pytest.raises(EvidenceError, match="no conditions"):
        reduce_gate("g.yaml", [], runs_dir=runs)


def test_the_third_identical_failure_exhausts_the_loop(runs):
    conditions = [
        {"field": "measurements.freq_mhz", "expect": 100, "source": "c", "run": "20260101T000000Z_build"}
    ]
    first = reduce_gate("g.yaml", conditions, iteration=1, runs_dir=runs)
    assert first.outcome == OPEN and first.strikes == 1

    second = reduce_gate(
        "g.yaml", conditions, iteration=2, history=[first.failure_signature], runs_dir=runs
    )
    assert second.outcome == OPEN and second.strikes == 2

    third = reduce_gate(
        "g.yaml",
        conditions,
        iteration=3,
        history=[first.failure_signature, second.failure_signature],
        runs_dir=runs,
    )
    assert third.outcome == EXHAUSTED and third.strikes == 3


def test_a_different_failure_resets_the_count(runs):
    """Failing at 84.4 then at a missing field is new information, not a repeat."""
    repeated = reduce_gate(
        "g.yaml",
        [{"field": "measurements.freq_mhz", "expect": 100, "source": "c", "run": "20260101T000000Z_build"}],
        runs_dir=runs,
    )
    different = reduce_gate(
        "g.yaml",
        [{"field": "measurements.bit_errors", "expect": 0, "source": "c", "run": "20260101T000000Z_build"}],
        iteration=2,
        history=[repeated.failure_signature, repeated.failure_signature],
        runs_dir=runs,
    )
    assert different.failure_signature != repeated.failure_signature
    assert different.outcome == OPEN
    assert different.strikes == 1


def test_the_reduction_renders_the_shape_a_verdict_envelope_needs(runs):
    reduction = reduce_gate(
        "g.yaml",
        [{"field": "verdict", "expect": "PASS", "source": "c", "run": "20260101T000000Z_build"}],
        runs_dir=runs,
    )
    described = reduction.describe()
    assert set(described) == {
        "gate",
        "iteration",
        "outcome",
        "conditions",
        "failure_signature",
        "hash_mismatches",
        "strikes",
        "corroboration",
    }
    condition = described["conditions"][0]
    assert set(condition) == {
        "field",
        "expect",
        "observed",
        "met",
        "status",
        "evidence",
        "detail",
        "provenance",
        "run_presence",
    }
    assert condition["met"] is True and condition["evidence"]
    assert condition["provenance"] == evidence.FROM_MANIFEST


# -- the audit ---------------------------------------------------------------


def test_claims_are_scoped_to_the_run_their_block_names(tmp_path):
    """A superseded build's numbers must not be compared against the kept run."""
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    for name, freq in (("20260101T000000Z_old", 50.0), ("20260101T000100Z_new", 84.4)):
        make_run(
            runs_dir,
            name,
            {
                "verdict": "PASS",
                "contract": {"test_spec": "c.yaml"},
                "steps": [{"tool": "cpld", "action": "build", "measurements": {"max_frequency_mhz": freq}}],
            },
        )
    (contracts / "c.yaml").write_text(
        "rejected:\n"
        "  run: 20260101T000000Z_old\n"
        "  max_frequency_mhz: 50.0\n"
        "kept:\n"
        "  run: 20260101T000100Z_new\n"
        "  max_frequency_mhz: 84.4\n",
        encoding="utf-8",
    )
    report = audit.run_audit(contracts, runs_dir)
    assert report.by_severity(audit.ERROR) == []


def test_a_claim_contradicting_its_own_run_is_an_error(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    make_run(
        runs_dir,
        "20260101T000000Z_b",
        {
            "verdict": "PASS",
            "contract": {"test_spec": "c.yaml"},
            "steps": [{"tool": "cpld", "action": "build", "measurements": {"drc_errors": 4}}],
        },
    )
    (contracts / "c.yaml").write_text(
        "result:\n  run: 20260101T000000Z_b\n  drc_errors: 0\n", encoding="utf-8"
    )
    errors = audit.run_audit(contracts, runs_dir).by_severity(audit.ERROR)
    assert [f.kind for f in errors] == ["value_claim_mismatch"]
    assert errors[0].claimed == 0 and errors[0].recorded == 4


def test_a_cited_run_that_does_not_exist_is_an_error(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (contracts / "c.yaml").write_text(
        "result:\n  run: 20260101T000000Z_gone\n  drc_errors: 0\n", encoding="utf-8"
    )
    errors = audit.run_audit(contracts, runs_dir).by_severity(audit.ERROR)
    assert [f.kind for f in errors] == ["primary_run_missing"]


def test_hex_and_decimal_forms_of_the_same_idcode_agree(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    make_run(
        runs_dir,
        "20260101T000000Z_s",
        {
            "verdict": "PASS",
            "contract": {"test_spec": "c.yaml"},
            "steps": [{"tool": "cpld", "action": "scan", "measurements": {"idcode": "0x012BC043"}}],
        },
    )
    (contracts / "c.yaml").write_text(
        "result:\n  run: 20260101T000000Z_s\n  idcode: 0x012BC043\n", encoding="utf-8"
    )
    assert audit.run_audit(contracts, runs_dir).by_severity(audit.ERROR) == []


# -- against the real corpus -------------------------------------------------


@pytest.mark.skipif(not (QKD_ROOT / "runs").is_dir(), reason="no runs/ in this checkout")
def test_the_real_corpus_audits_without_crashing():
    report = audit.run_audit()
    assert report.contracts > 0
    assert report.runs > 0
    for finding in report.findings:
        assert finding.severity in (audit.ERROR, audit.WARNING, audit.INFO)
        assert finding.detail


@pytest.mark.skipif(not (QKD_ROOT / "runs").is_dir(), reason="no runs/ in this checkout")
def test_no_candidate_has_drifted_from_its_pinned_digest():
    """Candidates are marked immutable and are often the only surviving copy."""
    report = audit.run_audit()
    drift = [f for f in report.findings if f.kind in ("candidate_drift", "artifact_drift")]
    assert drift == [], f"pinned evidence changed on disk: {[f.describe() for f in drift]}"


# -- run presence, provenance, corroboration ---------------------------------


def test_an_absent_run_and_a_recordless_run_are_told_apart(tmp_path):
    """Two different problems with two different fixes."""
    root = tmp_path / "runs"
    root.mkdir()
    (root / "20260101T000000Z_norecord").mkdir()
    (root / "20260101T000000Z_norecord" / "summary.json").write_text("{}", encoding="utf-8")

    assert evidence.classify_run("20260101T000000Z_gone", root) == RUN_ABSENT
    assert evidence.classify_run("20260101T000000Z_norecord", root) == RUN_NO_RECORD

    gone = evidence.evaluate(Condition("verdict", "PASS", "c", "20260101T000000Z_gone"), root)
    assert gone.run_presence == RUN_ABSENT
    assert "not on disk" in gone.detail

    recordless = evidence.evaluate(
        Condition("verdict", "PASS", "c", "20260101T000000Z_norecord"), root
    )
    assert recordless.run_presence == RUN_NO_RECORD
    assert "wrote no RunRecord" in recordless.detail


def test_a_missing_run_outranks_the_strike_count(tmp_path):
    """Re-running the loop cannot produce evidence that is not on disk."""
    root = tmp_path / "runs"
    root.mkdir()
    conditions = [{"field": "verdict", "expect": "PASS", "source": "c", "run": "20260101T000000Z_gone"}]
    first = reduce_gate("g.yaml", conditions, runs_dir=root)
    assert first.outcome == PRIMARY_RUN_MISSING

    third = reduce_gate(
        "g.yaml",
        conditions,
        iteration=3,
        history=[first.failure_signature, first.failure_signature],
        runs_dir=root,
    )
    assert third.outcome == PRIMARY_RUN_MISSING, "not exhausted -- the evidence is gone, not tried out"


def test_a_side_file_is_read_even_when_the_run_wrote_no_record(tmp_path):
    """The measurement is sitting there. Calling it missing would be false."""
    root = tmp_path / "runs"
    root.mkdir()
    directory = root / "20260101T000000Z_capture"
    directory.mkdir()
    (directory / "summary.json").write_text(
        json.dumps({"miso_hex": "a55a3cc3f00f"}), encoding="utf-8"
    )
    result = evidence.evaluate(
        Condition("file:summary.json:miso_hex", "a55a3cc3f00f", "c", "20260101T000000Z_capture"),
        root,
    )
    assert result.status == MET
    assert result.provenance == FROM_SIDE_FILE
    assert result.run_presence == RUN_NO_RECORD
    assert "not a registered artifact" in result.detail


def test_a_side_file_the_run_registered_counts_as_tool_pinned(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_reg",
        {
            "verdict": "PASS",
            "steps": [],
            "artifacts": [{"path": "summary.json", "kind": "s", "sha256": "0" * 64, "size_bytes": 1}],
        },
        files={"summary.json": json.dumps({"miso_hex": "a55a3cc3f00f"})},
    )
    result = evidence.evaluate(
        Condition("file:summary.json:miso_hex", "a55a3cc3f00f", "c", "20260101T000000Z_reg"), root
    )
    assert result.status == MET
    assert result.provenance == evidence.FROM_MANIFEST


def test_corroboration_never_closes_a_gate(tmp_path):
    """Moving a citation to a supporting run does not fill the gap it left."""
    root = tmp_path / "runs"
    root.mkdir()
    make_run(
        root,
        "20260101T000000Z_support",
        {"verdict": "PASS", "steps": [{"tool": "cpld", "action": "program", "measurements": {}}]},
    )
    reduction = reduce_gate(
        "g.yaml",
        [{"field": "verdict", "expect": "PASS", "source": "c", "run": "20260101T000000Z_gone"}],
        runs_dir=root,
        corroboration=[
            {
                "run": "20260101T000000Z_support",
                "supports": "the same transport works",
                "field": "measurements.bit_errors",
            }
        ],
    )
    assert reduction.outcome == PRIMARY_RUN_MISSING
    assert not reduction.satisfied
    supporting = reduction.corroboration[0]
    assert supporting["closes_gate"] is False
    assert supporting["presence"] == evidence.RUN_PRESENT
    assert supporting["recorded"] is False, "the supporting run does not record the field either"


def test_corroboration_reports_a_run_that_is_also_gone(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    checked = evidence.check_corroboration([{"run": "20260101T000000Z_gone"}], root)
    assert checked[0]["presence"] == RUN_ABSENT
    assert checked[0]["closes_gate"] is False


# -- audit: unscoped claims and pinned files ---------------------------------


def test_a_result_block_naming_no_run_is_an_error(tmp_path):
    """The gap scoping opened: a block with no run stops being compared at all."""
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (contracts / "c.yaml").write_text(
        "gpio_protocol_result:\n  status: passed\n  bit_errors: 0\n", encoding="utf-8"
    )
    errors = audit.run_audit(contracts, runs_dir).by_severity(audit.ERROR)
    assert [f.kind for f in errors] == ["unscoped_claim"]
    assert "bit_errors=0" in errors[0].detail


def test_a_result_block_that_names_its_run_is_not_reported_as_unscoped(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    make_run(
        runs_dir,
        "20260101T000000Z_r",
        {
            "verdict": "PASS",
            "contract": {"test_spec": "c.yaml"},
            "steps": [{"tool": "bbb", "action": "verify", "measurements": {"bit_errors": 0}}],
        },
    )
    (contracts / "c.yaml").write_text(
        "gpio_protocol_result:\n  run: 20260101T000000Z_r\n  bit_errors: 0\n", encoding="utf-8"
    )
    report = audit.run_audit(contracts, runs_dir)
    assert [f.kind for f in report.by_severity(audit.ERROR)] == []


@pytest.mark.skipif(not (QKD_ROOT / "runs").is_dir(), reason="no runs/ in this checkout")
def test_the_real_corpus_still_reports_the_two_gate10_gaps():
    """Different kinds, because they need different fixes."""
    report = audit.run_audit()
    kinds = {f.kind for f in report.findings if f.contract == "gate10-spi-clocked-return.yaml"}
    assert "primary_run_missing" in kinds
    assert "run_record_missing" in kinds
