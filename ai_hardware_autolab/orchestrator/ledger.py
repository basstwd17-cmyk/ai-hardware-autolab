"""How many times a bundle has actually been spent.

A bundle says how many executions a step is allowed. Something has to remember
how many have already happened, or "max_executions: 1" means "once per process
start" -- which is not a limit at all.

That record does not belong in the contract. The contract is a document a human
reviews and signs; if layer 2 wrote its own counters into it, every run would
produce a contract diff and the signature would stop meaning anything. So the
ledger is a separate machine-owned store that nobody signs, and it only ever
grows.

Three rules make it trustworthy:

  * An attempt is recorded *before* the tool runs, not after. If a step crashes
    the process, the attempt is still spent. Otherwise a crashing step could be
    retried without limit, which is the opposite of what a budget is for.
  * Rows are inserted, never deleted. The outcome of an attempt is written into
    the row that was reserved for it, and nothing else changes.
  * Reserving is one SQLite transaction opened with BEGIN IMMEDIATE, and the
    table itself carries the invariant:

        UNIQUE (bundle_sha256, step, attempt)
        CHECK  (attempt BETWEEN 1 AND of_max)

    So the budget is not enforced by remembering to check a counter. Two
    processes that race both try to insert the same attempt number and the
    database refuses the second one. A count read in application code can go
    stale between the read and the write; a UNIQUE index cannot.

The ledger is keyed by bundle digest, not by bundle id. Re-issuing a bundle with
the same id and a new signature produces a new digest and therefore a fresh
budget -- which is correct, because it is a new approval.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_hardware_autolab.hwtools.result import utc_now

SCHEMA_VERSION = 1

# How long a writer waits for another writer's transaction before giving up.
BUSY_TIMEOUT_MS = 20_000

# Executed one statement at a time rather than through executescript(), which
# commits any transaction already open before it runs -- and the schema is
# created inside one.
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_info (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts (
        entry_id        TEXT    PRIMARY KEY,
        bundle_sha256   TEXT    NOT NULL,
        step            TEXT    NOT NULL,
        attempt         INTEGER NOT NULL,
        of_max          INTEGER NOT NULL,
        reserved_at     TEXT    NOT NULL,
        reserved_by_pid INTEGER NOT NULL,
        completed_at    TEXT,
        verdict         TEXT,
        run             TEXT,
        UNIQUE (bundle_sha256, step, attempt),
        CHECK (attempt >= 1 AND attempt <= of_max)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS attempts_by_bundle_step
        ON attempts (bundle_sha256, step)
    """,
)

_COLUMNS = (
    "entry_id",
    "bundle_sha256",
    "step",
    "attempt",
    "of_max",
    "reserved_at",
    "reserved_by_pid",
    "completed_at",
    "verdict",
    "run",
)


class LedgerError(RuntimeError):
    """Raised instead of running a step that has no budget left."""


@dataclass
class Reservation:
    """One spent attempt, already committed, awaiting its outcome."""

    ledger: "ConsumptionLedger"
    entry_id: str
    step: str
    attempt: int

    def complete(self, verdict: str | None, run: str | None = None) -> None:
        """Write the outcome into the row this attempt already reserved.

        The attempt count does not change here. It was spent when the
        reservation committed.
        """
        self.ledger._record_outcome(self.entry_id, verdict=verdict, run=run)


def migrate_json_ledger(json_path: Path, db_path: Path) -> int:
    """Import a pre-SQLite ledger. Returns how many attempts were carried over.

    The JSON file is left exactly as it is. Nothing about a spent execution
    should depend on a conversion having gone well, so the old evidence stays
    where it is rather than being replaced by the new store.
    """
    json_path, db_path = Path(json_path), Path(db_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    ledger = ConsumptionLedger(db_path)
    carried = 0
    with ledger._transaction() as conn:
        for entry in entries:
            attempt = int(entry["attempt"])
            conn.execute(
                "INSERT OR IGNORE INTO attempts "
                "(entry_id, bundle_sha256, step, attempt, of_max, reserved_at, "
                " reserved_by_pid, completed_at, verdict, run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.get("entry_id")
                    or f"{entry['bundle_sha256'][:16]}:{entry['step']}:{attempt}",
                    entry["bundle_sha256"],
                    entry["step"],
                    attempt,
                    int(entry.get("of") or entry.get("of_max") or attempt),
                    entry.get("reserved_at") or utc_now(),
                    int(entry.get("reserved_by_pid") or 0),
                    entry.get("completed_at"),
                    entry.get("verdict"),
                    entry.get("run"),
                ),
            )
            carried += 1
    return carried


class ConsumptionLedger:
    def __init__(self, path: Path, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_legacy_json()
        self._ensure_schema()

    # -- opening -------------------------------------------------------------

    def _reject_legacy_json(self) -> None:
        """Refuse to silently reinterpret a JSON ledger as an empty database.

        Converting a safety-critical file behind the caller's back is the sort
        of thing that quietly hands back every execution it was tracking. The
        conversion exists, but somebody has to ask for it.
        """
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        with open(self.path, "rb") as handle:
            head = handle.read(16)
        if head.startswith(b"SQLite format 3"):
            return
        if head.lstrip()[:1] in (b"{", b"["):
            raise LedgerError(
                f"{self.path} is a JSON ledger from before the SQLite store. "
                f"Import it with migrate_json_ledger() into a new database path; "
                f"it will not be reinterpreted in place, because reading it as an "
                f"empty database would return every execution it accounts for."
            )
        raise LedgerError(f"{self.path} is neither a SQLite database nor a JSON ledger.")

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None turns off the driver's implicit transactions so
        # BEGIN IMMEDIATE below is the only thing that opens one.
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=0)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        # The whole point of reserving before the tool runs is that the record
        # survives the process. FULL makes the commit durable rather than
        # merely visible.
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                conn.execute("ROLLBACK")
                raise LedgerError(
                    f"{self.path} is ledger schema version {row['version']}; this "
                    f"code writes version {SCHEMA_VERSION}. Refusing to touch a "
                    f"ledger it may not understand."
                )
            conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            raise LedgerError(f"{self.path} could not be opened: {exc}") from exc
        finally:
            conn.close()

    class _Transaction:
        def __init__(self, ledger: "ConsumptionLedger") -> None:
            self.ledger = ledger
            self.conn: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.conn = self.ledger._connect()
            try:
                # IMMEDIATE, not DEFERRED: take the write lock now. A deferred
                # transaction would let two processes both read the count before
                # either writes.
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                self.conn.close()
                raise LedgerError(
                    f"Could not get a write transaction on {self.ledger.path.name} "
                    f"within {self.ledger.busy_timeout_ms} ms: {exc}. Another "
                    f"orchestrator is holding it."
                ) from exc
            return self.conn

        def __exit__(self, exc_type: object, *_: object) -> None:
            assert self.conn is not None
            try:
                self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.conn.close()

    def _transaction(self) -> "_Transaction":
        return ConsumptionLedger._Transaction(self)

    # -- reading -------------------------------------------------------------

    def entries(
        self, bundle_sha256: str, step: str | None = None
    ) -> list[dict[str, Any]]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM attempts WHERE bundle_sha256 = ?"
        params: list[Any] = [bundle_sha256]
        if step is not None:
            sql += " AND step = ?"
            params.append(step)
        sql += " ORDER BY reserved_at, attempt"
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def spent(self, bundle_sha256: str, step: str) -> int:
        """Attempts already made, successful or not."""
        conn = self._connect()
        try:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE bundle_sha256 = ? AND step = ?",
                    (bundle_sha256, step),
                ).fetchone()[0]
            )
        finally:
            conn.close()

    def remaining(self, bundle_sha256: str, step: str, max_executions: int) -> int:
        return max(0, max_executions - self.spent(bundle_sha256, step))

    def dangling(self, bundle_sha256: str) -> list[dict[str, Any]]:
        """Attempts that were reserved and never resolved.

        One of these means a tool was called and its outcome was never written:
        the process died somewhere between the two. Whether the board was
        touched is unknown from here, so nothing may assume it was not.
        """
        return [e for e in self.entries(bundle_sha256) if e["completed_at"] is None]

    # -- spending ------------------------------------------------------------

    def reserve(self, bundle_sha256: str, step: str, max_executions: int) -> Reservation:
        """Spend one attempt up front and return a handle for its outcome.

        Raises rather than returning a falsy value: a caller that forgets to
        check a return code must not end up running the step anyway.
        """
        if max_executions < 1:
            raise LedgerError(
                f"Step '{step}' was asked to reserve against max_executions="
                f"{max_executions}."
            )
        with self._transaction() as conn:
            already = int(
                conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE bundle_sha256 = ? AND step = ?",
                    (bundle_sha256, step),
                ).fetchone()[0]
            )
            if already >= max_executions:
                raise LedgerError(
                    f"Step '{step}' has used {already} of {max_executions} authorised "
                    f"executions for this bundle. Have a new bundle issued rather "
                    f"than editing {self.path.name}."
                )
            attempt = already + 1
            entry_id = f"{bundle_sha256[:16]}:{step}:{attempt}"
            try:
                conn.execute(
                    "INSERT INTO attempts "
                    "(entry_id, bundle_sha256, step, attempt, of_max, reserved_at, "
                    " reserved_by_pid) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry_id,
                        bundle_sha256,
                        step,
                        attempt,
                        max_executions,
                        utc_now(),
                        os.getpid(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The table's own invariant caught what the count did not: a
                # second process committed this attempt number first.
                raise LedgerError(
                    f"Attempt {attempt} of step '{step}' is already recorded for "
                    f"this bundle ({exc}). Another orchestrator took it."
                ) from exc
            return Reservation(
                ledger=self, entry_id=entry_id, step=step, attempt=attempt
            )

    def _record_outcome(
        self, entry_id: str, *, verdict: str | None, run: str | None
    ) -> None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE attempts SET completed_at = ?, verdict = ?, run = ? "
                "WHERE entry_id = ?",
                (utc_now(), verdict, run, entry_id),
            ).rowcount
        if not changed:
            raise LedgerError(
                f"Ledger entry {entry_id!r} vanished before its outcome could be "
                f"recorded. {self.path} has been edited by something else."
            )

    def summary(self, bundle_sha256: str) -> dict[str, Any]:
        steps: dict[str, Any] = {}
        for entry in self.entries(bundle_sha256):
            bucket = steps.setdefault(entry["step"], {"attempts": 0, "verdicts": []})
            bucket["attempts"] += 1
            bucket["verdicts"].append(entry["verdict"])
        return {
            "ledger": str(self.path).replace("\\", "/"),
            "bundle_sha256": bundle_sha256,
            "steps": steps,
            "dangling": [e["entry_id"] for e in self.dangling(bundle_sha256)],
        }
