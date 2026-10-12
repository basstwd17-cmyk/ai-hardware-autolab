"""The control plane opens discovery only for a verified contract gap."""

from __future__ import annotations

import argparse
import os
import time

import pytest

from ai_hardware_autolab.orchestrator import __main__ as CLI
from ai_hardware_autolab.orchestrator import service as S
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.scenario_preflight import ScenarioPreflight


def engine(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "known-gate.yaml").write_text(
        """test_id: known-gate
status: reviewed
purpose: [existing spi build]
allowed_operations: [diamond_build]
""",
        encoding="utf-8",
    )
    return ScenarioPreflight(contracts)


def ambiguous_engine(tmp_path):
    contracts = tmp_path / "ambiguous-contracts"
    contracts.mkdir()
    for name in ("bbb-dma-buffer-a.yaml", "bbb-dma-buffer-b.yaml"):
        (contracts / name).write_text(
            f"""test_id: {name.removesuffix('.yaml')}
status: reviewed
purpose: [BBB DMA random 2bit memory buffer]
allowed_operations: [diamond_build]
""",
            encoding="utf-8",
        )
    return ScenarioPreflight(contracts)


def test_discover_records_preflight_and_scenario_digest_in_the_brief(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine(tmp_path))
    scenario = "Without hardware, build and verify an STM32G474 HRTIM firmware simulation."
    preview = control.preflight(scenario)
    assert preview["status"] == "contract_gap"

    answer = control.handle({
        "command": "discover",
        "objective": "design the new protocol",
        "scenario": scenario,
        "scenario_sha256": preview["scenario_sha256"],
    })

    assert answer["ok"] is True
    job_id = answer["job"]["job_id"]
    brief = ledger.handoffs(job_id, "Brief", 0)[0].payload
    assert brief["resources"]["workflow"] == "discovery"
    assert brief["resources"]["scenario_sha256"]
    assert brief["resources"]["preflight"]["status"] == "contract_gap"


def test_discover_rejects_a_changed_scenario_after_preflight(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine(tmp_path))
    scenario = "Without hardware, build and verify an STM32G474 HRTIM firmware simulation."
    preview = control.preflight(scenario)

    answer = control.handle({
        "command": "discover",
        "objective": "design the new protocol",
        "scenario": scenario + " changed",
        "scenario_sha256": preview["scenario_sha256"],
    })

    assert answer["ok"] is False
    assert "scenario changed" in answer["error"]
    assert ledger.jobs() == []


def test_discover_refuses_a_scenario_that_already_names_a_reviewed_contract(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine(tmp_path))
    scenario = "Offline build known-gate.yaml"
    preview = control.preflight(scenario)
    assert preview["status"] == "ready"

    answer = control.handle({
        "command": "discover",
        "objective": "duplicate a known job",
        "scenario": scenario,
        "scenario_sha256": preview["scenario_sha256"],
    })

    assert answer["ok"] is False
    assert "already has a reviewed contract" in answer["error"]
    assert ledger.jobs() == []


def test_mission_accepts_a_reviewed_contract_as_a_baseline(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine(tmp_path))
    scenario = "Offline improve and verify known-gate.yaml"
    preview = control.preflight(scenario)
    assert preview["status"] == "ready"

    answer = control.legacy_mission(
        "improve the known implementation",
        scenario,
        preview["scenario_sha256"],
        "refresh",
    )

    brief = ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    assert brief["resources"]["workflow"] == "simple-mission"
    assert brief["resources"]["selected_contract"]["name"] == "known-gate.yaml"
    assert brief["resources"]["search_policy"] == "refresh"
    assert brief["resources"]["available_execution_agents"] == ["builder", "verifier"]
    assert brief["resources"]["agent_flow"] == ["lead-agent", "builder", "verifier"]


def test_mission_accepts_a_contract_gap_and_refuses_an_unknown_search_policy(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=engine(tmp_path))
    scenario = "Without hardware, design a new STM32 timing interface."
    preview = control.preflight(scenario)
    assert preview["status"] == "contract_gap"

    answer = control.legacy_mission("new interface", scenario, preview["scenario_sha256"], "always")
    brief = ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    assert brief["resources"]["selected_contract"] is None
    assert brief["resources"]["search_policy"] == "always"

    refused = control.handle({
        "command": "mission",
        "objective": "bad policy",
        "scenario": scenario,
        "scenario_sha256": preview["scenario_sha256"],
        "search_policy": "skip-everything",
    })
    assert refused["ok"] is False
    assert "search_policy" in refused["error"]


def test_mission_derives_one_offline_contract_from_ambiguous_parents(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    control = ControlPlane(ledger, preflight_engine=ambiguous_engine(tmp_path))
    scenario = "Without hardware, implement a BBB DMA random 2bit memory buffer."
    preview = control.preflight(scenario)
    assert preview["status"] == "contract_gap"
    assert preview["reason_code"] == "ambiguous_contract"

    answer = control.legacy_mission(
        "implement the scenario-specific buffer",
        scenario,
        preview["scenario_sha256"],
        "refresh",
    )

    brief = ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    resolution = brief["resources"]["contract_resolution"]
    assert resolution["mode"] == "derive"
    assert resolution["reason"] == "ambiguous_contract"
    assert resolution["requires_human_approval"] is False
    assert [item["name"] for item in resolution["parents"]] == [
        "bbb-dma-buffer-a.yaml",
        "bbb-dma-buffer-b.yaml",
    ]


@pytest.mark.skipif(os.name != "nt", reason="the production local transport is a Windows pipe")
def test_discover_crosses_the_actual_named_pipe(tmp_path):
    preflight = engine(tmp_path)
    name = rf"\\.\pipe\qkd-discovery-{time.time_ns()}"
    args = argparse.Namespace(
        ledger=str(tmp_path / "jobs.db"),
        runs=str(tmp_path / "runs"),
        contracts=str(preflight.contracts_dir),
        profile="offline",
        campaign="",
        campaign_sha256="",
        dry_run=True,
        max_iterations=4,
        backoff=1.0,
        retries=2,
        interval=0.1,
        pipe=name,
        knowledge_db=str(tmp_path / "knowledge.db"),
        knowledge_project="qkd-proj",
        knowledge_hardware="rtase-v1",
        knowledge_revision="v1.0",
    )
    service, raw = CLI.build_service(args)
    service.config.install_signal_handlers = False
    scenario = "Without hardware, build and verify an STM32G474 HRTIM firmware simulation."

    with service:
        preview = S.send({"command": "preflight", "scenario": scenario}, name)["preflight"]
        answer = S.send({
            "command": "discover",
            "objective": "design the new protocol",
            "scenario": scenario,
            "scenario_sha256": preview["scenario_sha256"],
        }, name)

    assert raw is None
    assert answer["ok"] is True
    assert service.ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload[
        "resources"
    ]["workflow"] == "discovery"
    assert service.transport_error is None


@pytest.mark.skipif(os.name != "nt", reason="the production local transport is a Windows pipe")
def test_mission_crosses_the_locked_actual_named_pipe(tmp_path, monkeypatch):
    preflight = ambiguous_engine(tmp_path)
    name = rf"\\.\pipe\qkd-mission-{time.time_ns()}"
    args = argparse.Namespace(
        ledger=str(tmp_path / "jobs.db"),
        runs=str(tmp_path / "runs"),
        contracts=str(preflight.contracts_dir),
        profile="offline",
        campaign="",
        campaign_sha256="",
        dry_run=True,
        max_iterations=4,
        backoff=1.0,
        retries=2,
        interval=0.1,
        pipe=name,
        knowledge_db=str(tmp_path / "knowledge.db"),
        knowledge_project="qkd-proj",
        knowledge_hardware="rtase-v1",
        knowledge_revision="v1.0",
    )
    from ai_hardware_autolab.orchestrator.workspace import WorkspaceManager
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    args.workspace_root = str(tmp_path / "workspaces")
    monkeypatch.setattr(WorkspaceManager, "prepare", lambda *_: workspace)
    service, _ = CLI.build_service(args)
    service.config.install_signal_handlers = False
    scenario = "Without hardware, implement a BBB DMA random 2bit memory buffer."

    with service:
        preview = S.send({"command": "preflight", "scenario": scenario}, name)["preflight"]
        answer = S.send({
            "type": "job.mission",
            "commandId": "mission-over-real-pipe",
            "objective": "derive and implement one bounded design",
            "scenario": scenario,
            "scenarioSha256": preview["scenario_sha256"],
            "searchPolicy": "refresh",
        }, name)

    assert answer["ok"] is True
    assert answer["type"] == "command.result"
    assert answer["commandId"] == "mission-over-real-pipe"
    brief = service.ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    assert brief["workflow"] == "conversation-prototype-v1"
    assert brief["inputs"]["intake_version"] == 1
    assert service.transport_error is None
