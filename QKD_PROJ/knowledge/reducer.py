"""Deterministic promotion of model proposals into evidence-backed claims."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Protocol

from .models import (
    EvidenceCheck,
    EvidenceRef,
    KnowledgeCandidate,
    KnowledgeClaim,
    KnowledgeValidationError,
    json_value_digest,
)


class EvidenceResolver(Protocol):
    def verify(self, evidence: EvidenceRef) -> EvidenceCheck: ...


class EvidenceResolutionError(RuntimeError):
    pass


class KnowledgeReducer:
    """Resolve every reference; the model never selects the resulting status."""

    def __init__(self, resolver: EvidenceResolver) -> None:
        self.resolver = resolver

    def reduce(self, candidate: KnowledgeCandidate) -> KnowledgeClaim:
        checks: list[EvidenceCheck] = []
        for evidence in candidate.evidence:
            try:
                check = self.resolver.verify(evidence)
                if check.evidence_id != evidence.evidence_id:
                    raise EvidenceResolutionError("resolver returned a check for different evidence")
            except Exception as exc:  # a failed inspection is not a verified fact
                check = EvidenceCheck(
                    evidence.evidence_id,
                    False,
                    "resolver_error",
                    _bounded_error(exc),
                )
            checks.append(check)

        expected_role = "supports" if candidate.disposition == "assert" else "refutes"
        promotable = bool(checks) and all(check.ok for check in checks)
        promotable = promotable and all(ref.role == expected_role for ref in candidate.evidence)
        if not promotable:
            status = "proposed"
        elif candidate.disposition == "assert":
            status = "verified"
        else:
            status = "refuted"
        return KnowledgeClaim(candidate, status, tuple(checks))


class FilesystemEvidenceResolver:
    """Verify content-addressed project files, run fields and PDF pages.

    Locators are always relative to ``project_root``.  Document pages are the
    exception: their locator is the content digest and resolves underneath the
    immutable reference store.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        references_root: Path | None = None,
        max_json_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.references_root = Path(references_root or self.project_root / "references").resolve()
        self.max_json_bytes = max_json_bytes

    def verify(self, evidence: EvidenceRef) -> EvidenceCheck:
        if evidence.kind == "document_page":
            return self._document_page(evidence)
        if evidence.kind == "handoff":
            return self._check(evidence, False, "unsupported_handoff", "handoff needs a ledger resolver")
        return self._project_file(evidence)

    def _project_file(self, evidence: EvidenceRef) -> EvidenceCheck:
        # Building from parts avoids treating a POSIX-style locator as a rooted
        # Windows path while keeping the model's stored slash form portable.
        relative = Path(*evidence.locator.split("/"))
        target = (self.project_root / relative).resolve()
        if not _inside(target, self.project_root):
            return self._check(evidence, False, "outside_project", "locator left the project root")
        parts = relative.parts
        if evidence.kind == "contract" and (not parts or parts[0].lower() != "contracts"):
            return self._check(evidence, False, "wrong_contract_root", "contract is not under contracts/")
        if evidence.kind == "run" and (not parts or parts[0].lower() != "runs"):
            return self._check(evidence, False, "wrong_run_root", "run evidence is not under runs/")
        if not target.is_file():
            return self._check(evidence, False, "missing_file", f"{evidence.locator} does not exist")
        actual = _sha256_file(target)
        if actual != evidence.sha256:
            return self._check(evidence, False, "sha256_mismatch", "file bytes changed")
        if evidence.json_pointer is None:
            return self._check(evidence, True, "verified_file", f"verified {evidence.locator}")
        try:
            if target.stat().st_size > self.max_json_bytes:
                raise EvidenceResolutionError("JSON evidence exceeds the configured byte limit")
            document = json.loads(target.read_text(encoding="utf-8"))
            selected = _json_pointer(document, evidence.json_pointer)
            actual_value = json_value_digest(selected)
        except Exception as exc:
            return self._check(evidence, False, "json_pointer_error", _bounded_error(exc))
        if actual_value != evidence.value_sha256:
            return self._check(evidence, False, "json_value_mismatch", "selected JSON value changed")
        return self._check(
            evidence,
            True,
            "verified_json_value",
            f"verified {evidence.locator}{evidence.json_pointer}",
        )

    def _document_page(self, evidence: EvidenceRef) -> EvidenceCheck:
        directory = (self.references_root / evidence.locator).resolve()
        if not _inside(directory, self.references_root):
            return self._check(evidence, False, "outside_references", "document left reference store")
        original = directory / "original.pdf"
        manifest_path = directory / "manifest.json"
        text_path = directory / "text.json"
        if not all(path.is_file() for path in (original, manifest_path, text_path)):
            return self._check(evidence, False, "missing_document", "reference record is incomplete")
        if _sha256_file(original) != evidence.sha256:
            return self._check(evidence, False, "document_sha256_mismatch", "document bytes changed")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            extracted = manifest.get("extraction") or {}
            if extracted.get("status") != "ok":
                raise EvidenceResolutionError("reference extraction is not complete")
            pages = json.loads(text_path.read_text(encoding="utf-8")).get("pages") or []
            page = next((row for row in pages if row.get("page") == evidence.page), None)
            if not isinstance(page, dict):
                raise EvidenceResolutionError("cited page is outside the extracted document")
            text = str(page.get("text") or "")
            stored_text_digest = page.get("text_sha256")
            if stored_text_digest != hashlib.sha256(text.encode("utf-8")).hexdigest():
                raise EvidenceResolutionError("extracted page text changed")
            if _squash(evidence.excerpt or "") not in _squash(text):
                raise EvidenceResolutionError("cited excerpt is not present on the page")
        except Exception as exc:
            return self._check(evidence, False, "document_page_error", _bounded_error(exc))
        return self._check(
            evidence,
            True,
            "verified_document_page",
            f"verified document {evidence.locator[:12]} page {evidence.page}",
        )

    @staticmethod
    def _check(evidence: EvidenceRef, ok: bool, code: str, detail: str) -> EvidenceCheck:
        return EvidenceCheck(evidence.evidence_id, ok, code, detail)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_pointer(value: object, pointer: str) -> object:
    if pointer == "":
        return value
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit():
                raise EvidenceResolutionError(f"{token!r} is not a list index")
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise EvidenceResolutionError("JSON pointer crossed a scalar value")
    return current


def _squash(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _bounded_error(exc: Exception) -> str:
    detail = str(exc).strip() or exc.__class__.__name__
    return detail[:2_000]
