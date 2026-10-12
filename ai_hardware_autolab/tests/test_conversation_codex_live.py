"""Opt-in one-turn model smoke. Not run in ordinary regression suites."""
import os
import json
import threading
import uuid
from pathlib import Path

import pytest

from ai_hardware_autolab.orchestrator.conversation_codex import ConversationCodexConfig, ConversationCodexRuntime, live_client
from ai_hardware_autolab.orchestrator.conversation_flow import ConversationFlow
from ai_hardware_autolab.orchestrator.jobs import JobLedger


@pytest.mark.skipif(os.environ.get("QKD_CODEX_LIVE_SMOKE") != "1", reason="explicit live-model smoke only")
@pytest.mark.parametrize("role,tools", [
    ("lead", ()), ("builder", ()), ("verifier", ()),
    ("builder", ("schema_probe",)),
])
def test_real_response_schema_without_tools(tmp_path, role, tools):
    root = Path(os.environ.get("QKD_CODEX_LIVE_ROOT", str(tmp_path))).resolve(strict=True)
    workspace = root / ("schema_" + uuid.uuid4().hex[:12])
    workspace.mkdir()
    # Use the actual production adapter, but force its tool-disabled client even
    # for Builder. A device reply is only decoded text: no executor is installed.
    def factory(config, workspace, repair, sink):
        return live_client(config, workspace, True, sink)
    arguments = {"enabled": True, "channels": [1, 4], "options": {"label": "schema test"}}
    objective = ("Response schema test only. Do not use tools or inspect files. "
                 "Return needs_user with summary 'schema accepted', empty artifacts/runs, "
                 "null target and null device_call.")
    if tools:
        objective = ("Response schema test only. Do not use tools or inspect files. "
                     "Return status=device, summary='schema accepted', empty artifacts/runs, "
                     "null target, tool=schema_probe and arguments as JSON text for: " + json.dumps(arguments))
    events = []
    runtime = ConversationCodexRuntime(ConversationCodexConfig(root,
        model=os.environ.get("QKD_CODEX_SCHEMA_MODEL") or None, timeout_seconds=45),
        client_factory=factory, event_sink=events.append)
    context = {"job_id": workspace.name, "attempt_id": 1, "role": role,
               "workspace": str(workspace), "objective": objective,
               "execution_mode": "hardware" if tools else "offline", "hardware_access": False,
               "inputs": {"device_tools": list(tools)}}
    reply = runtime(role, context)
    assert reply["status"] == ("device" if tools else "needs_user")
    if tools:
        assert reply["device_call"] == {"tool": "schema_probe", "arguments": arguments}
    forbidden = {"commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}
    assert not any((e["params"].get("item") or {}).get("type") in forbidden for e in events)


@pytest.mark.skipif(os.environ.get("QKD_CODEX_LIVE_SMOKE") != "1", reason="explicit live-model smoke only")
def test_one_real_builder_turn(tmp_path):
    # pytest's secure temp DACL (OWNER RIGHTS only) is not readable by the
    # Windows Codex restricted token. A host-created normal directory permits
    # sandbox testing without changing ACLs or disabling the sandbox.
    root = Path(os.environ.get("QKD_CODEX_LIVE_ROOT", str(tmp_path))).resolve(strict=True)
    workspace = root / ("isolated_" + uuid.uuid4().hex[:12])
    workspace.mkdir()
    ledger = JobLedger(root / (workspace.name + ".db"))
    flow = ConversationFlow(ledger, None)
    job = flow.create(
        "Offline smoke ONLY: create addition.py containing print(2 + 3). Run that script once "
        "with the available Python interpreter, store its actual stdout in result.txt. "
        "Return both relative artifact paths. Do not research, inspect other directories, "
        "install packages, contact hardware/network or run a test suite. This is a tiny code task.", workspace)
    context = flow._context(job.job_id, "builder")
    context["attempt_id"] = 1
    events = []
    runtime = ConversationCodexRuntime(ConversationCodexConfig(root, timeout_seconds=30), event_sink=events.append)
    approvals = []
    original_approval = runtime._approval
    def record_approval(*args, **kwargs):
        decision = original_approval(*args, **kwargs)
        approvals.append({"method": args[3], "params": args[4], "decision": decision})
        return decision
    runtime._approval = record_approval
    deadline = threading.Timer(90, lambda: runtime.cancel(job.job_id))
    deadline.start()
    try:
        reply = runtime("builder", context)
    finally:
        deadline.cancel()
    if reply["status"] != "ready":
        pytest.fail(json.dumps({"reply": reply, "approvals": approvals}, ensure_ascii=False))
    assert (workspace / "result.txt").read_text(encoding="utf-8-sig").strip() == "5"
    assert {"addition.py", "result.txt"}.issubset(reply["artifacts"])
    assert any((e["params"].get("item") or {}).get("type") == "commandExecution" for e in events)
