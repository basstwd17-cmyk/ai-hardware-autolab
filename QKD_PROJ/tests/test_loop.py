"""Offline tests for the plan/execute/reduce/record/decide loop.

No runtime is involved: the ``AgentRunner`` is a dict of canned envelopes, which
is the point of that seam. What is asserted is the loop's own behaviour --
where it stops, what it refuses to walk past, and that its position is derived
from the ledger rather than held in memory.

The resumability tests all follow one shape: run a few steps, throw the ``Loop``
away, build a new one on the same ledger, and carry on. If any phase were
remembered rather than derived, that would break.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import jobs as J  # noqa: E402
from QKD_PROJ.orchestrator import loop as L  # noqa: E402
from QKD_PROJ.orchestrator.jobs import JobLedger  # noqa: E402
from QKD_PROJ.orchestrator.loop import Loop, LoopError, LoopPolicy, start  # noqa: E402


# -- scaffolding -------------------------------------------------------------


def make_run(runs: Path, name: str, measurements: dict) -> str:
    directory = runs / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "verdict": "PASS",
                "steps": [{"tool": "cpld", "action": "build", "measurements": measurements}],
            }
        ),
        encoding="utf-8",
    )
    return name


def plan_with(steps: list[dict], **extra) -> dict:
    return {"kind": "Plan", "gate": "g.yaml", "steps": steps, **extra}


def build_step(step_id: str = "s1", agent: str = "design-builder", expect=0) -> dict:
    return {
        "id": step_id,
        "agent": agent,
        "tool": "build_cpld",
        "contract": "g.yaml",
        "intent": "build the design",
        "advance_when": [
            {"field": "measurements.drc_errors", "expect": expect, "source": "contract"}
        ],
    }


class Runner:
    """A canned runtime. Records what it was asked to do."""

    def __init__(self, **envelopes):
        self.envelopes = envelopes
        self.calls: list[tuple[str, dict]] = []
        self.fail_for: set[str] = set()
        # What a failing agent raises. Swappable so a test can distinguish a
        # dead runtime from a throttled one.
        self.failure: BaseException | None = None

    def __call__(self, agent, task):
        self.calls.append((agent.name, task))
        if agent.name in self.fail_for:
            raise self.failure or RuntimeError("runtime unreachable")
        value = self.envelopes.get(agent.name)
        if value is None:
            raise AssertionError(f"no canned envelope for {agent.name}")
        return value(task) if callable(value) else value


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    ledger = JobLedger(tmp_path / "jobs.db")
    return ledger, runs


def default_runner(run_name: str = "20260101T000000Z_build", plan: dict | None = None) -> Runner:
    return Runner(
        **{
            "gate-architect": plan or plan_with([build_step()]),
            "design-builder": {"kind": "StepResult", "status": "done", "runs": [run_name]},
            "reporter": lambda task: {
                "kind": "Report",
                "document": "docs/x.md",
                "outcome": task["outcome"],
            },
        }
    )


# -- the happy path ----------------------------------------------------------


def test_a_gate_closes_when_its_conditions_are_met(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, default_runner(), runs_dir=runs)

    actions = [a.action for a in loop.run(job.job_id)]
    assert actions == [L.PLAN, L.EXECUTE, L.REDUCE, L.FINISHED]
    finished = ledger.job(job.job_id)
    assert finished.state == J.DONE and finished.outcome == "satisfied"


def test_the_brief_is_in_the_evidence_chain(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it", scenario="make the DAC move", resources={"u": "U6"})
    brief = ledger.handoffs(job.job_id, "Brief")[0]
    assert brief.payload["scenario"] == "make the DAC move"
    assert brief.payload["resources"] == {"u": "U6"}


def test_the_architect_receives_the_full_durable_brief(env):
    ledger, runs = env
    runner = default_runner()
    job = start(
        ledger,
        "g.yaml",
        "short title",
        scenario="keep this full instruction even when the UI title is shortened",
        resources={"reference": "runs/known-good"},
    )

    Loop(ledger, runner, runs_dir=runs).advance(job.job_id)

    task = next(task for name, task in runner.calls if name == "gate-architect")
    assert task["objective"] == "short title"
    assert task["scenario"].startswith("keep this full instruction")
    assert task["resources"] == {"reference": "runs/known-good"}


def test_the_report_is_written_before_the_job_closes(env):
    """A done job accepts no more handoffs, so the order is not cosmetic."""
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)

    kinds = [e.kind for e in ledger.events_since(0, job.job_id)]
    assert kinds.index("handoff.Report") < kinds.index("job.done")
    assert ledger.handoffs(job.job_id, "Report")[0].payload["outcome"] == "satisfied"


def test_the_loop_writes_no_event_kind_of_its_own(env):
    """Runtime vocabulary belongs to the adapter. Every event here comes from the ledger."""
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)

    allowed = {f"job.{s}" for s in J.JOB_STATES} | {
        "job.created",
        "iteration.started",
        *(f"handoff.{k}" for k in J.HANDOFF_KINDS),
        "approval.pending",
        "approval.decided",
        "approval.expired",
        "approval.withdrawn",
    }
    produced = {e.kind for e in ledger.events_since(0, job.job_id)}
    assert produced <= allowed, produced - allowed


# -- the position is derived -------------------------------------------------


def test_a_new_loop_object_continues_where_the_last_one_stopped(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")

    first = Loop(ledger, default_runner(), runs_dir=runs)
    assert first.advance(job.job_id).action == L.PLAN
    assert first.advance(job.job_id).action == L.EXECUTE

    # The process died here. Everything it knew was in the ledger.
    resumed = Loop(JobLedger(ledger.path), default_runner(), runs_dir=runs)
    assert resumed.phase(job.job_id) == L.REDUCE
    rest = [a.action for a in resumed.run(job.job_id)]
    assert rest == [L.REDUCE, L.FINISHED]


@pytest.mark.parametrize(
    "advance_to,expected",
    [(0, L.PLAN), (1, L.EXECUTE), (2, L.REDUCE), (3, L.DECIDE), (4, L.FINISHED)],
)
def test_phase_is_a_pure_function_of_the_ledger(env, advance_to, expected):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, default_runner(), runs_dir=runs)
    for _ in range(advance_to):
        loop.advance(job.job_id)
    assert Loop(ledger, default_runner(), runs_dir=runs).phase(job.job_id) == expected


def test_run_stops_at_its_budget_and_leaves_the_job_resumable(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, default_runner(), runs_dir=runs)

    assert len(loop.run(job.job_id, max_steps=2)) == 2
    assert ledger.job(job.job_id).state == J.RUNNING
    assert [a.action for a in loop.run(job.job_id)] == [L.REDUCE, L.FINISHED]


# -- hardware steps stop for a person ----------------------------------------


def hardware_plan() -> dict:
    step = build_step("s1", agent="board-operator")
    step["tool"] = "program_cpld_sram"
    return plan_with([step])


def test_a_hardware_step_files_a_request_and_stops(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    runner = default_runner(plan=hardware_plan())
    runner.envelopes["board-operator"] = {
        "kind": "StepResult",
        "status": "done",
        "runs": ["20260101T000000Z_build"],
    }
    loop = Loop(ledger, runner, runs_dir=runs)

    history = loop.run(job.job_id)
    assert history[-1].blocked and history[-1].approval_id
    assert ledger.job(job.job_id).state == J.AWAITING_APPROVAL
    assert "board-operator" not in [name for name, _ in runner.calls]

    pending = ledger.pending_approvals(job.job_id)[0]
    assert pending.step_id == "s1"
    assert pending.scope == "build the design"


def test_the_step_runs_once_a_person_grants_it(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "program it")
    runner = default_runner(plan=hardware_plan())
    runner.envelopes["board-operator"] = {
        "kind": "StepResult",
        "status": "done",
        "runs": ["20260101T000000Z_build"],
    }
    loop = Loop(ledger, runner, runs_dir=runs)
    loop.run(job.job_id)

    approval = ledger.pending_approvals(job.job_id)[0]
    ledger.decide_approval(approval.approval_id, True, "quantum1", note="at the bench")

    rest = [a.action for a in Loop(ledger, runner, runs_dir=runs).run(job.job_id)]
    assert rest[-1] == L.FINISHED
    assert ledger.job(job.job_id).outcome == "satisfied"
    assert "board-operator" in [name for name, _ in runner.calls]


def test_a_refusal_is_not_turned_into_a_retry(env):
    """Filing a fresh request after a denial would make a refusal mean nothing."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    runner = default_runner(plan=hardware_plan())
    loop = Loop(ledger, runner, runs_dir=runs)
    loop.run(job.job_id)

    approval = ledger.pending_approvals(job.job_id)[0]
    ledger.decide_approval(approval.approval_id, False, "quantum1", note="board is apart")

    final = Loop(ledger, runner, runs_dir=runs).run(job.job_id)
    assert final[-1].outcome == "needs_approval"
    assert ledger.job(job.job_id).outcome == "needs_approval"
    assert len(loop._approvals_for(job.job_id)) == 1, "no second request was filed"


def test_an_offline_step_needs_no_approval(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)
    assert ledger.pending_approvals(job.job_id) == []


def test_a_step_that_reports_needs_approval_is_recorded_then_queued(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.envelopes["design-builder"] = {
        "kind": "StepResult",
        "status": "needs_approval",
        "runs": [],
        "notes": "the contract does not allow this yet",
    }
    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)

    assert history[-1].blocked
    assert ledger.handoffs(job.job_id, "StepResult")[0].payload["status"] == "needs_approval"
    assert ledger.pending_approvals(job.job_id)[0].scope == "the contract does not allow this yet"


# -- a runtime failure blocks, it does not conclude --------------------------


def test_a_runtime_that_cannot_run_blocks_the_job(env):
    """A dead subprocess says nothing about the board, so nothing is concluded."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    runner.fail_for = {"gate-architect"}

    step = Loop(ledger, runner, runs_dir=runs).advance(job.job_id)
    assert step.blocked and step.action == L.BLOCKED
    assert "unreachable" in step.detail
    assert ledger.job(job.job_id).state == J.BLOCKED
    assert ledger.handoffs(job.job_id, "Verdict") == []


def test_a_runner_returning_something_that_is_not_an_object_blocks(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = Runner(**{"gate-architect": lambda task: "done, it passed"})
    step = Loop(ledger, runner, runs_dir=runs).advance(job.job_id)
    assert step.reason_code == L.BLOCKED_INVALID
    assert "not an object" in step.detail
    assert ledger.job(job.job_id).state == J.BLOCKED


def test_a_plan_naming_an_unknown_agent_is_refused(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(plan=plan_with([build_step("s1", agent="intern")]))
    loop = Loop(ledger, runner, runs_dir=runs)
    step = loop.advance(job.job_id)

    assert step.blocked is True
    assert step.reason_code == L.BLOCKED_INVALID
    assert "known agents" in step.detail
    assert ledger.handoffs(job.job_id, "Plan") == []


# -- binding conditions to runs ----------------------------------------------


def test_a_condition_takes_the_run_its_step_produced():
    conditions = L.conditions_for(
        plan_with([build_step()]), {"s1": {"runs": ["20260101T000000Z_build"]}}
    )
    assert [c.run for c in conditions] == ["20260101T000000Z_build"]


def test_a_step_that_never_reported_leaves_its_condition_unbound():
    """Dropping it would let a gate close on the steps that happened to finish."""
    conditions = L.conditions_for(plan_with([build_step("s1"), build_step("s2")]), {"s1": {"runs": ["r"]}})
    assert [c.run for c in conditions] == ["r", None]


def test_an_ambiguous_run_is_refused_rather_than_picked():
    with pytest.raises(LoopError, match="does not say which one"):
        L.conditions_for(plan_with([build_step()]), {"s1": {"runs": ["r1", "r2"]}})


def test_a_condition_may_name_its_own_run():
    step = build_step()
    step["advance_when"][0]["run"] = "explicit"
    assert L.conditions_for(plan_with([step]), {"s1": {"runs": ["r1", "r2"]}})[0].run == "explicit"


def test_a_plan_with_no_conditions_cannot_be_reduced():
    step = build_step()
    step["advance_when"] = []
    with pytest.raises(LoopError, match="nothing about this gate can be decided"):
        L.conditions_for(plan_with([step]), {})


# -- deciding ----------------------------------------------------------------


def test_an_open_verdict_sends_the_loop_round_again_with_the_verdict_in_hand(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner()
    loop = Loop(ledger, runner, runs_dir=runs)

    # plan, execute, reduce, decide (opens iteration 2), plan again
    loop.run(job.job_id, max_steps=5)
    assert ledger.job(job.job_id).iteration >= 2

    replans = [task for name, task in runner.calls if name == "gate-architect"]
    assert replans[1]["previous_verdict"]["outcome"] == "open"
    previous_result = replans[1]["previous_step_results"][0]
    assert previous_result["status"] == "done"
    assert previous_result["runs"] == ["20260101T000000Z_build"]
    assert previous_result["step_id"] == "s1"
    assert replans[1]["iteration"] == 2


def test_three_identical_failures_close_the_job_as_exhausted(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    history = Loop(ledger, default_runner(), runs_dir=runs).run(job.job_id)

    assert history[-1].action == L.FINISHED
    assert ledger.job(job.job_id).outcome == "exhausted"
    assert ledger.strikes(job.job_id) == 3


def test_a_ceiling_stops_a_loop_that_keeps_failing_in_new_ways(env):
    """The strike rule cannot see this: every signature differs, so it never counts."""
    ledger, runs = env
    counter = {"n": 0}

    def moving_target(task):
        counter["n"] += 1
        make_run(runs, f"20260101T00000{counter['n']}Z_build", {"drc_errors": counter["n"]})
        return plan_with([build_step()] and [
            {
                "id": "s1",
                "agent": "design-builder",
                "tool": "build_cpld",
                "contract": "g.yaml",
                "advance_when": [
                    {"field": "measurements.drc_errors", "expect": 0, "source": "contract"}
                ],
            }
        ])

    runner = Runner(
        **{
            "gate-architect": moving_target,
            "design-builder": lambda task: {
                "kind": "StepResult",
                "status": "done",
                "runs": [f"20260101T00000{counter['n']}Z_build"],
            },
            "reporter": lambda task: {
                "kind": "Report",
                "document": "docs/ceiling.md",
                "outcome": task["outcome"],
            },
        }
    )
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, runner, runs_dir=runs, policy=LoopPolicy(max_iterations=3))
    history = loop.run(job.job_id, max_steps=60)

    assert history[-1].action == L.FINISHED
    assert ledger.job(job.job_id).outcome == "exhausted"
    assert ledger.strikes(job.job_id) == 1, "each failure was different; the strike rule never fired"
    assert ledger.job(job.job_id).iteration <= 3


def test_a_missing_run_closes_the_job_as_primary_run_missing(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(run_name="20260101T000000Z_never_written")
    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)

    assert history[-1].action == L.FINISHED
    assert ledger.job(job.job_id).outcome == "primary_run_missing"


def test_an_architect_that_stops_on_a_contract_gap_ends_the_job(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = default_runner(
        plan=plan_with([build_step()], blocked_on="contract_gap", objective="no contract allows this")
    )
    history = Loop(ledger, runner, runs_dir=runs).run(job.job_id)

    assert history[-1].outcome == "contract_gap"
    assert ledger.job(job.job_id).outcome == "contract_gap"
    assert ledger.handoffs(job.job_id, "Plan"), "the refusal is kept, not discarded"


def test_a_finished_job_advances_no_further(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    loop = Loop(ledger, default_runner(), runs_dir=runs)
    loop.run(job.job_id)

    again = loop.advance(job.job_id)
    assert again.action == L.FINISHED and again.outcome == "satisfied"
    assert len(loop.run(job.job_id)) == 1


def test_reporting_can_be_turned_off(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    runner = Runner(
        **{
            "gate-architect": plan_with([build_step()]),
            "design-builder": {
                "kind": "StepResult",
                "status": "done",
                "runs": ["20260101T000000Z_build"],
            },
        }
    )
    Loop(ledger, runner, runs_dir=runs, policy=LoopPolicy(report_on_finish=False)).run(job.job_id)
    assert ledger.job(job.job_id).outcome == "satisfied"
    assert ledger.handoffs(job.job_id, "Report") == []


def test_corroboration_travels_from_the_plan_into_the_verdict(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    make_run(runs, "20260101T000001Z_other", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    plan = plan_with(
        [build_step()],
        corroboration=[{"run": "20260101T000001Z_other", "supports": "a related build passed"}],
    )
    Loop(ledger, default_runner(plan=plan), runs_dir=runs).run(job.job_id)

    verdict = ledger.handoffs(job.job_id, "Verdict")[0].payload
    assert verdict["corroboration"][0]["closes_gate"] is False
