"""Stage 2: real temporary SQLite + deterministic offline runtime, no model/devices."""
import pytest

from ai_hardware_autolab.orchestrator.conversation_flow import ConversationFlow
from ai_hardware_autolab.orchestrator.jobs import JobLedger


def test_committed_verdict_finalizes_after_restart_without_runtime(setup, monkeypatch):
    ledger, workspace = setup
    (workspace / "result.txt").write_text("offline result")
    calls = []
    def runtime(role, context):
        calls.append(role)
        return reply("Offline result", "satisfied" if role == "verifier" else "ready",
                     artifacts=["result.txt"] if role != "lead" else [])
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Offline demonstration", workspace)
    flow.advance(job.job_id)
    flow.advance(job.job_id)
    original = ledger.transition
    def crash_before_finish(job_id, state, *args, **kwargs):
        if state == "done":
            raise RuntimeError("simulated process loss after reply commit")
        return original(job_id, state, *args, **kwargs)
    monkeypatch.setattr(ledger, "transition", crash_before_finish)
    with pytest.raises(RuntimeError, match="process loss"):
        flow.advance(job.job_id)
    restarted = ConversationFlow(JobLedger(ledger.path), runtime)
    assert restarted.advance(job.job_id).done
    assert calls == ["lead", "builder", "verifier"]


def test_hardware_mode_not_adopted_by_offline_prototype(setup):
    from ai_hardware_autolab.orchestrator.conversation_flow import WORKFLOW
    ledger, workspace = setup
    job = ledger.create_job("__conversation_prototype__", "Hardware request", brief={
        "workflow": WORKFLOW, "workspace": str(workspace), "execution_mode": "hardware",
    })
    def never(*args):
        pytest.fail("offline prototype must not invoke a hardware runtime")
    with pytest.raises(ValueError, match="live hardware adapter"):
        ConversationFlow(ledger, never).advance(job.job_id)
    assert ledger.job(job.job_id).state == "pending"


@pytest.fixture
def setup(tmp_path):
    workspace = tmp_path / "isolated"
    workspace.mkdir()
    return JobLedger(tmp_path / "test.db"), workspace


def reply(summary, status="ready", **kw):
    return dict(status=status, summary=summary, **kw)


def test_plain_plan_to_builder_to_verifier_without_contract(setup):
    ledger, workspace = setup
    calls = []
    def runtime(role, task):
        calls.append(role)
        assert task["objective"] == "Implement an unfamiliar board interface (offline prototype)"
        assert task["hardware_access"] is False
        assert "contract" not in task and "steps" not in task
        if role == "lead":
            return reply("Use the relevant datasheet, reuse suitable code; choose buffering in Builder.")
        if role == "builder":
            assert "buffering" in task["latest_messages"]["lead"]["summary"]
            (workspace / "result.txt").write_text("offline demonstration, not hardware PASS")
            return reply("Implemented the offline demonstration", artifacts=["result.txt"])
        assert task["artifacts"][0]["path"] == "result.txt"
        return reply("Offline demonstration meets the requested prototype", "satisfied", artifacts=["result.txt"])
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Implement an unfamiliar board interface (offline prototype)", workspace)
    for _ in range(3):
        # Each advance starts from a fresh engine and reopens the same ledger.
        result = ConversationFlow(JobLedger(ledger.path), runtime).advance(job.job_id)
    assert result.done and ledger.job(job.job_id).outcome == "satisfied"
    assert calls == ["lead", "builder", "verifier"]
    assert not (workspace.parent / "contracts").exists()
    assert not ledger.pending_approvals(job.job_id)


@pytest.mark.parametrize("target,expected", [
    ("builder", ["lead", "builder", "verifier", "builder", "verifier"]),
    ("lead", ["lead", "builder", "verifier", "lead", "builder", "verifier"]),
])
def test_verifier_routes_actual_failure_with_prior_context(setup, target, expected):
    ledger, workspace = setup
    calls, builds, reviews = [], [], []
    def runtime(role, task):
        calls.append(role)
        if role == "lead":
            if reviews:
                assert "wrong output" in task["latest_messages"]["verifier"]["summary"]
            return reply("Choose suitable implementation; no fixed tool sequence.")
        if role == "builder":
            if builds:
                assert task["artifacts"][0]["path"] == "attempt-1.txt"
                assert "wrong output" in task["latest_messages"]["verifier"]["summary"]
            builds.append(len(builds) + 1)
            name = f"attempt-{len(builds)}.txt"
            (workspace / name).write_text("bad" if len(builds) == 1 else "correct")
            return reply("Actual attempt recorded", artifacts=[name])
        reviews.append(1)
        if len(reviews) == 1:
            return reply("wrong output: correct the data ordering", "revise", target=target)
        return reply("Second result is correct", "satisfied", artifacts=["attempt-2.txt"])
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Implement data ordering", workspace)
    for _ in expected:
        flow.advance(job.job_id)
    assert calls == expected
    assert ledger.job(job.job_id).outcome == "satisfied"
    assert len(builds) == 2


def test_feedback_during_verification_prevents_stale_completion(setup):
    ledger, workspace = setup
    (workspace / "result.txt").write_text("old requirement")
    def runtime(role, task):
        if role == "lead": return reply("Approach")
        if role == "builder": return reply("Result", artifacts=["result.txt"])
        flow.feedback(job.job_id, "Also reverse the bytes", actor="operator")
        return reply("Old requirement met", "satisfied", artifacts=["result.txt"])
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Original requirement", workspace)
    for _ in range(3): state = flow.advance(job.job_id)
    assert state.role == "lead" and not state.done
    context = flow._context(job.job_id, "lead")
    assert context["feedback"][0]["message"] == "Also reverse the bytes"
    assert context["objective"] == "Original requirement"
    assert [e.agent for e in ledger.events_since(0, job.job_id) if e.kind == "conversation.feedback"] == ["human"]


def test_question_answer_returns_to_lead_without_new_authority(setup):
    ledger, workspace = setup
    flow = ConversationFlow(ledger, lambda *_: reply("Which connector is attached?", "needs_user"))
    job = flow.create("Inspect connection", workspace)
    flow.advance(job.job_id)
    assert ledger.job(job.job_id).state == "blocked"
    flow.answer(job.job_id, "CH3")
    assert flow.phase(job.job_id).role == "lead"
    assert flow._context(job.job_id, "lead")["hardware_access"] is False


@pytest.mark.parametrize("fail", ["crash", "invalid_reply"])
def test_uncommitted_attempt_does_not_replay(setup, fail):
    ledger, workspace = setup
    calls = []
    def runtime(*_):
        calls.append(1)
        if fail == "crash": raise RuntimeError("runtime disconnected")
        return {"summary": "Malformed final response"}
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Do something", workspace)
    assert flow.advance(job.job_id).action == "blocked"
    assert ConversationFlow(ledger, runtime).advance(job.job_id).action == "blocked"
    with pytest.raises(ValueError, match="only an explicit question"):
        flow.answer(job.job_id, "retry")
    assert calls == [1]


def test_process_loss_after_dispatch_is_not_a_new_turn(setup):
    ledger, workspace = setup
    flow = ConversationFlow(ledger, lambda *_: pytest.fail("do not replay"))
    job = flow.create("Existing work", workspace)
    ledger.transition(job.job_id, "running")
    ledger.append_event(job.job_id, "conversation.turn.started", {"feedback_through": 0}, "builder")
    assert flow.advance(job.job_id).action == "interrupted"
    assert flow.advance(job.job_id).action == "blocked"


def test_cancel_during_call_keeps_late_reply_from_finishing(setup):
    ledger, workspace = setup
    def runtime(*_):
        flow.cancel(job.job_id)
        return reply("Late result")
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Cancel test", workspace)
    assert flow.advance(job.job_id).done
    assert ledger.job(job.job_id).outcome == "cancelled"
    assert not [e for e in ledger.events_since(0, job.job_id) if e.kind == "conversation.turn.completed"]
    assert flow.advance(job.job_id).done


@pytest.mark.parametrize("mode", ["missing", "outside", "changed", "no_evidence", "builder_pass"])
def test_success_cannot_be_manufactured_by_model_fields(setup, mode):
    ledger, workspace = setup
    (workspace / "result.txt").write_text("actual")
    (workspace.parent / "outside.txt").write_text("outside")
    def runtime(role, task):
        if role == "lead":
            task["objective"] = "mutated"
            return reply("Plan", hardware_access=True, next_role="verifier")
        assert task["objective"] == "Original goal" and task["hardware_access"] is False
        if role == "builder":
            if mode == "builder_pass": return reply("done", "satisfied", artifacts=["result.txt"])
            return reply("built", artifacts=["result.txt"])
        if mode == "changed": (workspace / "result.txt").write_text("changed after handoff")
        refs = {"missing": ["missing.txt"], "outside": ["../outside.txt"], "no_evidence": []}.get(mode, ["result.txt"])
        return reply("Trust me", "satisfied", artifacts=refs)
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Original goal", workspace)
    for _ in range(3): flow.advance(job.job_id)
    assert ledger.job(job.job_id).state == "blocked"


def test_turn_budget_does_not_mean_success(setup):
    ledger, workspace = setup
    flow = ConversationFlow(ledger, lambda role, _: reply("Need another try", "revise", target="builder"))
    job = flow.create("Budget test", workspace, max_turns=3)
    for _ in range(4): flow.advance(job.job_id)
    assert ledger.job(job.job_id).state == "blocked"
    assert ledger.job(job.job_id).outcome is None
    assert len([e for e in ledger.events_since(0, job.job_id) if e.kind.endswith("turn.started")]) == 3


def test_old_job_is_not_adopted_and_hardware_mode_is_not_silently_enabled(setup):
    ledger, workspace = setup
    flow = ConversationFlow(ledger, lambda *_: pytest.fail("not the prototype's job"))
    old = ledger.create_job("old-gate", "Old job")
    with pytest.raises(ValueError, match="does not belong"):
        flow.advance(old.job_id)
    assert ledger.job(old.job_id).state == "pending"
