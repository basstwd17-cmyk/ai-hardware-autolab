"""Durable scheduling, cancellation state and bounded rate-limit retries."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ai_hardware_autolab.utils import utc_now
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator import execution as L
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.clarification import USER_INPUT, pending_clarification
from ai_hardware_autolab.orchestrator.pagination import last_event, tail_since
from ai_hardware_autolab.orchestrator.execution import Advance

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






# -- the scheduler -----------------------------------------------------------


class Daemon:
    """Decides which job advances, one ``Loop.advance()`` call at a time."""

    def __init__(
        self,
        ledger: JobLedger,
        loop: Any,
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
                "a model turn may have executed commands; inspect the recorded result before retrying"
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
