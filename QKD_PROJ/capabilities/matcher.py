"""Deterministically narrow verified capabilities before Lead sees them.

The matcher is a retrieval aid, not an authority. It never creates a
capability and never relaxes execution mode; the catalog loader has already
rechecked source hashes, contract files and PASS runs.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


_TOKENS = re.compile(r"[a-z0-9][a-z0-9._+-]{1,}")
_ALIASES = {
    "비글본": {"bbb", "beaglebone", "pru"},
    "보드": {"board"},
    "피알유": {"pru"},
    "통신": {"spi", "transport", "protocol"},
    "스코프": {"scope", "oscilloscope", "capture"},
    "오실로스코프": {"scope", "oscilloscope", "capture"},
    "측정": {"measure", "measurement", "scope", "capture"},
    "파형": {"waveform", "scope", "capture"},
    "씨피엘디": {"cpld", "diamond"},
    "빌드": {"build"},
    "업로드": {"upload", "program", "deploy"},
    "프로그램": {"program", "deploy", "firmware"},
    "에스피아이": {"spi"},
}


def match_capabilities(
    scenario: str,
    rows: Iterable[dict[str, Any]],
    *,
    board: dict[str, Any] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return scored, stable candidates relevant to a scenario and board."""

    wanted = _words(scenario)
    for korean, aliases in _ALIASES.items():
        if korean in scenario.lower():
            wanted.update(aliases)
    if isinstance(board, dict):
        wanted.update(_words(" ".join(str(board.get(key) or "") for key in (
            "board_id", "revision", "family", "vendor", "toolchain"
        ))))

    scored: list[tuple[int, str, dict[str, Any], list[str]]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        fields = [
            row.get("id"), row.get("title"), row.get("summary"), row.get("mode"),
            *(row.get("operations") or []), *(row.get("skills") or []),
            *(item.get("name", "") for item in (row.get("contracts") or []) if isinstance(item, dict)),
        ]
        available = _words(" ".join(str(item or "") for item in fields))
        overlap = sorted(wanted & available)
        score = len(overlap)
        identifier = str(row["id"])
        # Exact identifiers and toolchain terms are stronger than generic words.
        if identifier.lower() in scenario.lower():
            score += 8
            overlap.insert(0, identifier)
        if score:
            scored.append((score, identifier, row, list(dict.fromkeys(overlap))))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [
        {**row, "match_score": score, "match_reasons": reasons}
        for score, _identifier, row, reasons in scored[: max(1, limit)]
    ]


def _words(value: str) -> set[str]:
    return {match.group(0) for match in _TOKENS.finditer(value.lower())}
