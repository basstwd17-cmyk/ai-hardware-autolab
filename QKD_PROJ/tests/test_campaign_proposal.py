from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.orchestrator.campaign import CampaignController
from QKD_PROJ.orchestrator.campaign_proposal import CampaignProposalService
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import start
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.research.discovery_loop import start_mission


NOW = "2026-09-01T01:00:00+00:00"


def test_catalog_uses_current_hardware_jobs_not_historical_result_cards(tmp_path: Path):
    ledger, legacy, controller = _environment(tmp_path)
    control = ControlPlane(ledger, campaign_controller=controller)
    pending, _ = start_simple_mission(ledger, "prepare", "prepare scope", resources={"execution_mode": "hardware"})
    stopped, _ = start_simple_mission(ledger, "stopped", "old scope", resources={"execution_mode": "hardware"})
    offline, _ = start_simple_mission(ledger, "offline", "offline build")
    ledger.transition(stopped.job_id, J.DONE, "cancelled")

    rows = control.campaign_catalog()["candidates"]
    assert {row["job_id"] for row in rows} == {legacy.job_id, pending.job_id}
    assert rows[0]["job_id"] == legacy.job_id  # legacy continuation still supported
    waiting = next(row for row in rows if row["job_id"] == pending.job_id)
    assert waiting["preview_available"] is False
    assert "계획" in waiting["detail"]

    ledger.transition(pending.job_id, J.RUNNING)
    ledger.begin_iteration(pending.job_id)
    ledger.record_handoff(pending.job_id, "Plan", "lead-agent", {
        "kind": "Plan", "agent": "lead-agent", "steps": [
            {"id": "measure", "tool": "measure_board_scope", "touches_hardware": True}
        ],
    })
    rows = control.campaign_catalog()["candidates"]
    planned = next(row for row in rows if row["job_id"] == pending.job_id)
    assert planned["preview_available"] is True
    assert planned["revision"] != waiting["revision"]
    # An older hardware Plan must not enable approval in a new iteration.
    ledger.begin_iteration(pending.job_id)
    rows = control.campaign_catalog()["candidates"]
    assert next(row for row in rows if row["job_id"] == pending.job_id)["preview_available"] is False


def _environment(tmp_path: Path):
    contracts = tmp_path / "contracts"
    campaigns = tmp_path / "campaigns"
    profiles = tmp_path / "boards" / "example" / "v1"
    work = tmp_path / "work"
    for directory in (contracts, campaigns, profiles, work):
        directory.mkdir(parents=True, exist_ok=True)
    (tmp_path / "board-map.yaml").write_text("pins: {}\n", encoding="utf-8")
    (tmp_path / "source.pdf").write_bytes(b"official")
    profile_path = profiles / "profile.yaml"
    profile = {
        "schema_version": 1,
        "board_id": "example-board",
        "revision": "v1",
        "title": "Example reviewed board",
        "aliases": ["example board"],
        "board_map": "board-map.yaml",
        "sources": [{
            "id": "datasheet", "title": "Official data sheet",
            "source_kind": "official-datasheet", "location": "source.pdf",
        }],
        "stages": [
            {
                "id": "build-fw", "phase": "build", "mode": "offline",
                "agent": "firmware-builder", "tool": "run_board_build",
                "status": "ready", "requires_campaign": False,
                "command": [sys.executable, "-c", "print('build')"],
                "cwd": "work", "artifacts": [], "parameters": {},
            },
            {
                "id": "upload-fw", "phase": "upload", "mode": "hardware",
                "agent": "board-operator", "tool": "run_board_stage",
                "status": "ready", "requires_campaign": True,
                "command": [sys.executable, "-c", "print('upload')"],
                "cwd": "work", "artifacts": [], "parameters": {},
            },
            {
                "id": "measure-out", "phase": "measure", "mode": "hardware",
                "agent": "measurement", "tool": "measure_board_scope",
                "status": "ready", "requires_campaign": True,
                "command": [], "cwd": None, "artifacts": ["capture"],
                "parameters": {
                    "channels": [{
                        "channel": 1, "signal": "OUT", "scale_v_per_div": 1.0,
                        "offset_v": 0.0,
                    }],
                    "timebase_s_per_div": 1e-6, "trigger_channel": 1,
                    "trigger_level_v": 0.5, "trigger_slope": "POS", "timeout_s": 5,
                },
            },
        ],
    }
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    profile_digest = sha256_file(profile_path)
    scenario = "Upload firmware to the example board and measure OUT."
    contract_data = {
        "test_id": "generated-example",
        "status": "reviewed_generated_hardware",
        "hardware_safety": {
            "limits": [
                {
                    "subject": "example-board I/O",
                    "parameter": "voltage",
                    "maximum": 3.3,
                    "unit": "V",
                    "source": "Official data sheet",
                },
                {
                    "subject": "example-board supply",
                    "parameter": "current",
                    "maximum": 0.5,
                    "unit": "A",
                    "source": "Official data sheet",
                },
            ],
            "pin_assignments": [
                {
                    "board": "example-board",
                    "pin": "OUT",
                    "signal": "OUT",
                    "direction": "output",
                    "source": "board-map.yaml",
                }
            ],
            "scope_channels": [
                {"channel": 1, "signal": "OUT", "termination": "1MOhm"}
            ],
            "stop_conditions": ["stop on over-current or unexpected voltage"],
        },
        "board_profile": {
            "board_id": "example-board", "revision": "v1",
            "path": profile_path.relative_to(tmp_path).as_posix(),
            "sha256": profile_digest, "board_map": "board-map.yaml",
        },
        "orchestration": {
            "roles": ["firmware-builder", "board-operator", "measurement"],
            "skills": [], "constraints": [],
            "steps": [
                {
                    "id": "upload", "agent": "board-operator",
                    "tool": "run_board_stage", "tool_args": {"stage_id": "upload-fw"},
                    "intent": "upload", "touches_hardware": True,
                    "unattended": False, "advance_when": [],
                },
                {
                    "id": "measure", "agent": "measurement",
                    "tool": "measure_board_scope", "tool_args": {"stage_id": "measure-out"},
                    "intent": "measure", "touches_hardware": True,
                    "unattended": False, "advance_when": [],
                },
            ],
        },
    }
    contract = contracts / "generated-example.yaml"
    contract.write_text(yaml.safe_dump(contract_data, sort_keys=False), encoding="utf-8")
    ledger = JobLedger(tmp_path / "jobs.db")
    job = start_mission(
        ledger, "implement and measure", scenario,
        resources={"execution_mode": "hardware"},
    )
    ledger.transition(job.job_id, J.RUNNING, reason="test")
    ledger.append_event(job.job_id, "contract.promoted", {
        "contract": contract.name, "contract_sha256": sha256_file(contract),
    })
    ledger.transition(job.job_id, J.DONE, "contract_gap", reason="await campaign")
    loop = SimpleNamespace(auto_approver=None)
    gate = SimpleNamespace(active_campaign_sha256=None, active_campaign_contracts={})
    controller = CampaignController(
        ledger, loop, gate, campaigns_dir=campaigns, contracts_dir=contracts,
        project_root=tmp_path, runs_dir=tmp_path / "runs", hardware_enabled=True,
        now=lambda: NOW,
    )
    return ledger, job, controller


def test_completed_mission_becomes_one_digest_bound_campaign(tmp_path: Path):
    ledger, job, controller = _environment(tmp_path)
    service = CampaignProposalService(
        ledger, controller, project_root=tmp_path,
        contracts_dir=controller.contracts_dir,
        campaigns_dir=controller.campaigns_dir, now=lambda: NOW,
    )

    proposal = service.preview(job.job_id)

    assert proposal.ready is True
    assert proposal.board["board_id"] == "example-board"
    assert [row["tool"] for row in proposal.permissions] == [
        "run_board_stage", "measure_board_scope",
    ]
    assert proposal.permissions[0]["artifact_mode"] == "run_manifest"
    assert proposal.permissions[1]["artifact_mode"] == "pinned"

    result = service.approve(job.job_id, "quantum1", proposal.review_sha256)

    assert result["campaign"]["active"] is True
    assert result["proposal"]["review_sha256"] == proposal.review_sha256
    assert sha256_file(controller.campaigns_dir / result["filename"]) == result["sha256"]


def test_wire_preview_and_approve_use_the_same_review_digest(tmp_path: Path):
    ledger, job, controller = _environment(tmp_path)
    sent: list[dict] = []
    wire = W.WireSession(
        ledger, ControlPlane(ledger, campaign_controller=controller), sent.append
    )

    wire.handle({
        "type": W.CAMPAIGN_BUNDLE_PREVIEW, "commandId": "p", "jobId": job.job_id,
        "maxIterations": 6, "maxAutoApprovals": 12, "durationHours": 10,
    })
    proposal = sent[-1]["proposal"]
    assert sent[-1]["ok"] is True
    assert proposal["max_iterations"] == 6

    wire.handle({
        "type": W.CAMPAIGN_BUNDLE_APPROVE, "commandId": "a", "jobId": job.job_id,
        "approvedBy": "quantum1", "reviewSha256": proposal["review_sha256"],
        "maxIterations": 6, "maxAutoApprovals": 12, "durationHours": 10,
    })
    assert sent[-1]["ok"] is True
    assert sent[-1]["campaign"]["active"] is True


def test_review_digest_refuses_changed_limits(tmp_path: Path):
    ledger, job, controller = _environment(tmp_path)
    control = ControlPlane(ledger, campaign_controller=controller)
    first = control.campaign_bundle_preview(job.job_id, max_iterations=4)

    answer = control.handle({
        "command": "campaign-bundle-approve", "job_id": job.job_id,
        "approved_by": "quantum1", "review_sha256": first["review_sha256"],
        "max_iterations": 5,
    })

    assert answer["ok"] is False
    assert "review changed" in answer["error"]


def test_approval_comment_is_steered_to_the_execution_child(tmp_path: Path):
    ledger, mission, controller = _environment(tmp_path)
    proposal = CampaignProposalService(
        ledger, controller, project_root=tmp_path,
        contracts_dir=controller.contracts_dir,
        campaigns_dir=controller.campaigns_dir, now=lambda: NOW,
    ).preview(mission.job_id)
    child = start(ledger, proposal.contract, "execute reviewed mission")
    continuation = SimpleNamespace(
        child_job_id=child.job_id,
        describe=lambda: {"child_job_id": child.job_id, "status": "created"},
    )
    daemon = SimpleNamespace(
        loop=SimpleNamespace(
            continue_completed=lambda _job_id: continuation,
            policy=SimpleNamespace(max_iterations=8),
        )
    )
    control = ControlPlane(
        ledger,
        daemon=daemon,
        campaign_controller=controller,
    )

    result = control.campaign_bundle_approve(
        mission.job_id,
        "quantum1",
        proposal.review_sha256,
        comment="measure RF1 before optimizing RF2",
    )

    assert result["steering"]["will_be_delivered"] is True
    steers = [event for event in ledger.events_since(0, child.job_id) if event.kind == "control.steer"]
    assert steers[-1].payload["message"] == "measure RF1 before optimizing RF2"
