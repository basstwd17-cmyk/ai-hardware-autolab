"""Offline tests for the P2 job ledger.

The claims worth testing here are the ones a caller cannot see going wrong: a
state that changed without an event to explain it, a strike count that drifts
from the verdicts it summarises, an approval decided twice, a cursor that skips
an event a UI needed. Each of those produces a plausible-looking ledger, which
is exactly why they are asserted rather than assumed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.orchestrator import jobs as J  # noqa: E402
from ai_hardware_autolab.orchestrator.jobs import JobLedger, JobLedgerError  # noqa: E402


@pytest.fixture()
def ledger(tmp_path: Path) -> JobLedger:
    return JobLedger(tmp_path / "jobs.db")


@pytest.fixture()
def job(ledger: JobLedger):
    return ledger.create_job("gate12-pru-dac1-plan.yaml", "observe digital timing")


# -- jobs --------------------------------------------------------------------


def test_a_job_is_never_created_without_a_log(ledger):
    """A job with an empty log is indistinguishable from a lost connection."""
    created = ledger.create_job("g.yaml", "do the thing")
    events = ledger.events_since(0, created.job_id)
    assert [e.kind for e in events] == ["job.created"]
    assert events[0].payload["objective"] == "do the thing"
    assert created.state == J.PENDING and created.iteration == 0


def test_a_brief_is_stored_with_the_job(ledger):
    created = ledger.create_job("g.yaml", "o", brief={"kind": "Brief", "scenario": "s"})
    briefs = ledger.handoffs(created.job_id, "Brief")
    assert len(briefs) == 1 and briefs[0].agent == "human"


def test_a_job_needs_a_gate_and_an_objective(ledger):
    with pytest.raises(JobLedgerError, match="gate and an objective"):
        ledger.create_job("  ", "o")


def test_duplicate_job_ids_are_refused(ledger):
    ledger.create_job("g.yaml", "o", job_id="job_fixed")
    with pytest.raises(JobLedgerError, match="already exists"):
        ledger.create_job("g.yaml", "o", job_id="job_fixed")


def test_an_illegal_transition_is_refused(ledger, job):
    with pytest.raises(JobLedgerError, match="cannot become"):
        ledger.transition(job.job_id, J.AWAITING_APPROVAL)


def test_done_is_terminal(ledger, job):
    ledger.transition(job.job_id, J.RUNNING)
    ledger.transition(job.job_id, J.DONE, "satisfied")
    with pytest.raises(JobLedgerError, match="terminal"):
        ledger.transition(job.job_id, J.RUNNING)


def test_a_job_cannot_finish_without_an_outcome(ledger, job):
    ledger.transition(job.job_id, J.RUNNING)
    with pytest.raises(JobLedgerError, match="without an outcome"):
        ledger.transition(job.job_id, J.DONE)


def test_an_outcome_cannot_be_set_while_the_job_is_still_moving(ledger, job):
    with pytest.raises(JobLedgerError, match="what a finished job finished as"):
        ledger.transition(job.job_id, J.RUNNING, "satisfied")


def test_every_transition_writes_the_event_that_explains_it(ledger, job):
    """One transaction, or neither. A state no event accounts for is a lie by omission."""
    ledger.transition(job.job_id, J.RUNNING, reason="starting")
    ledger.transition(job.job_id, J.BLOCKED, reason="waiting on a part")
    kinds = [e.kind for e in ledger.events_since(0, job.job_id)]
    assert kinds == ["job.created", "job.running", "job.blocked"]
    blocked = ledger.events_since(0, job.job_id)[-1]
    assert blocked.payload == {
        "from": J.RUNNING,
        "to": J.BLOCKED,
        "outcome": None,
        "reason": "waiting on a part",
    }


def test_a_failed_transition_writes_no_event(ledger, job):
    before = len(ledger.events_since(0, job.job_id))
    with pytest.raises(JobLedgerError):
        ledger.transition(job.job_id, J.AWAITING_APPROVAL)
    assert len(ledger.events_since(0, job.job_id)) == before


def test_the_daemon_owns_the_iteration_counter(ledger, job):
    ledger.transition(job.job_id, J.RUNNING)
    assert ledger.begin_iteration(job.job_id) == 1
    assert ledger.begin_iteration(job.job_id) == 2
    assert ledger.job(job.job_id).iteration == 2


def test_a_finished_job_starts_no_further_iteration(ledger, job):
    ledger.transition(job.job_id, J.RUNNING)
    ledger.transition(job.job_id, J.DONE, "exhausted")
    with pytest.raises(JobLedgerError, match="a new attempt is a new job"):
        ledger.begin_iteration(job.job_id)


# -- events ------------------------------------------------------------------


def test_event_ids_are_global_and_sequences_are_per_job(ledger):
    first = ledger.create_job("g.yaml", "a")
    second = ledger.create_job("g.yaml", "b")
    ledger.append_event(first.job_id, "command", {"cmd": "ls"})
    ledger.append_event(second.job_id, "command", {"cmd": "ls"})
    ledger.append_event(first.job_id, "command", {"cmd": "pwd"})

    per_first = [e.seq for e in ledger.events_since(0, first.job_id)]
    per_second = [e.seq for e in ledger.events_since(0, second.job_id)]
    assert per_first == [1, 2, 3]
    assert per_second == [1, 2]

    ids = [e.event_id for e in ledger.events_since(0)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)


def test_the_cursor_replays_exactly_what_was_missed(ledger, job):
    for index in range(5):
        ledger.append_event(job.job_id, "file_change", {"n": index})
    everything = ledger.events_since(0)
    midpoint = everything[2].event_id
    assert [e.event_id for e in ledger.events_since(midpoint)] == [
        e.event_id for e in everything[3:]
    ]
    assert ledger.latest_event_id() == everything[-1].event_id


def test_two_writers_cannot_claim_the_same_sequence(ledger, job):
    """Enforced by the index, not by a count read in Python."""
    conn = sqlite3.connect(ledger.path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events (job_id, seq, at, kind, agent, payload) "
                "VALUES (?, 1, '2026-01-01T00:00:00+00:00', 'duplicate', NULL, '{}')",
                (job.job_id,),
            )
    finally:
        conn.close()


def test_an_event_for_an_unknown_job_is_refused(ledger):
    with pytest.raises(JobLedgerError, match="no such job"):
        ledger.append_event("job_nothing", "command", {})


def test_an_event_needs_a_kind(ledger, job):
    with pytest.raises(JobLedgerError, match="needs a kind"):
        ledger.append_event(job.job_id, "   ")


# -- handoffs ----------------------------------------------------------------


def test_a_handoff_is_stored_verbatim_and_by_digest(ledger, job):
    payload = {"kind": "Plan", "steps": [{"id": "s1"}], "gate": "g.yaml"}
    handoff = ledger.record_handoff(job.job_id, "Plan", "gate-architect", payload)
    assert handoff.payload == payload
    assert len(handoff.sha256) == 64

    import hashlib

    expected = hashlib.sha256(J.canonical_payload(payload)).hexdigest()
    assert handoff.sha256 == expected


def test_the_digest_ignores_key_order(ledger, job):
    """A reformat must not read as a change."""
    one = J.canonical_payload({"a": 1, "b": 2})
    two = J.canonical_payload({"b": 2, "a": 1})
    assert one == two


def test_recording_a_handoff_announces_it(ledger, job):
    ledger.record_handoff(job.job_id, "Plan", "gate-architect", {"kind": "Plan"})
    last = ledger.events_since(0, job.job_id)[-1]
    assert last.kind == "handoff.Plan"
    assert last.payload["agent"] == "gate-architect"
    assert last.payload["sha256"]


def test_a_second_handoff_of_the_same_kind_and_iteration_is_refused(ledger, job):
    ledger.record_handoff(job.job_id, "Plan", "gate-architect", {"kind": "Plan", "v": 1})
    with pytest.raises(JobLedgerError, match="already recorded"):
        ledger.record_handoff(job.job_id, "Plan", "gate-architect", {"kind": "Plan", "v": 2})


def test_step_results_from_one_agent_coexist_when_they_name_their_step(ledger, job):
    ledger.record_handoff(job.job_id, "StepResult", "board-operator", {"a": 1}, step_id="s1")
    ledger.record_handoff(job.job_id, "StepResult", "board-operator", {"a": 2}, step_id="s2")
    assert len(ledger.handoffs(job.job_id, "StepResult")) == 2


def test_an_unknown_handoff_kind_is_refused(ledger, job):
    with pytest.raises(JobLedgerError, match="unknown handoff kind"):
        ledger.record_handoff(job.job_id, "Opinion", "someone", {})


def test_a_handoff_payload_must_be_an_object(ledger, job):
    with pytest.raises(JobLedgerError, match="must be an object"):
        ledger.record_handoff(job.job_id, "Plan", "a", ["not", "an", "object"])


# -- strikes -----------------------------------------------------------------


def _verdict(ledger, job_id, iteration, signature):
    ledger.record_handoff(
        job_id,
        "Verdict",
        "verifier",
        {"kind": "Verdict", "iteration": iteration},
        iteration=iteration,
        failure_signature=signature,
    )


def test_strikes_count_the_trailing_identical_failures(ledger, job):
    for iteration in (1, 2, 3):
        _verdict(ledger, job.job_id, iteration, "gate|field|tool|3")
    assert ledger.strikes(job.job_id) == 3
    assert ledger.exhausted(job.job_id) is True


def test_a_different_failure_resets_the_count(ledger, job):
    _verdict(ledger, job.job_id, 1, "sig-A")
    _verdict(ledger, job.job_id, 2, "sig-A")
    _verdict(ledger, job.job_id, 3, "sig-B")
    assert ledger.strikes(job.job_id) == 1
    assert ledger.exhausted(job.job_id) is False


def test_a_satisfied_verdict_leaves_no_strikes(ledger, job):
    _verdict(ledger, job.job_id, 1, "sig-A")
    _verdict(ledger, job.job_id, 2, None)
    assert ledger.strikes(job.job_id) == 0


def test_strikes_are_zero_before_any_verdict(ledger, job):
    assert ledger.strikes(job.job_id) == 0


# -- approvals ---------------------------------------------------------------


def test_an_approval_needs_a_scope_a_person_can_be_held_to(ledger, job):
    with pytest.raises(JobLedgerError, match="not something a person"):
        ledger.request_approval(job.job_id, "g.yaml", "   ")


def test_a_request_lands_in_the_inbox_and_names_artifacts_by_digest(ledger, job):
    approval = ledger.request_approval(
        job.job_id,
        "gate12-pru-dac1-plan.yaml",
        "One volatile SRAM program of the pinned candidate",
        artifacts={"bitstream": "d" * 64},
    )
    assert approval.state == J.APPROVAL_PENDING
    assert approval.artifacts == {"bitstream": "d" * 64}
    assert [a.approval_id for a in ledger.pending_approvals()] == [approval.approval_id]
    assert ledger.events_since(0, job.job_id)[-1].kind == "approval.pending"


def test_a_decision_is_made_once(ledger, job):
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    granted = ledger.decide_approval(approval.approval_id, True, "quantum1", note="at the bench")
    assert granted.granted and granted.decided_by == "quantum1"
    with pytest.raises(JobLedgerError, match="already granted"):
        ledger.decide_approval(approval.approval_id, False, "someone else")


def test_a_decision_must_name_who_made_it(ledger, job):
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    with pytest.raises(JobLedgerError, match="must name who made it"):
        ledger.decide_approval(approval.approval_id, True, "  ")


def test_a_granted_approval_leaves_the_inbox(ledger, job):
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    ledger.decide_approval(approval.approval_id, True, "quantum1")
    assert ledger.pending_approvals() == []


def test_an_expired_request_is_not_in_the_inbox_and_cannot_be_granted(ledger, job):
    approval = ledger.request_approval(
        job.job_id, "g.yaml", "one program", expires_at="2020-01-01T00:00:00+00:00"
    )
    assert ledger.pending_approvals() == []
    with pytest.raises(JobLedgerError, match="expired at"):
        ledger.decide_approval(approval.approval_id, True, "quantum1")
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_EXPIRED


def test_an_expired_request_may_still_be_denied(ledger, job):
    """Denying something nobody can act on is harmless; granting it is not."""
    approval = ledger.request_approval(
        job.job_id, "g.yaml", "one program", expires_at="2020-01-01T00:00:00+00:00"
    )
    denied = ledger.decide_approval(approval.approval_id, False, "quantum1")
    assert denied.state == J.APPROVAL_DENIED


def test_a_grant_can_be_withdrawn_and_the_original_decision_stays_visible(ledger, job):
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    ledger.decide_approval(approval.approval_id, True, "quantum1", note="ok")
    withdrawn = ledger.withdraw_approval(approval.approval_id, "quantum1", "board moved")
    assert withdrawn.state == J.APPROVAL_WITHDRAWN
    assert withdrawn.decided_by == "quantum1"
    assert "withdrawn from granted" in withdrawn.decision_note
    assert ledger.events_since(0, job.job_id)[-1].kind == "approval.withdrawn"


def test_a_denied_approval_is_not_withdrawn_again(ledger, job):
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    ledger.decide_approval(approval.approval_id, False, "quantum1")
    with pytest.raises(JobLedgerError, match="already denied"):
        ledger.withdraw_approval(approval.approval_id, "quantum1")


def test_the_database_refuses_a_granted_approval_with_no_decider(ledger, job):
    """The invariant is in the schema, not only in the method that upholds it."""
    ledger.request_approval(job.job_id, "g.yaml", "one program")
    conn = sqlite3.connect(ledger.path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE approvals SET state = 'granted'")
    finally:
        conn.close()


# -- restarting --------------------------------------------------------------


def test_resume_reports_an_interrupted_job_without_repairing_it(ledger, job):
    """Whether the step reached the board is unknown here, so nothing assumes it did not."""
    ledger.transition(job.job_id, J.RUNNING)
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id, "Plan", "gate-architect", {"kind": "Plan"}, iteration=1)

    reopened = JobLedger(ledger.path)
    state = reopened.resume(job.job_id)
    assert state["interrupted"] is True
    assert state["job"]["state"] == J.RUNNING
    assert state["latest_plan"]["payload"]["kind"] == "Plan"
    assert reopened.interrupted_jobs()[0].job_id == job.job_id


def test_resume_carries_the_pending_inbox(ledger, job):
    ledger.transition(job.job_id, J.RUNNING)
    ledger.request_approval(job.job_id, "g.yaml", "one program")
    ledger.transition(job.job_id, J.AWAITING_APPROVAL)
    state = JobLedger(ledger.path).resume(job.job_id)
    assert len(state["pending_approvals"]) == 1
    assert state["interrupted"] is False


def test_a_reopened_ledger_sees_everything(ledger, job):
    ledger.append_event(job.job_id, "command", {"cmd": "x"})
    reopened = JobLedger(ledger.path)
    assert reopened.latest_event_id() == ledger.latest_event_id()
    assert [e.kind for e in reopened.events_since(0, job.job_id)] == ["job.created", "command"]


def test_a_future_schema_version_is_refused(tmp_path):
    path = tmp_path / "jobs.db"
    JobLedger(path)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("UPDATE job_schema_info SET version = 99")
    finally:
        conn.close()
    with pytest.raises(JobLedgerError, match="may not understand"):
        JobLedger(path)


def test_events_are_json_round_trippable(ledger, job):
    ledger.append_event(job.job_id, "mcp_call", {"tool": "read_run", "args": {"run": "x"}})
    event = ledger.events_since(0, job.job_id)[-1]
    assert json.loads(json.dumps(event.describe()))["payload"]["tool"] == "read_run"


def test_overdue_requests_are_settled_rather_than_left_invisible(ledger, job):
    """Read-time filtering hides them; a daemon that was down should record what happened."""
    overdue = ledger.request_approval(
        job.job_id, "g.yaml", "one program", expires_at="2020-01-01T00:00:00+00:00"
    )
    live = ledger.request_approval(job.job_id, "g.yaml", "another program", step_id="s2")

    settled = ledger.expire_overdue()
    assert [a.approval_id for a in settled] == [overdue.approval_id]
    assert ledger.approval(overdue.approval_id).state == J.APPROVAL_EXPIRED
    assert ledger.approval(live.approval_id).state == J.APPROVAL_PENDING
    assert "approval.expired" in [e.kind for e in ledger.events_since(0, job.job_id)]


def test_expiring_twice_is_harmless(ledger, job):
    ledger.request_approval(
        job.job_id, "g.yaml", "one program", expires_at="2020-01-01T00:00:00+00:00"
    )
    assert len(ledger.expire_overdue()) == 1
    assert ledger.expire_overdue() == []


# -- the reducer feeding the ledger ------------------------------------------


def _reduce(tmp_path, expect, run_present=True):
    from ai_hardware_autolab.orchestrator.evidence import reduce_gate

    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    if run_present:
        directory = runs / "20260101T000000Z_r"
        directory.mkdir(exist_ok=True)
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "verdict": "PASS",
                    "steps": [{"tool": "t", "action": "a", "measurements": {"v": 3}}],
                }
            ),
            encoding="utf-8",
        )
    return reduce_gate(
        "g.yaml",
        [{"field": "measurements.v", "expect": expect, "source": "c", "run": "20260101T000000Z_r"}],
        runs_dir=runs,
    )


def test_a_reduction_becomes_the_verdict_without_being_retyped(ledger, job, tmp_path):
    reduction = _reduce(tmp_path, 3)
    handoff, outcome = ledger.record_reduction(job.job_id, reduction)
    assert outcome == "satisfied"
    assert handoff.payload["conditions"][0]["observed"] == 3
    assert handoff.failure_signature is None


def test_only_the_ledger_can_turn_a_repeated_failure_into_exhausted(ledger, job, tmp_path):
    """The reducer sees one iteration. Three in a row is a fact only the ledger holds."""
    outcomes = []
    for iteration in (1, 2, 3):
        ledger.record_handoff(
            job.job_id,
            "Verdict",
            "verifier",
            _reduce(tmp_path, 99).describe() | {"iteration": iteration},
            iteration=iteration,
            failure_signature=_reduce(tmp_path, 99).failure_signature,
        )
        outcomes.append("exhausted" if ledger.exhausted(job.job_id) else "open")
    assert outcomes == ["open", "open", "exhausted"]


def test_a_missing_run_reaches_the_job_as_primary_run_missing(ledger, job, tmp_path):
    reduction = _reduce(tmp_path, 3, run_present=False)
    _, outcome = ledger.record_reduction(job.job_id, reduction)
    assert outcome == "primary_run_missing"
    assert outcome in J.JOB_OUTCOMES
