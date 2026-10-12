"""A campaign is bounded authority, not an agent-authored permission slip."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from ai_hardware_autolab.agents.schema import load_all
from ai_hardware_autolab.hwtools.result import sha256_file
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator import __main__ as entrypoint
from ai_hardware_autolab.orchestrator import wire as W
from ai_hardware_autolab.orchestrator.campaign import (
    CampaignAutoApprover,
    CampaignController,
    CampaignPolicy,
    CampaignPolicyError,
)
from ai_hardware_autolab.orchestrator.daemon import HardwareApprovalGate
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.loop import EXECUTE, Loop, start

AGENTS = {agent.name: agent for agent in load_all()}
SCENARIO = "Program the reviewed volatile image once, then measure RF1 and RF2."
NOW = "2026-08-24T09:00:00+00:00"
FUTURE = "2026-08-24T18:00:00+00:00"


def make_policy(tmp_path: Path, **changes):
    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    contract = contracts / "g.yaml"
    contract.write_text("test_id: campaign-test\n", encoding="utf-8")
    artifact = tmp_path / "candidate.bit"
    artifact.write_bytes(b"reviewed image")
    data = {
        "schema_version": 1,
        "campaign_id": "night-1",
        "approved_by": "quantum1",
        "expires_at": FUTURE,
        "scenario_sha256": hashlib.sha256(SCENARIO.encode()).hexdigest(),
        "max_auto_approvals": 2,
        "max_iterations": 3,
        "permissions": [
            {
                "id": "one-sram-load",
                "contract": "g.yaml",
                "contract_sha256": sha256_file(contract),
                "role": "board-operator",
                "tool": "program_cpld_sram",
                "artifacts": {"candidate.bit": sha256_file(artifact)},
                "max_approvals": 1,
                "max_executions": 1,
            }
        ],
    }
    data.update(changes)
    path = tmp_path / "night.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    policy = CampaignPolicy.load(
        path,
        sha256_file(path),
        contracts_dir=contracts,
        project_root=tmp_path,
    )
    return policy, contract, artifact


def make_policy_v2(tmp_path: Path):
    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    contract = contracts / "g.yaml"
    contract.write_text("test_id: campaign-test\n", encoding="utf-8")
    run_dir = tmp_path / "runs" / "run-one"
    run_dir.mkdir(parents=True)
    artifact = run_dir / "candidate.bit"
    artifact.write_bytes(b"iteration one")
    digest = sha256_file(artifact)
    manifest = {
        "run": "run-one",
        "verdict": "PASS",
        "contract": {"test_spec": "g.yaml"},
        "steps": [{"tool": "cpld", "action": "build", "verdict": "PASS"}],
        "artifacts": [
            {"path": "candidate.bit", "kind": "bitstream", "sha256": digest}
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = {
        "schema_version": 2,
        "campaign_id": "night-v2",
        "approved_by": "quantum1",
        "expires_at": FUTURE,
        "scenario_sha256": hashlib.sha256(SCENARIO.encode()).hexdigest(),
        "max_auto_approvals": 8,
        "max_iterations": 8,
        "permissions": [
            {
                "id": "rebuilt-sram-loads",
                "contract": "g.yaml",
                "contract_sha256": sha256_file(contract),
                "role": "board-operator",
                "tool": "program_cpld_sram",
                "artifacts": {},
                "artifact_mode": "run_manifest",
                "artifact_kinds": ["bitstream"],
                "producer_tools": ["cpld.build"],
                "max_approvals": 8,
                "max_executions": 1,
            }
        ],
    }
    path = tmp_path / "night-v2.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    policy = CampaignPolicy.load(
        path,
        sha256_file(path),
        contracts_dir=contracts,
        project_root=tmp_path,
    )
    reference = artifact.relative_to(tmp_path).as_posix()
    return policy, artifact, reference, manifest


def prepared_job(ledger: JobLedger):
    job = start(ledger, "g.yaml", "night work", scenario=SCENARIO)
    ledger.transition(job.job_id, J.RUNNING, reason="test setup")
    ledger.begin_iteration(job.job_id)
    return ledger.job(job.job_id)


def hardware_step(**changes):
    step = {
        "id": "s1",
        "agent": "board-operator",
        "tool": "program_cpld_sram",
        "contract": "g.yaml",
        "intent": "one volatile load",
        "unattended": True,
        "advance_when": [],
    }
    step.update(changes)
    return step


def test_campaign_file_is_bound_to_the_digest_a_person_activated(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    path = tmp_path / "night.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    with pytest.raises(CampaignPolicyError, match="changed after approval"):
        CampaignPolicy.load(
            path,
            policy.sha256,
            contracts_dir=tmp_path / "contracts",
            project_root=tmp_path,
        )


def test_matching_campaign_grants_a_durable_bounded_approval(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)

    evaluation = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])
    assert evaluation.allowed is True
    assert evaluation.artifacts == policy.permissions[0].artifacts
    request = ledger.request_approval(
        job.job_id,
        "g.yaml",
        "one volatile load",
        artifacts=evaluation.artifacts,
        step_id="s1",
        max_executions=evaluation.max_executions,
        expires_at=evaluation.expires_at,
    )
    decided = approver.apply(request, evaluation)

    assert decided.state == J.APPROVAL_GRANTED
    assert decided.decided_by == "policy:night-1"
    assert decided.bundle_sha256 == policy.sha256
    assert decided.expires_at == FUTURE


def test_v2_admits_each_new_build_through_its_pass_manifest(tmp_path):
    policy, artifact, reference, _ = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(
        ledger, policy, now=lambda: NOW, runs_dir=tmp_path / "runs"
    )
    step = hardware_step(artifacts={reference: sha256_file(artifact)})

    evaluation = approver.evaluate(job, step, AGENTS["board-operator"])

    assert evaluation.allowed is True
    assert evaluation.artifact_mode == "run_manifest"
    assert evaluation.artifacts == {reference: sha256_file(artifact)}
    assert evaluation.lineage_runs == ("run-one",)
    request = ledger.request_approval(
        job.job_id,
        "g.yaml",
        "load the latest bounded build",
        artifacts=evaluation.artifacts,
        step_id="s1",
        expires_at=evaluation.expires_at,
    )
    approver.apply(request, evaluation)
    recorded = [
        event for event in ledger.events_since(0, job.job_id)
        if event.kind == "campaign.approval.evaluated"
    ][-1]
    assert recorded.payload["lineage_runs"] == ["run-one"]
    assert recorded.payload["artifacts"] == {reference: sha256_file(artifact)}


def test_v2_late_binds_artifacts_from_an_earlier_step_result(tmp_path):
    policy, artifact, reference, _ = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    ledger.record_handoff(
        job.job_id,
        "StepResult",
        "builder",
        {
            "kind": "StepResult",
            "agent": "builder",
            "step_id": "build",
            "status": "done",
            "runs": ["run-one"],
        },
        iteration=job.iteration,
        step_id="build",
    )
    approver = CampaignAutoApprover(
        ledger, policy, now=lambda: NOW, runs_dir=tmp_path / "runs"
    )

    evaluation = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])

    assert evaluation.allowed is True
    assert evaluation.artifacts == {reference: sha256_file(artifact)}
    assert evaluation.lineage_runs == ("run-one",)


def test_v2_refuses_bytes_outside_the_approved_build_lineage(tmp_path):
    policy, artifact, reference, manifest = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(
        ledger, policy, now=lambda: NOW, runs_dir=tmp_path / "runs"
    )

    artifact.write_bytes(b"changed without a new manifest")
    evaluation = approver.evaluate(
        job,
        hardware_step(artifacts={reference: sha256_file(artifact)}),
        AGENTS["board-operator"],
    )
    assert evaluation.allowed is False
    assert "differs from its run manifest" in evaluation.reason

    digest = sha256_file(artifact)
    manifest["artifacts"][0]["sha256"] = digest
    manifest["artifacts"][0]["kind"] = "firmware"
    (artifact.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    evaluation = approver.evaluate(
        job,
        hardware_step(artifacts={reference: digest}),
        AGENTS["board-operator"],
    )
    assert evaluation.allowed is False
    assert "kind 'firmware'" in evaluation.reason

    outside = tmp_path / "candidate.bit"
    outside.write_bytes(b"not a run")
    evaluation = approver.evaluate(
        job,
        hardware_step(artifacts={"candidate.bit": sha256_file(outside)}),
        AGENTS["board-operator"],
    )
    assert evaluation.allowed is False
    assert "outside the immutable runs directory" in evaluation.reason


def test_a_single_web_activation_is_durable_and_restored_after_restart(tmp_path):
    policy, _, _, _ = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    loop = SimpleNamespace(auto_approver=None)
    router = SimpleNamespace(auto_approver=None)
    gate = SimpleNamespace(active_campaign_sha256=None, active_campaign_contracts={})
    controller = CampaignController(
        ledger,
        loop,
        gate,
        campaigns_dir=tmp_path,
        contracts_dir=tmp_path / "contracts",
        project_root=tmp_path,
        runs_dir=tmp_path / "runs",
        hardware_enabled=True,
        router=router,
        now=lambda: NOW,
    )

    activated = controller.activate(job.job_id, "night-v2.yaml", policy.sha256, "quantum1")

    assert activated["active"] is True
    assert activated["job_id"] == job.job_id
    assert loop.auto_approver.policy.sha256 == policy.sha256
    assert router.auto_approver is loop.auto_approver
    assert gate.active_campaign_sha256 == policy.sha256

    restarted_loop = SimpleNamespace(auto_approver=None)
    restarted_gate = SimpleNamespace(active_campaign_sha256=None, active_campaign_contracts={})
    restarted = CampaignController(
        ledger,
        restarted_loop,
        restarted_gate,
        campaigns_dir=tmp_path,
        contracts_dir=tmp_path / "contracts",
        project_root=tmp_path,
        runs_dir=tmp_path / "runs",
        hardware_enabled=True,
        now=lambda: NOW,
    )
    recovered = restarted.recover()
    assert recovered["active"] is True
    assert restarted_loop.auto_approver.policy.sha256 == policy.sha256

    stopped = restarted.deactivate("quantum1", "back at the bench")
    assert stopped == {"active": False, "hardware_enabled": True}
    assert restarted_loop.auto_approver is None
    assert restarted_gate.active_campaign_sha256 is None


def test_campaign_activation_refuses_a_different_scenario_and_offline_profile(tmp_path):
    policy, _, _, _ = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    wrong = start(ledger, "g.yaml", "other", scenario="different scenario")
    loop = SimpleNamespace(auto_approver=None)
    gate = SimpleNamespace(active_campaign_sha256=None, active_campaign_contracts={})
    controller = CampaignController(
        ledger, loop, gate,
        campaigns_dir=tmp_path, contracts_dir=tmp_path / "contracts",
        project_root=tmp_path, runs_dir=tmp_path / "runs", hardware_enabled=True,
        now=lambda: NOW,
    )
    with pytest.raises(CampaignPolicyError, match="job scenario hashes"):
        controller.activate(wrong.job_id, "night-v2.yaml", policy.sha256, "quantum1")

    offline = CampaignController(
        ledger, loop, gate,
        campaigns_dir=tmp_path, contracts_dir=tmp_path / "contracts",
        project_root=tmp_path, runs_dir=tmp_path / "runs", hardware_enabled=False,
        now=lambda: NOW,
    )
    with pytest.raises(CampaignPolicyError, match="hardware daemon profile"):
        offline.activate(wrong.job_id, "night-v2.yaml", policy.sha256, "quantum1")


def test_the_web_wire_can_review_and_activate_one_campaign(tmp_path):
    policy, _, _, _ = make_policy_v2(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    loop = SimpleNamespace(auto_approver=None)
    gate = SimpleNamespace(active_campaign_sha256=None, active_campaign_contracts={})
    controller = CampaignController(
        ledger, loop, gate,
        campaigns_dir=tmp_path, contracts_dir=tmp_path / "contracts",
        project_root=tmp_path, runs_dir=tmp_path / "runs", hardware_enabled=True,
        now=lambda: NOW,
    )
    sent = []
    wire = W.WireSession(ledger, ControlPlane(ledger, campaign_controller=controller), sent.append)

    wire.handle({"type": W.CAMPAIGN_CATALOG, "commandId": "catalog"})
    assert sent[-1]["ok"] is True
    assert any(item["filename"] == "night-v2.yaml" for item in sent[-1]["campaigns"])

    wire.handle({
        "type": W.CAMPAIGN_ACTIVATE,
        "commandId": "activate",
        "jobId": job.job_id,
        "filename": "night-v2.yaml",
        "sha256": policy.sha256,
        "approvedBy": "quantum1",
    })
    assert sent[-1]["ok"] is True
    assert sent[-1]["campaign"]["active"] is True


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"tool": "scan_jtag"}, "no campaign permission"),
        ({"agent": "measurement"}, "no campaign permission"),
        ({"contract": "other.yaml"}, "no campaign permission"),
    ],
)
def test_role_tool_and_contract_must_match_exactly(tmp_path, change, reason):
    policy, _, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    step = hardware_step(**change)
    agent = AGENTS.get(step["agent"], AGENTS["board-operator"])

    evaluation = approver.evaluate(job, step, agent)

    assert evaluation.allowed is False
    assert reason in evaluation.reason


def test_scenario_contract_and_artifact_drift_all_fail_closed(tmp_path):
    policy, contract, artifact = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    wrong = start(ledger, "g.yaml", "night work", scenario="a different request")
    ledger.transition(wrong.job_id, J.RUNNING, reason="test setup")
    ledger.begin_iteration(wrong.job_id)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    assert "scenario hashes" in approver.evaluate(
        ledger.job(wrong.job_id), hardware_step(), AGENTS["board-operator"]
    ).reason

    matching = prepared_job(ledger)
    contract.write_text("test_id: changed\n", encoding="utf-8")
    assert "contract g.yaml drifted" in approver.evaluate(
        matching, hardware_step(), AGENTS["board-operator"]
    ).reason
    contract.write_text("test_id: campaign-test\n", encoding="utf-8")
    artifact.write_bytes(b"rebuilt after review")
    assert "campaign artifact candidate.bit now hashes" in approver.evaluate(
        matching, hardware_step(), AGENTS["board-operator"]
    ).reason


def test_expiry_and_permission_quota_stop_automatic_grants(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    expired = CampaignAutoApprover(ledger, policy, now=lambda: FUTURE)
    assert expired.evaluate(job, hardware_step(), AGENTS["board-operator"]).allowed is False

    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    evaluation = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])
    request = ledger.request_approval(
        job.job_id,
        "g.yaml",
        "one volatile load",
        artifacts=evaluation.artifacts,
        step_id="s1",
        expires_at=evaluation.expires_at,
    )
    approver.apply(request, evaluation)

    second = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])
    assert second.allowed is False
    assert "used all 1 automatic approvals" in second.reason


def test_loop_records_auto_approval_as_one_tick_then_executes_next_tick(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    plan = {
        "kind": "Plan",
        "agent": "gate-architect",
        "gate": "g.yaml",
        "iteration": 1,
        "objective": "night work",
        "changed_from_previous": None,
        "blocked_on": None,
        "resources": {"board": [], "instruments": [], "host": "test"},
        "steps": [hardware_step()],
        "corroboration": [],
    }
    ledger.record_handoff(job.job_id, "Plan", "gate-architect", plan, iteration=1)
    calls = []

    def runner(agent, task):
        calls.append((agent.name, task))
        return {
            "kind": "StepResult",
            "agent": agent.name,
            "step_id": "s1",
            "status": "done",
            "runs": [],
            "artifacts": {},
            "measurements": {},
            "unmet": [],
            "notes": "test",
        }

    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    loop = Loop(ledger, runner, runs_dir=tmp_path, auto_approver=approver)

    first = loop.advance(job.job_id)

    assert first.action == EXECUTE and first.blocked is False
    assert calls == []
    approval = ledger.approval(first.approval_id)
    assert approval.state == J.APPROVAL_GRANTED
    loop.advance(job.job_id)
    assert [name for name, _ in calls] == ["board-operator"]


def test_auto_grant_requires_the_same_campaign_at_the_tool_boundary(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    evaluation = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])
    request = ledger.request_approval(
        job.job_id,
        "g.yaml",
        "one volatile load",
        artifacts=evaluation.artifacts,
        step_id="s1",
        max_executions=1,
        expires_at=FUTURE,
    )
    approver.apply(request, evaluation)
    task = {
        "job_id": job.job_id,
        "gate": "g.yaml",
        "step": hardware_step(),
    }

    inactive = HardwareApprovalGate(
        ledger,
        contracts_dir=tmp_path / "contracts",
        project_root=tmp_path,
        now=lambda: NOW,
    )
    assert not inactive(
        AGENTS["board-operator"], task, "program_cpld_sram", {}
    )
    assert "not active in this daemon" in inactive.refusals[-1]

    active = HardwareApprovalGate(
        ledger,
        contracts_dir=tmp_path / "contracts",
        project_root=tmp_path,
        active_campaign_sha256=policy.sha256,
        active_campaign_contracts={"g.yaml": policy.permissions[0].contract_sha256},
        now=lambda: NOW,
    )
    assert active(AGENTS["board-operator"], task, "program_cpld_sram", {})
    assert not active(AGENTS["board-operator"], task, "program_cpld_sram", {})
    assert "used all 1" in active.refusals[-1]


def test_contract_is_rehashed_again_at_the_hardware_tool_boundary(tmp_path):
    policy, contract, _ = make_policy(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.db")
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    evaluation = approver.evaluate(job, hardware_step(), AGENTS["board-operator"])
    request = ledger.request_approval(
        job.job_id,
        "g.yaml",
        "one volatile load",
        artifacts=evaluation.artifacts,
        step_id="s1",
        expires_at=FUTURE,
    )
    approver.apply(request, evaluation)
    contract.write_text("test_id: changed-after-auto-approval\n", encoding="utf-8")
    gate = HardwareApprovalGate(
        ledger,
        contracts_dir=tmp_path / "contracts",
        project_root=tmp_path,
        active_campaign_sha256=policy.sha256,
        active_campaign_contracts={"g.yaml": policy.permissions[0].contract_sha256},
        now=lambda: NOW,
    )
    task = {"job_id": job.job_id, "gate": "g.yaml", "step": hardware_step()}

    assert not gate(AGENTS["board-operator"], task, "program_cpld_sram", {})
    assert "campaign contract g.yaml now hashes" in gate.refusals[-1]
    assert not any(
        event.kind == "approval.authorized"
        for event in ledger.events_since(0, job.job_id)
    )


def test_unknown_fields_and_unbounded_artifacts_are_rejected(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    path = tmp_path / "night.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["self_approve"] = True
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CampaignPolicyError, match="unknown campaign fields"):
        CampaignPolicy.load(
            path,
            sha256_file(path),
            contracts_dir=tmp_path / "contracts",
            project_root=tmp_path,
        )

    # Preserve the valid policy object only to make explicit that parsing a
    # later file cannot mutate already-loaded authority.
    assert policy.campaign_id == "night-1"


def test_production_service_rejects_retired_campaign_settings(tmp_path):
    policy, _, _ = make_policy(tmp_path)
    args = argparse.Namespace(
        ledger=str(tmp_path / "service.db"),
        runs=str(tmp_path / "runs"),
        contracts=str(tmp_path / "contracts"),
        profile="hardware",
        campaign="night.yaml",
        campaign_sha256=policy.sha256,
        dry_run=False,
        max_iterations=4,
        backoff=1.0,
        retries=2,
        interval=0.1,
        pipe=None,
    )

    with pytest.raises(ValueError, match="Campaign execution has been retired"):
        entrypoint.build_service(args)
