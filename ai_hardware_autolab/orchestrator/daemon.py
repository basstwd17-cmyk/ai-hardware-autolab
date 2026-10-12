"""The scheduler: which job moves next, and what a hardware step is allowed to do.

``Loop.advance()`` does one step. Something has to decide *which* job to advance
and *when* -- and to do that after a crash, a reboot, or a week off, with
nothing carried in memory. That is this module.

Everything it needs is derived from the ledger:

* which jobs can move now (``jobs``, and their states),
* which are waiting on a person (``pending_approvals``),
* which are waiting on a clock, and until when (the trailing run of
  ``job.blocked`` events whose reason begins ``rate_limited:``).

The retry schedule in particular is **not stored**. A daemon that wrote "retry
at 06:31" into a column would need that column to survive the same crash the
rest of this design refuses to trust memory for, and it would be a second thing
that can disagree with the log. Counting the consecutive rate-limit blocks in
the log and multiplying gives the same answer from evidence that already exists.

## Three ways to be stopped, and why they are not one way

``needs_approval``  a person has to act. No clock helps.
``rate_limited``    the runtime will come back. A clock is the only thing that helps.
``runtime_failure`` the runtime is broken, or the answer was unusable. A person
                    has to look; retrying just repeats it.

Getting these wrong is asymmetric, which decides how the classification is
written. Calling a rate limit a runtime failure blocks a job until someone
notices -- annoying, safe. Calling a runtime failure a rate limit retries
forever, quietly, which is why the heuristic below is bounded and why an
explicit exception type always wins over pattern-matching a message.

## What this module will not do

It does not grant approvals. ``HardwareApprovalGate`` is a *check*: it reads
what a person decided directly, or what a deterministic policy decided inside
a campaign digest that person activated. It re-hashes the reviewed artifacts
and consumes one execution before returning yes. If the approval is missing,
for another step or campaign, exhausted, expired, or drifted, the answer is no.

It also writes no event kind of its own. Every event comes from a ``JobLedger``
method, exactly as in the loop.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ai_hardware_autolab.agents.schema import AgentDefinition
from ai_hardware_autolab.hwtools.result import sha256_file, utc_now
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator import loop as L
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.clarification import USER_INPUT, pending_clarification
from ai_hardware_autolab.orchestrator.pagination import last_event, tail_since
from ai_hardware_autolab.orchestrator.loop import Advance, Loop

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = PLATFORM_ROOT / "contracts"
PROJECT_ROOT = PLATFORM_ROOT.parent

# Why a job is not being advanced right now.
WAITING_APPROVAL = "waiting_approval"
WAITING_RETRY = "waiting_retry"
WAITING_PERSON = "waiting_person"
RUNNABLE = "runnable"
ACTIVE = "active"
FINISHED = "finished"

# Phrases a rate-limited runtime tends to produce. This is a fallback for
# runners that raise a plain error, and it is a heuristic -- which is why it can
# only ever cause a *wait*, never a conclusion, and why the wait is bounded.
_RATE_LIMIT_HINTS = re.compile(
    r"rate[ _-]?limit|too many requests|\b429\b|quota exceeded|usage limit|retry[- ]after",
    re.IGNORECASE,
)


class RateLimited(RuntimeError):
    """Raised by a runner that knows it was throttled.

    Preferred over the message heuristic: a runner that can tell the difference
    should say so rather than leave a scheduler to guess from prose that
    changes between vendor releases.
    """

    def __init__(self, message: str = "", retry_after_s: float | None = None) -> None:
        super().__init__(message or "the runtime is rate limited")
        self.retry_after_s = retry_after_s


def classify_failure(exc: BaseException) -> str:
    """Name a runtime failure so the scheduler knows whether waiting helps."""
    if isinstance(exc, RateLimited):
        return L.BLOCKED_RATE_LIMIT
    if _RATE_LIMIT_HINTS.search(str(exc)):
        return L.BLOCKED_RATE_LIMIT
    return L.BLOCKED_RUNTIME


@dataclass(frozen=True)
class SchedulerPolicy:
    base_backoff_s: float = 60.0
    max_backoff_s: float = 3600.0
    # After this many consecutive rate-limit blocks, stop treating it as one.
    # A runtime that has been "briefly throttled" six times in a row is not
    # throttled, and the heuristic above is exactly the thing that would keep
    # a broken runtime looking temporary forever.
    max_rate_limit_retries: int = 6

    def backoff_s(self, attempt: int) -> float:
        """Seconds to wait after the `attempt`-th consecutive rate limit."""
        if attempt < 1:
            return 0.0
        return min(self.max_backoff_s, self.base_backoff_s * (2 ** (attempt - 1)))


@dataclass(frozen=True)
class Waiting:
    """Why one job is not moving, in a form a caller can act on."""

    job_id: str
    state: str
    status: str
    detail: str
    retry_at: str | None = None
    approval_id: str | None = None
    attempt: int = 0

    @property
    def runnable(self) -> bool:
        return self.status == RUNNABLE

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Tick:
    """One pass of the scheduler."""

    advanced: Advance | None
    waiting: list[Waiting] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "advanced": self.advanced.describe() if self.advanced else None,
            "waiting": [w.describe() for w in self.waiting],
            "recovered": list(self.recovered),
        }


# -- reading the retry schedule out of the log -------------------------------


# Events that mean the job actually got somewhere. Anything else between two
# blocks -- notably the scheduler's own `job.running` resume -- is bookkeeping,
# not progress, and must not reset a retry count.
_PROGRESS_EVENTS = ("handoff.", "iteration.started", "approval.decided", "job.done")


def _made_progress(kind: str) -> bool:
    return any(kind.startswith(prefix) for prefix in _PROGRESS_EVENTS)


def _blocked_reason(ledger: JobLedger, job_id: str) -> tuple[str, str] | None:
    """(reason, timestamp) of the most recent `job.blocked`.

    Callers use this only for a job whose state *is* blocked, so the last such
    event is the current one by construction.
    """
    found = last_event(ledger, job_id, lambda e: e.kind == "job.blocked")
    if found is None:
        return None
    return str(found.payload.get("reason", "")), found.at


def rate_limit_attempts(ledger: JobLedger, job_id: str) -> int:
    """How many rate limits in a row this job has hit without getting anywhere.

    Derived, not counted into a column: the log already records every block and
    its reason, and a stored counter would be one more thing that can disagree
    with it after a crash.

    The subtlety is what resets it. The first version broke on any `job.*`
    event, which meant the scheduler's own resume transition -- written between
    every two retries -- reset the count every time. Backoff never grew past
    the first step and the retry ceiling could never be reached, so a runtime
    that was simply broken would have looked briefly throttled forever. Only
    *progress* resets it: a handoff recorded, an iteration opened, an approval
    answered.
    """
    attempts = 0
    for event in reversed(tail_since(ledger, job_id, lambda e: _made_progress(e.kind))):
        if event.kind == "job.blocked":
            if str(event.payload.get("reason", "")).startswith(L.BLOCKED_RATE_LIMIT):
                attempts += 1
                continue
            break
    return attempts


def _plus_seconds(timestamp: str, seconds: float) -> str:
    from datetime import datetime, timedelta

    moment = datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="seconds")


# -- the hardware approval gate ----------------------------------------------


class HardwareApprovalGate:
    """The ``hardware_approval`` callback, answering from the ledger and the disk.

    ``CodexAgentRunner`` calls this the moment a turn asks for a board-touching
    MCP tool. Returning ``True`` lets that one tool call through; anything else
    makes the runner raise, which blocks the job and puts a person back in the
    path.

    Two things must both hold, and the second is the one that matters:

    1. **A durable authority granted it**, for this job, contract and step. It
       is either a named person or the one active, digest-bound campaign. A
       pending request is not a yes and a denial is not a "not yet".
    2. **The artifacts still hash to what they signed.** A scope in words --
       "one SRAM program of the pinned candidate" -- cannot distinguish the
       bitstream that was reviewed from one built afterwards at the same path.
       A digest can, and re-computing it at the moment of use is the only point
       where that check is not already stale.

    Where the approval names no artifacts, the contract's pinned candidate is
    used instead. Where neither names any, the answer is no: an approval bound
    to nothing is a signature on a description, and this callback exists
    precisely because a description is not enough.
    """

    def __init__(
        self,
        ledger: JobLedger,
        *,
        contracts_dir: Path | None = None,
        project_root: Path | None = None,
        on_refusal: Callable[[str], None] | None = None,
        active_campaign_sha256: str | None = None,
        active_campaign_contracts: dict[str, str] | None = None,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.ledger = ledger
        self.contracts_dir = Path(contracts_dir or CONTRACTS_DIR)
        self.project_root = Path(project_root or PROJECT_ROOT)
        self.on_refusal = on_refusal
        self.active_campaign_sha256 = active_campaign_sha256
        self.active_campaign_expires_at: str | None = None
        self.active_campaign_contracts = dict(active_campaign_contracts or {})
        self.now = now
        self.refusals: list[str] = []
        self.scenario_authorizer = None

    def __call__(
        self,
        agent: AgentDefinition,
        task: dict[str, Any],
        tool: str,
        params: dict[str, Any],
    ) -> bool:
        try:
            return self._decide(agent, task, tool, params)
        except Exception as exc:  # noqa: BLE001 - a gate that errors must refuse
            self._refuse(f"{tool}: the approval check itself failed: {exc}")
            return False

    def _refuse(self, message: str) -> bool:
        self.refusals.append(message)
        if self.on_refusal:
            self.on_refusal(message)
        return False

    def _decide(self, agent: AgentDefinition, task: dict[str, Any], tool: str,
                params: dict[str, Any] | None = None) -> bool:
        job_id = str(task.get("job_id") or "")
        if not job_id:
            return self._refuse(f"{tool}: the task names no job, so no approval can be found")

        step = task.get("step") if isinstance(task.get("step"), dict) else {}
        step_id = str(step.get("id", ""))
        contract = str(
            step.get("contract")
            or task.get("contract")
            or task.get("mission_id")
            or task.get("gate")
            or ""
        )
        planned_tool = str(step.get("tool") or "")
        if planned_tool != tool:
            return self._refuse(
                f"{tool}: the approved step plans tool {planned_tool!r}, not {tool!r}"
            )

        if tool == "program_cpld_sram":
            planned_xcf = (step.get("tool_args") or {}).get("program_xcf")
            if planned_xcf is not None and (params or {}).get("program_xcf") != planned_xcf:
                return self._refuse(f"{tool}: actual program_xcf differs from the prepared and approved input")

        granted = [
            a
            for a in self._approvals(job_id)
            if a.state == J.APPROVAL_GRANTED and a.contract == contract and a.step_id == step_id
        ]
        if not granted:
            outstanding = [
                f"{a.approval_id}={a.state}"
                for a in self._approvals(job_id)
                if a.contract == contract and a.step_id == step_id
            ]
            return self._refuse(
                f"{tool}: no granted approval for step {step_id!r} of {contract!r}"
                + (f" (found {', '.join(outstanding)})" if outstanding else "")
            )

        approval = granted[-1]
        if str(approval.decided_by or "").startswith("scenario:"):
            if self.scenario_authorizer is None or not self.scenario_authorizer.validate_grant(
                approval, self.ledger.job(job_id), step, agent
            ):
                return self._refuse(f"{tool}: submitted scenario no longer authorizes this device operation")
        if str(approval.decided_by or "").startswith("policy:") and (
            not self.active_campaign_sha256
            or approval.bundle_sha256 != self.active_campaign_sha256
        ):
            return self._refuse(
                f"{tool}: automatic approval {approval.approval_id} belongs to campaign "
                f"{approval.bundle_sha256 or '(none)'}, which is not active in this daemon"
            )
        if str(approval.decided_by or "").startswith("policy:"):
            if self.active_campaign_expires_at and J._moment(self.now()) >= J._moment(self.active_campaign_expires_at):
                return self._refuse(f"{tool}: the active campaign has expired")
            expected_contract = self.active_campaign_contracts.get(contract)
            contract_path = self.contracts_dir / contract
            if expected_contract is None:
                return self._refuse(
                    f"{tool}: active campaign does not pin contract {contract!r}"
                )
            if Path(contract).name != contract or not contract_path.is_file():
                return self._refuse(
                    f"{tool}: campaign contract {contract!r} cannot be located"
                )
            actual_contract = sha256_file(contract_path)
            if actual_contract != expected_contract:
                return self._refuse(
                    f"{tool}: campaign contract {contract} now hashes to {actual_contract} "
                    f"instead of {expected_contract}"
                )
        artifacts = dict(approval.artifacts) or self._contract_artifacts(contract)
        if not artifacts:
            return self._refuse(
                f"{tool}: approval {approval.approval_id} names no artifact digests and "
                f"{contract} pins none. An approval bound to nothing is a signature on a "
                "description, and a description cannot tell one bitstream from another."
            )

        for reference, expected in artifacts.items():
            problem = self._artifact_problem(reference, str(expected))
            if problem:
                return self._refuse(
                    f"{tool}: approval {approval.approval_id} covers {reference}, which "
                    f"{problem}. What is on disk now is not what was signed."
                )
        try:
            self.ledger.authorize_approval(
                approval.approval_id, tool, agent.name, now=self.now()
            )
        except J.JobLedgerError as exc:
            return self._refuse(f"{tool}: {exc}")
        return True

    def _artifact_problem(self, reference: str, expected: str) -> str | None:
        if len(expected) != 64:
            return "is pinned to something that is not a sha256"
        path = self._resolve(reference)
        if path is None:
            return "cannot be located on disk"
        if not path.is_file():
            return "is not on disk"
        actual = sha256_file(path)
        if actual != expected:
            return f"now hashes to {actual[:12]}… instead of {expected[:12]}…"
        return None

    def _resolve(self, reference: str) -> Path | None:
        """Find the file an approval refers to, without accepting a path out of the tree."""
        candidate = (self.project_root / reference).resolve()
        try:
            candidate.relative_to(self.project_root.resolve())
        except ValueError:
            return None
        return candidate

    def _contract_artifacts(self, contract: str) -> dict[str, str]:
        """The digests a contract pins for its approved candidate.

        A fallback for approvals filed without explicit artifacts. It reads the
        same candidate block the audit checks, so the thing being verified here
        is the thing a reviewer looked at.
        """
        import yaml

        path = self.contracts_dir / contract
        if not contract.endswith(".yaml") or not path.is_file():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}
        found: dict[str, str] = {}
        for node in _walk_mappings(data):
            directory = node.get("directory")
            bitstream = node.get("bitstream")
            digest = node.get("bitstream_sha256")
            if isinstance(directory, str) and isinstance(bitstream, str) and isinstance(digest, str):
                found[f"{directory}/{bitstream}"] = digest
        return found

    def _approvals(self, job_id: str) -> list[J.Approval]:
        from ai_hardware_autolab.orchestrator.control import job_approvals

        return job_approvals(self.ledger, job_id)


def _walk_mappings(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_mappings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_mappings(value)


# -- the scheduler -----------------------------------------------------------


class Daemon:
    """Decides which job advances, one ``Loop.advance()`` call at a time."""

    def __init__(
        self,
        ledger: JobLedger,
        loop: Loop,
        *,
        policy: SchedulerPolicy | None = None,
        now: Callable[[], str] = utc_now,
    ) -> None:
        self.ledger = ledger
        self.loop = loop
        self.policy = policy or SchedulerPolicy()
        self.now = now
        self._activity_lock = threading.Lock()
        self._active_job_id: str | None = None

    def active_job_id(self) -> str | None:
        """The job whose one scheduler step is executing in this process."""
        with self._activity_lock:
            return self._active_job_id

    def is_active(self, job_id: str) -> bool:
        return self.active_job_id() == job_id

    def _set_active(self, job_id: str | None) -> None:
        with self._activity_lock:
            self._active_job_id = job_id

    # -- coming back after being away ---------------------------------------

    def recover(self) -> list[str]:
        """Settle what a stopped daemon left behind. Reports; does not repair.

        Overdue approval requests are marked expired, because a queue that
        silently became grantable again while nothing was running is exactly
        what an expiry is for. Jobs left ``running`` are reported and left
        alone: whether their step reached the board is unknown from here, and
        nothing may assume it did not.
        """
        notes = [f"expired {a.approval_id}" for a in self.ledger.expire_overdue(self.now())]
        recover_workflows = getattr(self.loop, "recover", None)
        if callable(recover_workflows):
            notes.extend(recover_workflows())
        for job in self.ledger.interrupted_jobs():
            notes.append(
                f"{job.job_id} was left running at iteration {job.iteration}; "
                "a step may have reached the board"
            )
        return notes

    # -- who can move -------------------------------------------------------

    def waiting_for(self, job_id: str, now: str | None = None) -> Waiting:
        """Why this job is or is not runnable, entirely from the ledger."""
        moment = now or self.now()
        job = self.ledger.job(job_id)

        if job.state == J.DONE:
            return Waiting(job_id, job.state, FINISHED, f"finished as {job.outcome}")

        supports = getattr(self.loop, "supports", None)
        if callable(supports) and not supports(job_id):
            return Waiting(job_id, job.state, WAITING_PERSON,
                           "historical workflow is retired; submit a new scenario to continue development")

        if self.is_active(job_id):
            return Waiting(job_id, job.state, ACTIVE, "agent turn in progress")

        if job.state == J.AWAITING_APPROVAL:
            pending = self.ledger.pending_approvals(job_id, moment)
            if pending:
                return Waiting(
                    job_id,
                    job.state,
                    WAITING_APPROVAL,
                    f"waiting on {len(pending)} approval(s)",
                    approval_id=pending[0].approval_id,
                )
            # Answered, one way or the other. The loop resumes and finds out
            # which; a denial ends the job there rather than here.
            return Waiting(job_id, job.state, RUNNABLE, "approvals answered")

        if job.state == J.BLOCKED:
            return self._blocked_waiting(job, moment)

        return Waiting(job_id, job.state, RUNNABLE, "ready")

    def _blocked_waiting(self, job: J.Job, moment: str) -> Waiting:
        question = pending_clarification(self.ledger, job.job_id)
        if question is not None:
            if question.answers:
                return Waiting(job.job_id, job.state, RUNNABLE, "user input received; re-plan")
            return Waiting(job.job_id, job.state, WAITING_PERSON, f"{USER_INPUT}: {question.question}")
        current = _blocked_reason(self.ledger, job.job_id)
        reason, at = current if current else ("", job.updated_at)

        if not reason.startswith(L.BLOCKED_RATE_LIMIT):
            return Waiting(
                job.job_id,
                job.state,
                WAITING_PERSON,
                reason or "blocked with no recorded reason",
            )

        attempt = rate_limit_attempts(self.ledger, job.job_id)
        if attempt >= self.policy.max_rate_limit_retries:
            return Waiting(
                job.job_id,
                job.state,
                WAITING_PERSON,
                f"rate limited {attempt} times in a row. A runtime that has been briefly "
                "throttled this often is not briefly throttled; a person should look.",
                attempt=attempt,
            )

        retry_at = _plus_seconds(at, self.policy.backoff_s(attempt))
        if retry_at <= moment:
            return Waiting(
                job.job_id, job.state, RUNNABLE, "the backoff has elapsed", attempt=attempt
            )
        return Waiting(
            job.job_id,
            job.state,
            WAITING_RETRY,
            f"rate limited; attempt {attempt}, waiting until {retry_at}",
            retry_at=retry_at,
            attempt=attempt,
        )

    def survey(self, now: str | None = None) -> list[Waiting]:
        moment = now or self.now()
        return [self.waiting_for(job.job_id, moment) for job in self.ledger.jobs()]

    def runnable(self, now: str | None = None) -> list[str]:
        return [w.job_id for w in self.survey(now) if w.runnable]

    # -- moving one job ------------------------------------------------------

    def tick(self, now: str | None = None) -> Tick:
        """Advance exactly one job by exactly one step, or explain why none moved.

        One step per tick on purpose. A daemon that drained a job to completion
        inside one tick would hold that job's progress in a stack frame, and
        every argument for deriving the phase from the ledger applies to the
        schedule as well.
        """
        moment = now or self.now()
        survey = self.survey(moment)
        ready = [w for w in survey if w.runnable]
        if not ready:
            return Tick(None, waiting=[w for w in survey if w.status != FINISHED])

        chosen = ready[0].job_id
        self._set_active(chosen)
        try:
            # A blocked-but-ready job has to be released before the loop will look
            # at it: `blocked` is a state a person or a clock leaves, not one the
            # loop walks out of on its own.
            if self.ledger.job(chosen).state == J.BLOCKED:
                self.ledger.transition(
                    chosen, J.RUNNING, reason=f"scheduler resumed: {ready[0].detail}"
                )
            try:
                advanced = self.loop.advance(chosen)
            except Exception as exc:  # noqa: BLE001 - one bad job must not kill the scheduler
                reason = f"scheduler_failure: {type(exc).__name__}: {exc}"
                job = self.ledger.job(chosen)
                if job.state == J.DONE:
                    advanced = Advance(
                        L.FINISHED,
                        chosen,
                        job.iteration,
                        f"job finished while its step was returning: {job.outcome}",
                        outcome=job.outcome,
                    )
                else:
                    if job.state != J.BLOCKED:
                        self.ledger.transition(chosen, J.BLOCKED, reason=reason)
                    advanced = Advance(
                        L.BLOCKED,
                        chosen,
                        job.iteration,
                        reason,
                        blocked=True,
                        reason_code="scheduler_failure",
                    )
        finally:
            self._set_active(None)
        return Tick(advanced, waiting=[w for w in survey if w.job_id != chosen])

    def run(self, ticks: int = 100, now: Callable[[], str] | None = None) -> list[Tick]:
        """Tick until nothing is runnable or the budget is spent.

        The budget bounds this call, not the work: whatever is left is in the
        ledger, and the next call continues from there.
        """
        clock = now or self.now
        history: list[Tick] = []
        for _ in range(ticks):
            tick = self.tick(clock())
            history.append(tick)
            if tick.advanced is None:
                break
        return history

    # -- what a caller shows -------------------------------------------------

    def status(self, now: str | None = None) -> dict[str, Any]:
        moment = now or self.now()
        survey = self.survey(moment)
        return {
            "at": moment,
            "cursor": self.ledger.latest_event_id(),
            "jobs": {
                status: [w.describe() for w in survey if w.status == status]
                for status in (
                    ACTIVE,
                    RUNNABLE,
                    WAITING_APPROVAL,
                    WAITING_RETRY,
                    WAITING_PERSON,
                    FINISHED,
                )
            },
            "inbox": [a.describe() for a in self.ledger.pending_approvals(now=moment)],
        }


def build(
    ledger: JobLedger,
    runner: Any,
    *,
    runs_dir: Path | None = None,
    loop_policy: L.LoopPolicy | None = None,
    scheduler_policy: SchedulerPolicy | None = None,
    now: Callable[[], str] = utc_now,
) -> Daemon:
    """A daemon whose loop knows how to name a rate limit."""
    loop = Loop(
        ledger,
        runner,
        runs_dir=runs_dir,
        policy=loop_policy,
        classify_failure=classify_failure,
    )
    return Daemon(ledger, loop, policy=scheduler_policy, now=now)
