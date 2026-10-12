"""Private subprocess entry point for bounded PDF text extraction.

This module is not an agent tool.  ``PypdfExtractor`` starts it so a hostile or
vector-heavy PDF cannot stall the long-lived daemon.  Progress is checkpointed
after every page by atomic file replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _write(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def extract(source: Path, progress: Path) -> int:
    pages: list[dict[str, Any]] = []
    payload: dict[str, Any] = {"status": "starting", "pages": pages, "error": None}
    _write(progress, payload)
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(source))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("encrypted PDF requires a password")
        total = len(reader.pages)
        if total < 1:
            raise ValueError("PDF has no pages")
        payload.update(status="running", total_pages=total)
        _write(progress, payload)
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
                error = None
            except Exception as exc:  # noqa: BLE001 - preserve a visible page failure
                text = ""
                error = f"{type(exc).__name__}: {exc}"
            encoded = text.encode("utf-8")
            record: dict[str, Any] = {
                "page": number,
                "text": text,
                "text_sha256": hashlib.sha256(encoded).hexdigest(),
                "characters": len(text),
                "status": "error" if error else ("empty" if not text.strip() else "ok"),
            }
            if error:
                record["error"] = error
            pages.append(record)
            payload["completed_pages"] = number
            _write(progress, payload)
        payload["status"] = "ok"
        _write(progress, payload)
        return 0
    except Exception as exc:  # noqa: BLE001 - child must always leave a diagnostic
        payload.update(status="error", error=f"{type(exc).__name__}: {exc}")
        _write(progress, payload)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: python -m ai_hardware_autolab.research.pdf_worker INPUT.pdf PROGRESS.json")
        return 2
    return extract(Path(args[0]), Path(args[1]))


if __name__ == "__main__":
    raise SystemExit(main())
