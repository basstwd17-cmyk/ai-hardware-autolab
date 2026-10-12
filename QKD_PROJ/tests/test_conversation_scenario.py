"""Scenario-level regression: real ledger/files, deterministic model boundary."""
import hashlib
import json
from pathlib import Path

import pytest

from QKD_PROJ.orchestrator import conversation_cli as cli
from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
from QKD_PROJ.orchestrator.jobs import JobLedger


def test_one_run_repairs_same_file_and_passes_latest_request(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    calls = []
    versions = []

    def runtime(role, context):
        calls.append(role)
        incoming = context["handoff"]
        if role == "lead":
            assert incoming is None
            return {"status": "ready", "summary": "Implement addition using the existing source."}
        assert context["latest_messages"][incoming["from"]]["event_id"] == incoming["event_id"]
        if role == "builder":
            if versions:
                assert incoming["from"] == "verifier"
                assert "subtracts" in context["latest_messages"]["verifier"]["summary"]
                assert (workspace / "addition.py").read_text() == "def add(a, b): return a - b"
            source = "def add(a, b): return a " + ("+" if versions else "-") + " b"
            (workspace / "addition.py").write_text(source)
            versions.append(hashlib.sha256(source.encode()).hexdigest())
            return {"status": "ready", "summary": "Implementation is available", "artifacts": ["addition.py"]}
        assert incoming["from"] == "builder"
        assert context["artifacts"] == [{"path": "addition.py", "sha256": versions[-1]}]
        namespace = {}
        exec((workspace / "addition.py").read_text(), namespace)
        if namespace["add"](2, 3) != 5:
            return {"status": "revise", "target": "builder", "summary": "The function subtracts; use addition."}
        return {"status": "satisfied", "summary": "The result is 5", "artifacts": ["addition.py"]}

    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Implement addition", workspace)
    assert flow.run(job.job_id).done
    assert calls == ["lead", "builder", "verifier", "builder", "verifier"]
    recorded = [e.payload["artifacts"][0]["sha256"] for e in ledger.events_since(0, job.job_id)
                if e.kind == "conversation.turn.completed" and e.agent == "builder"]
    assert recorded == versions and len(set(recorded)) == 2
    assert not ledger.handoffs(job.job_id, "Plan")
    assert not ledger.pending_approvals(job.job_id)


def test_old_file_version_cannot_satisfy_latest_handoff(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    builds = []
    def runtime(role, context):
        if role == "lead":
            return {"status": "ready", "summary": "Implement the requirement"}
        if role == "builder":
            builds.append(1)
            (root / "result.txt").write_text("old" if len(builds) == 1 else "new")
            return {"status": "ready", "summary": "Result", "artifacts": ["result.txt"]}
        if len(builds) == 1:
            return {"status": "revise", "target": "builder", "summary": "Update the result"}
        (root / "result.txt").write_text("old")
        return {"status": "satisfied", "summary": "Stale result", "artifacts": ["result.txt"]}
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Update result", root)
    assert flow.run(job.job_id).action == "blocked"
    assert ledger.job(job.job_id).outcome is None


def test_auto_run_stops_for_question_and_resumes_existing_workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    calls = []
    def runtime(role, context):
        calls.append(role)
        if not context["feedback"]:
            return {"status": "needs_user", "summary": "Which two inputs?"}
        assert context["feedback"][0]["message"] == "2 and 3"
        return {"status": "satisfied" if role == "verifier" else "ready", "summary": "The sum is 5"}
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Explain addition for my inputs", root)
    assert flow.run(job.job_id).action == "needs_user"
    restarted = ConversationFlow(JobLedger(ledger.path), runtime)
    assert "Which two inputs?" in restarted.phase(job.job_id).detail
    assert restarted.run(job.job_id).action == "blocked"
    assert calls == ["lead"]
    restarted.answer(job.job_id, "2 and 3")
    assert restarted.run(job.job_id).done
    assert calls == ["lead", "lead", "builder", "verifier"]


def test_auto_run_stops_at_existing_budget_without_extra_runtime(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    calls = []
    def runtime(role, context):
        calls.append(role)
        return {"status": "revise", "target": "builder", "summary": "Still working"}
    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Unfinished work", root, max_turns=3)
    assert flow.run(job.job_id).action == "blocked"
    assert len(calls) == 3
    assert "budget" in flow.phase(job.job_id).detail
    assert flow.run(job.job_id).action == "blocked" and len(calls) == 3


def args(tmp_path, action):
    return [action, "--ledger", str(tmp_path / "jobs.db"), "--workspaces", str(tmp_path / "workspaces")]


def test_cli_run_creates_and_finishes_one_scenario(tmp_path, monkeypatch, capsys):
    calls = []
    class Runtime:
        def __init__(self, config, **kwargs):
            pass
        def __call__(self, role, context):
            calls.append(role)
            return {"status": "satisfied" if role == "verifier" else "ready", "summary": "Explained the requested concept"}
    monkeypatch.setattr(cli, "ConversationCodexRuntime", Runtime)
    assert cli.main(args(tmp_path, "run") + ["--objective", "Explain addition"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["job_id"] == rows[-1]["job_id"]
    assert rows[-1]["action"] == "finished"
    assert calls == ["lead", "builder", "verifier"]
    assert cli.main(args(tmp_path, "run") + ["--job", rows[0]["job_id"]]) == 0
    assert calls == ["lead", "builder", "verifier"]


def test_cli_status_answer_feedback_and_cancel_do_not_construct_runtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ConversationCodexRuntime", lambda *_a, **_kw: pytest.fail("no runtime needed"))
    assert cli.main(args(tmp_path, "create") + ["--objective", "Explain addition"]) == 0
    job_id = json.loads(capsys.readouterr().out)["job_id"]
    assert cli.main(args(tmp_path, "status") + ["--job", job_id]) == 0
    assert cli.main(args(tmp_path, "feedback") + ["--job", job_id, "--message", "Use integers"]) == 0
    ledger = JobLedger(tmp_path / "jobs.db")
    flow = ConversationFlow(ledger, lambda *_: {"status": "needs_user", "summary": "Which numbers?"})
    flow.advance(job_id)
    assert cli.main(args(tmp_path, "answer") + ["--job", job_id, "--message", "2 and 3"]) == 0
    assert flow.phase(job_id).role == "lead"
    assert cli.main(args(tmp_path, "cancel") + ["--job", job_id]) == 0
    assert ledger.job(job_id).outcome == "cancelled"


def test_cli_ctrl_c_records_cancellation_and_releases_lock(tmp_path, monkeypatch, capsys):
    class Runtime:
        def __init__(self, *_a, **_kw):
            pass
        def __call__(self, *_a):
            raise KeyboardInterrupt
    monkeypatch.setattr(cli, "ConversationCodexRuntime", Runtime)
    assert cli.main(args(tmp_path, "run") + ["--objective", "Interrupted work"]) == 130
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["detail"] == "cancelled"
    assert cli.main(args(tmp_path, "status") + ["--job", rows[0]["job_id"]]) == 0


@pytest.mark.parametrize("extra", [["--objective", " "], ["--job", "job_missing", "--objective", "ignored"], ["--objective", "Task", "--message", "ignored"]])
def test_cli_rejects_ambiguous_input_before_creating_state(tmp_path, extra):
    with pytest.raises(SystemExit) as exc:
        cli.main(args(tmp_path, "run") + extra)
    assert exc.value.code == 2
    assert not (tmp_path / "jobs.db").exists()
