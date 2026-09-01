"""Native App Server web-search results become evidence only after collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pypdf import PdfWriter

from QKD_PROJ.research.native_web import (
    HttpsPdfProvider,
    NativeWebResearchSession,
    ResearchProviderError,
)
from QKD_PROJ.research.providers import FetchedDocument
from QKD_PROJ.research.references import ReferenceStore
from QKD_PROJ.research.tools import ResearchToolPolicy


class FakePdfProvider(HttpsPdfProvider):
    def fetch(self, result, destination, *, max_bytes, timeout_seconds):
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with Path(destination).open("wb") as handle:
            writer.write(handle)
        return FetchedDocument(
            Path(destination), result.url, "application/pdf", result.title
        )


def completed_search(url: str = "https://vendor.example/device.pdf") -> dict:
    return {
        "method": "item/completed",
        "params": {
            "item": {
                "type": "webSearch",
                "query": "device timing datasheet pdf",
                "results": [
                    {
                        "type": "text_result",
                        "title": "Device data sheet",
                        "url": url,
                        "snippet": "untrusted setup timing snippet",
                    }
                ],
            }
        },
    }


def response_payload(response: dict):
    return json.loads(response["contentItems"][0]["text"])


def test_captured_result_can_be_collected_but_arbitrary_url_cannot(tmp_path):
    session = NativeWebResearchSession(
        ReferenceStore(tmp_path / "references"),
        policy=ResearchToolPolicy(max_queries=2),
        provider=FakePdfProvider(),
    )
    session.observe(completed_search())

    refused = session.handle_dynamic_call(
        {"tool": "collect_source", "arguments": {"url": "https://other.example/x.pdf"}}
    )
    assert refused["success"] is False
    assert "not present" in response_payload(refused)["error"]

    accepted = session.handle_dynamic_call(
        {
            "tool": "collect_source",
            "arguments": {"url": "https://vendor.example/device.pdf"},
        }
    )
    assert accepted["success"] is True
    manifest = response_payload(accepted)
    assert len(manifest["document_id"]) == 64
    assert manifest["extraction"]["pages"] == 1
    assert "source_locator" not in manifest


def test_packet_must_name_a_document_that_really_entered_the_store(tmp_path):
    session = NativeWebResearchSession(
        ReferenceStore(tmp_path / "references"), provider=FakePdfProvider()
    )
    with pytest.raises(Exception, match="unknown reference"):
        session.validate_packet_documents(
            {"documents": [{"document_id": "a" * 64}], "claims": []}
        )


def test_private_network_destinations_are_refused(monkeypatch):
    monkeypatch.setattr(
        "QKD_PROJ.research.native_web.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ResearchProviderError, match="outside the public Internet"):
        HttpsPdfProvider._require_public_https("https://example.test/device.pdf")
