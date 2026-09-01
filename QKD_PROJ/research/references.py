"""Content-addressed storage for documents used by research agents.

An LLM may decide what it needs to search for and which result is relevant.
It does not get to decide whether downloaded bytes are a PDF, where those
bytes are stored, or what identity the document has.  This module owns that
deterministic boundary:

* the SHA-256 of the copied bytes is the document id;
* the original and its extracted text settle together by atomic directory
  replacement;
* page numbers are one-based and every extracted page carries its own digest;
* a manifest records the private acquisition locator, but callers must use
  :meth:`ReferenceRecord.public_manifest` before crossing a browser boundary.

Text extraction is a search aid, not a faithful representation of PDF layout.
An architecture claim still cites the original document and page, and a later
visual/OCR tool may inspect the rendered page when layout matters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
_DOCUMENT_ID = re.compile(r"[0-9a-f]{64}")


class ReferenceError(RuntimeError):
    """A document could not enter or be read from the reference store."""


class ReferenceDependencyError(ReferenceError):
    """An optional document parser required by the requested operation is absent."""


class PdfExtractor(Protocol):
    name: str

    def extract(self, source: Path) -> "ExtractionResult": ...


@dataclass(frozen=True)
class ExtractionResult:
    pages: list[dict[str, Any]]
    status: str = "ok"
    error: str | None = None


class PypdfExtractor:
    """Extract PDF text in an isolated child process with a hard timeout.

    Real schematics can make a parser spend minutes inside one vector-heavy
    page.  Running that parser in the daemon process would stop scheduling,
    approvals and UI replies.  The worker checkpoints completed pages by
    atomic replacement; on timeout this extractor returns the last checkpoint
    and marks the source non-ready instead of pretending extraction completed.
    """

    name = "pypdf"

    def __init__(self, *, timeout_seconds: float = 60.0) -> None:
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def extract(self, source: Path) -> ExtractionResult:
        progress = source.with_name(".pdf-extraction-progress.json")
        command = (
            sys.executable,
            "-m",
            "QKD_PROJ.research.pdf_worker",
            str(source),
            str(progress),
        )
        startupinfo = None
        if os.name == "nt":  # do not flash a console when the daemon is packaged
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed module, no shell
                command,
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            raise ReferenceDependencyError(f"could not start PDF extraction worker: {exc}") from exc

        timed_out = False
        try:
            _, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            _, stderr = process.communicate()

        payload = self._read_progress(progress)
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, list):
            pages = []
        if timed_out:
            return ExtractionResult(
                pages,
                "timeout" if not pages else "partial",
                f"pypdf extraction exceeded {self.timeout_seconds:g} seconds",
            )
        if process.returncode != 0:
            detail = payload.get("error") if isinstance(payload, dict) else None
            detail = detail or (stderr or "").strip() or f"worker exited {process.returncode}"
            return ExtractionResult(pages, "error", str(detail))
        status = str(payload.get("status") or "error")
        return ExtractionResult(pages, status, payload.get("error"))

    @staticmethod
    def _read_progress(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ReferenceRecord:
    document_id: str
    directory: Path
    manifest: dict[str, Any]

    @property
    def original(self) -> Path:
        return self.directory / str(self.manifest["content"]["stored_as"])

    @property
    def extracted_text(self) -> Path:
        return self.directory / str(self.manifest["extraction"]["stored_as"])

    @property
    def research_ready(self) -> bool:
        extraction = self.manifest.get("extraction") or {}
        return extraction.get("status") == "ok" and extraction.get("error_pages") == 0

    def public_manifest(self) -> dict[str, Any]:
        """Return metadata safe for a browser or unprivileged UI.

        The acquisition locator can be an absolute local path or, later, a URL
        containing a private query.  The UI receives provenance kind and the
        original display name, never the locator or store directory.
        """

        source = self.manifest.get("source") or {}
        return {
            "schema_version": self.manifest["schema_version"],
            "document_id": self.document_id,
            "title": self.manifest["title"],
            "source": {"kind": source.get("kind"), "name": source.get("name")},
            "content": dict(self.manifest["content"]),
            "extraction": dict(self.manifest["extraction"]),
        }


class ReferenceStore:
    """Store immutable source bytes under ``root/<sha256>/``."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = int(max_bytes)
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.root.mkdir(parents=True, exist_ok=True)

    def ingest_pdf(
        self,
        source: Path,
        *,
        title: str | None = None,
        source_kind: str = "local",
        source_locator: str | None = None,
        extractor: PdfExtractor | None = None,
    ) -> ReferenceRecord:
        """Copy and index one PDF without trusting its filename or caller hash."""

        path = Path(source).resolve(strict=True)
        if not path.is_file():
            raise ReferenceError(f"reference source is not a file: {path}")
        if not isinstance(source_kind, str) or not source_kind.strip():
            raise ReferenceError("source_kind must be a non-empty string")

        with tempfile.TemporaryDirectory(prefix=".ingest-", dir=self.root) as temporary:
            staging = Path(temporary)
            copied = staging / "original.pdf"
            digest, size, header = self._copy_and_hash(path, copied)
            if b"%PDF-" not in header:
                raise ReferenceError(f"source is not recognisably a PDF: {path.name}")

            existing = self.root / digest
            if existing.exists():
                return self.get(digest)

            pdf_extractor = extractor or PypdfExtractor()
            extraction = pdf_extractor.extract(copied)
            if isinstance(extraction, list):  # narrow compatibility for simple test/provider seams
                extraction = ExtractionResult(extraction)
            pages = extraction.pages
            text_payload = {
                "schema_version": SCHEMA_VERSION,
                "document_id": digest,
                "extractor": pdf_extractor.name,
                "status": extraction.status,
                "error": extraction.error,
                "pages": pages,
            }
            self._write_json(staging / "text.json", text_payload)

            errors = sum(1 for page in pages if page.get("status") == "error")
            empty = sum(1 for page in pages if page.get("status") == "empty")
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "document_id": digest,
                "title": (title or path.stem).strip() or path.stem,
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "source": {
                    "kind": source_kind.strip(),
                    "name": path.name,
                    "locator": source_locator if source_locator is not None else str(path),
                },
                "content": {
                    "sha256": digest,
                    "bytes": size,
                    "media_type": "application/pdf",
                    "stored_as": "original.pdf",
                },
                "extraction": {
                    "extractor": pdf_extractor.name,
                    "status": extraction.status,
                    "pages": len(pages),
                    "error_pages": errors,
                    "empty_pages": empty,
                    "stored_as": "text.json",
                    "layout_faithful": False,
                },
            }
            if extraction.error:
                manifest["extraction"]["error"] = extraction.error
            self._write_json(staging / "manifest.json", manifest)

            self._commit_staging(staging, existing)
            return self.get(digest)

    def get(self, document_id: str) -> ReferenceRecord:
        document_id = self._validate_document_id(document_id)
        directory = self.root / document_id
        manifest_path = directory / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReferenceError(f"unknown reference document: {document_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceError(f"unreadable reference manifest: {document_id}") from exc
        if manifest.get("document_id") != document_id:
            raise ReferenceError(f"reference manifest identity mismatch: {document_id}")
        content = manifest.get("content")
        if not isinstance(content, dict) or content.get("sha256") != document_id:
            raise ReferenceError(f"reference content identity mismatch: {document_id}")
        return ReferenceRecord(document_id, directory, manifest)

    def pages(
        self, document_id: str, *, require_complete: bool = True
    ) -> list[dict[str, Any]]:
        record = self.get(document_id)
        if require_complete and not record.research_ready:
            status = record.manifest.get("extraction", {}).get("status", "unknown")
            raise ReferenceError(
                f"reference text is not complete ({status}): {document_id}"
            )
        try:
            payload = json.loads(record.extracted_text.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReferenceError(f"unreadable extracted text: {document_id}") from exc
        pages = payload.get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, list):
            raise ReferenceError(f"extracted text has no page list: {document_id}")
        return pages

    def _copy_and_hash(self, source: Path, destination: Path) -> tuple[str, int, bytes]:
        digest = hashlib.sha256()
        size = 0
        header = bytearray()
        try:
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                while chunk := incoming.read(COPY_CHUNK_BYTES):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ReferenceError(
                            f"reference exceeds {self.max_bytes} byte limit: {source.name}"
                        )
                    if len(header) < 1024:
                        header.extend(chunk[: 1024 - len(header)])
                    digest.update(chunk)
                    outgoing.write(chunk)
        except ReferenceError:
            raise
        except OSError as exc:
            raise ReferenceError(f"could not copy reference {source.name}: {exc}") from exc
        if size == 0:
            raise ReferenceError(f"reference is empty: {source.name}")
        return digest.hexdigest(), size, bytes(header)

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _commit_staging(self, staging: Path, destination: Path) -> None:
        """Rename a complete record, tolerating only bounded Windows handle races."""

        for attempt in range(6):
            try:
                os.replace(staging, destination)
                return
            except FileExistsError:
                return  # another ingester committed the same digest first
            except PermissionError:
                # Windows may report an existing destination race as access
                # denied, and virus scanners briefly retain directory handles.
                if destination.exists():
                    return
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @staticmethod
    def _validate_document_id(value: str) -> str:
        if not isinstance(value, str) or _DOCUMENT_ID.fullmatch(value) is None:
            raise ReferenceError("document_id must be exactly 64 lowercase hexadecimal digits")
        return value
