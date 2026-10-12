"""Closed contracts for durable, evidence-backed platform knowledge.

The model never writes a ``KnowledgeClaim`` directly.  It may propose a
``KnowledgeCandidate``; deterministic code resolves every ``EvidenceRef`` and
produces the claim status.  The objects contain conclusions and observable
provenance, never hidden chain-of-thought.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
MAX_TEXT = 10_000
MAX_EVIDENCE = 64
MAX_TAGS = 32

ENTITY_KINDS = (
    "project",
    "hardware",
    "component",
    "interface",
    "signal",
    "requirement",
    "question",
    "source",
    "decision",
    "artifact",
    "run",
    "failure",
    "software",
    "skill",
)
EVIDENCE_KINDS = ("document_page", "file", "contract", "run", "handoff")
EVIDENCE_ROLES = ("supports", "refutes")
DISPOSITIONS = ("assert", "refute")
CLAIM_STATUSES = ("proposed", "verified", "refuted", "superseded")


class KnowledgeValidationError(ValueError):
    """A knowledge envelope is ambiguous, unbounded or structurally invalid."""


def canonical_json(value: Any) -> str:
    """One encoding for ids, event payloads and JSON-value evidence."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise KnowledgeValidationError("value must be finite JSON data") from exc


def stable_digest(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def json_value_digest(value: Any) -> str:
    """Digest a selected JSON value independently of its surrounding file."""

    return stable_digest(value)


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise KnowledgeValidationError(f"{where} must be an object")
    return value


def _closed(value: Mapping[str, Any], required: set[str], optional: set[str], where: str) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise KnowledgeValidationError(f"{where} is missing {sorted(missing)}")
    if extra:
        raise KnowledgeValidationError(f"{where} has unknown fields {sorted(extra)}")


def _text(value: Any, where: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeValidationError(f"{where} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise KnowledgeValidationError(f"{where} exceeds {maximum} characters")
    return result


def _optional_text(value: Any, where: str, *, maximum: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    return _text(value, where, maximum=maximum)


def _identifier(value: Any, where: str) -> str:
    result = _text(value, where, maximum=128)
    if _ID.fullmatch(result) is None:
        raise KnowledgeValidationError(
            f"{where} must use lowercase letters, digits, dot, underscore or hyphen"
        )
    return result


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise KnowledgeValidationError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _finite_json(value: Any, where: str) -> Any:
    try:
        canonical_json(value)
    except KnowledgeValidationError as exc:
        raise KnowledgeValidationError(f"{where} must be finite JSON data") from exc
    return value


def _relative_locator(value: Any, where: str) -> str:
    raw = _text(value, where, maximum=1_000).replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise KnowledgeValidationError(f"{where} must stay inside the project root")
    return str(path)


@dataclass(frozen=True)
class KnowledgeScope:
    """Where a fact is valid.  All ``None`` is deliberately global."""

    project_id: str | None = None
    hardware_id: str | None = None
    revision: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str = "scope") -> "KnowledgeScope":
        data = _object(value, where)
        _closed(data, set(), {"project_id", "hardware_id", "revision"}, where)
        project = data.get("project_id")
        hardware = data.get("hardware_id")
        revision = data.get("revision")
        project = _identifier(project, f"{where}.project_id") if project is not None else None
        hardware = _identifier(hardware, f"{where}.hardware_id") if hardware is not None else None
        revision = _identifier(revision, f"{where}.revision") if revision is not None else None
        if revision is not None and hardware is None:
            raise KnowledgeValidationError(f"{where}.revision requires hardware_id")
        return cls(project, hardware, revision)

    @property
    def key(self) -> str:
        parts = []
        if self.project_id:
            parts.append(f"project:{self.project_id}")
        if self.hardware_id:
            parts.append(f"hardware:{self.hardware_id}")
        if self.revision:
            parts.append(f"revision:{self.revision}")
        return "|".join(parts) or "global"

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.project_id:
            result["project_id"] = self.project_id
        if self.hardware_id:
            result["hardware_id"] = self.hardware_id
        if self.revision:
            result["revision"] = self.revision
        return result


@dataclass(frozen=True)
class KnowledgeEntity:
    kind: str
    entity_id: str
    label: str

    @classmethod
    def parse(cls, value: Any, where: str = "entity") -> "KnowledgeEntity":
        data = _object(value, where)
        _closed(data, {"kind", "id", "label"}, set(), where)
        kind = _identifier(data["kind"], f"{where}.kind")
        if kind not in ENTITY_KINDS:
            raise KnowledgeValidationError(f"{where}.kind must be one of {list(ENTITY_KINDS)}")
        return cls(
            kind,
            _identifier(data["id"], f"{where}.id"),
            _text(data["label"], f"{where}.label", maximum=512),
        )

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.entity_id}"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.entity_id, "label": self.label}


@dataclass(frozen=True)
class KnowledgeObject:
    entity: KnowledgeEntity | None = None
    value: Any = None
    unit: str | None = None
    literal: bool = False

    @classmethod
    def parse(cls, value: Any, where: str = "object") -> "KnowledgeObject":
        data = _object(value, where)
        kind = data.get("kind")
        if kind == "entity":
            _closed(data, {"kind", "entity"}, set(), where)
            return cls(entity=KnowledgeEntity.parse(data["entity"], f"{where}.entity"))
        if kind == "literal":
            _closed(data, {"kind", "value"}, {"unit"}, where)
            unit = _optional_text(data.get("unit"), f"{where}.unit", maximum=64)
            return cls(value=_finite_json(data["value"], f"{where}.value"), unit=unit, literal=True)
        raise KnowledgeValidationError(f"{where}.kind must be entity or literal")

    def to_dict(self) -> dict[str, Any]:
        if self.entity is not None:
            return {"kind": "entity", "entity": self.entity.to_dict()}
        result = {"kind": "literal", "value": self.value}
        if self.unit:
            result["unit"] = self.unit
        return result

    @property
    def display(self) -> str:
        if self.entity is not None:
            return self.entity.label
        shown = canonical_json(self.value)
        return f"{shown} {self.unit}" if self.unit else shown


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    role: str
    locator: str
    sha256: str
    page: int | None = None
    excerpt: str | None = None
    json_pointer: str | None = None
    value_sha256: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str = "evidence") -> "EvidenceRef":
        data = _object(value, where)
        _closed(
            data,
            {"kind", "role", "locator", "sha256"},
            {"page", "excerpt", "json_pointer", "value_sha256"},
            where,
        )
        kind = _identifier(data["kind"], f"{where}.kind")
        role = _identifier(data["role"], f"{where}.role")
        if kind not in EVIDENCE_KINDS:
            raise KnowledgeValidationError(f"{where}.kind must be one of {list(EVIDENCE_KINDS)}")
        if role not in EVIDENCE_ROLES:
            raise KnowledgeValidationError(f"{where}.role must be one of {list(EVIDENCE_ROLES)}")
        locator = _text(data["locator"], f"{where}.locator", maximum=1_000)
        digest = _digest(data["sha256"], f"{where}.sha256")
        page = data.get("page")
        excerpt = data.get("excerpt")
        pointer = data.get("json_pointer")
        value_digest = data.get("value_sha256")
        if kind == "document_page":
            if locator != digest:
                raise KnowledgeValidationError(f"{where}.locator must equal the document digest")
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise KnowledgeValidationError(f"{where}.page must be a positive integer")
            excerpt = _text(excerpt, f"{where}.excerpt", maximum=2_000)
            if pointer is not None or value_digest is not None:
                raise KnowledgeValidationError(f"{where} document pages cannot use JSON pointers")
        else:
            if page is not None or excerpt is not None:
                raise KnowledgeValidationError(f"{where}.{kind} cannot use page or excerpt")
            if kind in ("file", "contract", "run"):
                locator = _relative_locator(locator, f"{where}.locator")
            if pointer is not None:
                if not isinstance(pointer, str) or len(pointer) > 1_000:
                    raise KnowledgeValidationError(
                        f"{where}.json_pointer must be a string of at most 1000 characters"
                    )
                if not pointer.startswith("/") and pointer != "":
                    raise KnowledgeValidationError(f"{where}.json_pointer must be an RFC 6901 pointer")
                value_digest = _digest(value_digest, f"{where}.value_sha256")
            elif value_digest is not None:
                raise KnowledgeValidationError(f"{where}.value_sha256 requires json_pointer")
        return cls(kind, role, locator, digest, page, excerpt, pointer, value_digest)

    @property
    def evidence_id(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "role": self.role,
            "locator": self.locator,
            "sha256": self.sha256,
        }
        if self.page is not None:
            result["page"] = self.page
        if self.excerpt is not None:
            result["excerpt"] = self.excerpt
        if self.json_pointer is not None:
            result["json_pointer"] = self.json_pointer
            result["value_sha256"] = self.value_sha256
        return result


@dataclass(frozen=True)
class KnowledgeCandidate:
    producer: str
    scope: KnowledgeScope
    subject: KnowledgeEntity
    relation: str
    object: KnowledgeObject
    statement: str
    disposition: str
    evidence: tuple[EvidenceRef, ...]
    source_job_id: str | None = None
    tags: tuple[str, ...] = ()

    kind = "KnowledgeCandidate"

    @classmethod
    def parse(cls, value: Any) -> "KnowledgeCandidate":
        data = _object(value, cls.kind)
        _closed(
            data,
            {
                "kind",
                "producer",
                "scope",
                "subject",
                "relation",
                "object",
                "statement",
                "disposition",
                "evidence",
            },
            {"source_job_id", "tags"},
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise KnowledgeValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        disposition = _identifier(data["disposition"], f"{cls.kind}.disposition")
        if disposition not in DISPOSITIONS:
            raise KnowledgeValidationError(
                f"{cls.kind}.disposition must be one of {list(DISPOSITIONS)}"
            )
        raw_evidence = data["evidence"]
        if not isinstance(raw_evidence, list) or len(raw_evidence) > MAX_EVIDENCE:
            raise KnowledgeValidationError(
                f"{cls.kind}.evidence must be a list of at most {MAX_EVIDENCE} items"
            )
        evidence = tuple(
            EvidenceRef.parse(item, f"{cls.kind}.evidence[{index}]")
            for index, item in enumerate(raw_evidence)
        )
        ids = [item.evidence_id for item in evidence]
        if len(ids) != len(set(ids)):
            raise KnowledgeValidationError(f"{cls.kind}.evidence must not contain duplicates")
        raw_tags = data.get("tags", [])
        if not isinstance(raw_tags, list) or len(raw_tags) > MAX_TAGS:
            raise KnowledgeValidationError(f"{cls.kind}.tags must be a list of at most {MAX_TAGS}")
        tags = tuple(_identifier(item, f"{cls.kind}.tags[{i}]") for i, item in enumerate(raw_tags))
        if len(tags) != len(set(tags)):
            raise KnowledgeValidationError(f"{cls.kind}.tags must be unique")
        source_job = _optional_text(data.get("source_job_id"), f"{cls.kind}.source_job_id", maximum=128)
        return cls(
            _identifier(data["producer"], f"{cls.kind}.producer"),
            KnowledgeScope.parse(data["scope"], f"{cls.kind}.scope"),
            KnowledgeEntity.parse(data["subject"], f"{cls.kind}.subject"),
            _identifier(data["relation"], f"{cls.kind}.relation"),
            KnowledgeObject.parse(data["object"], f"{cls.kind}.object"),
            _text(data["statement"], f"{cls.kind}.statement"),
            disposition,
            evidence,
            source_job,
            tags,
        )

    @property
    def claim_id(self) -> str:
        subject = {"kind": self.subject.kind, "id": self.subject.entity_id}
        if self.object.entity is not None:
            object_value: dict[str, Any] = {
                "kind": "entity",
                "entity": {
                    "kind": self.object.entity.kind,
                    "id": self.object.entity.entity_id,
                },
            }
        else:
            object_value = self.object.to_dict()
        return stable_digest(
            {
                "scope": self.scope.to_dict(),
                "subject": subject,
                "relation": self.relation,
                "object": object_value,
            }
        )

    @property
    def version_id(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "producer": self.producer,
            "scope": self.scope.to_dict(),
            "subject": self.subject.to_dict(),
            "relation": self.relation,
            "object": self.object.to_dict(),
            "statement": self.statement,
            "disposition": self.disposition,
            "evidence": [item.to_dict() for item in self.evidence],
            "tags": list(self.tags),
        }
        if self.source_job_id:
            result["source_job_id"] = self.source_job_id
        return result


@dataclass(frozen=True)
class EvidenceCheck:
    evidence_id: str
    ok: bool
    code: str
    detail: str

    @classmethod
    def parse(cls, value: Any, where: str = "check") -> "EvidenceCheck":
        data = _object(value, where)
        _closed(data, {"evidence_id", "ok", "code", "detail"}, set(), where)
        if not isinstance(data["ok"], bool):
            raise KnowledgeValidationError(f"{where}.ok must be a boolean")
        return cls(
            _digest(data["evidence_id"], f"{where}.evidence_id"),
            data["ok"],
            _identifier(data["code"], f"{where}.code"),
            _text(data["detail"], f"{where}.detail", maximum=2_000),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "ok": self.ok,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class KnowledgeClaim:
    candidate: KnowledgeCandidate
    status: str
    checks: tuple[EvidenceCheck, ...]

    kind = "KnowledgeClaim"

    @classmethod
    def parse(cls, value: Any) -> "KnowledgeClaim":
        data = _object(value, cls.kind)
        _closed(
            data,
            {"kind", "claim_id", "version_id", "status", "candidate", "checks"},
            set(),
            cls.kind,
        )
        if data["kind"] != cls.kind:
            raise KnowledgeValidationError(f"{cls.kind}.kind must equal {cls.kind}")
        candidate = KnowledgeCandidate.parse(data["candidate"])
        if _digest(data["claim_id"], f"{cls.kind}.claim_id") != candidate.claim_id:
            raise KnowledgeValidationError(f"{cls.kind}.claim_id does not match its candidate")
        if _digest(data["version_id"], f"{cls.kind}.version_id") != candidate.version_id:
            raise KnowledgeValidationError(f"{cls.kind}.version_id does not match its candidate")
        status = _identifier(data["status"], f"{cls.kind}.status")
        if status not in CLAIM_STATUSES:
            raise KnowledgeValidationError(f"{cls.kind}.status must be one of {list(CLAIM_STATUSES)}")
        raw_checks = data["checks"]
        if not isinstance(raw_checks, list) or len(raw_checks) > MAX_EVIDENCE:
            raise KnowledgeValidationError(f"{cls.kind}.checks must be a bounded list")
        checks = tuple(
            EvidenceCheck.parse(item, f"{cls.kind}.checks[{index}]")
            for index, item in enumerate(raw_checks)
        )
        expected = [item.evidence_id for item in candidate.evidence]
        if [item.evidence_id for item in checks] != expected:
            raise KnowledgeValidationError(f"{cls.kind}.checks must match evidence in order")
        if status in ("verified", "refuted") and (not checks or not all(c.ok for c in checks)):
            raise KnowledgeValidationError(f"{cls.kind}.{status} requires verified evidence")
        if status == "verified" and (
            candidate.disposition != "assert"
            or any(item.role != "supports" for item in candidate.evidence)
        ):
            raise KnowledgeValidationError(f"{cls.kind}.verified requires supporting assertion evidence")
        if status == "refuted" and (
            candidate.disposition != "refute"
            or any(item.role != "refutes" for item in candidate.evidence)
        ):
            raise KnowledgeValidationError(f"{cls.kind}.refuted requires refuting evidence")
        return cls(candidate, status, checks)

    @property
    def claim_id(self) -> str:
        return self.candidate.claim_id

    @property
    def version_id(self) -> str:
        return self.candidate.version_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "claim_id": self.claim_id,
            "version_id": self.version_id,
            "status": self.status,
            "candidate": self.candidate.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
        }


def parse_claims(values: Sequence[Any]) -> tuple[KnowledgeClaim, ...]:
    return tuple(KnowledgeClaim.parse(value) for value in values)
