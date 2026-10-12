"""Agent-facing search, collection and page-reading boundary."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.research.providers import FetchedDocument, SearchResult  # noqa: E402
from ai_hardware_autolab.research.references import ExtractionResult, ReferenceStore  # noqa: E402
from ai_hardware_autolab.research.tools import (  # noqa: E402
    ResearchToolError,
    ResearchToolPolicy,
    ResearchTools,
)


class FakeExtractor:
    name = "fake-pdf"

    def extract(self, source: Path) -> ExtractionResult:
        assert source.read_bytes().startswith(b"%PDF-")
        text = "The DAC data setup time is 2 ns. The hold time is 1 ns."
        return ExtractionResult(
            [
                {
                    "page": 1,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "characters": len(text),
                    "status": "ok",
                }
            ]
        )


class FakeProvider:
    name = "official-web"

    def __init__(self, *, url: str = "https://docs.example.test/dac.pdf") -> None:
        self.result = SearchResult.create(
            provider=self.name,
            title="DAC manufacturer datasheet",
            url=url,
            snippet="Timing specifications",
            media_type="application/pdf",
        )
        self.searches: list[tuple[str, int]] = []
        self.fetches: list[str] = []

    def search(self, query: str, *, limit: int):
        self.searches.append((query, limit))
        return [self.result]

    def fetch(
        self,
        result: SearchResult,
        destination: Path,
        *,
        max_bytes: int,
        timeout_seconds: float,
    ) -> FetchedDocument:
        self.fetches.append(result.result_id)
        content = b"%PDF-1.7\nofficial datasheet"
        assert len(content) < max_bytes and timeout_seconds > 0
        destination.write_bytes(content)
        return FetchedDocument(destination, result.url, "application/pdf", result.title)


def tools(tmp_path: Path, provider: FakeProvider | None = None) -> ResearchTools:
    return ResearchTools(
        provider or FakeProvider(),
        ReferenceStore(tmp_path / "references"),
        policy=ResearchToolPolicy(allowed_hosts=("example.test",)),
        extractor=FakeExtractor(),
    )


def test_agent_selects_a_search_result_then_collects_and_reads_it(tmp_path: Path):
    research = tools(tmp_path)

    results = research.search_sources(
        "official DAC setup hold datasheet",
        "the 5 ns architecture needs sourced setup and hold limits",
    )
    manifest = research.collect_source(str(results[0]["result_id"]))
    matches = research.search_reference(str(manifest["document_id"]), "setup time")
    page = research.read_reference_page(str(manifest["document_id"]), 1)

    assert results[0]["untrusted"] is True
    assert matches[0]["page"] == 1 and "setup time is 2 ns" in matches[0]["excerpt"]
    assert page["text"].startswith("The DAC")
    assert "locator" not in manifest["source"]
    assert research.trace() == [
        {
            "query": "official DAC setup hold datasheet",
            "reason": "the 5 ns architecture needs sourced setup and hold limits",
            "selected_document_ids": [manifest["document_id"]],
        }
    ]


def test_agent_cannot_fetch_an_arbitrary_url_or_result_id(tmp_path: Path):
    research = tools(tmp_path)

    with pytest.raises(ResearchToolError, match="only a result returned"):
        research.collect_source("a" * 64)


def test_search_results_must_use_credential_free_https(tmp_path: Path):
    research = tools(tmp_path, FakeProvider(url="http://docs.example.test/dac.pdf"))

    with pytest.raises(ResearchToolError, match="credential-free HTTPS"):
        research.search_sources("DAC", "find the datasheet")


def test_host_allowlist_is_enforced(tmp_path: Path):
    research = tools(tmp_path, FakeProvider(url="https://unreviewed.invalid/dac.pdf"))

    with pytest.raises(ResearchToolError, match="host is not allowed"):
        research.search_sources("DAC", "find the datasheet")


def test_result_identity_and_provider_are_rechecked(tmp_path: Path):
    class LyingProvider(FakeProvider):
        def search(self, query: str, *, limit: int):
            return [
                SearchResult(
                    "a" * 64,
                    "someone-else",
                    self.result.title,
                    self.result.url,
                )
            ]

    research = tools(tmp_path, LyingProvider())

    with pytest.raises(ResearchToolError, match="does not match the active provider"):
        research.search_sources("DAC", "find the datasheet")


def test_provider_cannot_redirect_the_store_to_an_unassigned_path(tmp_path: Path):
    class EscapingProvider(FakeProvider):
        def fetch(self, result, destination, *, max_bytes, timeout_seconds):
            elsewhere = tmp_path / "elsewhere.pdf"
            elsewhere.write_bytes(b"%PDF-1.7\noutside")
            return FetchedDocument(elsewhere, result.url, "application/pdf", result.title)

    research = tools(tmp_path, EscapingProvider())
    result = research.search_sources("DAC", "find the datasheet")[0]

    with pytest.raises(ResearchToolError, match="outside its assigned destination"):
        research.collect_source(str(result["result_id"]))


def test_policy_rechecks_document_size_even_if_provider_ignores_it(tmp_path: Path):
    class OversizeProvider(FakeProvider):
        def fetch(self, result, destination, *, max_bytes, timeout_seconds):
            destination.write_bytes(b"%PDF-1.7\n" + b"x" * 100)
            return FetchedDocument(destination, result.url, "application/pdf", result.title)

    research = ResearchTools(
        OversizeProvider(),
        ReferenceStore(tmp_path / "references", max_bytes=1_000),
        policy=ResearchToolPolicy(
            max_document_bytes=32,
            allowed_hosts=("example.test",),
        ),
        extractor=FakeExtractor(),
    )
    result = research.search_sources("DAC", "find the datasheet")[0]

    with pytest.raises(ResearchToolError, match="larger than the policy limit"):
        research.collect_source(str(result["result_id"]))


def test_query_budget_is_enforced(tmp_path: Path):
    research = ResearchTools(
        FakeProvider(),
        ReferenceStore(tmp_path / "references"),
        policy=ResearchToolPolicy(max_queries=1, allowed_hosts=("example.test",)),
        extractor=FakeExtractor(),
    )
    research.search_sources("first", "first gap")

    with pytest.raises(ResearchToolError, match="budget is exhausted"):
        research.search_sources("second", "second gap")


def test_collection_search_and_page_read_budgets_are_independent(tmp_path: Path):
    research = ResearchTools(
        FakeProvider(),
        ReferenceStore(tmp_path / "references"),
        policy=ResearchToolPolicy(
            max_collected_documents=1,
            max_reference_searches=1,
            max_page_reads=1,
            allowed_hosts=("example.test",),
        ),
        extractor=FakeExtractor(),
    )
    result = research.search_sources("DAC", "find limits")[0]
    document = research.collect_source(str(result["result_id"]))
    document_id = str(document["document_id"])

    with pytest.raises(ResearchToolError, match="collection budget"):
        research.collect_source(str(result["result_id"]))

    research.search_reference(document_id, "setup")
    with pytest.raises(ResearchToolError, match="reference search budget"):
        research.search_reference(document_id, "hold")

    research.read_reference_page(document_id, 1)
    with pytest.raises(ResearchToolError, match="page-read budget"):
        research.read_reference_page(document_id, 1)
