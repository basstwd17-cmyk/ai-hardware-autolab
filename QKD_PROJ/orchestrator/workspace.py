"""Fail-closed lookup of code workspaces prepared for one durable job.

This module deliberately does not create a Git worktree.  Creation has to
choose what commit or reviewed snapshot the job starts from, and silently
using ``HEAD`` while the operator has uncommitted platform changes would run
old code.  A launcher may prepare the directory, but the daemon only accepts a
workspace carrying a marker that binds it to the exact job id.

Run evidence is not part of this directory.  ``CodexRunnerConfig.runs_dir`` is
shared with the reducer and artifact server so there remains one evidence
store even while source edits are isolated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MARKER = ".qkd-job-workspace.json"
MARKER_VERSION = 1
JOB_ID = re.compile(r"job_[0-9a-f]{12}")


class WorkspaceError(RuntimeError):
    """A job has no workspace that can safely be used."""


@dataclass(frozen=True)
class PreparedJobWorkspaceResolver:
    """Map ``task.job_id`` to a reviewed, already-prepared Git workspace."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    def __call__(self, task: dict[str, Any]) -> Path:
        job_id = task.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise WorkspaceError("an isolated agent task needs a canonical job_id")
        workspace = (self.root / job_id).resolve()
        try:
            workspace.relative_to(self.root)
        except ValueError as exc:  # defence in depth after the strict id check
            raise WorkspaceError(f"job workspace escapes its root: {job_id}") from exc

        marker_path = workspace / MARKER
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkspaceError(
                f"no prepared workspace for {job_id}; expected {marker_path}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"cannot read workspace marker for {job_id}: {exc}") from exc
        if not isinstance(marker, dict):
            raise WorkspaceError(f"workspace marker for {job_id} is not an object")
        if marker.get("version") != MARKER_VERSION or marker.get("job_id") != job_id:
            raise WorkspaceError(
                f"workspace marker does not bind {workspace} to {job_id} at version "
                f"{MARKER_VERSION}"
            )
        base_commit = marker.get("base_commit")
        if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", base_commit):
            raise WorkspaceError(f"workspace marker for {job_id} has no full base commit")
        if not (workspace / ".git").exists():
            raise WorkspaceError(f"prepared workspace for {job_id} is not a Git worktree")
        if not (workspace / "QKD_PROJ").is_dir():
            raise WorkspaceError(f"prepared workspace for {job_id} has no QKD_PROJ tree")
        return workspace

