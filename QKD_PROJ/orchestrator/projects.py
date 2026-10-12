"""Local project connections and per-project source workspaces.

A connection selects a Git checkout; no source is uploaded or moved. The Brief
pins its settings, while each job edits a separate snapshot of that checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from .workspace import WorkspaceManager


def git_root(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("프로젝트 폴더가 없습니다. 데몬이 실행되는 PC의 경로를 입력하세요.")
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    if result.returncode or Path(result.stdout.strip()).resolve() != root:
        raise ValueError("Git 저장소의 최상위 폴더를 연결하세요. 새 폴더는 git init 후 첫 커밋이 필요합니다.")
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=root,
                          capture_output=True, text=True, timeout=15)
    if head.returncode:
        raise ValueError("프로젝트에 첫 Git 커밋을 만든 뒤 연결하세요.")
    return root


def project_description(root, name=None, *, project_id=None, **settings):
    root = git_root(root)
    identifier = project_id or "project-" + hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:16]
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", identifier):
        raise ValueError("invalid project ID")
    result = {"project_id": identifier, "name": str(name or root.name)[:200], "root": str(root)}
    for key in ("description", "build_command", "test_command"):
        value = settings.get(key, "")
        if not isinstance(value, str) or len(value) > 8000:
            raise ValueError(f"invalid project {key}")
        result[key] = value.strip()
    return result


class ProjectRegistry:
    def __init__(self, path, default_root, *, default_id="qkd-proj"):
        self.path = Path(path)
        self.default = project_description(default_root, project_id=default_id)
        self._lock = threading.RLock()

    def _rows(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("projects"), dict):
            raise ValueError("invalid project registry")
        return data["projects"]

    def _catalog_rows(self):
        rows = self._rows()
        recorded = rows.get(self.default["project_id"])
        if not recorded or recorded.get("root") != self.default["root"]:
            rows[self.default["project_id"]] = self.default
        return rows

    def catalog(self):
        with self._lock:
            rows = self._catalog_rows()
            return {"projects": list(rows.values()), "default_project_id": self.default["project_id"]}

    def resolve(self, project_id=None):
        if project_id is None:
            project_id = self.default["project_id"]
        if not isinstance(project_id, str):
            raise ValueError("projectId must be a registered project ID")
        rows = self._catalog_rows()
        if project_id not in rows:
            raise ValueError("프로젝트가 등록되어 있지 않습니다. 목록에서 다시 선택하세요.")
        result = dict(rows[project_id])
        git_root(result["root"])
        return result

    def register(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("root"), str):
            raise ValueError("project requires a local root path")
        row = project_description(value["root"], value.get("name"),
                                  **{key: value.get(key, "") for key in ("description", "build_command", "test_command")})
        with self._lock:
            rows = self._rows()
            # Reconnecting the daemon's default checkout updates its guide,
            # while preserving its existing knowledge namespace.
            if row["root"] == self.default["root"]:
                row["project_id"] = self.default["project_id"]
            rows[row["project_id"]] = row
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "projects": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return row


class ProjectWorkspaces:
    def __init__(self, default_manager, registry):
        self.default_manager, self.registry = default_manager, registry
        self.root = default_manager.root
        self._managers = {}
        self._lock = threading.RLock()

    def _manager(self, project):
        if not project:
            return self.default_manager  # Recorded jobs predating project connections.
        if not isinstance(project, dict):
            raise ValueError("invalid recorded project")
        root = git_root(project["root"])
        identifier = project["project_id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", identifier):
            raise ValueError("invalid recorded project ID")
        if root == self.default_manager.project_root:
            return self.default_manager
        with self._lock:
            key = (identifier, str(root))
            if key not in self._managers:
                self._managers[key] = WorkspaceManager(self.root / identifier, root, source_prefixes=None)
            return self._managers[key]

    def prepare_project(self, job_id, project):
        return self._manager(project).prepare(job_id)

    def prepare(self, job_id):
        return self.default_manager.prepare(job_id)

    def __call__(self, task):
        return self._manager(task.get("project"))(task)

    def audit(self, task, agent):
        return self._manager(task.get("project")).audit(task, agent)
