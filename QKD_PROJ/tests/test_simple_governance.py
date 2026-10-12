from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from QKD_PROJ.agents.schema import ValidationError, load_simple_team, validate_plan_governance
from QKD_PROJ.capabilities.matcher import match_capabilities
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop, LoopPolicy
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.orchestrator.evidence import ConditionResult, Reduction
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate, SafetyViolation


def test_simple_team_uses_scenario_specific_bounds_instead_of_global_board_defaults():
    prompts = {agent.name: agent.prompt for agent in load_simple_team()}
    lead = prompts["lead-agent"]
    builder = prompts["builder"]
    verifier = prompts["verifier"]

    assert "No blanket offline-preparation phase" in lead
    assert "Scope input limits/channels are not board power-supply current limits" in lead
    assert "isolated job workspace" in builder
    assert "requirements_met" in verifier
    assert "measured fallback" in verifier


def _plan(*, strategy: str = "new", action: str = "new") -> dict:
    implementation = {
        "strategy": strategy,
        "base_capabilities": [],
        "write_scope": ["QKD_PROJ/generated/example.v"],
    }
    if strategy == "new":
        implementation["new_reason"] = "no offered capability implements this function"
    return {
        "kind": "Plan",
        "agent": "lead-agent",
        "gate": "__mission__",
        "iteration": 1,
        "objective": "build the requested offline logic",
        "contract_action": action,
        "contract_spec": {
            "objective": "build the requested offline logic",
            "allowed_operations": ["diamond_build"],
            "forbidden_operations": ["sram_fast_program", "oscilloscope_capture"],
            "pass_criteria": [
                {"field": "measurements.errors", "expect": 0, "source": "generated contract"}
            ],
            "evidence_required": ["build run manifest"],
        },
        "implementation": implementation,
        "steps": [
            {
                "id": "build",
                "agent": "builder",
                "tool": "build_cpld",
                "contract": "$generated",
                "touches_hardware": False,
                "advance_when": [
                    {"field": "measurements.errors", "expect": 0, "source": "generated contract"}
                ],
            }
        ],
    }


def test_governed_plan_rejects_unbounded_or_stale_reuse():
    row = {"id": "base", "version": "v1", "sha256": "a" * 64}
    plan = _plan(strategy="extend", action="amend")
    plan["implementation"] = {
        "strategy": "extend",
        "base_capabilities": [
            {"id": "base", "version": "v1", "catalog_sha256": "b" * 64}
        ],
        "write_scope": ["QKD_PROJ/generated/example.v"],
    }
    with pytest.raises(ValidationError, match="digest is stale"):
        validate_plan_governance(plan, available_capabilities=[row])


def test_reuse_cannot_claim_a_write_scope():
    row = {"id": "base", "version": "v1", "sha256": "a" * 64}
    plan = _plan(strategy="reuse", action="reuse")
    plan["implementation"] = {
        "strategy": "reuse",
        "base_capabilities": [
            {"id": "base", "version": "v1", "catalog_sha256": "a" * 64}
        ],
        "write_scope": ["QKD_PROJ/source.v"],
    }
    with pytest.raises(ValidationError, match="forbids every write path"):
        validate_plan_governance(plan, available_capabilities=[row])


def test_matcher_narrows_bbb_scope_candidates():
    rows = [
        {"id": "bbb-pru-control", "title": "BBB PRU", "summary": "PRU firmware", "operations": ["pru_deploy"]},
        {"id": "rtm3004-scope-capture", "title": "Scope", "summary": "waveform capture", "operations": ["oscilloscope_capture"]},
    ]
    matched = match_capabilities("BBB PRU로 펌웨어를 구현해줘", rows)
    assert [row["id"] for row in matched] == ["bbb-pru-control"]
    assert matched[0]["match_score"] > 0


def test_contract_compiler_installs_deterministic_create_only_yaml(tmp_path: Path):
    contracts = tmp_path / "QKD_PROJ" / "contracts"
    compiler = ContractCompiler(contracts, project_root=tmp_path)
    first = compiler.compile(
        _plan(),
        job_id="job_123",
        iteration=1,
        scenario_sha256="c" * 64,
        resources={"available_capabilities": []},
    )
    second = compiler.compile(
        _plan(),
        job_id="job_123",
        iteration=1,
        scenario_sha256="c" * 64,
        resources={"available_capabilities": []},
    )
    assert first.installed is True
    assert second.installed is False
    assert first.filename == second.filename
    path = contracts / first.filename
    assert hashlib.sha256(path.read_bytes()).hexdigest() == first.sha256
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert document["status"] == "reviewed_generated_offline"
    assert first.plan["steps"][0]["contract"] == first.filename


class _PlanOnlyRunner:
    def __call__(self, agent, task):
        if agent.name != "lead-agent":
            raise AssertionError(agent.name)
        return _plan()


class _RepairingPlanRunner:
    def __init__(self):
        self.tasks = []

    def __call__(self, agent, task):
        self.tasks.append(task)
        if len(self.tasks) == 1:
            broken = _plan()
            broken["steps"][0]["assigned_to"] = broken["steps"][0].pop("agent")
            return broken
        return _plan()


def test_simple_loop_records_contract_and_capability_decisions(tmp_path: Path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job, _ = start_simple_mission(ledger, "offline logic", "build an offline CPLD block", resources={"development_mode": "legacy"})
    loop = Loop(
        ledger,
        _PlanOnlyRunner(),
        agents=load_simple_team(),
        policy=LoopPolicy(report_on_finish=False),
        planner_agent="lead-agent",
        verifier_agent="verifier",
        require_plan_governance=True,
        contract_compiler=ContractCompiler(tmp_path / "contracts", project_root=tmp_path),
    )
    result = loop.advance(job.job_id)
    assert result.action == "plan"
    kinds = [event.kind for event in ledger.events_since(0, job.job_id)]
    assert "capability.selected" in kinds
    assert "contract.compiled" in kinds
    stored = ledger.handoffs(job.job_id, "Plan", 1)[0].payload
    assert stored["compiled_contract"]["sha256"]
    assert stored["steps"][0]["contract"].endswith(".yaml")


def test_simple_lead_gets_one_bounded_format_repair_before_job_blocks(tmp_path: Path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job, _ = start_simple_mission(ledger, "offline logic", "build an offline CPLD block", resources={"development_mode": "legacy"})
    runner = _RepairingPlanRunner()
    loop = Loop(
        ledger,
        runner,
        agents=load_simple_team(),
        policy=LoopPolicy(report_on_finish=False),
        planner_agent="lead-agent",
        verifier_agent="verifier",
        require_plan_governance=True,
        repair_invalid_plans=True,
        contract_compiler=ContractCompiler(tmp_path / "contracts", project_root=tmp_path),
    )

    result = loop.advance(job.job_id)

    assert result.action == "plan"
    assert len(runner.tasks) == 2
    assert "missing 'agent'" in runner.tasks[1]["format_correction"]["error"]
    assert ledger.job(job.job_id).state == "running"


def test_reasoning_verifier_can_advise_but_cannot_override_evidence(tmp_path: Path):
    ledger = JobLedger(tmp_path / "jobs.db")
    job, _ = start_simple_mission(ledger, "offline logic", "build it")
    ledger.transition(job.job_id, "running", reason="test")
    iteration = ledger.begin_iteration(job.job_id)
    reduction = Reduction(
        gate="__mission__",
        iteration=iteration,
        outcome="open",
        conditions=[
            ConditionResult(
                field="measurements.last_command_exit_code",
                expect="0",
                observed=1,
                status="unmet",
                evidence=["run_1"],
            )
        ],
        failure_signature="failed-command",
    )

    received = []

    class Reviewer:
        def __call__(self, agent, task):
            received.append(task)
            answer = dict(task["deterministic_verdict"])
            answer.update(
                {
                    "kind": "Verdict",
                    "agent": "verifier",
                    "outcome": "satisfied",  # an attempted unsafe override
                    "analysis": "the final command failed",
                    "recommended_next_action": "fix the implementation and rerun",
                }
            )
            return answer

    loop = Loop(
        ledger,
        Reviewer(),
        agents=load_simple_team(),
        policy=LoopPolicy(report_on_finish=False),
        planner_agent="lead-agent",
        verifier_agent="verifier",
        reasoning_verifier=True,
    )
    loop._run_reasoning_verifier(ledger.job(job.job_id), {}, reduction)
    assert received[0]["resources"]["source_context"]["active_team"] == "QKD_PROJ/agents/simple/"

    review = [
        event.payload
        for event in ledger.events_since(0, job.job_id)
        if event.kind == "verifier.review"
    ][0]
    assert review["status"] == "unavailable"
    assert "outcome" in review["error"]


def _hardware_safety():
    return {
        "limits": [
            {
                "subject": "VDD",
                "parameter": "voltage",
                "maximum": 3.6,
                "unit": "V",
                "source": "vendor datasheet table 20 page 80",
            },
            {
                "subject": "bench supply",
                "parameter": "current",
                "maximum": 300,
                "unit": "mA",
                "source": "board manual power table page 24",
            },
        ],
        "pin_assignments": [
            {
                "signal": "TEST_OUT",
                "pin": "PA8",
                "direction": "output",
                "source": "vendor datasheet alternate function table page 92",
            }
        ],
        "scope_channels": [
            {"channel": 1, "signal": "TEST_OUT", "termination": "1MOhm"}
        ],
        "stop_conditions": ["stop when current reaches 300 mA"],
    }


def test_new_hardware_plan_requires_cited_voltage_current_and_pin_bounds():
    plan = _plan()
    plan["steps"][0].update(
        {"tool": "run_board_stage", "touches_hardware": True}
    )
    plan["contract_spec"]["allowed_operations"] = ["board_upload"]

    with pytest.raises(ValidationError, match="hardware_safety"):
        validate_plan_governance(plan)

    plan["contract_spec"]["hardware_safety"] = _hardware_safety()
    assert validate_plan_governance(plan) is plan


def test_generated_hardware_contract_cannot_execute_without_safety_envelope(tmp_path: Path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "board-map.yaml").write_text("{}\n", encoding="utf-8")
    (contracts / "unsafe.yaml").write_text(
        "test_id: unsafe\nstatus: reviewed_generated_hardware\n"
        "allowed_operations: [board_upload]\nforbidden_operations: []\n",
        encoding="utf-8",
    )
    gate = SafetyGate(Contracts.load("unsafe.yaml", contracts), allow_hardware=True)

    with pytest.raises(SafetyViolation, match="hardware_safety"):
        gate.require("board_upload")


def test_generated_hardware_scope_is_bound_to_reviewed_channels_and_voltage(tmp_path: Path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "board-map.yaml").write_text("{}\n", encoding="utf-8")
    document = {
        "test_id": "safe",
        "status": "reviewed_generated_hardware",
        "allowed_operations": ["oscilloscope_capture"],
        "forbidden_operations": [],
        "hardware_safety": _hardware_safety(),
    }
    (contracts / "safe.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    gate = SafetyGate(Contracts.load("safe.yaml", contracts), allow_hardware=True)
    params = {"channels": [{"channel": 1, "signal": "TEST_OUT"}]}

    gate.require_scope_capture(params)
    gate.require_measured_voltage({"CH1_min_v": 0.0, "CH1_max_v": 3.3})

    with pytest.raises(SafetyViolation, match="differ from reviewed mapping"):
        gate.require_scope_capture(
            {"channels": [{"channel": 2, "signal": "TEST_OUT"}]}
        )
    with pytest.raises(SafetyViolation, match="exceeded"):
        gate.require_measured_voltage({"CH1_min_v": 0.0, "CH1_max_v": 3.7})


def test_compiled_hardware_contract_pins_profile_and_electrical_envelope(tmp_path: Path):
    plan = _plan()
    plan["steps"][0].update(
        {
            "tool": "run_board_stage",
            "touches_hardware": True,
            "tool_args": {"stage_id": "upload-firmware"},
        }
    )
    plan["contract_spec"]["allowed_operations"] = ["board_upload"]
    plan["contract_spec"]["hardware_safety"] = _hardware_safety()
    capability = {
        "id": "generic-board-upload",
        "version": "v1",
        "sha256": "d" * 64,
        "operations": ["board_upload"],
    }
    contracts = tmp_path / "QKD_PROJ" / "contracts"
    compiled = ContractCompiler(contracts, project_root=tmp_path).compile(
        plan,
        job_id="job_123",
        iteration=1,
        scenario_sha256="c" * 64,
        resources={
            "execution_mode": "hardware",
            "available_capabilities": [capability],
            "board_development": {
                "selected": {
                    "board_id": "demo-board",
                    "revision": "r1",
                    "profile": "QKD_PROJ/boards/demo-board/r1/profile.yaml",
                    "profile_sha256": "e" * 64,
                    "board_map": "QKD_PROJ/boards/demo-board/r1/board-map.yaml",
                }
            },
        },
    )
    document = yaml.safe_load((contracts / compiled.filename).read_text(encoding="utf-8"))

    assert document["hardware_safety"] == _hardware_safety()
    assert document["board_profile"]["path"].endswith("profile.yaml")
    assert document["board_profile"]["sha256"] == "e" * 64
