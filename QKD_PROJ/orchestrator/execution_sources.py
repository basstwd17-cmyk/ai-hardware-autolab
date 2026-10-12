"""Detect job edits to host-owned device code before trying unchanged drivers.

No deployment, imports from the workspace, or permission widening. Paths come
from recorded workspace audits, not from model prose or repository-wide scans.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_COMMON = ("QKD_PROJ/mcp_server/", "QKD_PROJ/orchestrator/",
           "QKD_PROJ/hwtools/safety.py", "QKD_PROJ/hwtools/record.py", "QKD_PROJ/hwtools/result.py")
_DRIVERS = {
    "measure_scope": ("scope.py", "scpi.py", "instruments/"),
    "run_bbb": ("bbb.py", "ssh.py", "local_credentials.py"),
    "deploy_pru": ("pru.py", "bbb.py", "ssh.py", "local_credentials.py"),
    "scan_jtag": ("jtag.py",), "program_cpld_sram": ("sram.py", "jtag.py"),
    "measure_gate1_gpio": ("scope.py", "scpi.py", "instruments/"),
    "measure_board_scope": ("board_adapter.py", "scope.py", "scpi.py", "instruments/"),
    "run_board_stage": ("board_adapter.py",),
}


def _prefixes(tool):
    return _COMMON + tuple('QKD_PROJ/hwtools/' + name for name in _DRIVERS.get(tool, ()))


def _snapshot(root, prefixes):
    result = {}
    for relative in prefixes:
        target = root / relative
        files = target.rglob('*.py') if relative.endswith('/') and target.is_dir() else [target]
        for file in files:
            resolved = file.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError('Device source escapes the trusted source tree')
            if resolved.is_file():
                result[file.relative_to(root).as_posix()] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return result


class SourceSnapshot:
    """Source baseline for one process/turn; does not load or publish drivers."""
    def __init__(self, root, tool=None):
        self.root = Path(root).resolve()
        self.prefixes = _prefixes(tool) if tool else tuple(dict.fromkeys(p for t in _DRIVERS for p in _prefixes(t)))
        self.before = _snapshot(self.root, self.prefixes)

    def changes(self, tool):
        prefixes = _prefixes(tool)
        current = _snapshot(self.root, prefixes)
        before = {p: h for p, h in self.before.items() if any(p.startswith(x) if x.endswith('/') else p == x for x in prefixes)}
        return [{'path': p, 'before_sha256': before.get(p), 'current_sha256': current.get(p)}
                for p in sorted(before.keys() | current.keys()) if before.get(p) != current.get(p)]


def source_block(changes, workspace):
    return {'execution_blocked': 'platform_deployment', 'repair_target': 'platform_deployment',
            'status': 'blocked', 'runs': [], 'hardware_contacted': False, 'hardware_state_unknown': False,
            'source_workspace': str(workspace), 'unapplied_sources': changes,
            'summary': '장비 실행 전 중단: 드라이버 수정본 적용 및 관련 프로세스 재시작 확인이 필요합니다. 장비 호출은 하지 않았습니다.'}


def unapplied_device_edits(task: dict, workspace: Path, trusted_root: Path) -> list[dict]:
    workspace, trusted_root = workspace.resolve(), trusted_root.resolve()
    if workspace == trusted_root:
        return []
    tool = (task.get("step") or {}).get("tool")
    prefixes = _COMMON + tuple("QKD_PROJ/hwtools/" + name for name in _DRIVERS.get(tool, ()))
    found = []
    for relative in sorted({p for p in task.get("unpublished_platform_paths") or [] if isinstance(p, str)}):
        relative = relative.replace("\\", "/")
        if not any(relative.startswith(p) if p.endswith("/") else relative == p for p in prefixes):
            continue
        candidate, installed = (workspace / relative).resolve(), (trusted_root / relative).resolve()
        if not candidate.is_relative_to(workspace) or not installed.is_relative_to(trusted_root):
            raise ValueError("device source path escapes its workspace/root")
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        desired, actual = digest(candidate), digest(installed)
        if desired != actual:
            found.append({"path": relative, "workspace_sha256": desired, "installed_sha256": actual})
    return found
