from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from QKD_PROJ.agents.schema import load_simple_team, ValidationError
from QKD_PROJ.orchestrator.development import MODE, normalize_plan, validate_development_plan
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission, hardware_completion_gap
from QKD_PROJ.orchestrator.loop import Loop, LoopPolicy
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.workspace import WorkspaceManager, WorkspaceError
from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig, build_app_server_command, _build_write_roots
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate, SafetyViolation
from QKD_PROJ.webui.backend.office import OfficeProjection


def plan():
    return {"kind": "Plan", "agent": "lead-agent", "gate": "__mission__", "iteration": 1,
            "objective": "Implement the requested algorithm and test its outputs", "requirements_complete": True,
            "steps": [{"id": "implement", "tool": "codex_workspace", "instructions": "implement and test",
                       "advance_when": [{"field": "measurements.last_command_exit_code", "expect": 0, "source": "scenario tests"}]}]}


def resources():
    return {"development_mode": MODE, "execution_mode": "offline", "available_tools": ["codex_workspace"]}


def test_code_plan_needs_no_contract_catalog_or_exact_files(tmp_path):
    compiled = ContractCompiler(tmp_path / "contracts", project_root=tmp_path).compile(
        plan(), job_id="job_123456789abc", iteration=1, scenario_sha256="", resources=resources())
    assert compiled.plan["steps"][0]["contract"] == "__workspace__"
    assert compiled.action == "workspace"
    assert "compiled_contract" not in compiled.plan
    assert not (tmp_path / "contracts").exists()
    assert hardware_completion_gap(None, compiled.plan, {**resources(), "execution_mode": "hardware"}) is None


def test_hardware_effect_cannot_be_disguised_as_offline():
    proposal = normalize_plan(plan(), resources())
    proposal["steps"][0].update(tool="scan_jtag", touches_hardware=False)
    with pytest.raises(ValidationError, match="hardware tool"):
        validate_development_plan(proposal, resources(), available_capabilities=[])


@pytest.mark.parametrize("steps", [None, "not steps", [{"id": [], "tool": "codex_workspace", "advance_when": []}]])
def test_malformed_plan_is_a_validation_error_not_an_orchestrator_crash(steps):
    p = plan()
    p["steps"] = steps
    normalized = normalize_plan(p, resources())
    with pytest.raises(ValidationError):
        validate_development_plan(normalized, resources(), available_capabilities=[])


def test_full_scenario_review_can_replan_without_changing_measured_numbers(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    (runs / "run-1").mkdir(parents=True)
    (runs / "run-1" / "manifest.json").write_text(json.dumps({"verdict": "PASS", "steps": [
        {"tool": "codex_workspace", "action": "implement", "measurements": {"last_command_exit_code": 0}}]}))
    calls = []

    def runner(agent, task):
        calls.append((agent.name, task))
        if agent.name == "lead-agent":
            answer = plan()
            answer["iteration"] = task["iteration"]
            return answer
        if agent.name == "builder":
            return {"kind": "StepResult", "status": "done", "runs": ["run-1"]}
        assert task["scenario"] == "implement the full requested behavior"
        return {**task["deterministic_verdict"], "kind": "Verdict", "agent": "verifier",
                "requirements_met": task["iteration"] > 1,
                "recommended_next_action": "The command passed but the requested edge case is not tested."}

    job, _ = start_simple_mission(ledger, "algorithm", "implement the full requested behavior", resources=resources())
    loop = Loop(ledger, runner, runs_dir=runs, agents=load_simple_team(), planner_agent="lead-agent",
                verifier_agent="verifier", policy=LoopPolicy(max_iterations=3, report_on_finish=False),
                require_plan_governance=True, repair_invalid_plans=True, reasoning_verifier=True,
                completion_guard=hardware_completion_gap,
                contract_compiler=ContractCompiler(tmp_path / "contracts", project_root=tmp_path))
    for _ in range(8):
        loop.advance(job.job_id)
    assert ledger.job(job.job_id).outcome == "satisfied"
    assert [name for name, _ in calls] == ["lead-agent", "builder", "verifier"] * 2
    assert calls[3][1]["previous_verifier_review"]["requirements_met"] is False
    assert all(row.payload["outcome"] == "satisfied" for row in ledger.handoffs(job.job_id, "Verdict"))
    assert not (tmp_path / "contracts").exists()


def test_builder_may_fix_parent_package_but_source_is_not_published(tmp_path):
    from QKD_PROJ.tests.test_workspace import _source_repo, JOB
    source = _source_repo(tmp_path)
    manager = WorkspaceManager(source / "QKD_PROJ" / "workspaces", source)
    task = {"job_id": JOB, "iteration": 1, "step": normalize_plan(plan(), resources())["steps"][0],
            "resources": {**resources(), "write_scope": ["QKD_PROJ/tools/new/"]}}
    workspace = manager(task)
    target = workspace / "QKD_PROJ" / "tools" / "__init__.py"
    target.parent.mkdir(parents=True)
    target.write_text("# Package required by the implementation\n")
    report = manager.audit(task, "builder")
    assert report["outside_write_scope"] == [] and report["review_required"] is True
    config = CodexRunnerConfig(project_root=workspace, qkd_root=workspace / "QKD_PROJ", runs_dir=source / "runs")
    assert _build_write_roots(task, config) == [workspace]
    runner = CodexAgentRunner(config=config, workspace_resolver=manager)
    result = runner._record_workspace_run({"status": "done"}, task, config, report, [
        {"method": "item/completed", "params": {"item": {"type": "commandExecution", "command": "pytest", "exitCode": 0}}}])
    assert result["workspace_review"]["published"] is False
    assert not (source / "QKD_PROJ" / "tools" / "__init__.py").exists()
    assert (config.runs_dir / result["runs"][0] / "workspace_outputs" / "QKD_PROJ/tools/__init__.py").is_file()
    manager(task)
    (workspace / "QKD_PROJ" / "contracts").mkdir(exist_ok=True)
    (workspace / "QKD_PROJ" / "contracts" / "unauthorized.yaml").write_text("allowed_operations: [anything]\n")
    with pytest.raises(WorkspaceError, match="outside"):
        manager.audit(task, "builder")


def boundary():
    return {"allowed_operations": ["scope_identify", "oscilloscope_capture"],
            "forbidden_operations": ["enable_generator_output", "board_upload"],
            "hardware_safety": {
                "devices": [{"model": "RTM3004", "host": "127.0.0.1", "port": 5025}],
                "scope_channels": [{"channel": 3, "signal": "test", "termination": "1MOhm",
                                    "max_abs_input_v": 5, "source": "test fixture, not a physical safety rating"}],
                "stop_conditions": ["stop on communication failure or voltage over bound"]}}


def test_hardware_boundary_identity_ignores_implementation_and_checks(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "board-map.yaml").write_text(yaml.safe_dump({"scope": {"model": "RTM3004", "host": "127.0.0.1", "port": 5025}}))
    compiler = ContractCompiler(contracts, project_root=tmp_path)
    p = plan()
    p["hardware_contract"] = boundary()
    p["steps"][0].update(tool="measure_scope", touches_hardware=True, agent="builder")
    r = {**resources(), "execution_mode": "hardware", "available_tools": ["measure_scope"]}
    first = compiler.compile(p, job_id="j1", iteration=1, scenario_sha256="", resources=r)
    p["steps"][0]["instructions"] = "Use a different acquisition timebase"
    p["steps"][0]["advance_when"][0]["expect"] = 12
    second = compiler.compile(p, job_id="j1", iteration=2, scenario_sha256="", resources=r)
    assert first.sha256 == second.sha256
    doc = yaml.safe_load((contracts / first.filename).read_text())
    assert "pass_criteria" not in doc and "orchestration" not in doc
    (contracts / "board-map.yaml").write_text(yaml.safe_dump({"scope": {"model": "RTM3004", "host": "127.0.0.1", "port": 5025}}))
    gate = SafetyGate(Contracts.load(first.filename, contracts), allow_hardware=True)
    gate.require("oscilloscope_capture")
    gate.require_scope_capture({"channels": [{"channel": 3, "signal": "test"}]})
    gate.require_measured_voltage({"CH3_min_v": 0, "CH3_max_v": 3.3})
    with pytest.raises(SafetyViolation):
        gate.require("board_upload")
    with pytest.raises(SafetyViolation):
        gate.require_measured_voltage({"CH3_min_v": 0, "CH3_max_v": float("nan")})
    with pytest.raises(SafetyViolation):
        gate.require_scope_capture({"channels": [{"channel": 2, "signal": "test"}]})
    gate.contracts.board_map["scope"]["host"] = "different-device"
    with pytest.raises(SafetyViolation, match="endpoint"):
        gate.require("oscilloscope_capture")


def test_scope_campaign_needs_no_board_profile_and_uses_current_plan(tmp_path):
    from QKD_PROJ.tests.test_campaign_proposal import _environment, NOW
    from QKD_PROJ.orchestrator.campaign_proposal import CampaignProposalService
    ledger, _, controller = _environment(tmp_path)
    contracts = controller.contracts_dir
    (contracts / "board-map.yaml").write_text(yaml.safe_dump({"scope": {"model": "RTM3004", "host": "127.0.0.1", "port": 5025}}))
    r = {**resources(), "execution_mode": "hardware", "available_tools": ["measure_scope"]}
    job, _ = start_simple_mission(ledger, "scope", "measure scope CH3", resources=r)
    ledger.transition(job.job_id, "running")
    ledger.begin_iteration(job.job_id)
    p = plan()
    p["hardware_contract"] = boundary()
    p["steps"][0].update(tool="measure_scope", touches_hardware=True, agent="builder")
    compiled = ContractCompiler(contracts, project_root=tmp_path).compile(p, job_id=job.job_id, iteration=1, scenario_sha256="", resources=r)
    ledger.record_handoff(job.job_id, "Plan", "lead-agent", compiled.plan, iteration=1)
    service = CampaignProposalService(ledger, controller, project_root=tmp_path, contracts_dir=contracts,
                                      campaigns_dir=controller.campaigns_dir, now=lambda: NOW)
    proposal = service.preview(job.job_id)
    assert proposal.ready, proposal.blockers
    assert proposal.board is None and proposal.contract == compiled.filename
    assert proposal.permissions[0]["artifacts"]
    approved = service.approve(job.job_id, "test-person", proposal.review_sha256)
    assert approved["campaign"]["active"] is True
    from QKD_PROJ.orchestrator.campaign import CampaignAutoApprover
    policy = CampaignAutoApprover(ledger, controller.active_policy, now=lambda: NOW)
    builder = next(a for a in load_simple_team() if a.name == "builder")
    assert policy.evaluate(ledger.job(job.job_id), compiled.plan["steps"][0], builder).allowed
    # Changed device address must invalidate the existing delegation.
    (contracts / "board-map.yaml").write_text("scope: {host: changed}\n")
    assert not policy.evaluate(ledger.job(job.job_id), compiled.plan["steps"][0], builder).allowed


def test_human_feedback_never_creates_an_agent_and_blocked_agents_wait():
    projection = OfficeProjection()
    projection.apply_ledger_event({"event_id": 1, "job_id": "job1", "agent": "builder", "kind": "runtime.item/started",
                                  "payload": {"method": "item/started", "item_type": "commandExecution"}})
    projection.apply_ledger_event({"event_id": 2, "job_id": "job1", "agent": "local-operator", "kind": "control.steer"})
    projection.apply_status({"jobs": {"waiting_person": [{"job_id": "job1"}]}})
    occupants = projection.snapshot()["occupants"]
    assert len(occupants) == 1 and occupants[0]["agent"] == "builder"
    assert occupants[0]["activity"] == "waiting"


def test_mcp_imports_and_contracts_are_not_taken_from_mutable_job_checkout(tmp_path):
    config = CodexRunnerConfig(project_root=tmp_path / "job", qkd_root=tmp_path / "job/QKD_PROJ", codex_command=("codex",))
    command = build_app_server_command(config, role="builder", trusted_root=tmp_path / "trusted",
                                        trusted_contracts=tmp_path / "trusted/contracts")
    joined = " ".join(command)
    assert str(tmp_path / "trusted/contracts").replace("\\", "\\\\") in joined
    assert ".cwd=" in joined


@pytest.mark.parametrize("reason", ["format", "budget"])
def test_lead_can_correct_format_or_plan_from_known_facts_without_web(tmp_path, reason):
    from QKD_PROJ.tests.test_codex_runner import FakeClient
    answer = plan()
    client = FakeClient({"kind": "Plan", "agent": "lead-agent", "handoff": answer})
    runner = CodexAgentRunner(config=CodexRunnerConfig(reference_root=tmp_path / "references"), client=client)
    task = {"resources": resources()}
    task.update({"format_correction": {"error": "missing field"}} if reason == "format" else {"research_plan": {"search_budget": 0}})
    lead = next(a for a in load_simple_team() if a.name == "lead-agent")
    assert runner(lead, task)["kind"] == "Plan"
    assert "dynamicTools" not in client.thread_params[0]


def test_interrupted_offline_turn_preserves_edits_but_cannot_claim_completion(tmp_path):
    from QKD_PROJ.tests.test_workspace import _source_repo, JOB
    from QKD_PROJ.tests.test_codex_runner import FakeClient
    from QKD_PROJ.orchestrator.codex_runner import CodexProtocolError
    source = _source_repo(tmp_path)
    manager = WorkspaceManager(source / "QKD_PROJ/workspaces", source)
    class Interrupted(FakeClient):
        def run_turn(self, thread_id, params, *, server_request):
            (Path(params["cwd"]) / "QKD_PROJ/base.py").write_text("fixed = True\n")
            raise CodexProtocolError("idle timeout")
    runner = CodexAgentRunner(config=CodexRunnerConfig(project_root=source, qkd_root=source / "QKD_PROJ", runs_dir=source / "runs"),
                              client=Interrupted({}), workspace_resolver=manager)
    task = {"job_id": JOB, "iteration": 1, "resources": resources(), "step": normalize_plan(plan(), resources())["steps"][0]}
    answer = runner(next(a for a in load_simple_team() if a.name == "builder"), task)
    assert answer["status"] == "failed" and answer["runs"] and answer["handoff_recovery"]
    manifest = json.loads((source / "runs" / answer["runs"][0] / "manifest.json").read_text())
    assert manifest["steps"][0]["measurements"]["runtime_completed"] is False
    assert (source / "QKD_PROJ/base.py").read_text() == "value = 1\n"


def test_device_call_is_bound_at_mcp_dispatch_even_without_a_runtime_approval_prompt(tmp_path, monkeypatch):
    from QKD_PROJ.mcp_server import registry as R
    from QKD_PROJ.mcp_server.protocol import ToolError
    from QKD_PROJ.hwtools.result import sha256_file
    contract = tmp_path / "approved.yaml"
    contract.write_text("allowed_operations: [oscilloscope_capture]\n")
    calls = []
    monkeypatch.setattr(R, "_measure_scope", lambda policy, args: calls.append(args) or {"ok": True})
    tools = {tool.name: tool for tool in R.build_tools(R.ServerPolicy(
        role="builder", allow_hardware=True, contracts_dir=tmp_path,
        hardware_tool="measure_scope", hardware_contract=contract.name,
        hardware_contract_sha256=sha256_file(contract)))}
    assert "program_cpld_sram" not in tools
    with pytest.raises(ToolError, match="different hardware boundary"):
        tools["measure_scope"].handler({"contract": "other.yaml"})
    assert not calls
    assert tools["measure_scope"].handler({"contract": contract.name, "params": {}}) == {"ok": True}
    with pytest.raises(ToolError, match="consumed"):
        tools["measure_scope"].handler({"contract": contract.name, "params": {}})
    assert len(calls) == 1


def test_active_hardware_daemon_does_not_give_offline_builder_device_tools(tmp_path):
    from QKD_PROJ.tests.test_codex_runner import FakeClient
    class Inspect(CodexAgentRunner):
        def _turn_params(self, *args):
            return {}
        def _client(self, agent, config):
            assert config.profile == "offline"
            return FakeClient({"kind": "StepResult", "agent": "builder", "handoff": {
                "kind": "StepResult", "agent": "builder", "status": "done", "runs": [], "step_id": "implement"}})
    runner = Inspect(config=CodexRunnerConfig(profile="hardware"))
    task = {"resources": resources(), "step": normalize_plan(plan(), resources())["steps"][0]}
    # Inspect _invoke only: no workspace/server/device is created.
    runner._invoke(next(a for a in load_simple_team() if a.name == "builder"), task, runner.config)


def test_only_matching_turn_activity_extends_idle_timeout(monkeypatch):
    import threading
    from QKD_PROJ.orchestrator import codex_runner as R
    client = R.StdioAppServer.__new__(R.StdioAppServer)
    client.timeout_seconds = 5
    client._active_lock = threading.Lock()
    client._active_turns = {}
    client._request = lambda *args, **kwargs: {"turn": {"id": "turn1"}}
    client._emit = lambda message: None
    clock = [0.0]
    monkeypatch.setattr(R.time, "monotonic", lambda: clock[0])
    frames = iter([
        (3, {"method": "item/started", "params": {"threadId": "thread1", "turnId": "turn1"}}),
        (7, {"method": "account/rateLimits/updated", "params": {}}),
        (7.5, {"method": "item/started", "params": {"threadId": "other"}}),
        (7.6, {"method": "item/completed", "params": {"threadId": "thread1", "turnId": "turn1", "item": {"type": "agentMessage", "text": "ok"}}}),
        (9, {"method": "turn/completed", "params": {"threadId": "thread1", "turn": {"id": "turn1", "status": "completed"}}}),
    ])
    deadlines = []
    def read(deadline):
        deadlines.append(deadline)
        clock[0], message = next(frames)
        assert clock[0] < deadline
        return message
    client._next_message = read
    assert client.run_turn("thread1", {}, server_request=lambda *args: {}) == "ok"
    assert deadlines == pytest.approx([5, 8, 8, 8, 12.6])
