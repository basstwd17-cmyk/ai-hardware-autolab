from __future__ import annotations

from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.remote_summary import build_remote_snapshot


def test_remote_card_uses_plan_progress_and_verdict_evidence(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("timing.yaml", "Reduce RF timing error below 5 ns")
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(
        job.job_id,
        "Plan",
        "gate-architect",
        {
            "kind": "Plan",
            "gate": "timing.yaml",
            "iteration": 1,
            "steps": [
                {"id": "build", "agent": "design-builder", "tool": "build_cpld", "intent": "Build the timing change."},
                {"id": "simulate", "agent": "design-builder", "tool": "run_simulation", "intent": "Measure simulated timing."},
            ],
        },
        iteration=1,
    )
    ledger.record_handoff(
        job.job_id,
        "StepResult",
        "design-builder",
        {"kind": "StepResult", "step_id": "build", "status": "done", "runs": ["run_build"]},
        iteration=1,
        step_id="build",
    )
    ledger.record_handoff(
        job.job_id,
        "Verdict",
        "verifier",
        {
            "kind": "Verdict",
            "gate": "timing.yaml",
            "iteration": 1,
            "outcome": "open",
            "conditions": [
                {"field": "measurements.build_errors", "met": True, "evidence": ["run_build"]},
                {"field": "measurements.timing_error_ns", "met": False, "evidence": []},
            ],
        },
        iteration=1,
        failure_signature="timing|open",
    )
    status = {
        "at": "2026-09-01T00:00:00+00:00",
        "cursor": ledger.latest_event_id(),
        "jobs": {"runnable": [{"job_id": job.job_id, "status": "runnable", "detail": "ready"}]},
        "inbox": [],
    }

    snapshot = build_remote_snapshot(ledger, status, phase_for=lambda _: "execute")
    card = snapshot["jobs"][0]

    assert card["objective"] == "Reduce RF timing error below 5 ns"
    assert card["progress"] == {"reported": 1, "total": 2}
    assert card["result"]["counts"]["met"] == 1
    assert card["result"]["counts"]["unmet"] == 1
    assert card["result"]["conditions"][1]["field"] == "measurements.timing_error_ns"
    assert card["next_action"]["step_id"] == "simulate"
    assert card["next_action"]["agent"] == "design-builder"
    assert len(card["recent_activity"]) == 3


def test_remote_summary_control_includes_pending_feedback_without_paths(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ledger.create_job("g.yaml", "Review the implementation")
    ledger.record_handoff(
        job.job_id,
        "ArchitectureDecision",
        "architecture-selector",
        {
            "kind": "ArchitectureDecision",
            "summary": "Keep the existing buffered path.",
            "document": "C:/secret/full/path/report.md",
        },
    )
    control = ControlPlane(ledger)
    control.steer(job.job_id, "Inspect RF2 before changing RF1.", "quantum1")
    ledger.request_approval(
        job.job_id,
        "g.yaml",
        "Program the reviewed volatile candidate once.",
        artifacts={"private/build/output.bit": "d" * 64},
        step_id="program",
    )

    answer = control.handle({"command": "remote-summary", "limit": 10})

    assert answer["ok"] is True
    card = answer["snapshot"]["jobs"][0]
    assert card["feedback"]["pending_count"] == 1
    assert card["feedback"]["latest"]["message"] == "Inspect RF2 before changing RF1."
    encoded = str(answer)
    assert "C:/secret/full/path" not in encoded
    assert "private/build/output.bit" not in encoded
    assert answer["snapshot"]["inbox"][0]["artifacts"] == [{"sha256": "d" * 64}]
    assert card["result"]["summary"] == "Keep the existing buffered path."
