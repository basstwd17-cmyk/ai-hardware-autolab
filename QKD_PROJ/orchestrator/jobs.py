"""The daemon's memory: what work exists, what happened, what a person owes it.

``ConsumptionLedger`` answers one question -- how many times has this approved
step been attempted. That is enough while a person runs a sequence at the
bench. It is not enough for a loop that has to survive a rate-limit window, a
closed editor, or a machine reboot, because four more things have to outlive
the process:

* **jobs** -- the goal, and which state it is in right now.
* **events** -- an append-only log with a monotonic id. This is the spine.
* **handoffs** -- the discovery and execution envelopes (including sourced
  architecture and contract proposals), stored verbatim and by digest.
* **approvals** -- a durable request a person can act on an hour later.

Three decisions shape all of it.

**The daemon owns the state, not the model.** A planner agent that keeps its
own iteration counter loses it the moment its context ends, and a loop that
forgets it already failed is a loop that runs forever. So ``iteration``, the
failure signature and the strike count live here, in a file, and an agent is
told what they are rather than being trusted to remember.

**The event log is the platform's clock, not the runtime's.** A UI reconnects
at ``events.event_id`` and replays forward. Whatever replay a vendor's runtime
offers is a convenience; correctness cannot depend on it, because the log has
to be readable after that runtime has been swapped out.

**A state change and the event that explains it commit together.** If they
could be written separately, a job could sit in a state that no event accounts
for -- and then the log, which is the thing everyone reads, would be lying by
omission. One transaction, or neither.

There is no ``failure_signatures`` table. The signature rides on the Verdict
handoff that produced it, and the strike count is the length of the trailing
run of identical ones. Deriving it means it cannot disagree with the handoffs
it came from.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from QKD_PROJ.hwtools.result import utc_now


def _moment(value: str) -> datetime:
    """Parse ledger ISO timestamps for ordering, accepting a trailing Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 20_000

# Job states. `awaiting_approval` is deliberately distinct from `blocked`: one
# is waiting for a person and belongs in an inbox, the other is waiting for
# something nobody can grant.
PENDING = "pending"
RUNNING = "running"
AWAITING_APPROVAL = "awaiting_approval"
BLOCKED = "blocked"
DONE = "done"
JOB_STATES = (PENDING, RUNNING, AWAITING_APPROVAL, BLOCKED, DONE)

# Which moves are legal. A job never leaves `done`: a finished gate that needs
# more work is a new job with a new brief, so the record of what was decided
# stays readable afterwards.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    PENDING: (RUNNING, BLOCKED, DONE),
    RUNNING: (AWAITING_APPROVAL, BLOCKED, DONE, RUNNING),
    AWAITING_APPROVAL: (RUNNING, BLOCKED, DONE),
    BLOCKED: (RUNNING, DONE),
    DONE: (),
}

# How a job finished. These are the reducer's outcomes plus the two a loop can
# reach without ever reducing anything.
JOB_OUTCOMES = (
    "satisfied",
    "needs_approval",
    "contract_gap",
    "exhausted",
    "primary_run_missing",
    "cancelled",
)

HANDOFF_KINDS = (
    "Brief",
    "MissionPlan",
    "ResearchPlan",
    "SourcePacket",
    "ChangeAnalysis",
    "ArchitectureDecision",
    "ContractDraft",
    "ContractReview",
    "Plan",
    "StepResult",
    "Verdict",
    "Report",
)

APPROVAL_PENDING = "pending"
APPROVAL_GRANTED = "granted"
APPROVAL_DENIED = "denied"
APPROVAL_EXPIRED = "expired"
APPROVAL_WITHDRAWN = "withdrawn"
APPROVAL_STATES = (
    APPROVAL_PENDING,
    APPROVAL_GRANTED,
    APPROVAL_DENIED,
    APPROVAL_EXPIRED,
    APPROVAL_WITHDRAWN,
)

DEFAULT_STRIKES = 3


class JobLedgerError(RuntimeError):
    """A write the ledger will not make, and the reason it will not."""


def _states(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS job_schema_info (
        version INTEGER NOT NULL
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id      TEXT    PRIMARY KEY,
        gate        TEXT    NOT NULL,
        objective   TEXT    NOT NULL,
        state       TEXT    NOT NULL,
        outcome     TEXT,
        iteration   INTEGER NOT NULL DEFAULT 0,
        created_at  TEXT    NOT NULL,
        updated_at  TEXT    NOT NULL,
        CHECK (state IN ({_states(JOB_STATES)})),
        CHECK (outcome IS NULL OR outcome IN ({_states(JOB_OUTCOMES)})),
        -- An outcome is what a finished job finished as. Recording one while
        -- the job is still moving would let a reader treat it as settled.
        CHECK (outcome IS NULL OR state = 'done'),
        CHECK (iteration >= 0)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS events (
        event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id    TEXT    NOT NULL REFERENCES jobs(job_id),
        seq       INTEGER NOT NULL,
        at        TEXT    NOT NULL,
        kind      TEXT    NOT NULL,
        agent     TEXT,
        payload   TEXT    NOT NULL,
        -- Two writers appending to one job both compute the same next seq and
        -- the index refuses the second. A count checked in Python could not.
        UNIQUE (job_id, seq),
        CHECK (seq >= 1),
        CHECK (length(kind) > 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS events_by_job ON events (job_id, seq)
    """,
    f"""
    CREATE TABLE IF NOT EXISTS handoffs (
        handoff_id        TEXT    PRIMARY KEY,
        job_id            TEXT    NOT NULL REFERENCES jobs(job_id),
        iteration         INTEGER NOT NULL,
        kind              TEXT    NOT NULL,
        agent             TEXT    NOT NULL,
        step_id           TEXT    NOT NULL DEFAULT '',
        at                TEXT    NOT NULL,
        payload           TEXT    NOT NULL,
        sha256            TEXT    NOT NULL,
        failure_signature TEXT,
        event_id          INTEGER REFERENCES events(event_id),
        -- One Plan per iteration, one Verdict per iteration, one StepResult per
        -- step. A second write is a bug in the caller, not a newer opinion.
        UNIQUE (job_id, iteration, kind, agent, step_id),
        CHECK (kind IN ({_states(HANDOFF_KINDS)})),
        CHECK (iteration >= 0)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS approvals (
        approval_id     TEXT    PRIMARY KEY,
        job_id          TEXT    NOT NULL REFERENCES jobs(job_id),
        contract        TEXT    NOT NULL,
        step_id         TEXT    NOT NULL DEFAULT '',
        scope           TEXT    NOT NULL,
        artifacts       TEXT    NOT NULL,
        max_executions  INTEGER NOT NULL DEFAULT 1,
        requested_at    TEXT    NOT NULL,
        requested_by    TEXT    NOT NULL,
        expires_at      TEXT,
        state           TEXT    NOT NULL,
        decided_at      TEXT,
        decided_by      TEXT,
        decision_note   TEXT,
        bundle_sha256   TEXT,
        CHECK (state IN ({_states(APPROVAL_STATES)})),
        CHECK (max_executions >= 1),
        -- Decided exactly when it is not pending. Keeps a granted approval from
        -- existing without a record of who granted it.
        CHECK ((state = 'pending') = (decided_at IS NULL)),
        CHECK ((state = 'pending') = (decided_by IS NULL)),
        CHECK (length(scope) > 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS approvals_pending
        ON approvals (state, requested_at)
    """,
)


# -- rows --------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    job_id: str
    gate: str
    objective: str
    state: str
    outcome: str | None
    iteration: int
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, row: sqlite3.Row) -> "Job":
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Event:
    event_id: int
    job_id: str
    seq: int
    at: str
    kind: str
    agent: str | None
    payload: dict[str, Any]

    @classmethod
    def of(cls, row: sqlite3.Row) -> "Event":
        return cls(
            event_id=row["event_id"],
            job_id=row["job_id"],
            seq=row["seq"],
            at=row["at"],
            kind=row["kind"],
            agent=row["agent"],
            payload=json.loads(row["payload"]),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "job_id": self.job_id,
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "agent": self.agent,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    job_id: str
    iteration: int
    kind: str
    agent: str
    step_id: str
    at: str
    payload: dict[str, Any]
    sha256: str
    failure_signature: str | None
    event_id: int | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> "Handoff":
        return cls(
            handoff_id=row["handoff_id"],
            job_id=row["job_id"],
            iteration=row["iteration"],
            kind=row["kind"],
            agent=row["agent"],
            step_id=row["step_id"],
            at=row["at"],
            payload=json.loads(row["payload"]),
            sha256=row["sha256"],
            failure_signature=row["failure_signature"],
            event_id=row["event_id"],
        )

    def describe(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        return data


@dataclass(frozen=True)
class Approval:
    approval_id: str
    job_id: str
    contract: str
    step_id: str
    scope: str
    artifacts: dict[str, str]
    max_executions: int
    requested_at: str
    requested_by: str
    expires_at: str | None
    state: str
    decided_at: str | None
    decided_by: str | None
    decision_note: str | None
    bundle_sha256: str | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> "Approval":
        return cls(
            approval_id=row["approval_id"],
            job_id=row["job_id"],
            contract=row["contract"],
            step_id=row["step_id"],
            scope=row["scope"],
            artifacts=json.loads(row["artifacts"]),
            max_executions=row["max_executions"],
            requested_at=row["requested_at"],
            requested_by=row["requested_by"],
            expires_at=row["expires_at"],
            state=row["state"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            decision_note=row["decision_note"],
            bundle_sha256=row["bundle_sha256"],
        )

    @property
    def granted(self) -> bool:
        return self.state == APPROVAL_GRANTED

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """The bytes a handoff digest covers.

    Sorted-key JSON rather than whatever the caller happened to serialise, so
    the same envelope written twice produces the same digest and a reformat is
    not mistaken for a change.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


# -- the ledger --------------------------------------------------------------


class JobLedger:
    """Insert-only job state, shared by the daemon and anything reading it."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # -- opening -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        # The point of writing an event before acting is that it survives the
        # process. FULL makes the commit durable, not merely visible.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM job_schema_info").fetchone()
            if row is None:
                conn.execute("INSERT INTO job_schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] in (1, 2, 3):
                self._migrate_handoffs(conn)
            elif row["version"] != SCHEMA_VERSION:
                conn.execute("ROLLBACK")
                raise JobLedgerError(
                    f"{self.path} is job schema version {row['version']}; this code writes "
                    f"version {SCHEMA_VERSION}. Refusing to touch a ledger it may not understand."
                )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise JobLedgerError(f"{self.path} could not be opened: {exc}") from exc
        finally:
            conn.close()

    @staticmethod
    def _migrate_handoffs(conn: sqlite3.Connection) -> None:
        """Widen the immutable handoff vocabulary without rewriting payloads.

        SQLite cannot alter a CHECK constraint in place. Rebuild only the
        handoffs table inside the same immediate transaction and copy every
        original row byte-for-byte before advancing the schema marker.
        """
        conn.execute("ALTER TABLE handoffs RENAME TO handoffs_previous")
        statement = next(
            value
            for value in SCHEMA_STATEMENTS
            if "CREATE TABLE IF NOT EXISTS handoffs" in value
        )
        conn.execute(statement)
        columns = (
            "handoff_id, job_id, iteration, kind, agent, step_id, at, payload, "
            "sha256, failure_signature, event_id"
        )
        conn.execute(
            f"INSERT INTO handoffs ({columns}) SELECT {columns} FROM handoffs_previous"
        )
        conn.execute("DROP TABLE handoffs_previous")
        conn.execute("UPDATE job_schema_info SET version = ?", (SCHEMA_VERSION,))

    class _Transaction:
        def __init__(self, ledger: "JobLedger") -> None:
            self.ledger = ledger
            self.conn: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.conn = self.ledger._connect()
            try:
                # IMMEDIATE, not DEFERRED: take the write lock before reading
                # the sequence number two writers would otherwise both see.
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                self.conn.close()
                raise JobLedgerError(
                    f"Could not get a write transaction on {self.ledger.path.name} within "
                    f"{self.ledger.busy_timeout_ms} ms: {exc}. Another daemon is holding it."
                ) from exc
            return self.conn

        def __exit__(self, exc_type: object, *_: object) -> None:
            assert self.conn is not None
            try:
                self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.conn.close()

    def _transaction(self) -> "_Transaction":
        return JobLedger._Transaction(self)

    # -- jobs ----------------------------------------------------------------

    def create_job(
        self, gate: str, objective: str, job_id: str | None = None, brief: dict[str, Any] | None = None
    ) -> Job:
        """Open a job and its log in one transaction.

        The `job.created` event is written here rather than by the caller, so a
        job cannot exist with an empty log -- which is the state a UI has no
        way to render and no way to distinguish from a lost connection.
        """
        if not gate.strip() or not objective.strip():
            raise JobLedgerError("a job needs a gate and an objective")
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        now = utc_now()
        with self._transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, gate, objective, state, outcome, iteration, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)",
                    (job_id, gate, objective, PENDING, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise JobLedgerError(f"job {job_id} already exists") from exc
            self._append(conn, job_id, "job.created", {"gate": gate, "objective": objective}, None)
            if brief is not None:
                self._write_handoff(conn, job_id, 0, "Brief", "human", "", brief, None)
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return Job.of(row)

    def job(self, job_id: str) -> Job:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise JobLedgerError(f"no such job: {job_id}")
        return Job.of(row)

    def jobs(self, state: str | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs"
        params: list[Any] = []
        if state is not None:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY created_at DESC"
        conn = self._connect()
        try:
            return [Job.of(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def transition(
        self,
        job_id: str,
        state: str,
        outcome: str | None = None,
        *,
        reason: str = "",
        agent: str | None = None,
    ) -> Job:
        """Move a job, and log why, in one transaction.

        Refusing an illegal move here is what keeps the state column meaningful:
        without it, `done` would mean "the last writer said done" rather than
        "this job reached a terminal state through legal steps".
        """
        if state not in JOB_STATES:
            raise JobLedgerError(f"unknown job state: {state!r}")
        if outcome is not None and outcome not in JOB_OUTCOMES:
            raise JobLedgerError(f"unknown outcome: {outcome!r}")
        if outcome is not None and state != DONE:
            raise JobLedgerError(
                f"outcome {outcome!r} was given with state {state!r}; an outcome is what a "
                "finished job finished as"
            )
        if state == DONE and outcome is None:
            raise JobLedgerError("a job may not finish without an outcome")

        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobLedgerError(f"no such job: {job_id}")
            current = row["state"]
            if state not in TRANSITIONS[current]:
                allowed = ", ".join(TRANSITIONS[current]) or "nothing; it is terminal"
                raise JobLedgerError(
                    f"job {job_id} is {current} and cannot become {state}. Allowed: {allowed}."
                )
            conn.execute(
                "UPDATE jobs SET state = ?, outcome = ?, updated_at = ? WHERE job_id = ?",
                (state, outcome, utc_now(), job_id),
            )
            self._append(
                conn,
                job_id,
                f"job.{state}",
                {"from": current, "to": state, "outcome": outcome, "reason": reason},
                agent,
            )
            return Job.of(conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())

    def begin_iteration(self, job_id: str) -> int:
        """Advance the loop counter. The daemon owns it; no agent is asked."""
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobLedgerError(f"no such job: {job_id}")
            if row["state"] == DONE:
                raise JobLedgerError(f"job {job_id} is done; a new attempt is a new job")
            iteration = int(row["iteration"]) + 1
            conn.execute(
                "UPDATE jobs SET iteration = ?, updated_at = ? WHERE job_id = ?",
                (iteration, utc_now(), job_id),
            )
            self._append(conn, job_id, "iteration.started", {"iteration": iteration}, None)
            return iteration

    # -- events --------------------------------------------------------------

    def _append(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        kind: str,
        payload: dict[str, Any],
        agent: str | None,
    ) -> int:
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        )
        cursor = conn.execute(
            "INSERT INTO events (job_id, seq, at, kind, agent, payload) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, seq, utc_now(), kind, agent, json.dumps(payload, ensure_ascii=False)),
        )
        return int(cursor.lastrowid)

    def append_event(
        self, job_id: str, kind: str, payload: dict[str, Any] | None = None, agent: str | None = None
    ) -> Event:
        if not kind.strip():
            raise JobLedgerError("an event needs a kind")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise JobLedgerError(f"no such job: {job_id}")
            event_id = self._append(conn, job_id, kind, payload or {}, agent)
            return Event.of(
                conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            )

    def events_since(
        self, event_id: int = 0, job_id: str | None = None, limit: int = 500
    ) -> list[Event]:
        """The reconnect cursor. Strictly increasing, gap-free, ours.

        A UI that went away at ``event_id`` asks for everything after it and is
        exactly caught up. That is why the cursor is this column and not a
        runtime's own stream position: the log outlives the runtime.
        """
        sql = "SELECT * FROM events WHERE event_id > ?"
        params: list[Any] = [int(event_id)]
        if job_id is not None:
            sql += " AND job_id = ?"
            params.append(job_id)
        sql += " ORDER BY event_id LIMIT ?"
        params.append(int(limit))
        conn = self._connect()
        try:
            return [Event.of(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def latest_event_id(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM events").fetchone()[0])
        finally:
            conn.close()

    # -- handoffs ------------------------------------------------------------

    def _write_handoff(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        iteration: int,
        kind: str,
        agent: str,
        step_id: str,
        payload: dict[str, Any],
        failure_signature: str | None,
    ) -> str:
        digest = hashlib.sha256(canonical_payload(payload)).hexdigest()
        handoff_id = f"{job_id}:{iteration}:{kind}:{agent}:{step_id}" if step_id else (
            f"{job_id}:{iteration}:{kind}:{agent}"
        )
        event_id = self._append(
            conn,
            job_id,
            f"handoff.{kind}",
            {"iteration": iteration, "agent": agent, "step_id": step_id, "sha256": digest},
            agent,
        )
        try:
            conn.execute(
                "INSERT INTO handoffs (handoff_id, job_id, iteration, kind, agent, step_id, at, "
                "payload, sha256, failure_signature, event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    handoff_id,
                    job_id,
                    iteration,
                    kind,
                    agent,
                    step_id,
                    utc_now(),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    digest,
                    failure_signature,
                    event_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise JobLedgerError(
                f"{kind} from {agent} for iteration {iteration} of {job_id} is already recorded. "
                "A second one is a caller bug, not a newer opinion; start an iteration for it."
            ) from exc
        return handoff_id

    def record_handoff(
        self,
        job_id: str,
        kind: str,
        agent: str,
        payload: dict[str, Any],
        *,
        iteration: int | None = None,
        step_id: str = "",
        failure_signature: str | None = None,
    ) -> Handoff:
        """Store an envelope verbatim, by digest.

        This is the Plan sink the architect agent deliberately lacks. It has no
        Write tool on purpose, so the plan exists only as its return value until
        something puts it somewhere -- and that something has to be a sink with
        one destination, not a general file write, or the read-only boundary was
        decoration.
        """
        if kind not in HANDOFF_KINDS:
            raise JobLedgerError(f"unknown handoff kind: {kind!r}")
        if not isinstance(payload, dict):
            raise JobLedgerError("a handoff payload must be an object")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobLedgerError(f"no such job: {job_id}")
            resolved = int(row["iteration"]) if iteration is None else int(iteration)
            handoff_id = self._write_handoff(
                conn, job_id, resolved, kind, agent, step_id, payload, failure_signature
            )
            return Handoff.of(
                conn.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?", (handoff_id,)
                ).fetchone()
            )

    def handoffs(
        self, job_id: str, kind: str | None = None, iteration: int | None = None
    ) -> list[Handoff]:
        sql = "SELECT * FROM handoffs WHERE job_id = ?"
        params: list[Any] = [job_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if iteration is not None:
            sql += " AND iteration = ?"
            params.append(int(iteration))
        # ``at`` is not a sequence: several handoffs can share one clock tick
        # on Windows. event_id is the ledger's monotonic order and therefore
        # the only stable order for replaying a workflow.
        sql += " ORDER BY iteration, event_id, at"
        conn = self._connect()
        try:
            return [Handoff.of(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def strikes(self, job_id: str) -> int:
        """How many iterations in a row ended with the identical failure.

        Derived from the Verdict handoffs rather than counted into a column,
        so it cannot drift from the verdicts it describes. A verdict with no
        signature -- a satisfied gate -- ends the run at zero.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT failure_signature FROM handoffs WHERE job_id = ? AND kind = 'Verdict' "
                "ORDER BY iteration DESC, at DESC",
                (job_id,),
            ).fetchall()
        finally:
            conn.close()
        if not rows or rows[0]["failure_signature"] is None:
            return 0
        newest = rows[0]["failure_signature"]
        count = 0
        for row in rows:
            if row["failure_signature"] != newest:
                break
            count += 1
        return count

    def exhausted(self, job_id: str, strikes: int = DEFAULT_STRIKES) -> bool:
        return self.strikes(job_id) >= strikes

    def record_reduction(
        self, job_id: str, reduction: Any, agent: str = "verifier"
    ) -> tuple[Handoff, str]:
        """Store an EvidenceReducer result as this iteration's Verdict.

        Returns the handoff and the outcome the *job* should take from it,
        which is not always the reduction's own. The reducer decides one
        iteration from the evidence in front of it; the ledger is the only
        thing that can see three iterations in a row failing the same way, so
        `open` becomes `exhausted` here and nowhere else.

        Taking a Reduction rather than a dict keeps a model out of the path
        entirely: the verdict written to the ledger is the one the deterministic
        code produced, not a re-typed version of it.
        """
        payload = reduction.describe() if hasattr(reduction, "describe") else dict(reduction)
        signature = payload.get("failure_signature")
        handoff = self.record_handoff(
            job_id, "Verdict", agent, payload, failure_signature=signature
        )
        outcome = str(payload.get("outcome", "open"))
        if outcome == "satisfied":
            return handoff, "satisfied"
        if outcome == "primary_run_missing":
            return handoff, "primary_run_missing"
        if self.exhausted(job_id):
            return handoff, "exhausted"
        return handoff, "open"

    # -- approvals -----------------------------------------------------------

    def request_approval(
        self,
        job_id: str,
        contract: str,
        scope: str,
        *,
        artifacts: dict[str, str] | None = None,
        requested_by: str = "board-operator",
        step_id: str = "",
        max_executions: int = 1,
        expires_at: str | None = None,
    ) -> Approval:
        """Put a request in front of a person and leave it there.

        The request names artifacts by digest, not by path, for the reason a
        bundle does: a path can be made to point at a different file between
        the signature and the run, and a digest cannot. An approval that
        outlives the process has more time to have that happen to it, not less.
        """
        if not scope.strip():
            raise JobLedgerError(
                "an approval needs a scope. 'approve the next step' is not something a person "
                "can be held to, and not something a tool can check a request against."
            )
        approval_id = f"apr_{uuid.uuid4().hex[:12]}"
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise JobLedgerError(f"no such job: {job_id}")
            conn.execute(
                "INSERT INTO approvals (approval_id, job_id, contract, step_id, scope, artifacts, "
                "max_executions, requested_at, requested_by, expires_at, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    job_id,
                    contract,
                    step_id,
                    scope,
                    json.dumps(artifacts or {}, sort_keys=True),
                    int(max_executions),
                    utc_now(),
                    requested_by,
                    expires_at,
                    APPROVAL_PENDING,
                ),
            )
            self._append(
                conn,
                job_id,
                "approval.pending",
                {
                    "approval_id": approval_id,
                    "contract": contract,
                    "step_id": step_id,
                    "scope": scope,
                    "artifacts": artifacts or {},
                    "expires_at": expires_at,
                },
                requested_by,
            )
            return Approval.of(
                conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
            )

    def approval(self, approval_id: str) -> Approval:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise JobLedgerError(f"no such approval: {approval_id}")
        return Approval.of(row)

    def pending_approvals(self, job_id: str | None = None, now: str | None = None) -> list[Approval]:
        """The inbox. Expired requests are not in it.

        Expiry is applied at read time rather than by a sweeper, because a
        request that quietly became grantable again while nothing was running
        is precisely the failure an expiry is for.
        """
        sql = "SELECT * FROM approvals WHERE state = ?"
        params: list[Any] = [APPROVAL_PENDING]
        if job_id is not None:
            sql += " AND job_id = ?"
            params.append(job_id)
        sql += " ORDER BY requested_at"
        moment = now or utc_now()
        conn = self._connect()
        try:
            rows = [Approval.of(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()
        return [a for a in rows if not (a.expires_at and a.expires_at <= moment)]

    def decide_approval(
        self,
        approval_id: str,
        granted: bool,
        decided_by: str,
        *,
        note: str = "",
        bundle_sha256: str | None = None,
        now: str | None = None,
    ) -> Approval:
        """A durable authority's answer, written once.

        The update is guarded on ``state = 'pending'`` and the row count is
        checked, so two clicks race into one decision rather than the second
        overwriting the first. A decision is never revised: withdrawing a
        granted approval is a separate move with its own record.
        """
        if not decided_by.strip():
            raise JobLedgerError("an approval decision must name who made it")
        moment = now or utc_now()

        # Expiry is settled in its own transaction, before the one that would
        # record a decision. Marking it inside that transaction and then
        # raising would roll the mark back with the refusal -- and the request
        # would still be sitting there, pending, for the next person to grant.
        existing = self.approval(approval_id)
        if existing.state != APPROVAL_PENDING:
            raise JobLedgerError(
                f"approval {approval_id} is already {existing.state}, decided by "
                f"{existing.decided_by} at {existing.decided_at}. It is not decided twice."
            )
        if granted and existing.expires_at and existing.expires_at <= moment:
            self._expire(approval_id, existing.expires_at, decided_by, moment)
            raise JobLedgerError(
                f"approval {approval_id} expired at {existing.expires_at} and cannot be granted "
                "now. Have the step request a fresh one against the current artifacts."
            )

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise JobLedgerError(f"no such approval: {approval_id}")
            if row["state"] != APPROVAL_PENDING:
                raise JobLedgerError(
                    f"approval {approval_id} is already {row['state']}, decided by "
                    f"{row['decided_by']} at {row['decided_at']}. It is not decided twice."
                )

            state = APPROVAL_GRANTED if granted else APPROVAL_DENIED
            cursor = conn.execute(
                "UPDATE approvals SET state = ?, decided_at = ?, decided_by = ?, "
                "decision_note = ?, bundle_sha256 = ? WHERE approval_id = ? AND state = ?",
                (state, moment, decided_by, note, bundle_sha256, approval_id, APPROVAL_PENDING),
            )
            if cursor.rowcount != 1:
                raise JobLedgerError(
                    f"approval {approval_id} was decided by someone else during this transaction"
                )
            self._append(
                conn,
                row["job_id"],
                "approval.decided",
                {
                    "approval_id": approval_id,
                    "state": state,
                    "decided_by": decided_by,
                    "note": note,
                },
                decided_by,
            )
            return Approval.of(
                conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
            )

    def authorize_approval(
        self,
        approval_id: str,
        tool: str,
        agent: str,
        *,
        now: str | None = None,
    ) -> Event:
        """Consume one bounded hardware authorisation before the tool starts.

        The event is written under ``BEGIN IMMEDIATE`` after counting earlier
        authorisations for the same approval.  Two concurrent callers can
        therefore consume slots 1 and 2, but they cannot both consume slot 1.
        Recording before execution is intentional: a process crash between
        this event and the board call consumes the attempt rather than making
        an uncertain board touch silently retryable.
        """
        if not tool.strip():
            raise JobLedgerError("an approval authorisation must name the tool")
        if not agent.strip():
            raise JobLedgerError("an approval authorisation must name the agent")
        moment = now or utc_now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise JobLedgerError(f"no such approval: {approval_id}")
            if row["state"] != APPROVAL_GRANTED:
                raise JobLedgerError(
                    f"approval {approval_id} is {row['state']}, not granted"
                )
            if row["expires_at"] and _moment(row["expires_at"]) <= _moment(moment):
                raise JobLedgerError(
                    f"approval {approval_id} expired at {row['expires_at']}"
                )
            used = 0
            for event_row in conn.execute(
                "SELECT payload FROM events WHERE job_id = ? AND kind = ?",
                (row["job_id"], "approval.authorized"),
            ):
                payload = json.loads(event_row["payload"])
                if payload.get("approval_id") == approval_id:
                    used += 1
            maximum = int(row["max_executions"])
            if used >= maximum:
                raise JobLedgerError(
                    f"approval {approval_id} used all {maximum} authorised execution(s)"
                )
            event_id = self._append(
                conn,
                row["job_id"],
                "approval.authorized",
                {
                    "approval_id": approval_id,
                    "tool": tool,
                    "authorization": used + 1,
                    "max_executions": maximum,
                },
                agent,
            )
            return Event.of(
                conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            )

    def _expire(self, approval_id: str, expires_at: str, noticed_by: str, moment: str) -> None:
        """Record that a pending request ran out of time. Committed on its own."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT job_id FROM approvals WHERE approval_id = ? AND state = ?",
                (approval_id, APPROVAL_PENDING),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                "UPDATE approvals SET state = ?, decided_at = ?, decided_by = ?, "
                "decision_note = ? WHERE approval_id = ? AND state = ?",
                (
                    APPROVAL_EXPIRED,
                    moment,
                    noticed_by,
                    f"expired at {expires_at}",
                    approval_id,
                    APPROVAL_PENDING,
                ),
            )
            self._append(
                conn,
                row["job_id"],
                "approval.expired",
                {"approval_id": approval_id, "expires_at": expires_at, "noticed_by": noticed_by},
                noticed_by,
            )

    def expire_overdue(self, now: str | None = None) -> list[Approval]:
        """Settle every request whose deadline has passed. Safe to call on startup.

        Read-time filtering keeps an overdue request out of the inbox, but the
        row still says `pending`, and a daemon that has been down for a week
        should record what happened rather than leave a queue of requests that
        are invisible and technically live.
        """
        moment = now or utc_now()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT approval_id, expires_at FROM approvals WHERE state = ? "
                "AND expires_at IS NOT NULL AND expires_at <= ?",
                (APPROVAL_PENDING, moment),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            self._expire(row["approval_id"], row["expires_at"], "daemon", moment)
        return [self.approval(row["approval_id"]) for row in rows]

    def withdraw_approval(self, approval_id: str, withdrawn_by: str, note: str = "") -> Approval:
        """Take back a request or a grant, leaving the original decision visible."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise JobLedgerError(f"no such approval: {approval_id}")
            if row["state"] in (APPROVAL_DENIED, APPROVAL_WITHDRAWN, APPROVAL_EXPIRED):
                raise JobLedgerError(f"approval {approval_id} is already {row['state']}")
            previous = row["state"]
            conn.execute(
                "UPDATE approvals SET state = ?, decided_at = COALESCE(decided_at, ?), "
                "decided_by = COALESCE(decided_by, ?), decision_note = ? WHERE approval_id = ?",
                (
                    APPROVAL_WITHDRAWN,
                    utc_now(),
                    withdrawn_by,
                    f"withdrawn from {previous} by {withdrawn_by}. {note}".strip(),
                    approval_id,
                ),
            )
            self._append(
                conn,
                row["job_id"],
                "approval.withdrawn",
                {"approval_id": approval_id, "from": previous, "note": note},
                withdrawn_by,
            )
            return Approval.of(
                conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
                ).fetchone()
            )

    # -- restarting ----------------------------------------------------------

    def resume(self, job_id: str) -> dict[str, Any]:
        """Everything a restarted daemon needs to pick this job back up.

        Deliberately a read, not a repair. A job left ``running`` by a process
        that died is reported as such and not quietly reset: whether its step
        reached the board is unknown from here, and nothing may assume it did
        not. Deciding that is the recovery step's job, with its own record.
        """
        job = self.job(job_id)
        events = self.events_since(0, job_id, limit=1)
        conn = self._connect()
        try:
            last = conn.execute(
                "SELECT event_id, seq, kind, at FROM events WHERE job_id = ? "
                "ORDER BY event_id DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        plans = self.handoffs(job_id, "Plan")
        verdicts = self.handoffs(job_id, "Verdict")
        return {
            "job": job.describe(),
            "strikes": self.strikes(job_id),
            "exhausted": self.exhausted(job_id),
            "pending_approvals": [a.describe() for a in self.pending_approvals(job_id)],
            "last_event": dict(last) if last else None,
            "first_event_id": events[0].event_id if events else None,
            "latest_plan": plans[-1].describe() if plans else None,
            "latest_verdict": verdicts[-1].describe() if verdicts else None,
            "interrupted": job.state == RUNNING,
        }

    def interrupted_jobs(self) -> list[Job]:
        """Jobs a previous process left mid-flight.

        The equivalent of a dangling reservation: something was running when
        the daemon stopped, and from the outside there is no way to tell how
        far it got.
        """
        return self.jobs(RUNNING)
