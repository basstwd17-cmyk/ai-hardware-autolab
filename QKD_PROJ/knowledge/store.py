"""Append-only knowledge events with rebuildable SQLite projections and FTS."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from QKD_PROJ.hwtools.result import utc_now

from .models import (
    CLAIM_STATUSES,
    KnowledgeClaim,
    KnowledgeScope,
    canonical_json,
    stable_digest,
)


SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 20_000
CURRENT_STATUSES = ("proposed", "verified", "refuted", "superseded")


class KnowledgeStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeEvent:
    event_id: int
    at: str
    kind: str
    claim_id: str
    version_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "at": self.at,
            "kind": self.kind,
            "claim_id": self.claim_id,
            "version_id": self.version_id,
            "payload": self.payload,
        }


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_schema_info (
        version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_events (
        event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        at         TEXT NOT NULL,
        kind       TEXT NOT NULL,
        claim_id   TEXT NOT NULL,
        version_id TEXT NOT NULL,
        payload    TEXT NOT NULL,
        UNIQUE (kind, version_id),
        CHECK (kind IN (
            'knowledge.proposed', 'knowledge.verified',
            'knowledge.refuted', 'knowledge.superseded'
        ))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_claims (
        claim_id          TEXT PRIMARY KEY,
        current_version   TEXT NOT NULL,
        status            TEXT NOT NULL,
        project_id        TEXT,
        hardware_id       TEXT,
        revision          TEXT,
        subject_key       TEXT NOT NULL,
        subject_kind      TEXT NOT NULL,
        subject_label     TEXT NOT NULL,
        relation          TEXT NOT NULL,
        object_entity_key TEXT,
        object_text       TEXT NOT NULL,
        statement         TEXT NOT NULL,
        producer          TEXT NOT NULL,
        source_job_id     TEXT,
        tags              TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        payload           TEXT NOT NULL,
        CHECK (status IN ('proposed', 'verified', 'refuted', 'superseded'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_claims_scope
      ON knowledge_claims(project_id, hardware_id, revision, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_claims_subject
      ON knowledge_claims(subject_key, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_claims_object
      ON knowledge_claims(object_entity_key, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_evidence (
        claim_id     TEXT NOT NULL REFERENCES knowledge_claims(claim_id) ON DELETE CASCADE,
        ordinal      INTEGER NOT NULL,
        evidence_id  TEXT NOT NULL,
        kind         TEXT NOT NULL,
        role         TEXT NOT NULL,
        locator      TEXT NOT NULL,
        page         INTEGER,
        ok           INTEGER NOT NULL,
        code         TEXT NOT NULL,
        PRIMARY KEY (claim_id, ordinal)
    )
    """,
)


class KnowledgeStore:
    """The event log is durable; claims, evidence rows and FTS are projections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts = True
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            for statement in SCHEMA:
                connection.execute(statement)
            rows = connection.execute("SELECT version FROM knowledge_schema_info").fetchall()
            if not rows:
                connection.execute(
                    "INSERT INTO knowledge_schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
                raise KnowledgeStoreError("unsupported knowledge schema version")
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                        claim_id UNINDEXED, statement, subject, object_text, tags,
                        tokenize='unicode61'
                    )
                    """
                )
                self._fts = True
            except sqlite3.OperationalError:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS knowledge_fts_fallback (
                        claim_id TEXT PRIMARY KEY,
                        statement TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        object_text TEXT NOT NULL,
                        tags TEXT NOT NULL
                    )
                    """
                )
                self._fts = False

    def record(self, claim: KnowledgeClaim) -> KnowledgeEvent:
        """Append one reduction and update the current projection atomically.

        A proposed revision never downgrades an already accepted claim.  It is
        still retained in the event log, so later evidence can be diagnosed.
        """

        # A hand-constructed dataclass must satisfy the same closed invariants
        # as a claim replayed from disk.
        claim = KnowledgeClaim.parse(claim.to_dict())
        kind = f"knowledge.{claim.status}"
        if claim.status == "superseded":
            raise KnowledgeStoreError("superseded is recorded through supersede()")
        payload = claim.to_dict()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM knowledge_events WHERE kind = ? AND version_id = ?",
                (kind, claim.version_id),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _event(existing)
            at = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO knowledge_events(at, kind, claim_id, version_id, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (at, kind, claim.claim_id, claim.version_id, canonical_json(payload)),
            )
            current = connection.execute(
                "SELECT status FROM knowledge_claims WHERE claim_id = ?", (claim.claim_id,)
            ).fetchone()
            if current is None or (
                current["status"] != "superseded" and claim.status != "proposed"
            ):
                self._apply_claim(connection, claim, at)
            event_id = int(cursor.lastrowid)
            connection.commit()
        return KnowledgeEvent(event_id, at, kind, claim.claim_id, claim.version_id, payload)

    def supersede(self, old_claim_id: str, new_claim_id: str, reason: str) -> KnowledgeEvent:
        if old_claim_id == new_claim_id:
            raise KnowledgeStoreError("a claim cannot supersede itself")
        reason = reason.strip()
        if not reason or len(reason) > 2_000:
            raise KnowledgeStoreError("supersession reason must contain 1..2000 characters")
        payload = {
            "old_claim_id": old_claim_id,
            "new_claim_id": new_claim_id,
            "reason": reason,
        }
        version = stable_digest(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM knowledge_events WHERE kind='knowledge.superseded' AND version_id=?",
                (version,),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _event(existing)
            old = connection.execute(
                "SELECT status FROM knowledge_claims WHERE claim_id=?", (old_claim_id,)
            ).fetchone()
            new = connection.execute(
                "SELECT status FROM knowledge_claims WHERE claim_id=?", (new_claim_id,)
            ).fetchone()
            if old is None or new is None:
                raise KnowledgeStoreError("both old and new claims must already exist")
            if old["status"] == "superseded":
                raise KnowledgeStoreError("the old claim was already superseded")
            if new["status"] not in ("verified", "refuted"):
                raise KnowledgeStoreError("the replacement claim must have verified evidence")
            at = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO knowledge_events(at, kind, claim_id, version_id, payload)
                VALUES (?, 'knowledge.superseded', ?, ?, ?)
                """,
                (at, old_claim_id, version, canonical_json(payload)),
            )
            connection.execute(
                "UPDATE knowledge_claims SET status='superseded', updated_at=? WHERE claim_id=?",
                (at, old_claim_id),
            )
            connection.commit()
        return KnowledgeEvent(
            int(cursor.lastrowid), at, "knowledge.superseded", old_claim_id, version, payload
        )

    def claim(self, claim_id: str) -> KnowledgeClaim | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, payload FROM knowledge_claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
        return _projected_claim(row) if row is not None else None

    def claims(
        self,
        *,
        scope: KnowledgeScope | None = None,
        statuses: Sequence[str] = ("verified", "refuted"),
        limit: int = 100,
    ) -> list[KnowledgeClaim]:
        statuses = _statuses(statuses)
        limit = _limit(limit)
        where, parameters = _scope_where(scope)
        marks = ",".join("?" for _ in statuses)
        sql = f"""
            SELECT status, payload FROM knowledge_claims
            WHERE status IN ({marks}) AND {where}
            ORDER BY updated_at DESC, claim_id
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, [*statuses, *parameters, limit]).fetchall()
        return [_projected_claim(row) for row in rows]

    def search(
        self,
        text: str,
        *,
        scope: KnowledgeScope | None = None,
        statuses: Sequence[str] = ("verified", "refuted"),
        limit: int = 20,
    ) -> list[KnowledgeClaim]:
        text = text.strip()
        if not text:
            return self.claims(scope=scope, statuses=statuses, limit=limit)
        statuses = _statuses(statuses)
        limit = _limit(limit)
        where, parameters = _scope_where(scope, alias="c")
        marks = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            if self._fts:
                query = _fts_query(text)
                if not query:
                    return []
                rows = connection.execute(
                    f"""
                    SELECT c.status, c.payload
                    FROM knowledge_fts
                    JOIN knowledge_claims c ON c.claim_id = knowledge_fts.claim_id
                    WHERE knowledge_fts MATCH ?
                      AND c.status IN ({marks}) AND {where}
                    ORDER BY bm25(knowledge_fts), c.updated_at DESC
                    LIMIT ?
                    """,
                    [query, *statuses, *parameters, limit],
                ).fetchall()
            else:
                like = f"%{text}%"
                rows = connection.execute(
                    f"""
                    SELECT c.status, c.payload
                    FROM knowledge_fts_fallback f
                    JOIN knowledge_claims c ON c.claim_id=f.claim_id
                    WHERE (f.statement LIKE ? OR f.subject LIKE ? OR f.object_text LIKE ? OR f.tags LIKE ?)
                      AND c.status IN ({marks}) AND {where}
                    ORDER BY c.updated_at DESC LIMIT ?
                    """,
                    [like, like, like, like, *statuses, *parameters, limit],
                ).fetchall()
        return [_projected_claim(row) for row in rows]

    def neighbors(
        self,
        entity_key: str,
        *,
        scope: KnowledgeScope | None = None,
        statuses: Sequence[str] = ("verified", "refuted"),
        limit: int = 20,
    ) -> list[KnowledgeClaim]:
        statuses = _statuses(statuses)
        limit = _limit(limit)
        where, parameters = _scope_where(scope)
        marks = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT status, payload FROM knowledge_claims
                WHERE (subject_key=? OR object_entity_key=?)
                  AND status IN ({marks}) AND {where}
                ORDER BY updated_at DESC LIMIT ?
                """,
                [entity_key, entity_key, *statuses, *parameters, limit],
            ).fetchall()
        return [_projected_claim(row) for row in rows]

    def events_since(self, cursor: int = 0, limit: int = 1_000) -> list[KnowledgeEvent]:
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise KnowledgeStoreError("cursor must be a non-negative integer")
        limit = _limit(limit, maximum=10_000)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_events WHERE event_id>? ORDER BY event_id LIMIT ?",
                (cursor, limit),
            ).fetchall()
        return [_event(row) for row in rows]

    def latest_event_id(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) AS event_id FROM knowledge_events"
            ).fetchone()
        return int(row["event_id"])

    def rebuild(self) -> None:
        """Discard every projection and replay the immutable event stream."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            events = connection.execute("SELECT * FROM knowledge_events ORDER BY event_id").fetchall()
            connection.execute("DELETE FROM knowledge_evidence")
            connection.execute("DELETE FROM knowledge_claims")
            self._clear_fts(connection)
            for row in events:
                payload = json.loads(row["payload"])
                if row["kind"] == "knowledge.superseded":
                    connection.execute(
                        "UPDATE knowledge_claims SET status='superseded', updated_at=? WHERE claim_id=?",
                        (row["at"], payload["old_claim_id"]),
                    )
                    continue
                claim = KnowledgeClaim.parse(payload)
                current = connection.execute(
                    "SELECT status FROM knowledge_claims WHERE claim_id=?", (claim.claim_id,)
                ).fetchone()
                if current is None or (
                    current["status"] != "superseded" and claim.status != "proposed"
                ):
                    self._apply_claim(connection, claim, row["at"])
            connection.commit()

    def _apply_claim(self, connection: sqlite3.Connection, claim: KnowledgeClaim, at: str) -> None:
        candidate = claim.candidate
        object_entity = candidate.object.entity.key if candidate.object.entity else None
        payload = canonical_json(claim.to_dict())
        connection.execute(
            """
            INSERT INTO knowledge_claims(
                claim_id, current_version, status, project_id, hardware_id, revision,
                subject_key, subject_kind, subject_label, relation, object_entity_key,
                object_text, statement, producer, source_job_id, tags, updated_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(claim_id) DO UPDATE SET
                current_version=excluded.current_version, status=excluded.status,
                project_id=excluded.project_id, hardware_id=excluded.hardware_id,
                revision=excluded.revision, subject_key=excluded.subject_key,
                subject_kind=excluded.subject_kind, subject_label=excluded.subject_label,
                relation=excluded.relation, object_entity_key=excluded.object_entity_key,
                object_text=excluded.object_text, statement=excluded.statement,
                producer=excluded.producer, source_job_id=excluded.source_job_id,
                tags=excluded.tags, updated_at=excluded.updated_at, payload=excluded.payload
            """,
            (
                claim.claim_id,
                claim.version_id,
                claim.status,
                candidate.scope.project_id,
                candidate.scope.hardware_id,
                candidate.scope.revision,
                candidate.subject.key,
                candidate.subject.kind,
                candidate.subject.label,
                candidate.relation,
                object_entity,
                candidate.object.display,
                candidate.statement,
                candidate.producer,
                candidate.source_job_id,
                " ".join(candidate.tags),
                at,
                payload,
            ),
        )
        connection.execute("DELETE FROM knowledge_evidence WHERE claim_id=?", (claim.claim_id,))
        for ordinal, (evidence, check) in enumerate(zip(candidate.evidence, claim.checks)):
            connection.execute(
                """
                INSERT INTO knowledge_evidence(
                    claim_id, ordinal, evidence_id, kind, role, locator, page, ok, code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.claim_id,
                    ordinal,
                    evidence.evidence_id,
                    evidence.kind,
                    evidence.role,
                    evidence.locator,
                    evidence.page,
                    int(check.ok),
                    check.code,
                ),
            )
        self._index_claim(connection, claim)

    def _index_claim(self, connection: sqlite3.Connection, claim: KnowledgeClaim) -> None:
        if self._fts:
            connection.execute("DELETE FROM knowledge_fts WHERE claim_id=?", (claim.claim_id,))
            connection.execute(
                "INSERT INTO knowledge_fts(claim_id, statement, subject, object_text, tags) VALUES (?, ?, ?, ?, ?)",
                (
                    claim.claim_id,
                    claim.candidate.statement,
                    claim.candidate.subject.label,
                    claim.candidate.object.display,
                    " ".join(claim.candidate.tags),
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO knowledge_fts_fallback(claim_id, statement, subject, object_text, tags)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(claim_id) DO UPDATE SET statement=excluded.statement,
                    subject=excluded.subject, object_text=excluded.object_text, tags=excluded.tags
                """,
                (
                    claim.claim_id,
                    claim.candidate.statement,
                    claim.candidate.subject.label,
                    claim.candidate.object.display,
                    " ".join(claim.candidate.tags),
                ),
            )

    def _clear_fts(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM knowledge_fts" if self._fts else "DELETE FROM knowledge_fts_fallback")


def _event(row: sqlite3.Row) -> KnowledgeEvent:
    return KnowledgeEvent(
        int(row["event_id"]),
        str(row["at"]),
        str(row["kind"]),
        str(row["claim_id"]),
        str(row["version_id"]),
        json.loads(row["payload"]),
    )


def _projected_claim(row: sqlite3.Row) -> KnowledgeClaim:
    claim = KnowledgeClaim.parse(json.loads(row["payload"]))
    status = str(row["status"])
    return claim if claim.status == status else KnowledgeClaim(claim.candidate, status, claim.checks)


def _statuses(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not values:
        raise KnowledgeStoreError("statuses must be a non-empty sequence")
    result = tuple(values)
    if any(value not in CLAIM_STATUSES for value in result):
        raise KnowledgeStoreError("unknown knowledge status")
    return result


def _limit(value: int, maximum: int = 1_000) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise KnowledgeStoreError(f"limit must be an integer from 1 to {maximum}")
    return value


def _scope_where(scope: KnowledgeScope | None, alias: str = "") -> tuple[str, list[Any]]:
    prefix = f"{alias}." if alias else ""
    if scope is None:
        return "1=1", []
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("project_id", scope.project_id),
        ("hardware_id", scope.hardware_id),
        ("revision", scope.revision),
    ):
        if value is None:
            clauses.append(f"{prefix}{column} IS NULL")
        else:
            clauses.append(f"({prefix}{column} IS NULL OR {prefix}{column}=?)")
            values.append(value)
    return " AND ".join(clauses), values


def _fts_query(text: str) -> str:
    tokens = [token for token in re.findall(r"[\w.-]+", text, flags=re.UNICODE) if token]
    # A scenario is a sentence, not a document title. Requiring every word to
    # occur discarded a precise PRU timing fact merely because the request
    # also said "use", "while" and "planning". BM25 still ranks claims that
    # match several terms first; OR makes partial relevance retrievable.
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:32])
