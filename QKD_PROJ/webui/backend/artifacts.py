"""Opening an artifact: what is re-checked, and why it reveals rather than runs.

The result card offers a button per artifact. The browser only ever holds
``(jobId, artifactId)`` — the path stays here, which is the rule
``orchestrator/results.py`` states about the registration frame: *"Never sent
to a browser."* So the click sends a pair and this module turns it into a file.

**Everything is re-checked at click time.** Registration happened when the
daemon reduced the job; the click happens whenever a person gets round to it.
In between, a run directory can be deleted, a report can be rewritten, and a
second registration can have tried to move the id. Trusting the registration
would mean a button that acts on a record of the past. So each click re-derives:

1. the pair is registered at all, and the id belongs to *that* job;
2. no second registration ever tried to re-point it (the projection keeps the
   first and warns; an id whose meaning was contested is refused, not guessed);
3. the path still resolves inside the project root it was registered under;
4. the file or directory is still there.

**It reveals, it does not execute.** This matters more than it looks.
``collect_artifacts`` registers the ``document`` field of the **Report
handoff** — a value an agent wrote. ``os.startfile`` on an agent-chosen path
would run whatever extension it happens to have, turning "show me the report"
into arbitrary execution by an agent that only had a writer. Revealing the file
in the file manager shows the same thing and executes nothing, so that is what
the button does, and it is why it is not configurable.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class ArtifactError(Exception):
    """A click this host will not act on, with the reason a person can read."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Registrations(Protocol):
    """The half of ``state.Projection`` this needs. Injected, so tests are free."""

    def artifact_path(self, job_id: str, artifact_id: str) -> str | None: ...

    def artifacts(self) -> dict[tuple[str, str], dict[str, Any]]: ...

    def warnings(self) -> list[str]: ...


@dataclass(frozen=True)
class Openable:
    """A resolved artifact, and the only facts about it that may be returned."""

    job_id: str
    artifact_id: str
    path: Path
    is_directory: bool

    def describe(self) -> dict[str, Any]:
        # Deliberately no path. The browser asked for a pair and gets an
        # acknowledgement; the host is the only thing that needs the location.
        return {
            "ok": True,
            "jobId": self.job_id,
            "artifactId": self.artifact_id,
            "kind": "directory" if self.is_directory else "file",
        }


def resolve(registrations: Registrations, job_id: str, artifact_id: str) -> Openable:
    """Re-derive the file behind a pair, or refuse and say which check failed."""
    if not job_id.strip() or not artifact_id.strip():
        raise ArtifactError(400, "both a jobId and an artifactId are required")

    known = registrations.artifacts().get((job_id, artifact_id))
    if known is None:
        raise ArtifactError(
            404,
            f"no artifact {artifact_id!r} is registered for job {job_id!r}. An id belonging "
            "to another job is a different artifact, not a near miss.",
        )

    if _contested(registrations.warnings(), job_id, artifact_id):
        raise ArtifactError(
            409,
            f"{job_id}/{artifact_id} was registered a second time against a different path. "
            "The record is ambiguous, so this host will not choose one of them.",
        )

    raw = registrations.artifact_path(job_id, artifact_id)
    if not raw:
        raise ArtifactError(404, f"{job_id}/{artifact_id} has no path recorded")

    root = str(known.get("projectRoot") or known.get("project_root") or "")
    if not root:
        raise ArtifactError(
            409,
            f"{job_id}/{artifact_id} was registered without a project root, so there is "
            "nothing to check it against.",
        )

    path = _inside(Path(root), Path(raw))
    if path is None:
        raise ArtifactError(
            409,
            f"{job_id}/{artifact_id} no longer resolves inside the project root it was "
            "registered under. Refusing rather than following it out.",
        )

    if not path.exists():
        raise ArtifactError(
            410,
            f"{job_id}/{artifact_id} was registered but is not on disk any more. "
            "The record of it stands; the file does not.",
        )

    return Openable(job_id, artifact_id, path, path.is_dir())


def _contested(warnings: list[str], job_id: str, artifact_id: str) -> bool:
    marker = f"{job_id}/{artifact_id} already points at"
    return any(marker in warning for warning in warnings)


def _inside(root: Path, candidate: Path) -> Path | None:
    """``candidate`` resolved, or nothing if it escapes ``root``.

    Resolved on both sides so a symlink planted between registration and click
    cannot walk out of the tree while still looking like a path under it.
    """
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


# -- revealing ----------------------------------------------------------------

Reveal = Callable[[Openable], None]


def reveal(target: Openable) -> None:
    """Show it in the file manager. Never open it, never run it.

    ``explorer`` returns a non-zero exit code in cases where it has done
    exactly what was asked, so its status is not checked -- the meaningful
    failures (missing file, escaped root) were already refused above.
    """
    if sys.platform != "win32":
        raise ArtifactError(
            501,
            f"revealing a file is not implemented for {sys.platform}. The artifact is at a "
            "path this host knows; nothing was opened.",
        )
    if target.is_directory:
        command = ["explorer", str(target.path)]
    else:
        # `/select,` needs to stay glued to the path: Explorer parses the pair
        # as one argument and drops the selection if they are split.
        command = ["explorer", f"/select,{target.path}"]
    subprocess.run(command, check=False)  # noqa: S603 - fixed argv, no shell
