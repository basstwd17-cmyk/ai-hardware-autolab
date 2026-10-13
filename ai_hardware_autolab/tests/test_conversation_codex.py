"""No model tokens or hardware: adapter boundary and real stdio peer tests."""
import json
import sys
from pathlib import Path

import pytest

from ai_hardware_autolab.orchestrator.codex_transport import CodexRunnerError, StdioAppServer
from ai_hardware_autolab.orchestrator.conversation_codex import (
    ConversationCodexConfig, ConversationCodexRuntime, live_client, server_command,
    ledger_event_sink, reply_schema,
)
from ai_hardware_autolab.orchestrator.conversation_flow import ConversationFlow
from ai_hardware_autolab.orchestrator.jobs import JobLedger


@pytest.fixture
def env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "ledger.db")
    job = ConversationFlow(ledger, None).create("Implement addition", workspace)
    context = ConversationFlow(ledger, None)._context(job.job_id, "builder")
    context["attempt_id"] = 123
    return ConversationCodexConfig(tmp_path), context, workspace, ledger, job


def answer(status="ready", **kwargs):
    return json.dumps(dict(status=status, summary="Actual result", artifacts=[], target=None, **kwargs))


class Client:
    def __init__(self, response, calls, repair, sink):
        self.response, self.calls, self.repair, self.sink = response, calls, repair, sink
        self.closed = False
        self.unsubscribed = False

    def start_thread(self, params):
        self.calls.append(("thread", params, self.repair))
        return "test-thread"

    def run_turn(self, thread_id, params, *, server_request):
        self.calls.append(("turn", params, self.repair))
        if callable(self.response):
            return self.response(params, server_request)
        return self.response

    def unsubscribe(self, thread_id): self.unsubscribed = True
    def close(self): self.closed = True
    def interrupt_turn(self, thread_id): return True


def runner(env, responses):
    config, context, *_ = env
    calls, clients, events = [], [], []
    def factory(config, workspace, repair, sink):
        client = Client(responses.pop(0), calls, repair, sink)
        clients.append(client)
        return client
    return ConversationCodexRuntime(config, client_factory=factory, event_sink=events.append), calls, clients, events


def test_native_workspace_shell_is_the_only_command_path(env):
    runtime, calls, _, _ = runner(env, [answer()])
    assert runtime('builder', env[1])['status'] == 'ready'
    assert 'dynamicTools' not in calls[0][1]
    assert 'features.shell_tool=true' in server_command(env[0])
    assert 'features.unified_exec=true' in server_command(env[0])


def test_builder_receives_direct_context_and_can_fix_native_command_errors(env):
    def work(params, request):
        assert request("item/commandExecution/requestApproval", {
            "command": "python -m pytest tests/test_sum.py -q", "cwd": env[2].as_posix()
        }) == {"decision": "accept"}
        # A nonzero tool exit is an ordinary native model-loop observation,
        # not an exception at the final-message transport seam.
        return answer()
    runtime, calls, clients, events = runner(env, [work])
    runtime("builder", env[1])
    params = calls[1][1]
    assert params["sandboxPolicy"]["writableRoots"] == [str(env[2])]
    assert params["sandboxPolicy"]["networkAccess"] is False
    payload = json.loads(params["input"][0]["text"].split("\n", 1)[1])
    assert payload["objective"] == "Implement addition"
    assert "handoff" not in params["outputSchema"]["properties"]
    assert "contract" not in payload
    assert clients[0].closed and clients[0].unsubscribed


def test_invalid_final_reply_only_repairs_text_without_replaying_work(env):
    runtime, calls, clients, events = runner(env, ['{"summary":"work already performed"}', answer()])
    assert runtime("builder", env[1])["status"] == "ready"
    turns = [c for c in calls if c[0] == "turn"]
    assert len(turns) == 2 and [c[2] for c in turns] == [False, True]
    assert turns[1][1]["sandboxPolicy"]["type"] == "readOnly"
    assert "work already performed" in turns[1][1]["input"][0]["text"]
    assert all(c.closed for c in clients)
    assert any(e["method"] == "conversation.runtime.format_repair" for e in events)


def test_repair_failure_is_bounded(env):
    runtime, calls, clients, events = runner(env, ['not JSON', 'still not JSON'])
    with pytest.raises(CodexRunnerError, match="not replayed"):
        runtime("builder", env[1])
    assert len(clients) == 2 and all(c.closed for c in clients)


def test_transport_loss_not_retried_or_treated_as_formatting(env):
    def failure(*args): raise CodexRunnerError("disconnected after command")
    runtime, calls, clients, events = runner(env, [failure])
    with pytest.raises(CodexRunnerError, match="disconnected"):
        runtime("builder", env[1])
    assert len(clients) == 1 and clients[0].closed
    assert not any(e["method"].endswith("format_repair") for e in events)


@pytest.mark.parametrize("role", ["lead", "verifier"])
def test_non_builder_roles_read_only(env, role):
    runtime, calls, clients, events = runner(env, [answer("needs_user")])
    context = dict(env[1], role=role)
    runtime(role, context)
    assert calls[1][1]["sandboxPolicy"]["type"] == "readOnly"


@pytest.mark.parametrize("change", ["hardware", "outside", "root", "role"])
def test_invalid_host_context_never_creates_thread(env, change):
    runtime, calls, clients, events = runner(env, [])
    context = dict(env[1])
    if change == "hardware": context["hardware_access"] = True
    if change == "outside": context["workspace"] = str(env[0].workspaces_root.parent)
    if change == "root": context["workspace"] = str(env[0].workspaces_root)
    if change == "role": context["role"] = "lead"
    with pytest.raises(ValueError): runtime("builder", context)
    assert not clients


@pytest.mark.parametrize("approval", [
    {"command": "ssh xilinx@192.168.50.2 reboot"},
    {"command": "python code.py", "additionalPermissions": {"network": True}},
    {"command": "python code.py", "cwd": "C:/"},
])
def test_no_hardware_or_scope_expansion_approval(env, approval):
    runtime, *_ = runner(env, [])
    assert runtime._approval("builder", env[2], False,
        "item/commandExecution/requestApproval", approval)["decision"] == "decline"


def test_repair_requests_no_tools(env):
    runtime, *_ = runner(env, [])
    assert runtime._approval("builder", env[2], True,
        "item/commandExecution/requestApproval", {"command": "python -m pytest"})["decision"] == "decline"
    command = server_command(env[0], True)
    assert "features.shell_tool=false" in command
    assert "features.unified_exec=false" in command
    assert "features.apply_patch_freeform=false" in command
    assert 'web_search="disabled"' in command
    assert not any("ai_hardware_autolab.mcp_server" in v for v in command)
    assert runtime._approval("builder", env[2], False, "item/commandExecution/requestApproval", {
        "command": "python addition.py", "proposedExecpolicyAmendment": ["python"]}) == {"decision": "accept"}


@pytest.mark.parametrize("path,repair,role,expected", [
    ("addition.py", False, "builder", "accept"),
    ("../outside.py", False, "builder", "decline"),
    ("addition.py", True, "builder", "decline"),
    ("addition.py", False, "verifier", "decline"),
])
def test_observed_workspace_file_patch_approval(env, path, repair, role, expected):
    runtime, *_ = runner(env, [])
    decision = runtime._approval(role, env[2], repair, "item/fileChange/requestApproval", {},
        changes=[{"path": path, "kind": {"type": "add"}}])
    assert decision["decision"] == expected


def test_cancel_between_turns_never_starts_repair(env):
    runtime = None
    def work(*args):
        assert runtime.cancel(env[1]["job_id"])
        return "invalid response"
    runtime, calls, clients, events = runner(env, [work])
    with pytest.raises(CodexRunnerError, match="cancelled"):
        runtime("builder", env[1])
    assert len(clients) == 1 and clients[0].closed


def test_inherited_integrations_disabled_before_thread(monkeypatch, env):
    launched, closed = [], []
    class ConfigPeer:
        def __init__(self, **kwargs): launched.append(kwargs["command"])
        def _request(self, *args):
            return {"config": {"web_search": "live", "mcp_servers": {"instrument": {"enabled": len(launched) == 1}}}}
        def close(self): closed.append(True)
    monkeypatch.setattr("ai_hardware_autolab.orchestrator.conversation_codex.StdioAppServer", ConfigPeer)
    client = live_client(env[0], env[2], False, None)
    assert len(launched) == 2 and len(closed) == 1
    assert 'mcp_servers.instrument.enabled=false' in launched[1]
    client.close()


def test_native_research_enabled_without_shell_network(env):
    assert 'web_search="live"' in server_command(env[0])
    runtime, calls, _, events = runner(env, [answer("ready", sources=[{
        "title": "Manufacturer datasheet", "url": "https://example.com/datasheet.pdf",
        "finding": "Section 4 describes the transfer interface.",
    }])])
    reply = runtime("lead", dict(env[1], role="lead"))
    assert reply["sources"][0]["finding"].startswith("Section 4")
    assert calls[1][1]["sandboxPolicy"]["networkAccess"] is False
    assert any(e["method"] == "conversation.research.configured" and
               e["params"]["web_search"] == "live" for e in events)


def test_effective_research_configuration_mismatch_is_reported(monkeypatch, env):
    closed = []
    class ConfigPeer:
        def __init__(self, **_kwargs): pass
        def _request(self, *_args): return {"config": {"web_search": "disabled"}}
        def close(self): closed.append(True)
    monkeypatch.setattr("ai_hardware_autolab.orchestrator.conversation_codex.StdioAppServer", ConfigPeer)
    with pytest.raises(CodexRunnerError, match="web_search configuration"):
        live_client(env[0], env[2], False, None)
    assert closed == [True]


def test_web_activity_is_available_to_next_role(env):
    ledger, job = env[3], env[4]
    sink = ledger_event_sink(ledger)
    sink({"job_id": job.job_id, "agent": "lead", "attempt_id": 42,
          "method": "item/completed", "params": {"item": {"id": "web1", "type": "webSearch",
          "action": {"type": "openPage", "url": "https://example.com/datasheet.pdf"}}}})
    context = ConversationFlow(JobLedger(ledger.path), None)._context(job.job_id, "builder")
    assert context["research_activity"][-1]["action"]["type"] == "openPage"
    assert context["research_sources"] == []  # observed activity is not a fabricated finding


def test_actual_stdio_peer_edits_runs_fixes_and_hands_off(env):
    config, context, workspace, ledger, job = env
    events = []
    def factory(config, workspace, repair, sink):
        assert not repair
        return StdioAppServer(command=(sys.executable, str(Path(__file__).parent /
            "fixtures/conversation_app_server.py")), cwd=workspace, timeout_seconds=15,
            client_info={"name": "test", "version": "1"}, event_sink=sink)
    runtime = ConversationCodexRuntime(config, client_factory=factory, event_sink=events.append)
    for _ in range(3):
        state = ConversationFlow(JobLedger(ledger.path), runtime).advance(job.job_id)
    assert state.done
    assert (workspace / "result.txt").read_text().strip() == "5"
    commands = [e for e in events if (e["params"].get("item") or {}).get("type") == "commandExecution"]
    assert [e["params"]["item"]["exitCode"] for e in commands] == [1, 0]
    assert all(e["agent"] == "builder" for e in commands)
    assert ledger.job(job.job_id).outcome == "satisfied"


def test_runtime_events_are_host_bound_and_deltas_not_persisted(env):
    ledger, job = env[3], env[4]
    sink = ledger_event_sink(ledger)
    for method in ("item/completed", "item/agentMessage/delta", "config/read"):
        sink({"job_id": job.job_id, "agent": "builder", "attempt_id": 42,
              "method": method, "params": {"attempt_id": 999, "item": {"type": "agentMessage", "text": "done"}}})
    events = [e for e in ledger.events_since(0, job.job_id) if e.kind.startswith("runtime.")]
    assert len(events) == 1
    assert events[0].agent == "builder" and events[0].payload["attempt_id"] == 42










def test_cli_create_and_status_do_not_launch_model(tmp_path, capsys):
    from ai_hardware_autolab.orchestrator.conversation_cli import main
    args = ["--ledger", str(tmp_path / "prototype.db"), "--workspaces", str(tmp_path / "workspaces")]
    assert main(["create", *args, "--objective", "Offline addition"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["model_calls"] == 0 and created["mode"] == "offline"
    assert main(["status", *args, "--job", created["job_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "lead"


def test_cancel_commits_before_runtime_interrupt_and_late_error_is_terminal(env):
    ledger, workspace = env[3], env[2]
    class Cancellable:
        def __call__(self, *args):
            flow.cancel(job.job_id)
            raise CodexRunnerError("turn interrupted")
        def cancel(self, job_id):
            assert ledger.job(job_id).outcome == "cancelled"
    flow = ConversationFlow(ledger, Cancellable())
    job = flow.create("Cancel example", workspace)
    assert flow.advance(job.job_id).done
    assert ledger.job(job.job_id).outcome == "cancelled"

def test_removed_hardware_mode_is_refused_before_model_dispatch(env):
    runtime,calls,*_=runner(env, [])
    with pytest.raises(ValueError): runtime('builder',dict(env[1],execution_mode='hardware'))
    assert calls==[]
