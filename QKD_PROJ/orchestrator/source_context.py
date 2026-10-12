"""Small source-navigation index, not a new authorization or evidence store.

Injected on every simple-team call, including old jobs resumed on new code.
Historical source remains available for explicit, scoped reuse.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def source_context(resources: dict[str, Any]) -> dict[str, Any]:
    selected_paths: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in str(path):
            return
        selected_paths.add(str(path))

    board = resources.get("board_development")
    selected = board.get("selected") if isinstance(board, dict) else None
    if isinstance(selected, dict):
        add(selected.get("profile"))
        add(selected.get("board_map"))

    return {
        "version": 1,
        "purpose": "navigation_only_not_permission_or_proof",
        "workspace_note": (
            "An existing job workspace can contain older README/STATUS copies. "
            "This injected navigation index supersedes their workflow instructions, "
            "not the current scenario or Campaign. Do not rewrite preserved snapshots."
        ),
        "read_first": ["QKD_PROJ/README.md", "QKD_PROJ/STATUS.md"],
        "active_team": "QKD_PROJ/agents/simple/",
        "selected_board_paths": sorted(selected_paths),
        "external_sources": {
            "bbb_pru": "BeagleBone-Black/",
            "stm32": "STM32Cube/",
            "procedure_skills": ".claude/skills/",
        },
        "historical": [
            "QKD_PROJ/docs/history/", "QKD_PROJ/docs/20*.md",
            "QKD_PROJ/agents/codex/",
        ],
        "exclude_from_broad_search": [
            "QKD_PROJ/workspaces/", "QKD_PROJ/tmp/",
            "QKD_PROJ/**/build/", "**/__pycache__/", "**/node_modules/",
            "QKD_PROJ/webui/state/", "QKD_PROJ/webui/frontend/dist/",
            "QKD_PROJ/webui/cloudflare/.wrangler/",
            "**/.offline_*", "**/.pytest_cache/",
        ],
        "rules": [
            "The current scenario, steering, selected board/revision and Plan define the task. For new hardware jobs, resources.scenario_access is the user's device delegation; no extra Campaign click is needed within it. Without it, use the job's explicit approval/Campaign. Never infer authority from old records or expand it yourself.",
            "Read exact profile/capability source paths and verify their hashes before reuse; historical code is reusable, not automatically current or approved.",
            "A dated status note is evidence about that date, never current power, SRAM, wiring or approval state. Newest filename/mtime is not proof.",
            "Historical docs and legacy agent definitions are opt-in for a named evidence gap; do not restart the legacy workflow from their instructions.",
            "Use bounded run/reference tools for the selected job/board; do not recursively scan all evidence or another job's workspace.",
            "When the task names an exact source/run/report, start there. A new turn is not a reason to repeat repository-wide discovery or reread every README. Read run summaries and small analysis artifacts before raw traces; check file sizes before loading data. Historical statistical counts/tolerances are examples, not additional scenario requirements.",
            "For another board, inspect its actual sources and datasheets. Catalog profiles are advisory; create only missing implementation. Never copy RTASE/BBB electrical constraints to unrelated hardware.",
            "If an explicitly selected historical path is hidden by .rgignore, use --no-ignore on that exact path only. Navigation guidance is not a security boundary.",
        ],
    }
