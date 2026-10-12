"""Envelope validation at the AgentRunner seam.

One case per handoff kind, valid and invalid. Every invalid case asserts the
same three things:

* the job blocked with ``reason_code == invalid_handoff``,
* nothing was recorded as a handoff,
* the strike count did not move.

The last one carries the most weight. A rejected answer must not look like an
iteration that was tried and failed, or the strike rule would eventually read a
broken agent as evidence that the board cannot do something -- which is the
exact confusion between "the tool judged this" and "the model said this" that
the whole layer exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.agents.schema import ValidationError, validate_handoff  # noqa: E402
from ai_hardware_autolab.orchestrator import jobs as J  # noqa: E402
from ai_hardware_autolab.orchestrator import loop as L  # noqa: E402
from ai_hardware_autolab.orchestrator.jobs import JobLedger  # noqa: E402
from ai_hardware_autolab.orchestrator.loop import Loop, start  # noqa: E402
from ai_hardware_autolab.tests.test_loop import (  # noqa: E402
    Runner,
    build_step,
    default_runner,
    make_run,
    plan_with,
)


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    return JobLedger(tmp_path / "jobs.db"), runs


def assert_rejected(ledger, job_id, step, kind: str, fragment: str = "") -> None:
    assert step.blocked and step.reason_code == L.BLOCKED_INVALID, step
    assert ledger.job(job_id).state == J.BLOCKED
    assert ledger.handoffs(job_id, kind) == [], f"a rejected {kind} was recorded anyway"
    assert ledger.strikes(job_id) == 0
    if fragment:
        assert fragment in step.detail, step.detail
    blocked = [e for e in ledger.events_since(0, job_id) if e.kind == "job.blocked"]
    assert blocked and blocked[-1].payload["reason"].startswith(L.BLOCKED_INVALID)


# -- Brief -------------------------------------------------------------------


def test_a_valid_brief_opens_a_job(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it", scenario="s", resources={"u": "U2"})
    assert ledger.handoffs(job.job_id, "Brief")[0].payload["kind"] == "Brief"


def test_start_always_produces_a_brief_that_validates(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    validate_handoff(ledger.handoffs(job.job_id, "Brief")[0].payload)


def test_a_malformed_brief_is_refused_before_any_job_exists(env):
    """Raised rather than blocked: there is no job yet to stop."""
    ledger, _ = env
    with pytest.raises(ValidationError, match="missing required fields"):
        validate_handoff({"kind": "Brief", "agent": "human"})
    assert ledger.jobs() == []


# -- Plan --------------------------------------------------------------------


def test_a_plan_missing_its_advance_conditions_is_rejected(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    step = build_step()
    del step["advance_when"]

    advance = Loop(ledger, default_runner(plan=plan_with([step])), runs_dir=runs).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "advance_when")


def test_a_plan_whose_threshold_has_no_source_is_rejected(env):
    """The rule that keeps invented numbers out of a plan, enforced at the seam."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    step = build_step()
    step["advance_when"][0]["source"] = "   "

    advance = Loop(ledger, default_runner(plan=plan_with([step])), runs_dir=runs).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "source is empty")


def test_a_plan_with_duplicate_step_ids_is_rejected(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    plan = plan_with([build_step("s1"), build_step("s1")])

    advance = Loop(ledger, default_runner(plan=plan), runs_dir=runs).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "ids must be unique")


def test_a_plan_that_refuses_needs_no_steps(env):
    """`blocked_on` is a legitimate plan with nothing in it."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(
        plan={"kind": "Plan", "steps": [], "blocked_on": "contract_gap", "objective": "no authority"}
    )
    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)

    assert history[-1].outcome == "contract_gap"
    assert ledger.handoffs(job.job_id, "Plan"), "the refusal is kept, not discarded"


def test_an_empty_plan_with_no_reason_is_rejected(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")

    advance = Loop(
        ledger, default_runner(plan={"kind": "Plan", "steps": []}), runs_dir=runs
    ).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "blocked_on is not set")


def test_an_architect_returning_the_wrong_envelope_kind_is_rejected(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(plan={"kind": "Report", "document": "d", "outcome": "satisfied"})

    advance = Loop(ledger, runner, runs_dir=runs).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "kind='Report'")


def test_a_plan_claiming_a_different_iteration_is_rejected(env):
    """The loop dispatched the call, so it is the authority on which round this is."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(plan=plan_with([build_step()], iteration=7))

    advance = Loop(ledger, runner, runs_dir=runs).advance(job.job_id)
    assert_rejected(ledger, job.job_id, advance, "Plan", "iteration=7")


def test_a_valid_plan_is_stamped_with_what_the_loop_knows(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).advance(job.job_id)

    plan = ledger.handoffs(job.job_id, "Plan")[0].payload
    assert plan["agent"] == "gate-architect"
    assert plan["iteration"] == 1
    assert plan["gate"] == "g.yaml"


# -- StepResult --------------------------------------------------------------


def _dispatch_step(ledger, runs, envelope):
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.envelopes["design-builder"] = envelope
    loop = Loop(ledger, runner, runs_dir=runs)
    loop.advance(job.job_id)
    return job, loop.advance(job.job_id)


def test_a_step_result_claiming_done_with_no_run_is_rejected(env):
    """Every tool call writes a RunRecord; done with no run did not happen."""
    ledger, runs = env
    job, advance = _dispatch_step(ledger, runs, {"kind": "StepResult", "status": "done", "runs": []})
    assert_rejected(ledger, job.job_id, advance, "StepResult", "did not happen")


def test_a_step_result_with_an_invented_verdict_is_rejected(env):
    ledger, runs = env
    job, advance = _dispatch_step(
        ledger,
        runs,
        {
            "kind": "StepResult",
            "status": "done",
            "runs": ["20260101T000000Z_build"],
            "verdict": "looks fine",
        },
    )
    assert_rejected(ledger, job.job_id, advance, "StepResult", "copied from a ToolResult")


def test_a_step_result_with_an_unknown_status_is_rejected(env):
    ledger, runs = env
    job, advance = _dispatch_step(
        ledger, runs, {"kind": "StepResult", "status": "mostly done", "runs": ["r"]}
    )
    assert_rejected(ledger, job.job_id, advance, "StepResult", "status must be one of")


def test_a_step_result_for_the_wrong_step_is_rejected(env):
    ledger, runs = env
    job, advance = _dispatch_step(
        ledger,
        runs,
        {
            "kind": "StepResult",
            "status": "done",
            "step_id": "s9",
            "runs": ["20260101T000000Z_build"],
        },
    )
    assert_rejected(ledger, job.job_id, advance, "StepResult", "step_id='s9'")


def test_a_valid_step_result_is_recorded_with_its_step_stamped(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, default_runner(), runs_dir=runs)
    loop.advance(job.job_id)
    loop.advance(job.job_id)

    recorded = ledger.handoffs(job.job_id, "StepResult")[0]
    assert recorded.step_id == "s1"
    assert recorded.payload["step_id"] == "s1"
    assert recorded.payload["agent"] == "design-builder"


# -- Verdict -----------------------------------------------------------------


def test_the_reduction_validates_as_a_verdict(env):
    """The reducer's own output crosses the same schema the agents do."""
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)

    verdict = ledger.handoffs(job.job_id, "Verdict")[0].payload
    validate_handoff({"kind": "Verdict", "agent": "verifier", **verdict})
    assert verdict["outcome"] == "satisfied"


def test_an_open_verdict_is_valid_and_an_open_report_is_not():
    """An iteration can end without the loop ending; a report describes a loop that stopped."""
    validate_handoff(
        {
            "kind": "Verdict",
            "agent": "verifier",
            "gate": "g.yaml",
            "iteration": 1,
            "outcome": "open",
            "conditions": [{"field": "f", "met": False, "evidence": ["r"]}],
        }
    )
    with pytest.raises(ValidationError, match="Report.outcome"):
        validate_handoff(
            {
                "kind": "Report",
                "agent": "reporter",
                "gate": "g.yaml",
                "outcome": "open",
                "document": "d",
            }
        )


def test_a_satisfied_verdict_over_an_unmet_condition_is_refused():
    with pytest.raises(ValidationError, match="may not close over an open condition"):
        validate_handoff(
            {
                "kind": "Verdict",
                "agent": "verifier",
                "gate": "g.yaml",
                "iteration": 1,
                "outcome": "satisfied",
                "conditions": [{"field": "f", "met": False, "evidence": ["r"]}],
            }
        )


def test_a_met_condition_with_no_evidence_is_refused():
    with pytest.raises(ValidationError, match="met by a run record"):
        validate_handoff(
            {
                "kind": "Verdict",
                "agent": "verifier",
                "gate": "g.yaml",
                "iteration": 1,
                "outcome": "open",
                "conditions": [{"field": "f", "met": True, "evidence": []}],
            }
        )


# -- Report ------------------------------------------------------------------


def test_a_report_without_a_document_is_rejected(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.envelopes["reporter"] = {"kind": "Report", "outcome": "satisfied"}

    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)
    assert_rejected(ledger, job.job_id, history[-1], "Report", "document")
    assert ledger.job(job.job_id).outcome is None, "the job did not close on a rejected report"


def test_a_report_that_softens_the_outcome_is_rejected(env):
    """A reporter claiming satisfied when the loop decided exhausted is the failure mode."""
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.envelopes["reporter"] = {
        "kind": "Report",
        "document": "docs/x.md",
        "outcome": "satisfied",
    }

    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)
    assert history[-1].reason_code == L.BLOCKED_INVALID
    assert "outcome='satisfied'" in history[-1].detail
    assert ledger.job(job.job_id).state == J.BLOCKED


def test_a_valid_report_closes_the_job(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)

    report = ledger.handoffs(job.job_id, "Report")[0].payload
    assert report["outcome"] == "satisfied" and report["document"]
    assert ledger.job(job.job_id).outcome == "satisfied"


# -- runtime failure vs invalid handoff --------------------------------------


def test_the_two_block_reasons_are_told_apart(env):
    ledger, runs = env

    dead = start(ledger, "g.yaml", "a")
    dead_runner = default_runner()
    dead_runner.fail_for = {"gate-architect"}
    dead_step = Loop(ledger, dead_runner, runs_dir=runs).advance(dead.job_id)

    invalid = start(ledger, "g.yaml", "b")
    invalid_step = Loop(
        ledger, default_runner(plan={"kind": "Plan", "steps": []}), runs_dir=runs
    ).advance(invalid.job_id)

    assert dead_step.reason_code == L.BLOCKED_RUNTIME
    assert invalid_step.reason_code == L.BLOCKED_INVALID
    assert dead_step.blocked and invalid_step.blocked
    assert ledger.job(dead.job_id).state == J.BLOCKED
    assert ledger.job(invalid.job_id).state == J.BLOCKED

    for job_id, code in ((dead.job_id, L.BLOCKED_RUNTIME), (invalid.job_id, L.BLOCKED_INVALID)):
        reason = [e for e in ledger.events_since(0, job_id) if e.kind == "job.blocked"][-1]
        assert reason.payload["reason"].startswith(code)


def test_a_dead_runtime_records_nothing_about_the_gate(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.fail_for = {"gate-architect"}
    Loop(ledger, runner, runs_dir=runs).advance(job.job_id)

    assert ledger.handoffs(job.job_id, "Verdict") == []
    assert ledger.handoffs(job.job_id, "Plan") == []
    assert ledger.strikes(job.job_id) == 0


def test_neither_block_reason_invents_an_event_kind(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(plan={"kind": "Plan", "steps": []}), runs_dir=runs).advance(
        job.job_id
    )

    kinds = {e.kind for e in ledger.events_since(0, job.job_id)}
    assert kinds <= {
        "job.created",
        "handoff.Brief",
        "job.running",
        "job.blocked",
        "iteration.started",
    }


def test_a_rejected_answer_does_not_advance_the_strike_count(env):
    """The point of not recording it: a broken agent is not evidence about the board."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = Runner(**{"gate-architect": {"kind": "Plan", "steps": []}})

    for _ in range(3):
        loop = Loop(ledger, runner, runs_dir=runs)
        loop.advance(job.job_id)
        if ledger.job(job.job_id).state == J.BLOCKED:
            ledger.transition(job.job_id, J.RUNNING, reason="operator retried")

    assert ledger.strikes(job.job_id) == 0
    assert not ledger.exhausted(job.job_id)
    assert ledger.handoffs(job.job_id, "Verdict") == []
