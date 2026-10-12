"""Small read-only evidence views; original run files remain the source of truth."""
from __future__ import annotations

import copy
from typing import Any


# Bulk observations, not requirements or scalar results. Do not recursively
# shorten arbitrary lists: safety bounds, failures and acceptance criteria matter.
BULK_LISTS = frozenset({"commands", "sequential_rounds", "transcript", "scpi_transcript"})
BULK_TEXT = frozenset({"stdout", "stderr", "aggregatedOutput"})


def run_view(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = copy.deepcopy(manifest)
    omitted: list[dict[str, Any]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in list(value.items()):
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key in BULK_LISTS and isinstance(child, list) and len(child) > 8:
                    omitted.append({"pointer": pointer, "count": len(child), "shown": 4})
                    value[key] = child[:2] + child[-2:]
                    visit(value[key], pointer)
                elif key in BULK_TEXT and isinstance(child, str) and len(child) > 1600:
                    omitted.append({"pointer": pointer, "characters": len(child), "shown_characters": 1200})
                    value[key] = child[:800] + '\n...[excerpt; full output in original run]...\n' + child[-400:]
                else:
                    visit(child, pointer)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")

    visit(result, "")
    return result, omitted
