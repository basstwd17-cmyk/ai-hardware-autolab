"""Who is in the office right now, derived and nothing else.

The graph holds every agent that ever appeared; the office must hold the ones
working *now*. Those are different questions and conflating them leaves a
finished job's agents standing at their desks forever, which is worse than an
empty room because it looks like work.

## The rules this projection keeps

**Occupancy comes from the control plane, activity comes from events.** A
character exists because a job is open and an agent has acted on it; what that
character is *doing* comes from ``runtime.*`` rows. The two are kept apart on
purpose:

* ``apply_status`` decides which jobs are open. That is the ledger's answer,
  read through ``ControlPlane.status``, never re-derived here.
* ``apply_ledger_event`` decides who is at a desk and what animation they are
  playing. It never concludes that a job finished, succeeded or was approved.

**A ``runtime.*`` row is an animation and nothing more.** It says an agent
typed, read or ran something. It is not evidence, not a verdict, and not an
approval. Everything in this module that touches a runtime row writes to
``activity`` and to no other field, so a runtime vocabulary change can never
move a job's state.

**Human rows make no character.** ``job.created`` is written with no agent, and
the Brief is handed off by ``human``. Neither is somebody at a desk; putting an
avatar there would invent a participant the ledger does not have.

**Pure.** Same frames plus same status gives the same room. There is no clock
in here -- a character does not "go idle after 30 seconds", because that would
make the projection a function of when it was asked. It goes idle when its job
stops being open, which is a fact somebody recorded.

## Replay equivalence

Rebuilding from cursor 0 and reconnecting at a cursor must agree, so the state
is a fold over rows in order with no dependence on which rows arrived in which
connection. ``seen_at`` holds an ``event_id``, never a wall-clock time, for the
same reason.

The fold is also **idempotent in itself**, not only because the subscription
happens to drop duplicates upstream. A resubscribe replays rows the office has
already applied, and a counter that incremented on each of them -- ``turns``
was exactly that -- would make the room depend on how many times the socket
dropped. So a row at or below the applied cursor is not news here either.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from QKD_PROJ.orchestrator import wire as W

# The scheduler statuses that mean a job is still somebody's problem. Anything
# else -- finished, and whatever the scheduler adds later -- empties the desk.
OPEN_STATUSES = frozenset(
    {"active", "runnable", "waiting_approval", "waiting_retry", "waiting_person"}
)

# Job states, for the shape of `status.jobs` that has no scheduler in front of
# it. `Job.state` is the ledger column; these are the ones still in play.
OPEN_STATES = frozenset({"pending", "running", "blocked"})

# Rows written by nobody, or by a person. Neither is an occupant.
NOT_AN_AGENT = frozenset({"human", "platform", ""})

# What a character is shown doing. Derived only from `runtime.*` item types, and
# used for nothing but the animation.
IDLE = "idle"
TYPING = "typing"
READING = "reading"
RUNNING = "running"
WAITING = "waiting"
DELEGATING = "delegating"

_ACTIVITY_BY_ITEM = {
    "agentMessage": TYPING,
    "userMessage": READING,
    "reasoning": READING,
    "commandExecution": RUNNING,
    "fileChange": TYPING,
    "subAgentActivity": DELEGATING,
    "collabAgentToolCall": DELEGATING,
    "mcpToolCall": RUNNING,
}


@dataclass
class Occupant:
    """One character on the floor."""

    agent: str
    job_id: str
    activity: str = IDLE
    detail: str = ""
    # An event_id, not a timestamp: two replays of the same history must
    # produce the same room, and a clock would make them differ.
    seen_at: int = 0
    turns: int = 0

    @property
    def id(self) -> str:
        return f"{self.job_id}:{self.agent}"

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent": self.agent,
            "jobId": self.job_id,
            "activity": self.activity,
            "detail": self.detail,
            "seenAt": self.seen_at,
            "turns": self.turns,
        }


class OfficeProjection:
    """The floor, as a fold over frames and the control plane's status."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._occupants: dict[str, Occupant] = {}
        self._open: set[str] = set()
        self._cursor = 0
        self._revision = 0

    # -- ingest -----------------------------------------------------------

    def apply_frame(self, frame: dict[str, Any]) -> bool:
        kind = frame.get("type")
        if kind == W.LEDGER_EVENT:
            return self.apply_ledger_event(frame.get("event") or {})
        return False

    def apply_ledger_event(self, event: dict[str, Any]) -> bool:
        """Seat an agent and set its animation. Never decides a job's state."""
        job = _text(event.get("job_id"))
        agent = _text(event.get("agent"))
        if not job:
            return False

        event_id = event.get("event_id")
        event_id = event_id if isinstance(event_id, int) else 0

        with self._lock:
            before = self._revision
            if event_id and event_id <= self._cursor:
                # Already folded in. Replay is idempotent by design, and a
                # counter that ran again on a replayed row would make the room
                # a function of how often the connection dropped.
                return False
            if event_id > self._cursor:
                self._cursor = event_id

            kind = _text(event.get("kind"))
            if agent.lower() in NOT_AN_AGENT or not kind.startswith(("runtime.", "handoff.", "conversation.turn.")) or kind == "handoff.Brief":
                # A person, or the platform itself. No desk.
                return self._revision != before

            key = f"{job}:{agent}"
            occupant = self._occupants.get(key)
            if occupant is None:
                occupant = Occupant(agent=agent, job_id=job, seen_at=event_id)
                self._occupants[key] = occupant
                self._revision += 1

            kind = _text(event.get("kind"))
            if kind.startswith("conversation.turn."):
                if kind == "conversation.turn.completed":
                    occupant.turns += 1
                self._set(occupant, TYPING if kind == "conversation.turn.started" else WAITING,
                          "working" if kind == "conversation.turn.started" else "turn ended", event_id)
            elif kind.startswith("runtime."):
                self._animate(occupant, event, event_id)
            elif kind.startswith("handoff."):
                # A turn boundary. It is *not* read as success or completion --
                # only as "this one stopped typing".
                occupant.turns += 1
                self._set(occupant, WAITING, f"handed off a {kind[len('handoff.'):]}", event_id)
            return self._revision != before

    def apply_status(self, snapshot: dict[str, Any]) -> bool:
        """Which jobs are still open. The only thing that empties a desk."""
        with self._lock:
            before = self._revision
            open_jobs = {job for job in _open_jobs(snapshot)}
            if open_jobs != self._open:
                self._open = open_jobs
                self._revision += 1

            # A character whose job is no longer open leaves. Nothing about
            # *how* it ended is inferred here -- only that it is not open.
            for key in [k for k, o in self._occupants.items() if o.job_id not in self._open]:
                del self._occupants[key]
                self._revision += 1
            jobs = snapshot.get("jobs", {})
            waiting = set()
            if isinstance(jobs, dict):
                for state, rows in jobs.items():
                    if state not in {"active", "runnable", "running"}:
                        waiting.update(row.get("job_id") for row in rows or [] if isinstance(row, dict))
            elif isinstance(jobs, list):
                waiting = {row.get("job_id") for row in jobs if isinstance(row, dict)
                           and (row.get("state") or row.get("status")) in {"blocked", "awaiting_approval"}}
            for occupant in self._occupants.values():
                if occupant.job_id in waiting:
                    self._set(occupant, WAITING, "job waiting", occupant.seen_at)
            return self._revision != before

    def reset(self) -> None:
        with self._lock:
            self._occupants.clear()
            self._open.clear()
            self._cursor = 0
            self._revision += 1

    # -- animation --------------------------------------------------------

    def _animate(self, occupant: Occupant, event: dict[str, Any], event_id: int) -> None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        method = _text(payload.get("method"))
        item_type = _text(payload.get("item_type"))

        if "requestApproval" in method:
            # An approval *request* is a thing the character is doing. Whether
            # it was granted is the ledger's business and is not read here.
            self._set(occupant, WAITING, "asked for approval", event_id)
            return

        activity = _ACTIVITY_BY_ITEM.get(item_type)
        if activity is None:
            # A runtime vocabulary this office has not seen. It still means the
            # agent is doing something, so the character moves rather than
            # freezing, and the label says what arrived.
            self._set(occupant, RUNNING, item_type or method, event_id)
            return
        self._set(occupant, activity, item_type, event_id)

    def _set(self, occupant: Occupant, activity: str, detail: str, event_id: int) -> None:
        if occupant.activity == activity and occupant.detail == detail:
            occupant.seen_at = max(occupant.seen_at, event_id)
            return
        occupant.activity = activity
        occupant.detail = detail
        occupant.seen_at = max(occupant.seen_at, event_id)
        self._revision += 1

    # -- read -------------------------------------------------------------

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> dict[str, Any]:
        """Sorted by id, so the same history renders the same room."""
        with self._lock:
            return {
                "cursor": self._cursor,
                "openJobs": sorted(self._open),
                "occupants": [self._occupants[k].describe() for k in sorted(self._occupants)],
            }


# -- helpers ------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _open_jobs(snapshot: dict[str, Any]) -> Iterable[str]:
    """Job ids still in play, from either shape of ``status.jobs``.

    Under a daemon the dict is keyed by scheduler status and the answer is
    which key a row sits under. Without one the rows are ledger rows and the
    answer is `Job.state`. Neither is re-derived from events.
    """
    jobs = snapshot.get("jobs")
    if isinstance(jobs, dict):
        for status, rows in jobs.items():
            if status not in OPEN_STATUSES:
                continue
            for row in rows or []:
                if isinstance(row, dict) and _text(row.get("job_id")):
                    yield _text(row["job_id"])
        return
    for row in jobs or []:
        if not isinstance(row, dict):
            continue
        state = _text(row.get("status")) or _text(row.get("state"))
        if state in OPEN_STATUSES or state in OPEN_STATES:
            if _text(row.get("job_id")):
                yield _text(row["job_id"])
