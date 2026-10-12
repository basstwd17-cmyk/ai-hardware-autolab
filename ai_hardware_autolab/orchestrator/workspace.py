"""Deterministic, per-job source workspaces.

``WorkspaceManager`` is a platform service, not an agent. It snapshots the
connected checkout into a detached Git worktree and records source changes
without modifying the original project. Historical selected-input checks remain
available for old jobs. Run evidence lives in the daemon's shared runs folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from .development import autonomous, protected_path

MARKER = ".qkd-job-workspace.json"
MARKER_VERSION = 1
JOB_ID = re.compile(r"job_[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXCLUDED_PREFIXES = (
    "ai_hardware_autolab/docs/history/",
    "ai_hardware_autolab/tmp/",
    "ai_hardware_autolab/webui/cloudflare/.wrangler/",
    "ai_hardware_autolab/runs/",
    "ai_hardware_autolab/references/",
    "ai_hardware_autolab/webui/state/",
    "ai_hardware_autolab/webui/frontend/node_modules/",
    "ai_hardware_autolab/webui/frontend/dist/",
)


class WorkspaceError(RuntimeError):
    """A job workspace cannot be created or trusted safely."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _relative_path(root: Path, value: str, *, what: str) -> tuple[Path, str]:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise WorkspaceError(f"{what} must be a non-empty project-relative path")
    candidate = (root / value).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as exc:
        raise WorkspaceError(f"{what} escapes the project: {value!r}") from exc
    return candidate, relative


@dataclass(frozen=True)
class PreparedJobWorkspaceResolver:
    """Map ``task.job_id`` to an already-prepared Git workspace."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())

    def __call__(self, task: dict[str, Any]) -> Path:
        job_id = _job_id(task)
        workspace = _workspace_path(self.root, job_id)
        _validate_workspace(workspace, job_id)
        return workspace


@dataclass
class WorkspaceManager:
    """Create, populate, and audit one detached worktree per durable job.

    Dirty tracked files and non-ignored project source are copied into the new
    worktree and committed there. This gives the job the code the daemon is
    actually running without changing the user's branch, index, or files.
    """

    root: Path
    project_root: Path
    capability_catalog: Any | None = None
    preserve: bool = True
    source_prefixes: tuple[str, ...] | None = ("ai_hardware_autolab", "scripts")
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.project_root = Path(self.project_root).resolve()
        git_root = self._git("rev-parse", "--show-toplevel").strip()
        if Path(git_root).resolve() != self.project_root:
            raise WorkspaceError(
                f"project_root must be the Git root {Path(git_root).resolve()}, got {self.project_root}"
            )
        try:
            relative = self.root.relative_to(self.project_root).as_posix().rstrip("/") + "/"
        except ValueError:
            relative = ""
        object.__setattr__(self, "_workspace_prefix", relative)
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, job_id: str) -> Path:
        """Create/reuse an isolated source snapshot without starting a turn audit."""
        with self._lock:
            workspace = _workspace_path(self.root, _job_id({"job_id": job_id}))
            if not workspace.exists():
                self._create(workspace, job_id)
            _validate_workspace(workspace, job_id)
            return workspace

    def __call__(self, task: dict[str, Any]) -> Path:
        """Prepare the workspace and durable pre-turn audit baseline."""
        job_id = _job_id(task)
        with self._lock:
            workspace = _workspace_path(self.root, job_id)
            if not workspace.exists():
                self._create(workspace, job_id)
            marker = _validate_workspace(workspace, job_id)
            if marker.get("pending_audit") is not None:
                raise WorkspaceError(
                    f"workspace {job_id} has an unfinished turn audit; inspect {workspace / MARKER}"
                )
            self._materialize_inputs(workspace, marker, task)
            marker["pending_audit"] = {
                "started_at": _utc_now(),
                "iteration": int(task.get("iteration") or 0),
                "step_id": _step_id(task),
                "before": self._working_manifest(workspace),
            }
            self._write_marker(workspace, marker)
            return workspace

    def audit(self, task: dict[str, Any], agent: str) -> dict[str, Any]:
        """Record files changed by one turn and enforce Builder's write scope."""
        job_id = _job_id(task)
        with self._lock:
            workspace = _workspace_path(self.root, job_id)
            marker = _validate_workspace(workspace, job_id)
            pending = marker.get("pending_audit")
            if not isinstance(pending, dict) or not isinstance(pending.get("before"), dict):
                raise WorkspaceError(f"workspace {job_id} has no matching pre-turn audit")
            before = pending["before"]
            after = self._working_manifest(workspace)
            # Git ignores some evidence/config files. Check managed immutable
            # inputs independently so broad source access cannot hide a change.
            for reference, digest in marker.get("managed_inputs", {}).items():
                target, normalized = _relative_path(workspace, reference, what="managed input")
                before[normalized] = digest
                after[normalized] = _sha256(target) if target.is_file() else None
            changed = sorted(
                path for path in set(before) | set(after) if before.get(path) != after.get(path)
            )
            write_scope = self._write_scope(task) if agent == "builder" else []
            free_development = agent == "builder" and autonomous(task.get("resources"))
            managed_inputs = marker.get("managed_inputs", {})
            protected = [
                path
                for path in changed
                if isinstance(managed_inputs, dict) and path in managed_inputs
            ]
            outside = [
                path
                for path in changed
                if path in protected or (protected_path(path) if free_development else not _is_allowed(path, write_scope))
            ]
            entries = [
                {"path": path, "before": before.get(path), "after": after.get(path)}
                for path in changed
            ]
            outputs = self._scope_outputs(
                workspace, [path for path in changed if path not in outside and after.get(path)]
                if free_development else write_scope
            )
            report = {
                "workspace": str(workspace),
                "base_commit": marker["base_commit"],
                "agent": agent,
                "iteration": pending.get("iteration", 0),
                "step_id": pending.get("step_id", ""),
                "write_scope": write_scope,
                "write_scope_advisory": free_development,
                "review_required": free_development,
                "changes": entries,
                "outputs": outputs,
                "change_digest": _canonical_digest(entries),
                "protected_inputs_changed": protected,
                "outside_write_scope": outside,
                "audited_at": _utc_now(),
            }
            audits = marker.setdefault("audits", [])
            if not isinstance(audits, list):
                raise WorkspaceError(f"workspace {job_id} has an invalid audit history")
            audits.append(report)
            marker["pending_audit"] = None
            marker["last_audit"] = report
            self._write_marker(workspace, marker)
            if outside:
                raise WorkspaceError(
                    f"{agent} changed files outside its reviewed write_scope: " + ", ".join(outside)
                )
            return report

    @staticmethod
    def _scope_outputs(workspace: Path, write_scope: list[str]) -> list[dict[str, Any]]:
        """Hash every file Builder is allowed to hand to a later stage.

        Changed-file lists are an audit delta, not an artifact manifest: a
        build may consume an unchanged source file or overwrite a previously
        generated binary with identical bytes.  Preserve the complete bounded
        write scope so a later upload can pin exactly what Builder produced.
        """
        root = workspace.resolve()
        files: set[Path] = set()
        for reference in write_scope:
            target, _ = _relative_path(root, reference, what="write_scope output")
            if target.is_file():
                files.add(target)
            elif target.is_dir():
                files.update(
                    path
                    for path in target.rglob("*")
                    if path.is_file() and ".git" not in path.relative_to(root).parts
                )
        if len(files) > 5000:
            raise WorkspaceError(
                "write_scope contains more than 5000 output files; narrow it before execution"
            )
        return [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
            for path in sorted(files)
            if path.name not in {MARKER, f"{MARKER}.tmp"}
        ]

    def abandon_audit(
        self, task: dict[str, Any], agent: str, error: BaseException
    ) -> dict[str, Any]:
        """Close an audit after a failed runtime call without hiding its edits."""
        try:
            report = self.audit(task, agent)
        except WorkspaceError as audit_error:
            raise WorkspaceError(
                f"runtime failed ({error}); workspace audit also failed: {audit_error}"
            ) from error
        report["runtime_failed"] = True
        report["runtime_error"] = type(error).__name__
        workspace = _workspace_path(self.root, _job_id(task))
        marker = _validate_workspace(workspace, _job_id(task))
        marker["last_audit"] = report
        audits = marker.get("audits")
        if isinstance(audits, list) and audits:
            audits[-1] = report
        self._write_marker(workspace, marker)
        return report

    def release(self, job_id: str, *, remove: bool = False) -> Path:
        """Return the preserved result, or explicitly remove a clean worktree."""
        workspace = _workspace_path(self.root, job_id)
        _validate_workspace(workspace, job_id)
        if self.preserve or not remove:
            return workspace
        manifest = self._working_manifest(workspace)
        if manifest:
            raise WorkspaceError(
                f"refusing to remove changed workspace {job_id}; {len(manifest)} path(s) remain"
            )
        self._git("worktree", "remove", str(workspace))
        return workspace

    def publish(self, task: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
        """Apply a verified Builder delta back to the user's checkout safely.

        The job continues to work in its isolated worktree.  Publishing makes
        completed source, adapters and board profiles available to the normal
        project and to a freshly loaded catalog.  Every destination must still
        equal the pre-turn hash; concurrent user/team edits are reported as a
        conflict instead of being overwritten.
        """
        job_id = _job_id(task)
        with self._lock:
            workspace = _workspace_path(self.root, job_id)
            _validate_workspace(workspace, job_id)
            write_scope = self._write_scope(task)
            changes = audit.get("changes")
            if not isinstance(changes, list):
                raise WorkspaceError("workspace audit has no changed-file list")
            if any(
                isinstance(entry, dict)
                and str(entry.get("path") or "").startswith("ai_hardware_autolab/boards/")
                for entry in changes
            ):
                try:
                    from ai_hardware_autolab.boards.profiles import BoardCatalog, BoardProfileError

                    BoardCatalog.load_default(workspace)
                except BoardProfileError as exc:
                    raise WorkspaceError(
                        f"refusing to publish an invalid board profile: {exc}"
                    ) from exc
            prepared: list[tuple[Path, Path, str | None, str | None]] = []
            conflicts: list[str] = []
            for entry in changes:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise WorkspaceError("workspace audit contains a malformed change")
                relative = entry["path"]
                if not _is_allowed(relative, write_scope):
                    raise WorkspaceError(f"publish path is outside write_scope: {relative}")
                source, normalized = _relative_path(workspace, relative, what="publish source")
                target, _ = _relative_path(self.project_root, normalized, what="publish target")
                before = entry.get("before")
                after = entry.get("after")
                current = _sha256(target) if target.is_file() else None
                if target.exists() and not target.is_file():
                    conflicts.append(f"{normalized} is no longer a file")
                elif current != before:
                    conflicts.append(
                        f"{normalized} changed in the main checkout after the Builder turn began"
                    )
                elif after is not None and (not source.is_file() or _sha256(source) != after):
                    conflicts.append(f"{normalized} changed inside the job workspace after audit")
                prepared.append((source, target, before, after))
            if conflicts:
                raise WorkspaceError("refusing to publish conflicting outputs: " + "; ".join(conflicts))

            published: list[dict[str, Any]] = []
            for source, target, before, after in prepared:
                if after is None:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.qkd-publish-{os.getpid()}.tmp")
                    try:
                        shutil.copy2(source, temporary)
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                published.append(
                    {"path": target.relative_to(self.project_root).as_posix(), "sha256": after}
                )
            return {
                "job_id": job_id,
                "published": published,
                "published_at": _utc_now(),
            }

    # -- creation ---------------------------------------------------------

    def _create(self, workspace: Path, job_id: str) -> None:
        source_commit = self._git("rev-parse", "HEAD").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
            raise WorkspaceError("Git did not return a full source commit")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", str(workspace), source_commit)
        try:
            self._remove_excluded_trees(workspace)
            # Also remove tracked secrets/dependencies from this new snapshot.
            # Never touch the connected checkout.
            for raw in self._git_at_bytes(workspace, "ls-files", "-z").split(b"\0"):
                if not raw:
                    continue
                relative = raw.decode("utf-8", "surrogateescape").replace("\\", "/")
                if self._excluded(relative):
                    target, _ = _relative_path(workspace, relative, what="excluded snapshot file")
                    if target.is_file() or target.is_symlink():
                        target.unlink()
            files = self._snapshot_paths()
            for relative in files:
                source = self.project_root / relative
                target = workspace / relative
                if source.is_symlink():
                    raise WorkspaceError(f"snapshot refuses symbolic link: {relative}")
                if source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                elif source.is_dir():
                    # Git mode 160000 is a gitlink. The containing checkout
                    # may have an initialized submodule directory, while the
                    # detached worktree intentionally keeps only the pinned
                    # gitlink. It is neither a file to copy nor a deletion.
                    continue
                elif target.is_file() or target.is_symlink():
                    target.unlink()
            self._git_at(workspace, "add", "-A")
            self._git_at(
                workspace,
                "-c",
                "user.name=QKD Workspace Manager",
                "-c",
                "user.email=workspace@localhost",
                "commit",
                "--allow-empty",
                "-m",
                f"snapshot for {job_id}",
            )
            base_commit = self._git_at(workspace, "rev-parse", "HEAD").strip()
            snapshot = [
                {"path": relative, "sha256": _sha256(workspace / relative)}
                for relative in files
                if (workspace / relative).is_file()
            ]
            marker = {
                "version": MARKER_VERSION,
                "job_id": job_id,
                "source_commit": source_commit,
                "base_commit": base_commit,
                "snapshot_kind": "working-tree",
                "project_root": str(self.project_root),
                "generic_project": self.source_prefixes is None,
                "snapshot_sha256": _canonical_digest(snapshot),
                "snapshot_files": len(snapshot),
                "created_at": _utc_now(),
                "selected_capabilities": {},
                "managed_inputs": {},
                "audits": [],
                "pending_audit": None,
            }
            self._write_marker(workspace, marker)
        except BaseException:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(workspace)],
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            raise

    @staticmethod
    def _remove_excluded_trees(workspace: Path) -> None:
        """Remove shared/runtime stores from the brand-new worktree only."""
        resolved_workspace = workspace.resolve()
        for prefix in _EXCLUDED_PREFIXES:
            target = (workspace / prefix.rstrip("/")).resolve()
            try:
                target.relative_to(resolved_workspace)
            except ValueError as exc:  # pragma: no cover - constants are fixed above
                raise WorkspaceError(f"excluded workspace path escaped: {target}") from exc
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

    def _snapshot_paths(self) -> list[str]:
        tracked = (self._git_bytes("ls-files", "-z", "--cached").split(b"\0")
                   + self._git_bytes("ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0"))
        untracked = self._git_bytes(
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            "--",
            *(self.source_prefixes or ()),
        ).split(b"\0")
        paths: set[str] = set()
        for raw in (*tracked, *untracked):
            if not raw:
                continue
            relative = raw.decode("utf-8", "surrogateescape").replace("\\", "/")
            if self._excluded(relative):
                continue
            _relative_path(self.project_root, relative, what="snapshot path")
            paths.add(relative)
        return sorted(paths)

    def _excluded(self, relative: str) -> bool:
        parts = relative.split("/")
        if any(part in {"node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".git"} for part in parts):
            return True
        if any(part == ".env" or part.startswith(".env.") for part in parts):
            return True
        prefixes = list(_EXCLUDED_PREFIXES)
        dynamic = getattr(self, "_workspace_prefix", "")
        if dynamic:
            prefixes.append(dynamic)
        return any(
            relative == prefix.rstrip("/") or relative.startswith(prefix) for prefix in prefixes
        )

    # -- selected inputs --------------------------------------------------

    def _materialize_inputs(
        self, workspace: Path, marker: dict[str, Any], task: dict[str, Any]
    ) -> None:
        selected = self._selected_capability_ids(task)
        if selected:
            if self.capability_catalog is None:
                raise WorkspaceError("selected capabilities require a capability catalog")
            available = {item.capability_id: item for item in self.capability_catalog.capabilities}
            recorded = marker.setdefault("selected_capabilities", {})
            for identifier in selected:
                capability = available.get(identifier)
                if capability is None:
                    raise WorkspaceError(f"unknown selected capability: {identifier}")
                assets: list[dict[str, str]] = []
                for asset in capability.assets:
                    source, relative = _relative_path(
                        self.project_root,
                        asset["path"],
                        what=f"capability {identifier} asset",
                    )
                    target = workspace / relative
                    expected = asset["sha256"]
                    if not source.is_file() or _sha256(source) != expected:
                        raise WorkspaceError(
                            f"verified capability source drifted: {identifier}:{relative}"
                        )
                    previously_selected = identifier in recorded
                    if not target.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                    if not previously_selected and _sha256(target) != expected:
                        raise WorkspaceError(
                            f"workspace capability copy drifted: {identifier}:{relative}"
                        )
                    assets.append({"path": relative, "sha256": expected})
                recorded[identifier] = {"sha256": capability.digest, "assets": assets}

        compiled = self._compiled_contract(task)
        if compiled is not None:
            name, expected = compiled
            source, relative = _relative_path(
                self.project_root, f"ai_hardware_autolab/contracts/{name}", what="compiled contract"
            )
            target = workspace / relative
            if not source.is_file() or _sha256(source) != expected:
                raise WorkspaceError(f"compiled contract source does not match {expected}: {name}")
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif _sha256(target) != expected:
                raise WorkspaceError(f"compiled contract changed inside workspace: {name}")
            marker.setdefault("managed_inputs", {})[relative] = expected

        self._write_marker(workspace, marker)

    @staticmethod
    def _selected_capability_ids(task: dict[str, Any]) -> list[str]:
        resources = task.get("resources")
        rows = resources.get("selected_capabilities") if isinstance(resources, dict) else None
        if not isinstance(rows, list):
            return []
        return sorted(
            {
                str(row["id"])
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("id"), str)
            }
        )

    @staticmethod
    def _compiled_contract(task: dict[str, Any]) -> tuple[str, str] | None:
        plan = task.get("plan")
        raw = plan.get("compiled_contract") if isinstance(plan, dict) else None
        if not isinstance(raw, dict):
            return None
        name, digest = raw.get("name"), raw.get("sha256")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".yaml")
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise WorkspaceError("compiled_contract must bind a plain YAML name and SHA-256")
        return name, digest

    @staticmethod
    def _write_scope(task: dict[str, Any]) -> list[str]:
        resources = task.get("resources")
        values = resources.get("write_scope") if isinstance(resources, dict) else None
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if (
                not isinstance(value, str)
                or not value
                or Path(value).is_absolute()
                or ".." in Path(value).parts
            ):
                raise WorkspaceError("write_scope must contain project-relative text paths")
            result.append(Path(value).as_posix().rstrip("/"))
        return sorted(set(result))

    # -- manifests and durable marker ------------------------------------

    def _working_manifest(self, workspace: Path) -> dict[str, str | None]:
        changed = set(
            self._git_at_bytes(workspace, "diff", "--name-only", "-z", "HEAD").split(b"\0")
        )
        changed.update(
            self._git_at_bytes(
                workspace, "diff", "--cached", "--name-only", "-z", "HEAD"
            ).split(b"\0")
        )
        changed.update(
            self._git_at_bytes(
                workspace, "ls-files", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
        )
        manifest: dict[str, str | None] = {}
        for raw in changed:
            if not raw:
                continue
            relative = raw.decode("utf-8", "surrogateescape").replace("\\", "/")
            if relative in {MARKER, f"{MARKER}.tmp"}:
                continue
            path, normalized = _relative_path(workspace, relative, what="changed path")
            manifest[normalized] = _sha256(path) if path.is_file() else None
        return dict(sorted(manifest.items()))

    @staticmethod
    def _write_marker(workspace: Path, marker: dict[str, Any]) -> None:
        target = workspace / MARKER
        temporary = workspace / f"{MARKER}.tmp"
        temporary.write_text(
            json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)

    # -- git --------------------------------------------------------------

    def _git(self, *args: str) -> str:
        return self._run_git(self.project_root, *args).stdout

    def _git_bytes(self, *args: str) -> bytes:
        return self._run_git(self.project_root, *args, text=False).stdout

    def _git_at(self, workspace: Path, *args: str) -> str:
        return self._run_git(workspace, *args).stdout

    def _git_at_bytes(self, workspace: Path, *args: str) -> bytes:
        return self._run_git(workspace, *args, text=False).stdout

    @staticmethod
    def _run_git(cwd: Path, *args: str, text: bool = True):
        try:
            kwargs: dict[str, Any] = {
                "cwd": cwd,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": text,
                "check": True,
            }
            if text:
                kwargs.update({"encoding": "utf-8", "errors": "strict"})
            return subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args], **kwargs)
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                raw = exc.stderr
                detail = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
            raise WorkspaceError(f"git {' '.join(args)} failed: {detail.strip() or exc}") from exc


def _job_id(task: dict[str, Any]) -> str:
    job_id = task.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise WorkspaceError("an isolated agent task needs a canonical job_id")
    return job_id


def _workspace_path(root: Path, job_id: str) -> Path:
    resolved_root = Path(root).resolve()
    workspace = (resolved_root / job_id).resolve()
    try:
        workspace.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceError(f"job workspace escapes its root: {job_id}") from exc
    return workspace


def _validate_workspace(workspace: Path, job_id: str) -> dict[str, Any]:
    marker_path = workspace / MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkspaceError(f"no prepared workspace for {job_id}; expected {marker_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read workspace marker for {job_id}: {exc}") from exc
    if not isinstance(marker, dict):
        raise WorkspaceError(f"workspace marker for {job_id} is not an object")
    if marker.get("version") != MARKER_VERSION or marker.get("job_id") != job_id:
        raise WorkspaceError(
            f"workspace marker does not bind {workspace} to {job_id} at version {MARKER_VERSION}"
        )
    base_commit = marker.get("base_commit")
    if not isinstance(base_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise WorkspaceError(f"workspace marker for {job_id} has no full base commit")
    if not (workspace / ".git").exists():
        raise WorkspaceError(f"prepared workspace for {job_id} is not a Git worktree")
    if not marker.get("generic_project") and not (workspace / "ai_hardware_autolab").is_dir():
        raise WorkspaceError(f"prepared workspace for {job_id} has no ai_hardware_autolab tree")
    return marker


def _step_id(task: dict[str, Any]) -> str:
    step = task.get("step")
    return str(step.get("id") or "") if isinstance(step, dict) else ""


def _is_allowed(path: str, scopes: list[str]) -> bool:
    return any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes)
