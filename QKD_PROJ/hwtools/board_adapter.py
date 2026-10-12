"""Execute one exact argv from a digest-bound board profile.

The agent supplies only a stage id.  The reviewed profile supplies cwd, argv
and expected artifacts; no shell is involved and all outputs become a run.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from QKD_PROJ.boards.profiles import BoardProfile
from QKD_PROJ.orchestrator.execution_guard import require_offline_workspace_command

from .record import RunRecord
from .result import FAIL, PASS, ToolResult
from .safety import SafetyGate


class BoardAdapterError(RuntimeError):
    pass


def _inside(root: Path, reference: str, what: str, *, must_exist: bool = True) -> Path:
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BoardAdapterError(f"{what} escapes the project root") from exc
    if must_exist and not candidate.exists():
        raise BoardAdapterError(f"{what} does not exist: {reference}")
    return candidate


def _argv(command: tuple[str, ...], root: Path) -> list[str]:
    if not command:
        raise BoardAdapterError("adapter stage has no command")
    executable = command[0]
    executable_path = Path(executable)
    if executable_path.is_absolute():
        if not executable_path.is_file():
            raise BoardAdapterError(f"adapter executable does not exist: {executable}")
        executable = str(executable_path.resolve())
    elif "/" in executable or "\\" in executable:
        executable = str(_inside(root, executable, "adapter executable"))
    else:
        found = shutil.which(executable)
        if found is None:
            raise BoardAdapterError(f"adapter executable is not on PATH: {executable}")
        executable = found
    return [executable, *command[1:]]


def run_profile_stage(
    profile: BoardProfile,
    stage_id: str,
    gate: SafetyGate,
    *,
    record: RunRecord | None = None,
    project_root: Path,
    timeout_s: float = 900.0,
) -> ToolResult:
    started = time.monotonic()
    result = ToolResult(tool="board_adapter", action=stage_id)
    root = Path(project_root).resolve()
    try:
        stage = next(item for item in profile.stages if item.stage_id == stage_id)
        if stage.status != "ready":
            raise BoardAdapterError(f"stage {stage_id} still requires an adapter")
        operation = {
            "design": "firmware_build",
            "build": "firmware_build",
            "upload": "board_upload",
            "measure": "oscilloscope_capture",
        }.get(stage.phase)
        if operation is None:
            raise BoardAdapterError(f"stage phase {stage.phase!r} is not executable")
        gate.require(operation)
        cwd = _inside(root, str(stage.cwd), f"stage {stage_id} cwd")
        argv = _argv(stage.command, root)
        if stage.mode == "offline":
            # A profile cannot relabel a programmer/SSH/SCPI command as an
            # offline build and thereby inherit the build operation's authority.
            require_offline_workspace_command(subprocess.list2cmdline(argv))
        result.commands = [subprocess.list2cmdline(argv)]
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        result.measurements.update(
            {
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "profile_sha256": profile.sha256,
                "board_id": profile.board_id,
                "revision": profile.revision,
            }
        )
        if completed.returncode != 0:
            raise BoardAdapterError(f"adapter exited {completed.returncode}")
        for reference in stage.artifacts:
            artifact = _inside(cwd, reference, f"stage {stage_id} artifact")
            if not artifact.is_file():
                raise BoardAdapterError(f"stage artifact is not a file: {reference}")
            if record is not None:
                adopted = record.adopt(artifact, f"board_{stage.phase}")
                result.add_artifact(adopted, f"board_{stage.phase}", relative_to=record.dir)
            else:
                result.add_artifact(artifact, f"board_{stage.phase}", relative_to=cwd)
        result.verdict = PASS
    except Exception as exc:  # every attempted adapter call becomes evidence
        result.ok = False
        result.verdict = FAIL
        result.error = f"{type(exc).__name__}: {exc}"
    result.duration_s = round(time.monotonic() - started, 3)
    if record is not None:
        record.add(result)
    return result
