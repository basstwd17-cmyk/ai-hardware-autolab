"""Durable, progress-sensitive research budgeting for scenario missions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


RESEARCH_PROGRESS = "research.progress"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def normalize_query(value: str) -> str:
    return " ".join(str(value).casefold().split())


def canonical_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit(
        (parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path or "/", query, "")
    )


def fact_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResearchBudgetPolicy:
    soft_queries_per_turn: int = 12
    hard_queries_per_job: int = 64
    hard_time_limit_s: float = 3600.0
    max_stagnant_rounds: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.soft_queries_per_turn <= self.hard_queries_per_job:
            raise ValueError("research soft limit must be positive and no larger than hard limit")
        if self.max_stagnant_rounds < 1:
            raise ValueError("max_stagnant_rounds must be positive")
        if self.hard_time_limit_s <= 0:
            raise ValueError("hard_time_limit_s must be positive")


@dataclass
class ResearchBudgetState:
    queries_used: int = 0
    query_keys: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    documents: set[str] = field(default_factory=set)
    fact_hashes: set[str] = field(default_factory=set)
    stagnant_rounds: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    elapsed_s: float = 0.0

    @classmethod
    def from_events(cls, events: Iterable[Any]) -> "ResearchBudgetState":
        state = cls()
        for event in events:
            kind = event.kind if hasattr(event, "kind") else event.get("kind")
            if kind != RESEARCH_PROGRESS:
                continue
            payload = event.payload if hasattr(event, "payload") else event.get("payload", {})
            if not isinstance(payload, dict):
                continue
            queries = [
                normalize_query(str(item))
                for item in payload.get("queries", [])
                if str(item).strip()
            ]
            used = payload.get("queries_used")
            state.queries_used += (
                used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else len(queries)
            )
            state.query_keys.update(queries)
            state.urls.update(
                canonical_url(str(item))
                for item in payload.get("urls", [])
                if str(item).strip()
            )
            state.documents.update(str(item) for item in payload.get("document_ids", []) if item)
            added = {
                str(item)
                for item in payload.get("new_fact_hashes", [])
                if _SHA256.fullmatch(str(item))
            }
            if added - state.fact_hashes:
                state.stagnant_rounds = 0
            else:
                state.stagnant_rounds += 1
            state.fact_hashes.update(added)
            usage = payload.get("tokens")
            if isinstance(usage, dict):
                state.input_tokens = _sum_optional(state.input_tokens, usage.get("input"))
                state.output_tokens = _sum_optional(state.output_tokens, usage.get("output"))
            elapsed = payload.get("elapsed_s")
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 0:
                state.elapsed_s += float(elapsed)
        return state

    def remaining(self, policy: ResearchBudgetPolicy) -> int:
        if self.elapsed_s >= policy.hard_time_limit_s:
            return 0
        return max(0, policy.hard_queries_per_job - self.queries_used)

    def turn_limit(self, policy: ResearchBudgetPolicy) -> int:
        return min(policy.soft_queries_per_turn, self.remaining(policy))

    def describe(self, policy: ResearchBudgetPolicy) -> dict[str, Any]:
        return {
            "queries_used": self.queries_used,
            "queries_remaining": self.remaining(policy),
            "hard_query_limit": policy.hard_queries_per_job,
            "unique_queries": len(self.query_keys),
            "unique_urls": len(self.urls),
            "collected_documents": len(self.documents),
            "fact_count": len(self.fact_hashes),
            "stagnant_rounds": self.stagnant_rounds,
            "elapsed_s": self.elapsed_s,
            "hard_time_limit_s": policy.hard_time_limit_s,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
        }


def _sum_optional(current: int | None, value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return current
    return (current or 0) + value
