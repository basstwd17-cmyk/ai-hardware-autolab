"""Contract-free agent handoff over the existing device safety boundary."""
import hashlib
from pathlib import Path

import pytest
from QKD_PROJ.boards.hardware import HardwareProfile
from QKD_PROJ.mcp_server.protocol import Tool
from QKD_PROJ.mcp_server.registry import ServerPolicy, ToolError, _program_cpld_sram, _run_bbb
from QKD_PROJ.hwtools.result import ToolResult
from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
from QKD_PROJ.orchestrator.conversation_hardware import (
    ConversationHardwareConfig,
    ConversationHardwareExecutor,
    device_tool_catalog,
)
from QKD_PROJ.orchestrator.conversation_service import WebConversationFlow
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.results import build_result
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight


def hardware_inputs(digest="a" * 64):
    return {
        "hardware_profile": {"board_id": "rtase-v1", "revision": "v1.0", "sha256": digest},
        "device_tools": ["run_bbb"],
        "device_tool_catalog": [{"name": "run_bbb", "input_schema": {
            "type": "object", "properties": {"identity_only": {"type": "boolean"}}
        }}],
    }


def test_builder_device_request_returns_run_to_same_conversation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "runs" / "bbb-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "result.json").write_text('{"ok": true}', encoding="utf-8")
    ledger = JobLedger(tmp_path / "jobs.db")
    calls = []

    class Runtime:
        def __call__(self, role, context):
            calls.append(role)
            assert "device_boundary" not in context["inputs"]
            assert "scenario_access" not in context["inputs"]
            if role == "lead":
                return {"status": "ready", "summary": "Identify the requested BBB"}
            if role == "builder" and not context["device_results"]:
                assert "contract" not in context["inputs"]["device_tool_catalog"][0]["input_schema"]["properties"]
                return {"status": "device", "summary": "Query the device identity",
                        "device_call": {"tool": "run_bbb", "arguments": {"identity_only": True}}}
            if role == "builder":
                assert context["handoff"]["from"] == "platform"
                assert context["device_results"][-1]["run"] == "bbb-run-1"
                return {"status": "ready", "summary": "BBB identity was recorded",
                        "runs": ["bbb-run-1"]}
            return {"status": "satisfied", "summary": "Fresh identity evidence satisfies the request",
                    "runs": ["bbb-run-1"]}

        def execute_device(self, job_id, request_turn_id, request):
            calls.append("device")
            assert request == {"tool": "run_bbb", "arguments": {"identity_only": True}}
            return {"tool": "run_bbb", "allowed": True, "hardware_contacted": True,
                    "run": "bbb-run-1", "run_verdict": "PASS", "result": {"ok": True}}

    flow = ConversationFlow(ledger, Runtime())
    job = flow.create("Report this BBB's identity", workspace, execution_mode="hardware",
                      inputs=hardware_inputs())
    assert flow.run(job.job_id).done
    assert calls == ["lead", "builder", "device", "builder", "verifier"]
    assert not ledger.handoffs(job.job_id, "Plan")
    assert ledger.job(job.job_id).outcome == "satisfied"
    result, artifacts = build_result(
        ledger, job.job_id, runs_dir=tmp_path / "runs", project_root=tmp_path
    )
    assert result["artifacts"] == [
        {"artifactId": "run:bbb-run-1", "kind": "run", "label": "bbb-run-1"}
    ]
    assert artifacts[0].target_path == str(run_dir)


def test_hardware_completion_requires_a_run_from_this_job(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")

    def runtime(role, context):
        if role == "lead":
            return {"status": "ready", "summary": "Use the device"}
        if role == "builder":
            return {"status": "ready", "summary": "No device result"}
        return {"status": "satisfied", "summary": "Unsupported success", "runs": ["old-run"]}

    flow = ConversationFlow(ledger, runtime)
    job = flow.create("Measure live hardware", workspace, execution_mode="hardware",
                      inputs=hardware_inputs())
    assert flow.run(job.job_id).action == "blocked"
    assert ledger.job(job.job_id).outcome is None


def test_verifier_run_annotation_is_canonicalized_to_the_host_run(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")

    class Runtime:
        def __call__(self, role, context):
            if role == "lead":
                return {"status": "ready", "summary": "Identify the BBB"}
            if role == "builder" and not context["device_results"]:
                return {"status": "device", "summary": "Read the identity",
                        "device_call": {"tool": "run_bbb", "arguments": {"identity_only": True}}}
            if role == "builder":
                return {"status": "ready", "summary": "Identity check failed and was recorded",
                        "runs": ["bbb-run-1 — FAIL: SSH timeout"]}
            return {"status": "satisfied", "summary": "The failure is correctly reported",
                    "runs": ["bbb-run-1 — FAIL: SSH timeout"]}

        def execute_device(self, *_args):
            return {"tool": "run_bbb", "allowed": True, "hardware_contacted": True,
                    "run": "bbb-run-1", "run_verdict": "FAIL", "result": {"ok": False}}

    flow = ConversationFlow(ledger, Runtime())
    job = flow.create("Report the live result", workspace, execution_mode="hardware",
                      inputs=hardware_inputs())

    assert flow.run(job.job_id).done
    completed = [event for event in ledger.events_since(0, job.job_id)
                 if event.kind == "conversation.turn.completed"]
    assert completed[-1].payload["reply"]["runs"] == ["bbb-run-1"]


def test_unknown_device_outcome_is_not_replayed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    device_calls = []

    class Runtime:
        def __call__(self, role, context):
            if role == "lead":
                return {"status": "ready", "summary": "Identify BBB"}
            return {"status": "device", "summary": "Query identity",
                    "device_call": {"tool": "run_bbb", "arguments": {"identity_only": True}}}

        def execute_device(self, *args):
            device_calls.append(1)
            raise RuntimeError("transport disappeared after dispatch")

    flow = ConversationFlow(ledger, Runtime())
    job = flow.create("Identify BBB", workspace, execution_mode="hardware",
                      inputs=hardware_inputs())
    assert flow.run(job.job_id).action == "blocked"
    assert flow.run(job.job_id).action == "blocked"
    assert device_calls == [1]


def test_device_request_cannot_supply_contract_or_run_name(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    flow = ConversationFlow(ledger, lambda role, _: (
        {"status": "ready", "summary": "Use BBB"} if role == "lead" else
        {"status": "device", "summary": "Bad request", "device_call": {
            "tool": "run_bbb", "arguments": {"identity_only": True, "contract": "other.yaml"}}}
    ))
    job = flow.create("Identify BBB", workspace, execution_mode="hardware",
                      inputs=hardware_inputs())
    assert flow.run(job.job_id).action == "blocked"
    assert not [e for e in ledger.events_since(0, job.job_id)
                if e.kind == "conversation.device.started"]


def test_public_device_catalog_hides_platform_bindings():
    rows = device_tool_catalog()
    assert rows
    for row in rows:
        schema = row["input_schema"]
        assert "contract" not in schema["properties"]
        assert "record_name" not in schema["properties"]
        assert "contract" not in schema.get("required", [])
    assert {row["name"] for row in device_tool_catalog({"scope"})} == {"measure_scope", "check_connections"}
    check = next(row for row in device_tool_catalog({"scope"}) if row["name"] == "check_connections")
    assert check["input_schema"]["properties"]["devices"]["items"]["enum"] == ["scope"]
    program = next(row for row in rows if row["name"] == "program_cpld_sram")
    assert "program_xcf" in program["input_schema"]["properties"]
    assert "program_xcf" in program["input_schema"]["required"]
    bbb = next(row for row in rows if row["name"] == "run_bbb")
    assert "job workspace" in bbb["input_schema"]["properties"]["script"]["description"]


def test_executor_binds_profile_and_auto_runs_safe_call(tmp_path):
    project = Path(__file__).resolve().parents[2]
    runs = tmp_path / "runs"
    runs.mkdir()
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    digest = profile.sha256
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    flow = ConversationFlow(ledger, None)
    job = flow.create("Identify BBB", workspace, execution_mode="hardware",
                      inputs=hardware_inputs(digest=digest))
    observed = []

    def tools(policy):
        assert policy.hardware_contract == profile.label
        assert policy.hardware_contract_sha256 == digest
        assert policy.project_root == workspace.resolve()
        assert policy.scenario_workspace == workspace.resolve()
        return [Tool("run_bbb", "identity", {"type": "object"},
                     lambda args: observed.append(args) or {
                         "run": "run-1", "run_verdict": "PASS",
                         "result": {"ok": True, "measurements": {"identity_verified": True}},
                     }, touches_hardware=True)]

    execute = ConversationHardwareExecutor(
        ledger, profile,
        ConversationHardwareConfig(project, runs), tool_builder=tools,
    )
    result = execute(job.job_id, 7, {"tool": "run_bbb", "arguments": {"identity_only": True}})
    assert observed == [{"identity_only": True, "contract": profile.label}]
    assert result["run"] == "run-1" and result["allowed"] is True
    automatic = [e for e in ledger.events_since(0, job.job_id)
                 if e.kind == "hardware.policy.automatic"]
    assert len(automatic) == 1 and not ledger.approvals(job.job_id)


def test_state_changing_program_is_automatic_within_the_profile(tmp_path):
    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ConversationFlow(ledger, None).create(
        "Program the CPLD SRAM", workspace, execution_mode="hardware",
        inputs={
            "hardware_profile": {"board_id": profile.board_id,
                                 "revision": profile.revision, "sha256": profile.sha256},
            "device_tools": list(profile.tools),
            "device_tool_catalog": device_tool_catalog(profile),
        },
    )
    observed = []

    def tools(_policy):
        return [Tool("program_cpld_sram", "program", {"type": "object"},
                     lambda args: observed.append(args) or {
                         "run": "run-program", "run_verdict": "PASS",
                         "result": {"ok": True, "measurements": {"hardware_contacted": True}},
                     }, touches_hardware=True)]

    execute = ConversationHardwareExecutor(
        ledger, profile, ConversationHardwareConfig(project, runs), tool_builder=tools,
    )
    request = {"tool": "program_cpld_sram", "arguments": {"program_xcf": "build/design.xcf"}}
    result = execute(job.job_id, 7, request)
    assert result["allowed"] is True and result["run"] == "run-program"
    assert result["approval_required"] is False
    assert observed == [{"program_xcf": "build/design.xcf", "contract": profile.label}]
    assert not ledger.approvals(job.job_id)
    automatic = [event for event in ledger.events_since(0, job.job_id)
                 if event.kind == "hardware.policy.automatic"]
    assert len(automatic) == 1 and automatic[0].payload["tool"] == "program_cpld_sram"


def test_conversation_runs_new_workspace_bbb_script_without_registration(tmp_path, monkeypatch):
    from QKD_PROJ import hwtools
    from QKD_PROJ.mcp_server import registry

    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    workspace = tmp_path / "workspace"
    script = workspace / "BeagleBone-Black" / "experiments" / "rf1_check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('rf1')\n", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(registry, "_check_bbb_target", lambda *_: None)
    def run(_gate, name, record, **kwargs):
        observed.update(name=name, source_path=kwargs.get("source_path"))
        return ToolResult("bbb", "run_script", verdict="PASS")
    monkeypatch.setattr(hwtools, "run_bbb", run)
    policy = ServerPolicy(
        project_root=workspace, device_root=project, runs_dir=tmp_path / "runs",
        allow_hardware=True, role="builder", hardware_tool="run_bbb",
        hardware_contract=profile.label, hardware_contract_sha256=profile.sha256,
        hardware_profile_path=profile.path, hardware_profile_sha256=profile.sha256,
        scenario_workspace=workspace,
    )

    result = _run_bbb(policy, {
        "contract": profile.label,
        "script": "BeagleBone-Black/experiments/rf1_check.py",
    })

    assert result["run_verdict"] == "PASS"
    assert observed == {"name": "BeagleBone-Black/experiments/rf1_check.py",
                        "source_path": script.resolve()}
    with pytest.raises(ToolError, match="job workspace"):
        _run_bbb(policy, {"contract": profile.label, "script": "../outside.py"})


def test_workspace_python_script_uses_python_interpreter(tmp_path):
    import shlex
    from QKD_PROJ.hwtools.bbb import run_bbb
    from QKD_PROJ.tests.test_bbb import FakeSsh, transport_for

    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    script = tmp_path / "experiment.py"
    source = "print('new scenario')\n"
    script.write_text(source, encoding="utf-8")
    fake = FakeSsh()
    result = run_bbb(profile.compatibility_gate(), script.name,
                     source_path=script, transport=transport_for(fake))
    assert result.ok, result.error
    assert "python3 -c " + shlex.quote(source) in fake.scripts[-1]
    assert result.measurements["script_sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()


def test_missing_workspace_script_is_correctable_before_device_dispatch(tmp_path, monkeypatch):
    from QKD_PROJ import hwtools
    from QKD_PROJ.mcp_server import registry
    from QKD_PROJ.mcp_server.protocol import DevicePreparationError

    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    job = ConversationFlow(ledger, None).create("Implement an experiment", workspace,
        execution_mode="hardware", inputs=hardware_inputs(profile.sha256))
    monkeypatch.setattr(registry, "_check_bbb_target", lambda *_: None)
    called = []
    def device(gate, script, record, **kwargs):
        called.append(kwargs["source_path"].read_text())
        result = ToolResult("bbb", "run_script", verdict="PASS")
        record.add(result)
        return result
    monkeypatch.setattr(hwtools, "run_bbb", device)
    executor = ConversationHardwareExecutor(ledger, profile, ConversationHardwareConfig(project, tmp_path / "runs"))
    request = {"tool": "run_bbb", "arguments": {"script": "new_experiment.py"}}
    with pytest.raises(DevicePreparationError, match="existing file"):
        executor(job.job_id, 1, request)
    assert called == []
    assert not (tmp_path / "runs").exists()
    (workspace / "new_experiment.py").write_text("print(42)")
    result = executor(job.job_id, 2, request)
    assert result["run_verdict"] == "PASS" and called == ["print(42)"]
    assert not ledger.approvals(job.job_id)


def test_conversation_programs_workspace_xcf_without_profile_candidate(tmp_path, monkeypatch):
    from QKD_PROJ import hwtools
    from QKD_PROJ.hwtools import sram

    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    workspace = tmp_path / "workspace"
    xcf = workspace / "QKD_PROJ" / "build" / "rf1.xcf"
    xcf.parent.mkdir(parents=True)
    xcf.write_text("test fixture", encoding="utf-8")
    observed = {}

    monkeypatch.setattr(sram, "workspace_program_input_artifacts",
                        lambda _gate, path, root: observed.update(preflight=(path, root)) or {})
    monkeypatch.setattr(hwtools, "scan_jtag",
                        lambda *_args, **_kwargs: ToolResult("cpld", "jtag_scan", verdict="PASS"))
    def program(_gate, _scan, **kwargs):
        observed.update(program=kwargs)
        return ToolResult("cpld", "program_sram", verdict="PASS")
    monkeypatch.setattr(hwtools, "program_cpld_sram", program)
    policy = ServerPolicy(
        project_root=workspace, device_root=project, runs_dir=tmp_path / "runs",
        allow_hardware=True, role="builder", hardware_tool="program_cpld_sram",
        hardware_contract=profile.label, hardware_contract_sha256=profile.sha256,
        hardware_profile_path=profile.path, hardware_profile_sha256=profile.sha256,
        scenario_workspace=workspace,
    )

    result = _program_cpld_sram(policy, {
        "contract": profile.label, "program_xcf": "QKD_PROJ/build/rf1.xcf",
    })

    assert result["run_verdict"] == "PASS"
    assert observed["preflight"] == (xcf.resolve(), workspace.resolve())
    assert observed["program"]["program_xcf"] == xcf.resolve()
    assert observed["program"]["workspace_root"] == workspace.resolve()


def test_inbox_grant_resumes_hardware_conversation_without_user_reply(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    flow = ConversationFlow(ledger, None)
    job = flow.create(
        "Program the CPLD SRAM", workspace, execution_mode="hardware",
        inputs=hardware_inputs(),
    )
    ledger.transition(job.job_id, "running", reason="test conversation started")

    lead = ledger.append_event(job.job_id, "conversation.turn.started", {
        "feedback_through": 0, "execution_mode": "hardware", "hardware_access": False,
    }, "lead")
    ledger.append_event(job.job_id, "conversation.turn.completed", {
        "turn_id": lead.event_id, "feedback_through": 0,
        "reply": {"status": "ready", "summary": "Program the CPLD", "artifacts": [], "runs": []},
        "artifacts": [],
    }, "lead")
    request = ledger.append_event(job.job_id, "conversation.turn.started", {
        "feedback_through": 0, "execution_mode": "hardware", "hardware_access": False,
    }, "builder")
    requested = ledger.append_event(job.job_id, "conversation.turn.completed", {
        "turn_id": request.event_id, "feedback_through": 0,
        "reply": {"status": "device", "summary": "Program SRAM", "artifacts": [], "runs": [],
                  "device_call": {"tool": "run_bbb", "arguments": {"identity_only": True}}},
        "artifacts": [],
    }, "builder")
    approval = ledger.request_approval(
        job.job_id, "profile:rtase-v1@v1.0", "program CPLD SRAM",
        requested_by="builder", step_id="hardware:program_cpld_sram:test",
    )
    ledger.append_event(job.job_id, "conversation.device.completed", {
        "request_turn_id": requested.event_id, "tool": "program_cpld_sram",
        "allowed": False, "approval_required": True,
        "approval_id": approval.approval_id, "hardware_contacted": False,
    }, "platform")
    waiting = ledger.append_event(job.job_id, "conversation.turn.started", {
        "feedback_through": 0, "execution_mode": "hardware", "hardware_access": False,
    }, "builder")
    ledger.append_event(job.job_id, "conversation.turn.completed", {
        "turn_id": waiting.event_id, "feedback_through": 0,
        "reply": {"status": "needs_user", "summary": "Approve the pending request",
                  "artifacts": [], "runs": []},
        "artifacts": [],
    }, "builder")
    ledger.transition(job.job_id, "blocked", reason="user_input: approve the pending request")

    class Loop:
        conversation = flow

    class Daemon:
        loop = Loop()

    result = ControlPlane(ledger, Daemon()).approve(
        approval.approval_id, True, "operator",
    )

    assert result["job_resumed"] is True
    assert ledger.job(job.job_id).state == "running"
    assert flow.phase(job.job_id).role == "builder"
    feedback = [event for event in ledger.events_since(0, job.job_id)
                if event.kind == "conversation.feedback"]
    assert feedback[-1].payload["approval_id"] == approval.approval_id
    assert feedback[-1].payload["decision"] == "granted"


def test_control_routes_hardware_to_installed_profile_without_plan(tmp_path):
    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, "rtase-v1", "v1.0")
    ledger = JobLedger(tmp_path / "jobs.db")
    workspace = tmp_path / "conversation-workspace"
    workspace.mkdir()
    class Manager:
        def prepare(self, _job_id): return workspace
    manager = Manager()
    conversation = WebConversationFlow(ledger, None, manager)
    class Loop: pass
    loop = Loop(); loop.conversation = conversation
    class Daemon: pass
    daemon = Daemon(); daemon.loop = loop
    control = ControlPlane(
        ledger, daemon=daemon, preflight_engine=ScenarioPreflight(tmp_path / "no-contracts"),
        hardware_profile=profile, hardware_enabled=True,
    )
    scenario = (
        "Execution scope: hardware; submitting this scenario delegates control of the "
        "devices requested below, within their electrical safety bounds. Other devices "
        "are not authorized.\n\nRTASE V1 오실로스코프 CH3를 측정해줘"
    )
    response = control.mission(
        scenario, scenario, hashlib.sha256(scenario.encode()).hexdigest()
    )
    assert response["workflow"] == "conversation"
    assert response["execution_mode"] == "hardware"
    brief = ledger.handoffs(response["job"]["job_id"], "Brief", 0)[0].payload
    assert brief["workflow"] == "conversation-prototype-v1"
    assert brief["execution_mode"] == "hardware"
    assert brief["inputs"]["hardware_profile"]["sha256"] == profile.sha256
    assert set(brief["inputs"]["device_tools"]) == set(profile.tools) | {'check_connections'}
    assert "contract" not in brief["inputs"]["device_tool_catalog"][0]["input_schema"]["properties"]
    assert not ledger.handoffs(response["job"]["job_id"], "Plan")
