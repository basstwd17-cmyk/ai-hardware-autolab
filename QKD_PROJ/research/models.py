"""Closed, durable handoffs for the discovery research stage.

These objects contain what an agent concluded, not its hidden reasoning.  The
schemas keep claims small, page-cited and tied to the exact scenario digest so
that a later architecture turn can be reconstructed without a chat transcript.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping


_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
MAX_QUESTIONS = 32
MAX_SEARCH_BUDGET = 64
MAX_EXCERPT_CHARS = 2_000


class ResearchValidationError(ValueError):
    """A discovery handoff is ambiguous, ungrounded or structurally invalid."""


def scenario_sha256(scenario: str) -> str:
    if not isinstance(scenario, str) or not scenario.strip():
        raise ResearchValidationError("scenario must be a non-empty string")
    normalized = "\n".join(line.rstrip() for line in scenario.strip().splitlines())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchValidationError(f"{where} must be an object")
    return value


def _closed(value: Mapping[str, Any], required: set[str], optional: set[str], where: str) -> None:
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ResearchValidationError(f"{where} is missing {sorted(missing)}")
    if extra:
        raise ResearchValidationError(f"{where} has unknown fields {sorted(extra)}")


def _text(value: Any, where: str, *, maximum: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchValidationError(f"{where} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ResearchValidationError(f"{where} exceeds {maximum} characters")
    return result


def _identifier(value: Any, where: str) -> str:
    result = _text(value, where, maximum=64)
    if _ID.fullmatch(result) is None:
        raise ResearchValidationError(
            f"{where} must use lowercase letters, digits, dot, underscore or hyphen"
        )
    return result


def _digest(value: Any, where: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ResearchValidationError(f"{where} must be a lowercase SHA-256 digest")
    return value


def _text_list(value: Any, where: str, *, maximum: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ResearchValidationError(f"{where} must be a list of at most {maximum} strings")
    return tuple(_text(item, f"{where}[{index}]") for index, item in enumerate(value))


@dataclass(frozen=True)
class ResearchQuestion:
    question_id: str
    question: str
    why: str
    required_source_types: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str = "question") -> "ResearchQuestion":
        data = _object(value, where)
        _closed(data, {"id", "question", "why", "required_source_types"}, set(), where)
        return cls(
            _identifier(data["id"], f"{where}.id"),
            _text(data["question"], f"{where}.question"),
            _text(data["why"], f"{where}.why"),
            _text_list(data["required_source_types"], f"{where}.required_source_types", maximum=8),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.question_id,
            "question": self.question,
            "why": self.why,
            "required_source_types": list(self.required_source_types),
        }


@dataclass(frozen=True)
class ResearchPlan:
    agent: str
    scenario_sha256: str
    questions: tuple[ResearchQuestion, ...]
    completion_criteria: tuple[str, ...]
    search_budget: int

    kind = "ResearchPlan"

    @classmethod
    def parse(cls, value: Any) -> "ResearchPlan":
        data = _object(value, "ResearchPlan")
        _closed(
            data,
            {"kind", "agent", "scenario_sha256", "questions", "completion_criteria", "search_budget"},
            set(),
            "ResearchPlan",
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError("ResearchPlan.kind must equal ResearchPlan")
        raw_questions = data["questions"]
        if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= MAX_QUESTIONS:
            raise ResearchValidationError(f"ResearchPlan.questions must contain 1..{MAX_QUESTIONS} items")
        questions = tuple(
            ResearchQuestion.parse(item, f"ResearchPlan.questions[{index}]")
            for index, item in enumerate(raw_questions)
        )
        ids = [question.question_id for question in questions]
        if len(ids) != len(set(ids)):
            raise ResearchValidationError("ResearchPlan question ids must be unique")
        budget = data["search_budget"]
        if not isinstance(budget, int) or isinstance(budget, bool) or not 1 <= budget <= MAX_SEARCH_BUDGET:
            raise ResearchValidationError(
                f"ResearchPlan.search_budget must be an integer from 1 to {MAX_SEARCH_BUDGET}"
            )
        criteria = _text_list(data["completion_criteria"], "ResearchPlan.completion_criteria")
        if not criteria:
            raise ResearchValidationError("ResearchPlan.completion_criteria cannot be empty")
        return cls(
            _identifier(data["agent"], "ResearchPlan.agent"),
            _digest(data["scenario_sha256"], "ResearchPlan.scenario_sha256"),
            questions,
            criteria,
            budget,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "questions": [question.to_dict() for question in self.questions],
            "completion_criteria": list(self.completion_criteria),
            "search_budget": self.search_budget,
        }


@dataclass(frozen=True)
class Citation:
    document_id: str
    page: int
    excerpt: str
    section: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str = "citation") -> "Citation":
        data = _object(value, where)
        _closed(data, {"document_id", "page", "excerpt"}, {"section"}, where)
        page = data["page"]
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ResearchValidationError(f"{where}.page must be a positive integer")
        section = data.get("section")
        if section is not None:
            section = _text(section, f"{where}.section", maximum=256)
        return cls(
            _digest(data["document_id"], f"{where}.document_id"),
            page,
            _text(data["excerpt"], f"{where}.excerpt", maximum=MAX_EXCERPT_CHARS),
            section,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "document_id": self.document_id,
            "page": self.page,
            "excerpt": self.excerpt,
        }
        if self.section:
            result["section"] = self.section
        return result


@dataclass(frozen=True)
class SourceDocument:
    document_id: str
    title: str
    source_kind: str

    @classmethod
    def parse(cls, value: Any, where: str = "document") -> "SourceDocument":
        data = _object(value, where)
        _closed(data, {"document_id", "title", "source_kind"}, set(), where)
        return cls(
            _digest(data["document_id"], f"{where}.document_id"),
            _text(data["title"], f"{where}.title", maximum=512),
            _identifier(data["source_kind"], f"{where}.source_kind"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class SourceClaim:
    claim_id: str
    question_id: str
    statement: str
    value: Any
    unit: str | None
    citations: tuple[Citation, ...]

    @classmethod
    def parse(cls, value: Any, where: str = "claim") -> "SourceClaim":
        data = _object(value, where)
        _closed(
            data,
            {"id", "question_id", "statement", "value", "citations"},
            {"unit"},
            where,
        )
        try:
            json.dumps(data["value"], ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ResearchValidationError(f"{where}.value must be finite JSON data") from exc
        raw_citations = data["citations"]
        if not isinstance(raw_citations, list) or not raw_citations:
            raise ResearchValidationError(f"{where}.citations cannot be empty")
        citations = tuple(
            Citation.parse(item, f"{where}.citations[{index}]")
            for index, item in enumerate(raw_citations)
        )
        unit = data.get("unit")
        if unit is not None:
            unit = _text(unit, f"{where}.unit", maximum=64)
        return cls(
            _identifier(data["id"], f"{where}.id"),
            _identifier(data["question_id"], f"{where}.question_id"),
            _text(data["statement"], f"{where}.statement"),
            data["value"],
            unit,
            citations,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.claim_id,
            "question_id": self.question_id,
            "statement": self.statement,
            "value": self.value,
            "citations": [citation.to_dict() for citation in self.citations],
        }
        if self.unit:
            result["unit"] = self.unit
        return result


@dataclass(frozen=True)
class SearchTrace:
    query: str
    reason: str
    selected_document_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: Any, where: str = "query") -> "SearchTrace":
        data = _object(value, where)
        _closed(data, {"query", "reason", "selected_document_ids"}, set(), where)
        selected = _text_list(data["selected_document_ids"], f"{where}.selected_document_ids")
        for index, document_id in enumerate(selected):
            _digest(document_id, f"{where}.selected_document_ids[{index}]")
        return cls(
            _text(data["query"], f"{where}.query", maximum=512),
            _text(data["reason"], f"{where}.reason"),
            selected,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "reason": self.reason,
            "selected_document_ids": list(self.selected_document_ids),
        }


@dataclass(frozen=True)
class SourcePacket:
    agent: str
    scenario_sha256: str
    queries: tuple[SearchTrace, ...]
    documents: tuple[SourceDocument, ...]
    claims: tuple[SourceClaim, ...]
    open_questions: tuple[str, ...]

    kind = "SourcePacket"

    @classmethod
    def parse(cls, value: Any, *, plan: ResearchPlan | None = None) -> "SourcePacket":
        data = _object(value, "SourcePacket")
        _closed(
            data,
            {"kind", "agent", "scenario_sha256", "queries", "documents", "claims", "open_questions"},
            set(),
            "SourcePacket",
        )
        if data["kind"] != cls.kind:
            raise ResearchValidationError("SourcePacket.kind must equal SourcePacket")
        queries = cls._parse_many(data["queries"], SearchTrace.parse, "SourcePacket.queries")
        documents = cls._parse_many(
            data["documents"], SourceDocument.parse, "SourcePacket.documents"
        )
        claims = cls._parse_many(data["claims"], SourceClaim.parse, "SourcePacket.claims")
        packet = cls(
            _identifier(data["agent"], "SourcePacket.agent"),
            _digest(data["scenario_sha256"], "SourcePacket.scenario_sha256"),
            queries,
            documents,
            claims,
            tuple(
                _identifier(item, f"SourcePacket.open_questions[{index}]")
                for index, item in enumerate(
                    _text_list(data["open_questions"], "SourcePacket.open_questions")
                )
            ),
        )
        packet._validate_links(plan)
        return packet

    @staticmethod
    def _parse_many(value: Any, parser: Any, where: str) -> tuple[Any, ...]:
        if not isinstance(value, list) or len(value) > 128:
            raise ResearchValidationError(f"{where} must be a list of at most 128 items")
        return tuple(parser(item, f"{where}[{index}]") for index, item in enumerate(value))

    def _validate_links(self, plan: ResearchPlan | None) -> None:
        document_ids = [document.document_id for document in self.documents]
        if len(document_ids) != len(set(document_ids)):
            raise ResearchValidationError("SourcePacket document ids must be unique")
        known_documents = set(document_ids)
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ResearchValidationError("SourcePacket claim ids must be unique")
        for claim in self.claims:
            for citation in claim.citations:
                if citation.document_id not in known_documents:
                    raise ResearchValidationError(
                        f"claim {claim.claim_id} cites an undeclared document"
                    )
        for query in self.queries:
            unknown = set(query.selected_document_ids) - known_documents
            if unknown:
                raise ResearchValidationError(
                    f"query {query.query!r} selected undeclared documents {sorted(unknown)}"
                )
        if plan is None:
            return
        if self.scenario_sha256 != plan.scenario_sha256:
            raise ResearchValidationError("SourcePacket belongs to a different scenario")
        question_ids = {question.question_id for question in plan.questions}
        claimed = {claim.question_id for claim in self.claims}
        open_ids = set(self.open_questions)
        unknown = (claimed | open_ids) - question_ids
        if unknown:
            raise ResearchValidationError(f"SourcePacket names unknown questions {sorted(unknown)}")
        missing = question_ids - claimed - open_ids
        if missing:
            raise ResearchValidationError(
                f"SourcePacket neither answers nor leaves open questions {sorted(missing)}"
            )
        # A source may establish a useful partial fact without resolving the
        # whole research question.  Preserve that claim in the knowledge map
        # and keep the question open.  ArchitectureDecision still refuses the
        # `selected` state while any open question remains, so allowing this
        # overlap cannot silently promote an incomplete design.

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent": self.agent,
            "scenario_sha256": self.scenario_sha256,
            "queries": [query.to_dict() for query in self.queries],
            "documents": [document.to_dict() for document in self.documents],
            "claims": [claim.to_dict() for claim in self.claims],
            "open_questions": list(self.open_questions),
        }
