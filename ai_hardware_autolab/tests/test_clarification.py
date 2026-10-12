"""Question-only Plans, durable replies and resumption without live devices/LLMs."""
from __future__ import annotations

import copy
import json

import pytest

from ai_hardware_autolab.agents.schema import ValidationError, validate_handoff
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator import loop as L
from ai_hardware_autolab.orchestrator.clarification import (
    LEGACY_EMPTY_PLAN_ERROR, pending_clarification, plan_question,
)
from ai_hardware_autolab.orchestrator.control import ControlPlane, pending_steers
from ai_hardware_autolab.orchestrator.daemon import Daemon, WAITING_PERSON
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission
from ai_hardware_autolab.tests.test_autonomous_development import plan, resources
from ai_hardware_autolab.tests.test_autonomous_flow_review import make_loop


def question(**extra):
    return {"kind": "Plan", "agent": "lead-agent", "objective": "Measure the original requested waveform",
            "requirements_complete": False, "steps": [], "blocked_on": "user_input",
            "clarification_question": "스코프 연결 상태와 프로브 배율은 무엇인가요?", **extra}


def setup_job(tmp_path, runner):
    ledger = JobLedger(tmp_path / "jobs.db")
    job, _ = start_simple_mission(ledger, "scope", "Measure the original waveform", resources=resources())
    return ledger, job, make_loop(ledger, runner, tmp_path)


def record_wait(ledger, job, *, legacy=False):
    ledger.transition(job.job_id, J.RUNNING)
    iteration = ledger.begin_iteration(job.job_id)
    record = ledger.record_handoff(job.job_id, "Plan", "lead-agent", question(), iteration=iteration)
    ledger.transition(job.job_id, J.BLOCKED, reason=LEGACY_EMPTY_PLAN_ERROR if legacy else "user_input: probe ratio?")
    return record


def test_question_waits_without_reducer_approval_or_repeated_model_turns(tmp_path):
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        return question()
    ledger, job, loop = setup_job(tmp_path, runner)
    assert loop.advance(job.job_id).reason_code == "user_input"
    assert ledger.job(job.job_id).state == J.BLOCKED
    assert not ledger.handoffs(job.job_id, "Verdict")
    assert not ledger.handoffs(job.job_id, "StepResult")
    assert not ledger.pending_approvals(job.job_id)
    tip = ledger.latest_event_id()
    restarted = make_loop(JobLedger(ledger.path), runner, tmp_path)
    daemon = Daemon(ledger, restarted)
    assert daemon.waiting_for(job.job_id).status == WAITING_PERSON
    assert daemon.waiting_for(job.job_id).detail.startswith("user_input:")
    for _ in range(3):
        assert daemon.tick().advanced is None
        assert restarted.advance(job.job_id).reason_code == "user_input"
    assert ledger.latest_event_id() == tip
    assert calls == ["lead-agent"]


@pytest.mark.parametrize("legacy", [False, True])
def test_web_feedback_replans_then_executes_with_preserved_authority(tmp_path, legacy):
    calls = []
    def runner(agent, task):
        calls.append((agent.name, copy.deepcopy(task)))
        if agent.name == "lead-agent":
            assert task["steering"][0]["message"] == "LAN 연결을 복구했고 실제 프로브는 10x입니다."
            assert task["previous_clarification"]["steps"] == []
            return {**plan(), "iteration": task["iteration"]}
        if agent.name == "builder":
            directory = tmp_path / "runs" / "fresh-run"
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(json.dumps({"verdict": "PASS", "steps": [
                {"tool": "codex_workspace", "measurements": {"execution_integrity": True, "last_command_exit_code": 0}}]}))
            return {"status": "done", "runs": ["fresh-run"]}
        return {"requirements_met": True, "analysis": "checked the new run"}
    ledger, job, loop = setup_job(tmp_path, runner)
    saved = record_wait(ledger, job, legacy=legacy)
    brief = ledger.handoffs(job.job_id, "Brief")[0]
    before = ledger.latest_event_id()
    daemon = Daemon(ledger, loop)
    assert daemon.waiting_for(job.job_id).detail.startswith("user_input:")
    assert ledger.latest_event_id() == before  # old rows are projected, not rewritten
    control = ControlPlane(ledger, daemon)
    reply = control.steer(job.job_id, "LAN 연결을 복구했고 실제 프로브는 10x입니다.", "local-operator")
    assert reply["will_be_delivered"] is True
    assert calls == []  # Sending a reply does not execute anything inline.
    restarted = Daemon(JobLedger(ledger.path), make_loop(JobLedger(ledger.path), runner, tmp_path))
    assert restarted.waiting_for(job.job_id).runnable
    assert restarted.tick().advanced.action == L.PLAN
    assert ledger.job(job.job_id).iteration == 2
    assert calls == []
    restarted.tick()  # Lead, not a previous hardware step
    assert [name for name, _ in calls] == ["lead-agent"]
    assert not pending_steers(ledger, job.job_id)
    restarted.loop.run(job.job_id)
    assert [name for name, _ in calls] == ["lead-agent", "builder", "verifier"]
    assert ledger.job(job.job_id).outcome == "satisfied"
    assert ledger.handoffs(job.job_id, "Plan", 1)[0].sha256 == saved.sha256
    assert ledger.handoffs(job.job_id, "Brief")[0].sha256 == brief.sha256
    assert not ledger.pending_approvals(job.job_id)


def test_reply_before_question_does_not_release_it_and_a_new_question_waits_again(tmp_path):
    calls = []
    def runner(agent, task):
        calls.append(task)
        return question(clarification_question="아직 프로브의 실제 배율이 필요합니다.")
    ledger, job, loop = setup_job(tmp_path, runner)
    control = ControlPlane(ledger, Daemon(ledger, loop))
    control.steer(job.job_id, "older feedback", "human")
    loop.advance(job.job_id)
    assert not pending_clarification(ledger, job.job_id).answers
    control.steer(job.job_id, "케이블은 연결했어요.", "human")
    loop.advance(job.job_id)
    loop.advance(job.job_id)
    assert len(calls) == 2
    assert not pending_clarification(ledger, job.job_id).answers
    assert not control.daemon.waiting_for(job.job_id).runnable


@pytest.mark.parametrize("reason", ["runtime_failure: process died", "unknown_hardware_state: interrupted",
                                  "scheduler_failure: unrelated error", "rate_limited: 429"])
def test_feedback_does_not_unlock_other_failures(tmp_path, reason):
    ledger, job, loop = setup_job(tmp_path, lambda *_: pytest.fail("must not run"))
    record_wait(ledger, job)
    ledger.transition(job.job_id, J.RUNNING)
    ledger.transition(job.job_id, J.BLOCKED, reason=reason)
    ledger.append_event(job.job_id, "control.steer", {"message": "continue", "by": "human"}, "human")
    assert pending_clarification(ledger, job.job_id) is None
    assert loop.advance(job.job_id).blocked


def test_cancelled_question_cannot_resume(tmp_path):
    ledger, job, loop = setup_job(tmp_path, lambda *_: question())
    loop.advance(job.job_id)
    control = ControlPlane(ledger, Daemon(ledger, loop))
    control.cancel(job.job_id, "human", "cancelled")
    assert pending_clarification(ledger, job.job_id) is None
    assert loop.advance(job.job_id).action == L.FINISHED
    assert control.handle({"command": "steer", "job_id": job.job_id, "message": "answer", "by": "human"})["ok"] is False


def test_question_plan_survives_crash_before_block_transition(tmp_path):
    ledger, job, loop = setup_job(tmp_path, lambda *_: pytest.fail("must wait"))
    ledger.transition(job.job_id, J.RUNNING)
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id, "Plan", "lead-agent", question(), iteration=1)
    assert loop.phase(job.job_id) == L.BLOCKED
    assert loop.advance(job.job_id).reason_code == "user_input"


def test_reply_survives_crash_between_resume_and_next_iteration(tmp_path):
    ledger, job, loop = setup_job(tmp_path, lambda *_: pytest.fail("only open the next iteration"))
    record_wait(ledger, job)
    ledger.append_event(job.job_id, "control.steer", {"message": "connection restored", "by": "human"}, "human")
    ledger.transition(job.job_id, J.RUNNING, reason="scheduler resumed after reply")
    restarted = make_loop(JobLedger(ledger.path), loop.runner, tmp_path)
    assert restarted.advance(job.job_id).action == L.PLAN
    assert ledger.job(job.job_id).iteration == 2
    assert restarted.phase(job.job_id) == L.PLAN


def test_answer_does_not_silently_increase_iteration_budget(tmp_path):
    ledger, job, loop = setup_job(tmp_path, lambda *_: pytest.fail("budget exhausted"))
    record_wait(ledger, job)
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id, "Plan", "lead-agent", question(), iteration=2)
    ledger.transition(job.job_id, J.RUNNING)
    ledger.transition(job.job_id, J.BLOCKED, reason="user_input: another question")
    control = ControlPlane(ledger, Daemon(ledger, loop))
    assert control.steer(job.job_id, "answer", "human")["will_be_delivered"] is False
    assert loop.advance(job.job_id).outcome == "exhausted"
    assert ledger.job(job.job_id).iteration == 2


def test_prior_execution_and_review_survive_question_only_iteration(tmp_path):
    ledger, job, loop = setup_job(tmp_path, lambda *_: question())
    ledger.transition(job.job_id, J.RUNNING)
    ledger.begin_iteration(job.job_id)
    result = {"kind": "StepResult", "agent": "builder", "status": "failed", "runs": ["failed-capture"], "summary": "network timeout"}
    ledger.record_handoff(job.job_id, "StepResult", "builder", result, iteration=1, step_id="capture")
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id, "Plan", "lead-agent", question(), iteration=2)
    assert loop._previous_step_results(job.job_id, 3) == [result]
    assert loop._previous_step_results(job.job_id, 2) == [result]


@pytest.mark.parametrize("bad", [True, {}, [], " "])
def test_invalid_question_reason_gets_format_validation_not_scheduler_failure(bad):
    with pytest.raises(ValidationError, match="blocked_on"):
        validate_handoff(question(blocked_on=bad, gate="__mission__", iteration=1))


def test_legacy_terminal_plans_and_executable_plans_are_not_questions():
    assert plan_question(question(blocked_on="contract_gap")) is None
    assert plan_question(question(blocked_on="exhausted")) is None
    assert plan_question(question(steps=[{"id": "capture"}])) is None
    assert plan_question(question(blocked_on="operator must confirm", clarification_question=None)) == "operator must confirm"
