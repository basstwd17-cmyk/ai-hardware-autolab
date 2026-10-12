from __future__ import annotations

import json
from pathlib import Path

import yaml

from ai_hardware_autolab.agents.schema import load_simple_team
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator import loop as L
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.loop import Loop, LoopPolicy
from ai_hardware_autolab.orchestrator.simple_mission import (
    SIMPLE_MISSION_WORKFLOW,
    SimpleMissionResourceRefresher,
    hardware_completion_gap,
    start_simple_mission,
)


def _run(runs: Path, name: str, value: int) -> str:
    target = runs / name
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "verdict": "PASS" if value == 0 else "FAIL",
                "steps": [
                    {
                        "tool": "cpld",
                        "action": "build",
                        "measurements": {"errors": value},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return name


class Runner:
    def __init__(self, run: str):
        self.run = run
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        if agent.name == "lead-agent":
            contract = task["resources"]["selected_contract"]["name"]
            return {
                "kind": "Plan",
                "objective": "Implement and verify the requested timing path.",
                "steps": [
                    {
                        "id": "implement-and-build",
                        "agent": "builder",
                        "tool": "build_cpld",
                        "contract": contract,
                        "touches_hardware": False,
                        "intent": "Edit the implementation and build it.",
                        "advance_when": [
                            {
                                "field": "measurements.errors",
                                "expect": 0,
                                "source": "the selected contract",
                            }
                        ],
                    }
                ],
            }
        if agent.name == "builder":
            return {"kind": "StepResult", "status": "done", "runs": [self.run]}
        raise AssertionError(agent.name)


def _loop(ledger: JobLedger, runs: Path, runner: Runner) -> Loop:
    return Loop(
        ledger,
        runner,
        runs_dir=runs,
        agents=load_simple_team(),
        policy=LoopPolicy(max_iterations=4, report_on_finish=False),
        planner_agent="lead-agent",
        verifier_agent="verifier",
    )


def test_new_mission_is_exactly_lead_builder_verifier(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _run(runs, "run-1", 0)
    job, reused = start_simple_mission(
        ledger,
        "timed output",
        "Use PRU and verify the output timing.",
        resources={"selected_contract": {"name": "g.yaml", "sha256": "a" * 64}},
    )
    runner = Runner(run)

    actions = [item.action for item in _loop(ledger, runs, runner).run(job.job_id)]

    assert reused is False
    assert actions == [L.PLAN, L.EXECUTE, L.REDUCE, L.FINISHED]
    assert [name for name, _task in runner.calls] == ["lead-agent", "builder"]
    assert ledger.handoffs(job.job_id, "Verdict")[0].agent == "verifier"
    assert ledger.job(job.job_id).outcome == "satisfied"
    resources = ledger.handoffs(job.job_id, "Brief", 0)[0].payload["resources"]
    assert resources["workflow"] == SIMPLE_MISSION_WORKFLOW
    assert resources["agent_flow"] == ["lead-agent", "builder", "verifier"]


def test_duplicate_nonterminal_scenario_reuses_one_job(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    first, reused_first = start_simple_mission(ledger, "one", "same scenario")
    second, reused_second = start_simple_mission(ledger, "two", "same scenario")

    assert reused_first is False
    assert reused_second is True
    assert second.job_id == first.job_id
    assert len(ledger.jobs()) == 1


def test_offline_builder_step_does_not_request_hardware_approval(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _run(runs, "run-1", 0)
    job, _ = start_simple_mission(
        ledger,
        "offline build",
        "Build without hardware.",
        resources={"selected_contract": {"name": "g.yaml", "sha256": "a" * 64}},
    )
    loop = _loop(ledger, runs, Runner(run))

    assert loop.advance(job.job_id).action == L.PLAN
    assert loop.advance(job.job_id).action == L.EXECUTE
    assert ledger.pending_approvals(job.job_id) == []


def test_builder_receives_whole_plan_and_prior_results(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _run(runs, "run-1", 0)
    job, _ = start_simple_mission(
        ledger,
        "build",
        "Keep context between steps.",
        resources={"selected_contract": {"name": "g.yaml", "sha256": "a" * 64}},
    )
    runner = Runner(run)
    loop = _loop(ledger, runs, runner)
    loop.advance(job.job_id)
    loop.advance(job.job_id)

    task = [task for name, task in runner.calls if name == "builder"][0]
    assert task["scenario"] == "Keep context between steps."
    assert task["plan"]["steps"][0]["agent"] == "builder"
    assert task["previous_step_results"] == []
    assert task["resources"]["source_context"]["active_team"] == "ai_hardware_autolab/agents/simple/"
    lead_task = next(task for name, task in runner.calls if name == "lead-agent")
    assert lead_task["resources"]["source_context"]["read_first"] == [
        "ai_hardware_autolab/README.md", "ai_hardware_autolab/STATUS.md"
    ]


def test_hardware_mission_replans_after_offline_bootstrap_instead_of_false_completion(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _run(runs, "bootstrap", 0)
    job, _ = start_simple_mission(
        ledger,
        "new board",
        "Prepare and then program the new board.",
        resources={
            "execution_mode": "hardware",
            "selected_contract": {"name": "g.yaml", "sha256": "a" * 64},
        },
    )
    loop = Loop(
        ledger,
        Runner(run),
        runs_dir=runs,
        agents=load_simple_team(),
        policy=LoopPolicy(max_iterations=4, report_on_finish=False),
        planner_agent="lead-agent",
        verifier_agent="verifier",
        completion_guard=hardware_completion_gap,
    )

    actions = [loop.advance(job.job_id).action for _ in range(4)]

    assert actions == [L.PLAN, L.EXECUTE, L.REDUCE, L.DECIDE]
    assert ledger.job(job.job_id).state == J.RUNNING
    assert ledger.job(job.job_id).iteration == 2


def test_resource_refresher_discovers_a_builder_published_board_profile(tmp_path):
    board_dir = tmp_path / "ai_hardware_autolab" / "boards" / "demo-board" / "r1"
    work = tmp_path / "firmware"
    board_dir.mkdir(parents=True)
    work.mkdir()
    (board_dir / "board-map.yaml").write_text("pins: {}\n", encoding="utf-8")
    profile = {
        "schema_version": 1,
        "board_id": "demo-board",
        "revision": "r1",
        "title": "Demo Board",
        "aliases": ["demo board"],
        "board_map": "ai_hardware_autolab/boards/demo-board/r1/board-map.yaml",
        "sources": [{
            "id": "manual", "title": "Vendor manual",
            "source_kind": "official-manual", "location": "https://example.com/manual.pdf",
        }],
        "stages": [{
            "id": "build-fw", "phase": "build", "mode": "offline",
            "agent": "builder", "tool": "run_board_build", "status": "ready",
            "requires_campaign": False, "command": ["python", "-V"],
            "cwd": "firmware", "artifacts": [], "parameters": {},
        }],
    }
    (board_dir / "profile.yaml").write_text(
        yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
    )

    refreshed = SimpleMissionResourceRefresher(tmp_path)(
        "Build firmware for the demo board.",
        {"execution_mode": "offline", "available_tools": ["read_contract"]},
    )

    assert refreshed["board_development"]["status"] == "profiled"
    assert refreshed["board_development"]["selected"]["board_id"] == "demo-board"
    assert "run_board_build" in refreshed["available_tools"]
