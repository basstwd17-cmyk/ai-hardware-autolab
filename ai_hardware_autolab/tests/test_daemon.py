"""The scheduler, and the gate in front of a hardware step.

Two things are being pinned here.

The scheduler's state is derived from the ledger and nothing else, so every
test that checks a wait builds a fresh ``Daemon`` on the same database first.
Time is taken from the ledger's own timestamps rather than a fake clock,
because the daemon compares its `now` against timestamps the ledger wrote --
a test with its own clock would be measuring against the wrong one and would
pass while the real thing drifted.

The approval gate is checked mostly by the ways it says no. A gate that
answers yes when it should not is the one failure in this codebase that
reaches a powered board, so the tests spend their effort on the refusals:
no approval, someone else's step, a denial, an expiry, an approval bound to
nothing, and -- the one that matters -- an approval whose artifacts no longer
hash to what was signed.
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.agents.schema import load_all  # noqa: E402
from ai_hardware_autolab.orchestrator import daemon as D  # noqa: E402
from ai_hardware_autolab.orchestrator import jobs as J  # noqa: E402
from ai_hardware_autolab.orchestrator import loop as L  # noqa: E402
from ai_hardware_autolab.orchestrator.jobs import JobLedger  # noqa: E402
from ai_hardware_autolab.orchestrator.loop import start  # noqa: E402
from ai_hardware_autolab.tests.test_loop import Runner, build_step, make_run, plan_with  # noqa: E402

AGENTS = {a.name: a for a in load_all()}


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    return JobLedger(tmp_path / "jobs.db"), runs


def working_runner(run_name: str = "20260101T000000Z_build", plan: dict | None = None) -> Runner:
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


def after(ledger: JobLedger, job_id: str, seconds: float) -> str:
    """A moment `seconds` after this job's most recent block.

    Taken from the ledger because that is the clock the daemon compares
    against. A test that invented its own would drift from the thing it checks.
    """
    blocked = [e for e in ledger.events_since(0, job_id) if e.kind == "job.blocked"]
    base = blocked[-1].at if blocked else ledger.job(job_id).created_at
    return (datetime.fromisoformat(base) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


# -- classifying a failure ---------------------------------------------------


def test_an_explicit_rate_limit_is_recognised():
    assert D.classify_failure(D.RateLimited("slow down")) == L.BLOCKED_RATE_LIMIT


@pytest.mark.parametrize(
    "message",
    ["HTTP 429 Too Many Requests", "rate limit reached", "usage limit exceeded", "retry-after: 30"],
)
def test_a_rate_limited_message_is_recognised(message):
    assert D.classify_failure(RuntimeError(message)) == L.BLOCKED_RATE_LIMIT


@pytest.mark.parametrize(
    "message",
    ["connection refused", "the app server exited", "no such file", "turn returned no handoff"],
)
def test_anything_else_is_a_permanent_failure(message):
    """The safe direction: a wrongly-permanent failure stops; a wrongly-temporary one loops."""
    assert D.classify_failure(RuntimeError(message)) == L.BLOCKED_RUNTIME


def test_backoff_doubles_and_is_capped():
    policy = D.SchedulerPolicy(base_backoff_s=60, max_backoff_s=300)
    assert [policy.backoff_s(n) for n in (1, 2, 3, 4, 9)] == [60, 120, 240, 300, 300]
    assert policy.backoff_s(0) == 0


def test_a_live_turn_is_active_not_runnable_or_interrupted(env):
    ledger, _runs = env
    job = start(ledger, "g.yaml", "hold one turn")
    entered = threading.Event()
    release = threading.Event()

    class BlockingLoop:
        def advance(self, job_id):
            entered.set()
            release.wait(timeout=2)
            current = ledger.job(job_id)
            return L.Advance(L.PLAN, job_id, current.iteration, "released")

    daemon = D.Daemon(ledger, BlockingLoop())
    worker = threading.Thread(target=daemon.tick)
    worker.start()
    assert entered.wait(timeout=1)
    waiting = daemon.waiting_for(job.job_id)
    assert waiting.status == D.ACTIVE
    assert not waiting.runnable

    from ai_hardware_autolab.orchestrator.control import ControlPlane

    assert ControlPlane(ledger, daemon).job(job.job_id)["interrupted"] is False
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


# -- the three ways to be stopped are three ----------------------------------


def test_a_rate_limited_job_waits_on_a_clock(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    daemon = D.build(ledger, runner, runs_dir=runs)

    first = daemon.tick()
    assert first.advanced.reason_code == L.BLOCKED_RATE_LIMIT

    waiting = D.Daemon(ledger, daemon.loop).waiting_for(job.job_id, after(ledger, job.job_id, 5))
    assert waiting.status == D.WAITING_RETRY
    assert waiting.retry_at and not waiting.runnable


def test_a_broken_runtime_waits_on_a_person(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    daemon = D.build(ledger, runner, runs_dir=runs)

    assert daemon.tick().advanced.reason_code == L.BLOCKED_RUNTIME
    waiting = daemon.waiting_for(job.job_id, after(ledger, job.job_id, 100_000))
    assert waiting.status == D.WAITING_PERSON, "no amount of waiting helps"


def test_a_hardware_step_waits_on_an_approval(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    step = build_step("s1", agent="board-operator")
    runner = working_runner(plan=plan_with([step]))
    daemon = D.build(ledger, runner, runs_dir=runs)
    daemon.run(ticks=5)

    waiting = daemon.waiting_for(job.job_id)
    assert waiting.status == D.WAITING_APPROVAL
    assert waiting.approval_id
    assert not waiting.runnable


def test_the_backoff_elapses_and_the_job_becomes_runnable_again(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    daemon = D.build(ledger, runner, runs_dir=runs, scheduler_policy=D.SchedulerPolicy(base_backoff_s=60))
    daemon.tick()

    assert daemon.waiting_for(job.job_id, after(ledger, job.job_id, 30)).status == D.WAITING_RETRY
    assert daemon.waiting_for(job.job_id, after(ledger, job.job_id, 61)).runnable


def test_a_resume_does_not_reset_the_retry_count(env):
    """The scheduler's own `job.running` is bookkeeping, not progress.

    Counting it as progress made the backoff never grow and the retry ceiling
    unreachable, so a runtime that was simply broken looked briefly throttled
    forever.
    """
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    daemon = D.build(ledger, runner, runs_dir=runs, scheduler_policy=D.SchedulerPolicy(base_backoff_s=1))

    for _ in range(3):
        daemon.tick(after(ledger, job.job_id, 10_000))

    assert D.rate_limit_attempts(ledger, job.job_id) == 3


def test_repeated_rate_limits_eventually_need_a_person(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    policy = D.SchedulerPolicy(base_backoff_s=1, max_rate_limit_retries=3)
    daemon = D.build(ledger, runner, runs_dir=runs, scheduler_policy=policy)

    for _ in range(4):
        daemon.tick(after(ledger, job.job_id, 10_000))

    waiting = daemon.waiting_for(job.job_id, after(ledger, job.job_id, 10_000))
    assert waiting.status == D.WAITING_PERSON
    assert "not briefly throttled" in waiting.detail


def test_progress_resets_the_retry_count(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    daemon = D.build(ledger, runner, runs_dir=runs, scheduler_policy=D.SchedulerPolicy(base_backoff_s=1))

    daemon.tick()
    assert D.rate_limit_attempts(ledger, job.job_id) == 1

    runner.fail_for = set()
    daemon.tick(after(ledger, job.job_id, 10))
    assert D.rate_limit_attempts(ledger, job.job_id) == 0


# -- one step per tick, all of it recoverable --------------------------------


def test_one_internal_loop_error_blocks_only_its_job_and_the_daemon_continues(env):
    ledger, runs = env
    first = start(ledger, "g.yaml", "first")
    second = start(ledger, "g.yaml", "second")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    original = daemon.loop.advance

    def fail_first(job_id: str):
        if job_id == first.job_id:
            raise L.LoopError("bad derived phase")
        return original(job_id)

    daemon.loop.advance = fail_first
    failed = daemon.tick()
    continued = daemon.tick()

    assert failed.advanced is not None
    assert failed.advanced.reason_code == "scheduler_failure"
    assert ledger.job(first.job_id).state == J.BLOCKED
    assert continued.advanced is not None
    assert continued.advanced.job_id == second.job_id
    assert ledger.job(second.job_id).state == J.RUNNING


def test_a_tick_advances_exactly_one_step(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)

    assert daemon.tick().advanced.action == L.PLAN
    assert daemon.tick().advanced.action == L.EXECUTE
    assert ledger.job(job.job_id).state == J.RUNNING


def test_a_fresh_daemon_carries_on_from_the_ledger(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    D.build(ledger, working_runner(), runs_dir=runs).run(ticks=2)

    resumed = D.build(JobLedger(ledger.path), working_runner(), runs_dir=runs)
    actions = [t.advanced.action for t in resumed.run() if t.advanced]
    assert actions[-1] == L.FINISHED
    assert ledger.job(job.job_id).outcome == "satisfied"


def test_the_scheduler_picks_one_job_and_leaves_the_others_reported(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    first = start(ledger, "g.yaml", "a")
    second = start(ledger, "g.yaml", "b")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)

    tick = daemon.tick()
    assert tick.advanced.job_id == first.job_id
    assert [w.job_id for w in tick.waiting] == [second.job_id]


def test_nothing_runnable_reports_why(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    daemon = D.build(ledger, runner, runs_dir=runs)
    daemon.tick()

    tick = daemon.tick()
    assert tick.advanced is None
    assert [w.status for w in tick.waiting] == [D.WAITING_PERSON]


def test_a_finished_job_is_not_picked_again(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    daemon.run()

    assert daemon.tick().advanced is None
    assert daemon.waiting_for(job.job_id).status == D.FINISHED


def test_recover_expires_the_queue_and_reports_an_interrupted_job(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    ledger.transition(job.job_id, J.RUNNING)
    ledger.request_approval(job.job_id, "g.yaml", "one program", expires_at="2020-01-01T00:00:00+00:00")

    notes = D.build(JobLedger(ledger.path), working_runner(), runs_dir=runs).recover()
    assert any(n.startswith("expired") for n in notes)
    assert any("was left running" in n for n in notes)
    assert ledger.pending_approvals(job.job_id) == []


def test_status_is_derived_and_carries_the_cursor(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    daemon.tick()

    status = daemon.status()
    assert status["cursor"] == ledger.latest_event_id()
    assert status["jobs"][D.RUNNABLE]
    assert isinstance(status["inbox"], list)


def test_the_daemon_writes_no_event_kind_of_its_own(env):
    ledger, runs = env
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    runner.fail_for = {"gate-architect"}
    runner.failure = D.RateLimited("429")
    daemon = D.build(ledger, runner, runs_dir=runs, scheduler_policy=D.SchedulerPolicy(base_backoff_s=1))
    daemon.tick()
    runner.fail_for = set()
    daemon.tick(after(ledger, job.job_id, 10))

    allowed = {f"job.{s}" for s in J.JOB_STATES} | {
        "job.created",
        "iteration.started",
        *(f"handoff.{k}" for k in J.HANDOFF_KINDS),
        "approval.pending",
        "approval.decided",
        "approval.authorized",
        "approval.expired",
        "approval.withdrawn",
    }
    assert {e.kind for e in ledger.events_since(0, job.job_id)} <= allowed


# -- the hardware approval gate ----------------------------------------------


@pytest.fixture()
def bench(tmp_path):
    """A ledger, a candidate file on disk, and the digest a person signed."""
    ledger = JobLedger(tmp_path / "jobs.db")
    candidate = tmp_path / "candidates" / "gate12"
    candidate.mkdir(parents=True)
    bitstream = candidate / "design.bit"
    bitstream.write_bytes(b"the reviewed bitstream")

    from ai_hardware_autolab.hwtools.result import sha256_file

    digest = sha256_file(bitstream)
    reference = "candidates/gate12/design.bit"
    gate = D.HardwareApprovalGate(ledger, contracts_dir=tmp_path / "contracts", project_root=tmp_path)
    return ledger, gate, bitstream, reference, digest


def task_for(job_id: str, step_id: str = "s1", contract: str = "g.yaml") -> dict:
    return {
        "kind": "Plan",
        "job_id": job_id,
        "iteration": 1,
        "gate": contract,
        "step": {
            "id": step_id,
            "agent": "board-operator",
            "contract": contract,
            "tool": "program_cpld_sram",
        },
    }


def grant(ledger, job_id, artifacts, step_id="s1", contract="g.yaml"):
    approval = ledger.request_approval(
        job_id, contract, "one volatile SRAM program", artifacts=artifacts, step_id=step_id
    )
    ledger.decide_approval(approval.approval_id, True, "quantum1", note="at the bench")
    return approval


def test_a_granted_approval_with_matching_digests_lets_one_call_through(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest})

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is True
    assert gate.refusals == []


def test_an_approval_execution_is_consumed_before_a_second_call(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    approval = grant(ledger, job.job_id, {reference: digest})

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {})
    assert not gate(
        AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}
    )
    assert "used all 1" in gate.refusals[-1]
    authorised = [
        event
        for event in ledger.events_since(0, job.job_id)
        if event.kind == "approval.authorized"
    ]
    assert [event.payload["approval_id"] for event in authorised] == [
        approval.approval_id
    ]


def test_an_approval_cannot_authorise_a_different_tool(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest})

    assert not gate(AGENTS["board-operator"], task_for(job.job_id), "scan_jtag", {})
    assert "plans tool 'program_cpld_sram'" in gate.refusals[-1]


def test_a_changed_artifact_is_refused(bench):
    """The whole reason the check happens at the moment of use."""
    ledger, gate, bitstream, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest})

    bitstream.write_bytes(b"a different bitstream, same path")
    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "now hashes to" in gate.refusals[-1]
    assert "not what was signed" in gate.refusals[-1]


def test_a_missing_artifact_is_refused(bench):
    ledger, gate, bitstream, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest})
    bitstream.unlink()

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "is not on disk" in gate.refusals[-1]


def test_no_approval_at_all_is_refused(bench):
    ledger, gate, _, _, _ = bench
    job = start(ledger, "g.yaml", "program it")
    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "no granted approval" in gate.refusals[-1]


def test_a_pending_request_is_not_a_yes(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    ledger.request_approval(
        job.job_id, "g.yaml", "one program", artifacts={reference: digest}, step_id="s1"
    )

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "pending" in gate.refusals[-1]


def test_a_denial_is_not_a_not_yet(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(
        job.job_id, "g.yaml", "one program", artifacts={reference: digest}, step_id="s1"
    )
    ledger.decide_approval(approval.approval_id, False, "quantum1")

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "denied" in gate.refusals[-1]


def test_an_approval_for_another_step_does_not_carry(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest}, step_id="s1")

    refused = gate(AGENTS["board-operator"], task_for(job.job_id, step_id="s2"), "program_cpld_sram", {})
    assert refused is False
    assert "step 's2'" in gate.refusals[-1]


def test_an_approval_for_another_contract_does_not_carry(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {reference: digest}, contract="g.yaml")

    task = task_for(job.job_id, contract="other.yaml")
    assert gate(AGENTS["board-operator"], task, "program_cpld_sram", {}) is False


def test_a_withdrawn_grant_is_refused(bench):
    ledger, gate, _, reference, digest = bench
    job = start(ledger, "g.yaml", "program it")
    approval = grant(ledger, job.job_id, {reference: digest})
    ledger.withdraw_approval(approval.approval_id, "quantum1", "board moved")

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False


def test_an_approval_bound_to_nothing_is_refused(bench):
    """A signature on a description cannot tell one bitstream from another."""
    ledger, gate, _, _, _ = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {})

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "names no artifact digests" in gate.refusals[-1]


def test_the_contract_candidate_is_used_when_the_approval_names_nothing(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    candidate = tmp_path / "candidates" / "gate12"
    candidate.mkdir(parents=True)
    bitstream = candidate / "design.bit"
    bitstream.write_bytes(b"pinned by the contract")

    from ai_hardware_autolab.hwtools.result import sha256_file

    digest = sha256_file(bitstream)
    (contracts / "g.yaml").write_text(
        "sram_program_candidate:\n"
        "  directory: candidates/gate12\n"
        "  bitstream: design.bit\n"
        f"  bitstream_sha256: {digest}\n",
        encoding="utf-8",
    )
    gate = D.HardwareApprovalGate(ledger, contracts_dir=contracts, project_root=tmp_path)
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {})

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is True

    bitstream.write_bytes(b"rebuilt after the review")
    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False


def test_a_path_outside_the_tree_is_refused(bench):
    ledger, gate, _, _, digest = bench
    job = start(ledger, "g.yaml", "program it")
    grant(ledger, job.job_id, {"../../etc/passwd": digest})

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "cannot be located" in gate.refusals[-1]


def test_a_task_with_no_job_is_refused(bench):
    ledger, gate, _, _, _ = bench
    assert gate(AGENTS["board-operator"], {"kind": "Plan"}, "program_cpld_sram", {}) is False
    assert "names no job" in gate.refusals[-1]


def test_a_gate_that_errors_refuses(bench):
    """A check that cannot complete has not passed."""
    ledger, gate, _, _, _ = bench
    job = start(ledger, "g.yaml", "program it")
    gate.ledger = None  # simulate the ledger going away mid-check

    assert gate(AGENTS["board-operator"], task_for(job.job_id), "program_cpld_sram", {}) is False
    assert "check itself failed" in gate.refusals[-1]
