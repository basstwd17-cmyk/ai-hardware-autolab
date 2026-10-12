"""Short requests use real orchestration + fake devices, never live hardware/LLM."""
import copy
import json

import pytest
import yaml

from ai_hardware_autolab.agents.schema import load_simple_team, ValidationError
from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate, SafetyViolation
from ai_hardware_autolab.hwtools.result import sha256_file
from ai_hardware_autolab.hwtools.scope import identify_scope
from ai_hardware_autolab.hwtools.bbb import identify_bbb
from ai_hardware_autolab.hwtools.instruments import RTM3004
from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_tools
from ai_hardware_autolab.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig, CodexApprovalRequired, CodexRunnerError
from ai_hardware_autolab.orchestrator.development import MODE, compact_task, normalize_plan, validate_development_plan
from ai_hardware_autolab.orchestrator.contract_compiler import ContractCompiler
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.pagination import iter_events
from ai_hardware_autolab.tests.test_bbb import FakeSsh
from ai_hardware_autolab.tests.test_bbb_connection_binding import configure, ENDPOINT, SECRET
from ai_hardware_autolab.tests.test_scope_session_approval import setup_scope, require
from ai_hardware_autolab.tests.test_scenario_access import start
from ai_hardware_autolab.tests.test_codex_runner import FakeClient


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbid(*args, **kwargs):
        raise AssertionError("test attempted live hardware access")
    monkeypatch.setattr("socket.create_connection", forbid)
    monkeypatch.setattr("ai_hardware_autolab.hwtools.ssh.ParamikoClient", forbid)
    for key in ("BB_HOST", "BB_USER", "BB_PORT", "BB_PW"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("ai_hardware_autolab.hwtools.local_credentials.read_password", lambda endpoint: None)


def identity_setup(s, monkeypatch):
    configure(s)
    contracts = s.controller.contracts_dir
    steps = []
    for name, tool, op in (("bbb-id", "run_bbb", "bbb_identify"),
                           ("scope-id", "measure_scope", "scope_identify")):
        path = contracts / f"{name}.yaml"
        path.write_text(yaml.safe_dump({"allowed_operations": [op],
            "status": "proposed_hardware_boundary",
            "hardware_safety": {"stop_conditions": ["close on error"]}}))
        steps.append({"id": name, "agent": "builder", "tool": tool, "contract": path.name,
            "contract_sha256": sha256_file(path), "touches_hardware": True,
            "instructions": "read actual identity once", "execution": "direct",
            "tool_args": {"identity_only": True},
            "advance_when": [{"field": "measurements.identity_verified", "expect": True, "source": "current identity response"}]})
    ssh = FakeSsh()
    monkeypatch.setattr("ai_hardware_autolab.hwtools.ssh.ParamikoClient", lambda *a, **k: ssh)
    monkeypatch.setenv("BB_PW", SECRET)
    scopes = []
    def scope_factory(**kwargs):
        scopes.append(RTM3004(**kwargs, mock=True,
            mock_responses={"*IDN?": "Rohde&Schwarz,RTM3004,test-serial,test-fw", "*OPT?": ""}))
        return scopes[-1]
    monkeypatch.setattr("ai_hardware_autolab.hwtools.scope.RTM3004", scope_factory)
    runner = CodexAgentRunner(CodexRunnerConfig(profile="hardware", project_root=s.root,
        qkd_root=s.root, runs_dir=s.root / "runs"), hardware_approval=s.gate,
        client=FakeClient({}))
    return steps, runner, ssh, scopes


def test_one_iteration_identity_needs_no_builder_model_research_code_or_capture(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    job, _ = start(s, "BBB와 오실로스코프의 접속 상태만 확인해줘")
    briefs = s.ledger.handoffs(job.job_id, "Brief", 0)
    resources = briefs[0].payload["resources"]
    events, models = [], []
    runtime.event_sink = events.append
    # The reused fixture advertises capture-only tools. Production intake
    # advertises the installed Builder registry, including both native calls.
    s.loop.resource_refresher = lambda scenario, r: {**r, "available_tools": ["run_bbb", "measure_scope", "codex_workspace"]}
    def runner(agent, task):
        if agent.name == "builder":
            return runtime(agent, task)
        models.append(agent.name)
        if agent.name == "lead-agent":
            return {"kind": "Plan", "agent": agent.name, "objective": task["scenario"],
                    "requirements_complete": True, "steps": steps}
        assert len(task["step_results"]) == 2
        assert all(r["model_invoked"] is False and r["runs"] for r in task["step_results"])
        return {"requirements_met": True, "analysis": "Actual SSH model/kernel and scope identity recorded."}
    s.loop.runner = runner
    s.loop.run(job.job_id)
    assert s.ledger.job(job.job_id).outcome == "satisfied"
    assert s.ledger.job(job.job_id).iteration == 1
    assert models == ["lead-agent", "verifier"]
    assert runtime.client.thread_params == []  # no hidden Builder/App Server turn
    assert len(ssh.scripts) == 1 and "uname -r" in ssh.scripts[0]
    assert not ssh.uploads and ssh.closed
    assert len(scopes) == 1 and scopes[0].scpi.commands() == ["*IDN?", "*OPT?"]
    results = [h.payload for h in s.ledger.handoffs(job.job_id, "StepResult")]
    assert len({r["runs"][0] for r in results}) == 2
    assert not s.ledger.pending_approvals(job.job_id)
    assert sum(e.kind == "approval.authorized" for e in iter_events(s.ledger, job.job_id)) == 2
    assert [e["method"] for e in events] == ["platform/tool/started", "platform/tool/completed"] * 2
    assert all(e["params"]["item"]["model_invoked"] is False for e in events)
    assert SECRET not in json.dumps(results)
    assert "scenario_access" in resources


def direct_task(s, job, step):
    return {"job_id": job.job_id, "iteration": 1, "step": step,
            "resources": {"development_mode": MODE, "execution_mode": "hardware"}}


def test_failed_identity_returns_evidence_for_replanning_not_fake_pass(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    job, _ = start(s, "BBB에 접속해줘")
    ssh.rules = [("/proc/device-tree/model", (0, "wrong board\nkernel\n", ""))]
    assert not require(s, job, steps[0]).blocked
    result = runtime(s.loop.agent("builder"), direct_task(s, job, steps[0]))
    assert result["status"] == "failed" and result["runs"]
    assert "not the expected BBB" in result["execution_errors"][0]["error"]
    assert len(ssh.scripts) == 1 and not scopes
    assert runtime.client.thread_params == []


@pytest.mark.parametrize("problem", ["no_grant", "cancel", "drift", "offline"])
def test_direct_preserves_approval_cancellation_hash_and_offline_boundary(setup_scope, monkeypatch, problem):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    step = steps[0]
    job, _ = start(s, "BBB에 접속해줘")
    if problem != "no_grant":
        assert not require(s, job, step).blocked
    if problem == "cancel":
        ControlPlane(s.ledger).cancel(job.job_id, "human")
    elif problem == "drift":
        path = s.controller.contracts_dir / step["contract"]
        path.write_text(path.read_text() + "\n# changed\n")
    elif problem == "offline":
        runtime.config = CodexRunnerConfig(profile="offline", project_root=s.root, qkd_root=s.root)
    with pytest.raises((CodexApprovalRequired, CodexRunnerError)):
        runtime(s.loop.agent("builder"), direct_task(s, job, step))
    assert not ssh.scripts and not scopes and not runtime.client.thread_params


def test_new_feedback_goes_to_builder_model_before_using_old_direct_args(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    job, _ = start(s, "BBB에 접속해줘")
    assert not require(s, job, steps[0]).blocked
    task = direct_task(s, job, steps[0])
    task["steering"] = [{"message": "Do not connect yet, review the address first", "by": "human"}]
    (s.root / ".qkd-job-workspace.json").write_text(json.dumps({"job_id": job.job_id}))
    runtime.client.answer = {"kind": "StepResult", "agent": "builder",
        "handoff": json.dumps({"kind": "StepResult", "agent": "builder", "runs": [], "status": "done"})}
    runtime(s.loop.agent("builder"), task)
    assert len(runtime.client.thread_params) == 1
    assert not ssh.scripts
    assert "Do not connect yet" in runtime.client.turn_params[0]["input"][0]["text"]


def test_scope_wrong_identity_never_sends_stop_or_capture(setup_scope, monkeypatch):
    s = setup_scope
    steps, _, _, _ = identity_setup(s, monkeypatch)
    scope = RTM3004(host="test", mock=True, mock_responses={"*IDN?": "maker,wrong,serial,firmware", "*OPT?": ""})
    monkeypatch.setattr("ai_hardware_autolab.hwtools.scope.RTM3004", lambda **kwargs: scope)
    gate = SafetyGate(Contracts.load(steps[1]["contract"], s.controller.contracts_dir), allow_hardware=True)
    result = identify_scope(gate)
    assert not result.ok
    assert scope.scpi.commands() == ["*IDN?"]  # wrong device: do not query options


def test_identity_boundary_does_not_enable_scripts_or_capture(setup_scope, monkeypatch):
    s = setup_scope
    steps, _, ssh, scopes = identity_setup(s, monkeypatch)
    for step, forbidden in zip(steps, ["bbb_ssh_command", "oscilloscope_capture"]):
        gate = SafetyGate(Contracts.load(step["contract"], s.controller.contracts_dir), allow_hardware=True)
        with pytest.raises(SafetyViolation):
            gate.require(forbidden)
    assert not ssh.scripts and not scopes


def test_compiler_accepts_minimal_identity_proposals_without_electrical_capture_fields(setup_scope, monkeypatch):
    s = setup_scope
    steps, _, _, _ = identity_setup(s, monkeypatch)
    proposals = {}
    for step in steps:
        proposals[step["id"]] = yaml.safe_load((s.controller.contracts_dir / step["contract"]).read_text())
        step["contract"] = "$" + step["id"]
    plan = {"kind": "Plan", "agent": "lead-agent", "gate": "__mission__", "iteration": 1,
            "objective": "identity only", "requirements_complete": True, "steps": steps,
            "hardware_boundaries": proposals}
    compiled = ContractCompiler(s.controller.contracts_dir, project_root=s.root).compile(
        plan, job_id="job_identity", iteration=1, scenario_sha256="", resources={"development_mode": MODE, "execution_mode": "hardware"})
    assert len(compiled.plan["hardware_contracts"]) == 2
    assert all(step["contract"].startswith("hardware-") for step in compiled.plan["steps"])


def test_direct_uses_trusted_contract_not_workspace_copy(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, _ = identity_setup(s, monkeypatch)
    job, _ = start(s, "BBB에 접속해줘")
    assert not require(s, job, steps[0]).blocked
    workspace = s.root / "isolated"
    wrong = workspace / "ai_hardware_autolab" / "contracts"
    wrong.mkdir(parents=True)
    (wrong / steps[0]["contract"]).write_text("allowed_operations: []\n")
    (wrong / "board-map.yaml").write_text("bbb: {access: {host: wrong, user: wrong, port: 22}}\n")
    runtime.workspace_resolver = lambda task: workspace
    result = runtime(s.loop.agent("builder"), direct_task(s, job, steps[0]))
    assert result["status"] == "done"
    assert result["observations"]["measurements"]["target"] == ENDPOINT
    assert len(ssh.scripts) == 1


def test_unrecorded_handler_failure_is_unknown_and_never_replayed(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    job, _ = start(s, "BBB에 접속해줘")
    assert not require(s, job, steps[0]).blocked
    calls = []
    def broken(*args):
        calls.append(1)
        raise RuntimeError("connection lost after dispatch")
    monkeypatch.setattr("ai_hardware_autolab.mcp_server.registry._run_bbb", broken)
    result = runtime(s.loop.agent("builder"), direct_task(s, job, steps[0]))
    assert result["hardware_state_unknown"] is True and result["runs"] == []
    assert calls == [1] and not runtime.client.thread_params


@pytest.mark.parametrize("bad", [{"execution": "magic"}, {"entrypoint": "unreviewed.py:run"},
    {"tool_args": None}, {"tool_args": {"identity_only": "true"}},
    {"tool_args": {"contract": "elsewhere.yaml"}}, {"tool_args": {"unknown": True}},
    {"tool_args": {"identity_only": True, "script": "change-gpio"}}])
def test_bad_direct_plans_are_repairable_before_execution(bad):
    step = {"id": "check", "agent": "builder", "tool": "run_bbb", "contract": "bbb.yaml",
            "touches_hardware": True, "execution": "direct", "tool_args": {"identity_only": True}, **bad}
    resources = {"development_mode": MODE, "execution_mode": "hardware"}
    plan = normalize_plan({"kind": "Plan", "agent": "lead-agent", "gate": "__mission__", "iteration": 1,
                           "objective": "check", "requirements_complete": True, "steps": [step]}, resources)
    with pytest.raises(ValidationError):
        validate_development_plan(plan, resources, available_capabilities=[])


def test_new_board_keeps_research_and_workspace_development_without_profile(tmp_path):
    r = {"development_mode": MODE, "execution_mode": "offline", "available_tools": ["codex_workspace"]}
    plan = {"kind": "Plan", "agent": "lead-agent", "gate": "__mission__", "iteration": 1,
            "objective": "Develop the new board from its datasheet",
            "requirements_complete": False, "remaining_work": "Upload/measure after board is identified",
            "steps": [{"id": "new-firmware", "agent": "builder", "tool": "codex_workspace",
                "instructions": "Use researched datasheet pin mapping to implement and simulate the requested function."}]}
    compiled = ContractCompiler(tmp_path / "contracts", project_root=tmp_path).compile(
        plan, job_id="job_new", iteration=1, scenario_sha256="", resources=r)
    assert compiled.action == "workspace"
    assert compiled.plan["steps"][0].get("execution", "agent") == "agent"
    assert next(a for a in load_simple_team() if a.name == "lead-agent").native_web
    assert not (tmp_path / "contracts").exists()


def test_compaction_preserves_requirement_feedback_authority_and_evidence():
    context = {"claims": [{"statement": "a fact", "evidence": [{"locator": "manual.pdf", "page": 9}]}]}
    task = {"scenario": "original", "steering": [{"message": "correction"}],
        "knowledge_context": context, "resources": {"knowledge_context": context,
        "scenario_access": {"sha256": "authority"}, "preflight": {"candidates": ["old"] * 500},
        "available_capabilities": ["catalog"] * 500}, "step_results": [{"runs": ["actual"]}]}
    before = copy.deepcopy(task)
    result = compact_task(task)
    assert task == before
    for key in ("scenario", "steering", "step_results"):
        assert result[key] == task[key]
    assert result["resources"]["scenario_access"] == task["resources"]["scenario_access"]
    assert result["resources"]["knowledge_context"] == context
    assert len(json.dumps(result)) < len(json.dumps(task)) / 2


def test_lead_sees_actual_identity_arguments_without_reading_platform_source(tmp_path):
    runner = CodexAgentRunner(CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path))
    lead = next(a for a in load_simple_team() if a.name == "lead-agent")
    task = {"kind": "Brief", "iteration": 1, "scenario": "identify devices", "resources": {
        "development_mode": MODE, "execution_mode": "hardware", "available_tools": ["run_bbb", "measure_scope"]}}
    params = runner._turn_params(lead, task, "Plan")
    wire = json.loads(params["input"][0]["text"].split("TASK:\n")[1])
    catalog = {row["tool"]: row for row in wire["ready_device_tools"]}
    assert set(catalog) == {"run_bbb", "measure_scope"}
    assert all(c["arguments"]["properties"]["identity_only"] == {"type": "boolean"} for c in catalog.values())
    assert task.get("ready_device_tools") is None  # prompt projection only
