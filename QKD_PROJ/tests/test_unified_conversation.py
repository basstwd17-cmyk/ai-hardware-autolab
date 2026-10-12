"""New requests share one flow; no operating DB, live model or device is used."""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from QKD_PROJ.orchestrator.control import ControlPlane, ControlError
from QKD_PROJ.orchestrator.conversation_flow import conversation_brief
from QKD_PROJ.orchestrator.conversation_service import WebConversationFlow
from QKD_PROJ.orchestrator.conversation_hardware import ConversationHardwareConfig, ConversationHardwareExecutor
from QKD_PROJ.orchestrator.daemon import Daemon
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
from QKD_PROJ.orchestrator.scenario_access import ScenarioAutoApprover
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.orchestrator.workflows import WorkflowRouter
from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.mcp_server.protocol import Tool
from QKD_PROJ.tests.test_scope_session_approval import setup_scope


HARDWARE = "Execution scope: hardware; submitting this scenario delegates control of the devices requested below, within their electrical safety bounds. Other devices are not authorized.\n\n"


def make_control(tmp_path, runtime=None):
    ledger = JobLedger(tmp_path / "jobs.db")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    def prepare(job_id):
        path = tmp_path / "workspaces" / job_id
        path.mkdir(parents=True)
        return path
    flow = WebConversationFlow(ledger, runtime, SimpleNamespace(prepare=prepare))
    old = SimpleNamespace(policy=SimpleNamespace(max_iterations=8), auto_approver=None,
                          advance=lambda _: pytest.fail("new request entered legacy execution"))
    router = WorkflowRouter(ledger, old, SimpleNamespace(owns=lambda _: False), simple=old, conversation=flow)
    daemon = Daemon(ledger, router)
    control = ControlPlane(ledger, daemon, preflight_engine=ScenarioPreflight(contracts))
    return ledger, flow, daemon, control


def submit(control, scenario):
    return control.mission(scenario, scenario, hashlib.sha256(scenario.encode()).hexdigest())


@pytest.mark.parametrize("scenario", [
    "Implement addition", "보드 없이 SPI 시뮬레이션 코드를 작성해줘",
    HARDWARE + "새로운 보드의 펌웨어를 만들고 동작을 확인해줘",
])
def test_no_catalog_or_scope_prefix_is_needed_to_create_the_same_flow(tmp_path, scenario):
    ledger, flow, _, control = make_control(tmp_path)
    preview = control.preflight(scenario)
    assert preview["workflow"] == "conversation"
    response = submit(control, scenario)
    job_id = response["job"]["job_id"]
    assert response["workflow"] == "conversation" and flow.owns(job_id)
    assert response["preflight"]["scenario_sha256"] == preview["scenario_sha256"]
    assert not ledger.handoffs(job_id, "Plan")
    if scenario.startswith(HARDWARE):
        assert conversation_brief(ledger, job_id)["inputs"]["device_tools"] == []


def test_ambiguous_contracts_do_not_select_a_second_workflow(tmp_path):
    ledger, flow, _, control = make_control(tmp_path)
    for name in ("one", "two"):
        (control.preflight_engine.contracts_dir / (name + ".yaml")).write_text(
            f"test_id: {name}\nstatus: ready\npurpose: [BBB DMA buffer]\nallowed_operations: [bbb_ssh_command]\n")
    response = submit(control, HARDWARE + "one.yaml two.yaml BBB DMA buffer를 만들어줘")
    assert response["workflow"] == "conversation"
    assert response["preflight"]["reason_code"] == "ambiguous_contract"
    assert flow.owns(response["job"]["job_id"])


def test_office_protocol_returns_success_and_reuses_the_live_job(tmp_path):
    ledger, flow, _, control = make_control(tmp_path)
    sent = []
    wire = W.WireSession(ledger, control, sent.append)
    scenario = "Implement addition"
    preview = control.preflight(scenario)
    frame = {"type": "job.mission", "commandId": "start", "objective": scenario,
             "scenario": scenario, "scenarioSha256": preview["scenario_sha256"], "searchPolicy": "refresh"}
    wire.handle(frame)
    first = sent[-1]
    assert first["ok"] and first["preflight"] and first["workflow"] == "conversation"
    wire.handle({**frame, "commandId": "start-again"})
    assert sent[-1]["ok"] and sent[-1]["reused"]
    assert sent[-1]["job"]["job_id"] == first["job"]["job_id"]
    assert len(ledger.jobs()) == 1


def test_missing_runtime_is_an_explicit_error_not_a_legacy_fallback(tmp_path):
    control = ControlPlane(JobLedger(tmp_path / "jobs.db"))
    with pytest.raises(ControlError, match="대화 실행기"):
        submit(control, "Implement addition")
    assert control.ledger.jobs() == []


def test_old_job_keeps_its_recorded_route_after_new_submission(tmp_path):
    ledger, flow, daemon, control = make_control(tmp_path)
    old, _ = start_simple_mission(ledger, "old goal", "old scenario", resources={})
    original = copy.deepcopy(ledger.handoffs(old.job_id, "Brief", 0)[0].payload)
    calls = []
    daemon.loop.simple.advance = lambda job_id: calls.append(job_id)
    submit(control, "new request")
    daemon.loop.advance(old.job_id)
    assert calls == [old.job_id] and not flow.owns(old.job_id)
    assert ledger.handoffs(old.job_id, "Brief", 0)[0].payload == original


def test_offline_question_cannot_gain_device_tools_by_feedback(tmp_path):
    seen = []
    def runtime(role, context):
        seen.append(context)
        if not context["feedback"]:
            return {"status": "needs_user", "summary": "Which board's source?"}
        assert context["execution_mode"] == "offline"
        assert not context["inputs"].get("device_tools")
        return {"status": "ready", "summary": "Continue source work only"}
    ledger, flow, daemon, control = make_control(tmp_path, runtime)
    job_id = submit(control, "보드 없이 코드를 작성해줘")["job"]["job_id"]
    daemon.tick()
    control.steer(job_id, "BBB에 연결해서 업로드해", "human")
    daemon.tick()
    assert len(seen) == 2 and flow.owns(job_id)
    assert not [e for e in flow._events(job_id) if e.kind == "conversation.hardware.prepared"]


@pytest.mark.parametrize("clarify", [False, True])
def test_hardware_clarification_continues_same_job_through_real_approval_gate(setup_scope, clarify):
    s = setup_scope
    contract = yaml.safe_load((s.controller.contracts_dir / s.step["contract"]).read_text())
    contract.update(test_id="scope-ready", status="ready", purpose=["RTM3004 oscilloscope CH3 capture"])
    (s.controller.contracts_dir / "scope-ready.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    workspace = s.root / "conversation-workspace"
    workspace.mkdir()
    calls = []
    approver = ScenarioAutoApprover(s.ledger, project_root=s.root, contracts_dir=s.controller.contracts_dir,
                                   runs_dir=s.root / "runs", enabled=True, now=lambda: s.clock[0])
    s.gate.scenario_authorizer = approver
    def handler(args):
        calls.append(args)
        return {"run": "scope-fresh", "run_verdict": "PASS", "result": {"ok": True}}
    executor = ConversationHardwareExecutor(s.ledger, approver, s.gate,
        ConversationHardwareConfig(s.root, s.controller.contracts_dir, s.root / "runs"),
        tool_builder=lambda _: [Tool("measure_scope", "mock identity", {}, handler, touches_hardware=True)])
    contexts = []
    class Runtime:
        execute_device = staticmethod(executor)
        def __call__(self, role, context):
            contexts.append((role, context))
            assert "scenario_access" not in context["inputs"]
            assert "device_boundary" not in context["inputs"]
            if context["inputs"]["device_readiness"]["status"] != "ready":
                return {"status": "needs_user", "summary": "어떤 장비를 사용할까요?"}
            if role == "lead":
                return {"status": "ready", "summary": "요청한 스코프를 식별합니다."}
            if role == "builder" and not context["device_results"]:
                return {"status": "device", "summary": "Read identity", "device_call": {
                    "tool": "measure_scope", "arguments": {"identity_only": True}}}
            assert context["device_results"][-1].get("allowed"), context["device_results"][-1]
            return {"status": "satisfied" if role == "verifier" else "ready",
                    "summary": "Fresh identity was returned", "runs": ["scope-fresh"]}
    flow = WebConversationFlow(s.ledger, Runtime(), SimpleNamespace(prepare=lambda _: workspace))
    control = ControlPlane(s.ledger, SimpleNamespace(loop=SimpleNamespace(conversation=flow)),
                           campaign_controller=s.controller, preflight_engine=ScenarioPreflight(s.controller.contracts_dir))
    request = "연결된 장비 상태를 보고해줘" if clarify else "scope-ready.yaml의 RTM3004 오실로스코프 CH3 연결을 확인해줘"
    job_id = submit(control, HARDWARE + request)["job"]["job_id"]
    original = copy.deepcopy(s.ledger.handoffs(job_id, "Brief", 0)[0].payload)
    if clarify:
        assert flow.run(job_id).action == "needs_user" and not calls
        control.steer(job_id, "scope-ready.yaml의 RTM3004 오실로스코프 CH3 연결을 확인해줘", "human")
    effective = conversation_brief(s.ledger, job_id)
    assert effective["inputs"]["device_readiness"]["status"] == "ready"
    assert effective["inputs"]["scenario_access"]["expires_at"] == original["inputs"]["scenario_access"]["expires_at"]
    assert approver.source(job_id)["devices"][0]["kind"] == "scope"
    outcome = flow.run(job_id)
    assert outcome.done, contexts[-1][1]["device_results"][-1].get("error")
    assert len(calls) == 1 and calls[0]["contract"] == "scope-ready.yaml"
    assert s.ledger.handoffs(job_id, "Brief", 0)[0].payload == original
    assert {context["workspace"] for _, context in contexts} == {str(workspace)}
    assert not s.ledger.handoffs(job_id, "Plan")
    if clarify:
        event = next(e for e in flow._events(job_id) if e.kind == "conversation.hardware.prepared")
        assert "inputs" in event.payload and "inputs" not in event.describe()["payload"]


def test_cancelled_pending_hardware_is_never_prepared_or_reopened(tmp_path):
    ledger, flow, _, control = make_control(tmp_path)
    job_id = submit(control, HARDWARE + "장비를 확인해줘")["job"]["job_id"]
    control.cancel(job_id, "human")
    with pytest.raises(ControlError, match="finished"):
        control.steer(job_id, "BBB를 사용해줘", "human")
    assert ledger.job(job_id).outcome == "cancelled"
