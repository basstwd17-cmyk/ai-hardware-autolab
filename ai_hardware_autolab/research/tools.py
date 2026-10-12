"""Agent-facing read-only research tools over a provider and ReferenceStore."""

from __future__ import annotations

import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import ResearchValidationError
from .providers import FetchedDocument, ResearchProvider, SearchResult
from .references import DEFAULT_MAX_BYTES, PdfExtractor, ReferenceError, ReferenceStore


_RESULT_ID = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ResearchToolPolicy:
    max_results_per_query: int = 10
    max_queries: int = 32
    max_collected_documents: int = 16
    max_reference_searches: int = 64
    max_page_reads: int = 128
    max_document_bytes: int = DEFAULT_MAX_BYTES
    fetch_timeout_seconds: float = 30.0
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.max_results_per_query <= 50:
            raise ValueError("max_results_per_query must be between 1 and 50")
        if not 1 <= self.max_queries <= 128:
            raise ValueError("max_queries must be between 1 and 128")
        if not 1 <= self.max_collected_documents <= 64:
            raise ValueError("max_collected_documents must be between 1 and 64")
        if not 1 <= self.max_reference_searches <= 512:
            raise ValueError("max_reference_searches must be between 1 and 512")
        if not 1 <= self.max_page_reads <= 1_024:
            raise ValueError("max_page_reads must be between 1 and 1024")
        if self.max_document_bytes < 1 or self.fetch_timeout_seconds <= 0:
            raise ValueError("document byte and fetch timeout limits must be positive")


class ResearchToolError(RuntimeError):
    pass


class ResearchTools:
    """The surface a Codex researcher will receive through MCP.

    `collect_source` accepts only a result id returned by `search_sources` in
    this turn.  The agent cannot turn an arbitrary URL into a fetch request.
    Every collected PDF enters the immutable reference store before it can be
    read or cited.
    """

    def __init__(
        self,
        provider: ResearchProvider,
        store: ReferenceStore,
        *,
        policy: ResearchToolPolicy | None = None,
        extractor: PdfExtractor | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.policy = policy or ResearchToolPolicy()
        self.extractor = extractor
        self._results: dict[str, SearchResult] = {}
        self._traces: list[dict[str, Any]] = []
        self._collection_calls = 0
        self._reference_searches = 0
        self._page_reads = 0

    def search_sources(self, query: str, reason: str, *, limit: int = 5) -> list[dict[str, Any]]:
        query = self._text(query, "query", maximum=512)
        reason = self._text(reason, "reason", maximum=2_000)
        if len(self._traces) >= self.policy.max_queries:
            raise ResearchToolError("research query budget is exhausted")
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ResearchToolError("limit must be an integer")
        limit = max(1, min(limit, self.policy.max_results_per_query))
        try:
            results = list(self.provider.search(query, limit=limit))
        except Exception as exc:  # noqa: BLE001 - normalize provider failures
            raise ResearchToolError(f"search provider failed: {exc}") from exc
        return self.register_search_results(query, reason, results[:limit])

    def register_search_results(
        self, query: str, reason: str, results: list[SearchResult]
    ) -> list[dict[str, Any]]:
        """Register results produced by a trusted search surface.

        Codex App Server's native web search streams its results as protocol
        events instead of calling ``ResearchProvider.search``.  Feeding those
        observed results through this method gives both paths the same URL,
        identity, budget and same-turn collection checks.
        """
        query = self._text(query, "query", maximum=512)
        reason = self._text(reason, "reason", maximum=2_000)
        if len(self._traces) >= self.policy.max_queries:
            raise ResearchToolError("research query budget is exhausted")
        if not isinstance(results, list):
            raise ResearchToolError("search results must be a list")
        normalized: list[SearchResult] = []
        for result in results[: self.policy.max_results_per_query]:
            if not isinstance(result, SearchResult):
                raise ResearchToolError("search provider returned a non-SearchResult")
            self._validate_result(result)
            self._results[result.result_id] = result
            normalized.append(result)
        self._traces.append(
            {
                "query": query,
                "reason": reason,
                "result_ids": [result.result_id for result in normalized],
                "selected_document_ids": [],
            }
        )
        return [result.public_dict() for result in normalized]

    def collect_source(self, result_id: str) -> dict[str, Any]:
        if not isinstance(result_id, str) or _RESULT_ID.fullmatch(result_id) is None:
            raise ResearchToolError("result_id must be a search result SHA-256")
        try:
            result = self._results[result_id]
        except KeyError as exc:
            raise ResearchToolError(
                "collect_source accepts only a result returned by search_sources in this turn"
            ) from exc
        if self._collection_calls >= self.policy.max_collected_documents:
            raise ResearchToolError("research document collection budget is exhausted")
        self._collection_calls += 1

        with tempfile.TemporaryDirectory(prefix=".fetch-", dir=self.store.root) as temporary:
            destination = Path(temporary) / "download.pdf"
            try:
                fetched = self.provider.fetch(
                    result,
                    destination,
                    max_bytes=self.policy.max_document_bytes,
                    timeout_seconds=self.policy.fetch_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                raise ResearchToolError(f"document provider failed: {exc}") from exc
            self._validate_fetch(fetched, destination)
            try:
                record = self.store.ingest_pdf(
                    fetched.path,
                    title=fetched.title or result.title,
                    source_kind=self.provider.name,
                    source_locator=fetched.final_url,
                    extractor=self.extractor,
                )
            except ReferenceError as exc:
                raise ResearchToolError(f"reference store refused the document: {exc}") from exc

        for trace in reversed(self._traces):
            if result_id in trace["result_ids"]:
                trace["selected_document_ids"].append(record.document_id)
                break
        return record.public_manifest()

    def read_reference_page(self, document_id: str, page: int) -> dict[str, Any]:
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ResearchToolError("page must be a positive integer")
        if self._page_reads >= self.policy.max_page_reads:
            raise ResearchToolError("reference page-read budget is exhausted")
        self._page_reads += 1
        try:
            pages = self.store.pages(document_id)
        except ReferenceError as exc:
            raise ResearchToolError(str(exc)) from exc
        if page > len(pages):
            raise ResearchToolError(f"page {page} is outside the extracted document")
        return dict(pages[page - 1])

    def search_reference(
        self, document_id: str, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        query = self._text(query, "query", maximum=512)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ResearchToolError("limit must be an integer from 1 to 50")
        if self._reference_searches >= self.policy.max_reference_searches:
            raise ResearchToolError("reference search budget is exhausted")
        self._reference_searches += 1
        try:
            pages = self.store.pages(document_id)
        except ReferenceError as exc:
            raise ResearchToolError(str(exc)) from exc
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        for page in pages:
            text = str(page.get("text") or "")
            start = text.casefold().find(needle)
            if start < 0:
                continue
            left = max(0, start - 160)
            right = min(len(text), start + len(query) + 320)
            matches.append({"page": page["page"], "excerpt": text[left:right]})
            if len(matches) >= limit:
                break
        return matches

    def trace(self) -> list[dict[str, Any]]:
        return [
            {
                "query": item["query"],
                "reason": item["reason"],
                "selected_document_ids": list(item["selected_document_ids"]),
            }
            for item in self._traces
        ]

    def _validate_result(self, result: SearchResult) -> None:
        if _RESULT_ID.fullmatch(result.result_id) is None:
            raise ResearchToolError("search result has an invalid result_id")
        if result.provider != self.provider.name:
            raise ResearchToolError("search result provider does not match the active provider")
        expected = hashlib.sha256(
            f"{result.provider}\0{result.url}".encode("utf-8")
        ).hexdigest()
        if result.result_id != expected:
            raise ResearchToolError("search result identity does not match its provider and URL")
        self._validate_url(result.url)
        if not result.title.strip():
            raise ResearchToolError("search result title is empty")

    def _validate_fetch(self, fetched: FetchedDocument, destination: Path) -> None:
        if not isinstance(fetched, FetchedDocument):
            raise ResearchToolError("provider returned a non-FetchedDocument")
        self._validate_url(fetched.final_url)
        path = Path(fetched.path).resolve(strict=True)
        if path != destination.resolve():
            raise ResearchToolError("provider returned a path outside its assigned destination")
        if path.stat().st_size > self.policy.max_document_bytes:
            raise ResearchToolError("provider returned a document larger than the policy limit")
        if fetched.media_type.split(";", 1)[0].strip().lower() != "application/pdf":
            raise ResearchToolError(f"provider returned unsupported media type {fetched.media_type!r}")

    def _validate_url(self, value: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ResearchToolError("research sources must use credential-free HTTPS URLs")
        if self.policy.allowed_hosts:
            host = parsed.hostname.lower().rstrip(".")
            allowed = any(
                host == candidate.lower().rstrip(".")
                or host.endswith("." + candidate.lower().rstrip("."))
                for candidate in self.policy.allowed_hosts
            )
            if not allowed:
                raise ResearchToolError(f"research source host is not allowed: {host}")

    @staticmethod
    def _text(value: Any, where: str, *, maximum: int) -> str:
        try:
            if not isinstance(value, str) or not value.strip():
                raise ResearchValidationError(f"{where} must be a non-empty string")
            result = value.strip()
            if len(result) > maximum:
                raise ResearchValidationError(f"{where} exceeds {maximum} characters")
            return result
        except ResearchValidationError as exc:
            raise ResearchToolError(str(exc)) from exc
