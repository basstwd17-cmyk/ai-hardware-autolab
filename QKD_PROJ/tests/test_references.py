"""Offline tests for content-addressed research documents."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.research.references import (  # noqa: E402
    ExtractionResult,
    PypdfExtractor,
    ReferenceError,
    ReferenceStore,
)


class FakeExtractor:
    name = "fake-pdf"

    def extract(self, source: Path) -> ExtractionResult:
        assert source.read_bytes().startswith(b"%PDF-")
        return ExtractionResult([
            {
                "page": 1,
                "text": "PRU timing reference",
                "text_sha256": hashlib.sha256(b"PRU timing reference").hexdigest(),
                "characters": 20,
                "status": "ok",
            },
            {
                "page": 2,
                "text": "",
                "text_sha256": hashlib.sha256(b"").hexdigest(),
                "characters": 0,
                "status": "empty",
            },
        ])


def fake_pdf(path: Path, body: bytes = b"reference") -> Path:
    path.write_bytes(b"%PDF-1.7\n" + body)
    return path


def test_pdf_is_stored_under_its_content_digest(tmp_path: Path):
    source = fake_pdf(tmp_path / "datasheet.pdf")
    store = ReferenceStore(tmp_path / "references")

    record = store.ingest_pdf(source, title="Timing datasheet", extractor=FakeExtractor())

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    assert record.document_id == expected
    assert record.directory == (tmp_path / "references" / expected).resolve()
    assert record.original.read_bytes() == source.read_bytes()
    assert record.manifest["title"] == "Timing datasheet"
    assert record.manifest["extraction"]["pages"] == 2
    assert record.research_ready
    assert store.pages(expected)[0]["page"] == 1


def test_reingesting_identical_bytes_is_idempotent(tmp_path: Path):
    one = fake_pdf(tmp_path / "one.pdf", b"same")
    two = fake_pdf(tmp_path / "two.pdf", b"same")
    store = ReferenceStore(tmp_path / "references")

    first = store.ingest_pdf(one, title="first", extractor=FakeExtractor())
    second = store.ingest_pdf(two, title="second", extractor=FakeExtractor())

    assert second.document_id == first.document_id
    assert second.manifest == first.manifest
    assert [p.name for p in store.root.iterdir()] == [first.document_id]


def test_windows_style_transient_directory_denial_is_bounded_and_retried(
    tmp_path: Path, monkeypatch
):
    source = fake_pdf(tmp_path / "transient.pdf")
    store = ReferenceStore(tmp_path / "references")
    real_replace = os.replace
    attempts = 0

    def transient(source_path, destination_path):
        nonlocal attempts
        if Path(source_path).name.startswith(".ingest-"):
            attempts += 1
            if attempts < 3:
                raise PermissionError("transient scanner handle")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr("QKD_PROJ.research.references.os.replace", transient)

    record = store.ingest_pdf(source, extractor=FakeExtractor())

    assert attempts == 3
    assert record.original.exists()


def test_private_locator_is_not_in_the_public_manifest(tmp_path: Path):
    source = fake_pdf(tmp_path / "private.pdf")
    store = ReferenceStore(tmp_path / "references")
    record = store.ingest_pdf(
        source,
        source_locator="C:/private/lab/datasheet.pdf?token=secret",
        extractor=FakeExtractor(),
    )

    public = record.public_manifest()
    assert "locator" not in public["source"]
    assert "secret" not in json.dumps(public)
    assert record.manifest["source"]["locator"].endswith("token=secret")


def test_non_pdf_bytes_are_refused_before_extraction(tmp_path: Path):
    source = tmp_path / "not.pdf"
    source.write_text("not a PDF", encoding="utf-8")
    store = ReferenceStore(tmp_path / "references")

    with pytest.raises(ReferenceError, match="not recognisably a PDF"):
        store.ingest_pdf(source, extractor=FakeExtractor())


def test_size_limit_is_enforced_while_copying(tmp_path: Path):
    source = fake_pdf(tmp_path / "large.pdf", b"x" * 100)
    store = ReferenceStore(tmp_path / "references", max_bytes=16)

    with pytest.raises(ReferenceError, match="exceeds 16 byte limit"):
        store.ingest_pdf(source, extractor=FakeExtractor())


@pytest.mark.parametrize("document_id", ["../escape", "A" * 64, "0" * 63, "0" * 65])
def test_document_ids_cannot_escape_the_store(tmp_path: Path, document_id: str):
    store = ReferenceStore(tmp_path / "references")

    with pytest.raises(ReferenceError, match="64 lowercase hexadecimal"):
        store.get(document_id)


def test_real_pypdf_extractor_indexes_a_generated_page(tmp_path: Path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with source.open("wb") as stream:
        writer.write(stream)

    record = ReferenceStore(tmp_path / "references").ingest_pdf(
        source, extractor=PypdfExtractor()
    )

    pages = json.loads(record.extracted_text.read_text(encoding="utf-8"))["pages"]
    assert pages == [
        {
            "characters": 0,
            "page": 1,
            "status": "empty",
            "text": "",
            "text_sha256": hashlib.sha256(b"").hexdigest(),
        }
    ]


def test_non_ready_extraction_is_visible_in_manifest(tmp_path: Path):
    class TimedOutExtractor:
        name = "timed-out"

        def extract(self, source: Path) -> ExtractionResult:
            return ExtractionResult([], "timeout", "exceeded 1 second")

    source = fake_pdf(tmp_path / "slow.pdf")
    store = ReferenceStore(tmp_path / "references")
    record = store.ingest_pdf(
        source, extractor=TimedOutExtractor()
    )

    assert not record.research_ready
    assert record.manifest["extraction"]["status"] == "timeout"
    assert record.manifest["extraction"]["error"] == "exceeded 1 second"
    assert record.original.read_bytes() == source.read_bytes()
    assert json.loads(record.extracted_text.read_text(encoding="utf-8"))["pages"] == []
    with pytest.raises(ReferenceError, match="text is not complete"):
        store.pages(record.document_id)
    assert store.pages(record.document_id, require_complete=False) == []
