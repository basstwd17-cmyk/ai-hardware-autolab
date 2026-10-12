"""Validated board profiles for cross-board planning and execution.

A board name in prose is not enough to select an uploader or a scope recipe.
Profiles bind aliases, primary-source seeds, a board map and each development
stage to reviewable files.  Unknown boards still enter research, but physical
execution remains closed until a profile supplies an adapter and a campaign
pins the profile and generated contract digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_PHASES = ("research", "design", "build", "upload", "measure", "verify")
_MODES = ("offline", "hardware")
_STATUSES = ("ready", "adapter_required")
_TOP = {
    "schema_version", "board_id", "revision", "title", "aliases",
    "board_map", "sources", "stages",
}
_SOURCE = {"id", "title", "source_kind", "location"}
_STAGE = {
    "id", "phase", "mode", "agent", "tool", "status", "requires_campaign",
    "command", "cwd", "artifacts", "parameters",
}


class BoardProfileError(ValueError):
    """A profile is ambiguous, unsafe, or references missing local state."""


def _plain(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoardProfileError(f"{where} must be non-empty text")
    return value.strip()


def _inside(root: Path, reference: str, where: str, *, must_exist: bool = True) -> Path:
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BoardProfileError(f"{where} escapes the project root") from exc
    if must_exist and not candidate.exists():
        raise BoardProfileError(f"{where} does not exist: {reference}")
    return candidate


def _normalized(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value).lower().replace("_", " ").replace("-", " ").split()
    )


@dataclass(frozen=True)
class BoardStage:
    stage_id: str
    phase: str
    mode: str
    agent: str | None
    tool: str | None
    status: str
    requires_campaign: bool
    command: tuple[str, ...]
    cwd: str | None
    artifacts: tuple[str, ...]
    parameters: dict[str, Any]

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.stage_id,
            "phase": self.phase,
            "mode": self.mode,
            "agent": self.agent,
            "tool": self.tool,
            "status": self.status,
            "requires_campaign": self.requires_campaign,
            "command": list(self.command),
            "cwd": self.cwd,
            "artifacts": list(self.artifacts),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True)
class BoardProfile:
    board_id: str
    revision: str
    title: str
    aliases: tuple[str, ...]
    board_map: str
    sources: tuple[dict[str, str], ...]
    stages: tuple[BoardStage, ...]
    path: Path
    sha256: str

    @classmethod
    def load(cls, path: Path, *, project_root: Path) -> "BoardProfile":
        root = Path(project_root).resolve()
        path = Path(path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BoardProfileError("board profile is outside the project root") from exc
        try:
            raw_bytes = path.read_bytes()
            data = yaml.safe_load(raw_bytes) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise BoardProfileError(f"cannot read board profile {path}: {exc}") from exc
        if not isinstance(data, dict) or set(data) != _TOP:
            unknown = set(data) - _TOP if isinstance(data, dict) else set()
            missing = _TOP - set(data) if isinstance(data, dict) else _TOP
            raise BoardProfileError(
                f"board profile fields differ (missing={sorted(missing)}, unknown={sorted(unknown)})"
            )
        if data["schema_version"] != 1:
            raise BoardProfileError("board profile schema_version must be 1")
        board_id = _plain(data["board_id"], "board_id")
        if _ID.fullmatch(board_id) is None:
            raise BoardProfileError("board_id must be a lowercase identifier")
        revision = _plain(data["revision"], "revision")
        aliases_raw = data["aliases"]
        if not isinstance(aliases_raw, list) or not aliases_raw:
            raise BoardProfileError("aliases must be a non-empty list")
        aliases = tuple(_plain(item, "alias") for item in aliases_raw)
        if len({_normalized(item) for item in aliases}) != len(aliases):
            raise BoardProfileError("board aliases must be unique")
        board_map = _plain(data["board_map"], "board_map")
        _inside(root, board_map, "board_map")

        sources_raw = data["sources"]
        if not isinstance(sources_raw, list) or not sources_raw:
            raise BoardProfileError("sources must be a non-empty list")
        sources: list[dict[str, str]] = []
        for index, source in enumerate(sources_raw):
            if not isinstance(source, dict) or set(source) != _SOURCE:
                raise BoardProfileError(f"sources[{index}] has unknown or missing fields")
            row = {key: _plain(source[key], f"sources[{index}].{key}") for key in _SOURCE}
            if not row["location"].startswith(("https://", "http://")):
                _inside(root, row["location"], f"sources[{index}].location")
            sources.append(row)

        stages_raw = data["stages"]
        if not isinstance(stages_raw, list) or not stages_raw:
            raise BoardProfileError("stages must be a non-empty list")
        stages = tuple(cls._stage(item, index, root) for index, item in enumerate(stages_raw))
        ids = [item.stage_id for item in stages]
        if len(ids) != len(set(ids)):
            raise BoardProfileError("stage ids must be unique")
        phase_order = [_PHASES.index(item.phase) for item in stages]
        if phase_order != sorted(phase_order):
            raise BoardProfileError("stages must be ordered research through verify")
        return cls(
            board_id,
            revision,
            _plain(data["title"], "title"),
            aliases,
            board_map,
            tuple(sources),
            stages,
            path,
            hashlib.sha256(raw_bytes).hexdigest(),
        )

    @staticmethod
    def _stage(value: Any, index: int, root: Path) -> BoardStage:
        where = f"stages[{index}]"
        if not isinstance(value, dict) or set(value) - _STAGE:
            raise BoardProfileError(f"{where} has unknown fields")
        required = {
            "id", "phase", "mode", "status", "requires_campaign",
            "agent", "tool", "command", "cwd", "artifacts",
        }
        if not required <= set(value):
            raise BoardProfileError(f"{where} is missing {sorted(required - set(value))}")
        stage_id = _plain(value["id"], f"{where}.id")
        if _ID.fullmatch(stage_id) is None:
            raise BoardProfileError(f"{where}.id must be a lowercase identifier")
        phase = _plain(value["phase"], f"{where}.phase")
        mode = _plain(value["mode"], f"{where}.mode")
        status = _plain(value["status"], f"{where}.status")
        if phase not in _PHASES or mode not in _MODES or status not in _STATUSES:
            raise BoardProfileError(f"{where} has an unsupported phase, mode or status")
        campaign = value["requires_campaign"]
        if not isinstance(campaign, bool) or campaign != (mode == "hardware"):
            raise BoardProfileError(f"{where}.requires_campaign must match hardware mode")
        agent = value["agent"]
        tool = value["tool"]
        if agent is not None:
            agent = _plain(agent, f"{where}.agent")
        if tool is not None:
            tool = _plain(tool, f"{where}.tool")
        command_raw = value["command"]
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) and item for item in command_raw
        ):
            raise BoardProfileError(f"{where}.command must be a text list")
        cwd = value["cwd"]
        if cwd is not None:
            cwd = _plain(cwd, f"{where}.cwd")
            _inside(root, cwd, f"{where}.cwd")
        artifacts_raw = value["artifacts"]
        if not isinstance(artifacts_raw, list) or not all(
            isinstance(item, str) and item for item in artifacts_raw
        ):
            raise BoardProfileError(f"{where}.artifacts must be a text list")
        parameters = value.get("parameters", {})
        if not isinstance(parameters, dict):
            raise BoardProfileError(f"{where}.parameters must be an object")
        try:
            json.dumps(parameters, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise BoardProfileError(f"{where}.parameters must be finite JSON data") from exc
        if status == "ready" and (agent is None or tool is None):
            raise BoardProfileError(f"{where} is ready but has no agent/tool")
        if status == "adapter_required" and (agent is not None or tool is not None or command_raw):
            raise BoardProfileError(f"{where} needs an adapter and cannot name executable fields")
        if command_raw and (tool not in ("run_board_build", "run_board_stage") or cwd is None):
            raise BoardProfileError(f"{where} commands require a generic board runner and cwd")
        if tool == "measure_board_scope" and (phase != "measure" or command_raw or cwd is not None):
            raise BoardProfileError(
                f"{where} measure_board_scope must be a command-free measurement stage"
            )
        if tool == "measure_board_scope":
            BoardProfile._scope_parameters(parameters, where)
        return BoardStage(
            stage_id, phase, mode, agent, tool, status, campaign,
            tuple(command_raw), cwd, tuple(artifacts_raw), dict(parameters),
        )

    @staticmethod
    def _scope_parameters(parameters: dict[str, Any], where: str) -> None:
        required = {
            "channels", "timebase_s_per_div", "trigger_channel",
            "trigger_level_v", "trigger_slope", "timeout_s",
        }
        if set(parameters) != required:
            raise BoardProfileError(
                f"{where}.parameters must contain exactly {sorted(required)}"
            )
        rows = parameters["channels"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
            raise BoardProfileError(f"{where}.parameters.channels must contain 1..4 rows")
        channels: list[int] = []
        for index, row in enumerate(rows):
            expected = {"channel", "signal", "scale_v_per_div", "offset_v"}
            if not isinstance(row, dict) or set(row) != expected:
                raise BoardProfileError(
                    f"{where}.parameters.channels[{index}] must contain {sorted(expected)}"
                )
            channel = row["channel"]
            if not isinstance(channel, int) or isinstance(channel, bool) or not 1 <= channel <= 4:
                raise BoardProfileError(f"{where}.parameters channel must be 1..4")
            if channel in channels:
                raise BoardProfileError(f"{where}.parameters channels must be unique")
            channels.append(channel)
            _plain(row["signal"], f"{where}.parameters.channels[{index}].signal")
            scale = row["scale_v_per_div"]
            offset = row["offset_v"]
            if (
                not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0
                or not isinstance(offset, (int, float)) or isinstance(offset, bool)
            ):
                raise BoardProfileError(f"{where}.parameters channel scale/offset are invalid")
        if parameters["trigger_channel"] not in channels:
            raise BoardProfileError(f"{where}.parameters trigger_channel is not captured")
        if str(parameters["trigger_slope"]).upper() not in ("POS", "NEG"):
            raise BoardProfileError(f"{where}.parameters trigger_slope must be POS or NEG")
        for key in ("timebase_s_per_div", "timeout_s"):
            value = parameters[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise BoardProfileError(f"{where}.parameters.{key} must be positive")
        level = parameters["trigger_level_v"]
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            raise BoardProfileError(f"{where}.parameters.trigger_level_v must be numeric")

    def describe(self) -> dict[str, Any]:
        parts = self.path.parts
        try:
            project_index = parts.index("QKD_PROJ")
            profile_path = Path(*parts[project_index:]).as_posix()
        except ValueError:
            profile_path = self.path.name
        return {
            "board_id": self.board_id,
            "revision": self.revision,
            "title": self.title,
            "aliases": list(self.aliases),
            "board_map": self.board_map,
            "profile": profile_path,
            "sources": [dict(item) for item in self.sources],
            "stages": [item.describe() for item in self.stages],
            "profile_sha256": self.sha256,
            "ready_through": self.ready_through,
        }

    @property
    def ready_through(self) -> str:
        ready: list[str] = []
        for item in self.stages:
            if item.status != "ready":
                break
            ready.append(item.phase)
        return ready[-1] if ready else "none"


@dataclass(frozen=True)
class BoardCatalog:
    root: Path
    profiles: tuple[BoardProfile, ...]

    @classmethod
    def load_default(cls, project_root: Path) -> "BoardCatalog":
        root = Path(project_root).resolve()
        profiles = tuple(
            BoardProfile.load(path, project_root=root)
            for path in sorted((root / "QKD_PROJ" / "boards").glob("*/*/profile.yaml"))
        )
        identities = [(item.board_id, item.revision) for item in profiles]
        if len(identities) != len(set(identities)):
            raise BoardProfileError("board profile identities must be unique")
        return cls(root, profiles)

    def match(self, scenario: str) -> BoardProfile | None:
        normalized = _normalized(scenario)
        matched = [
            profile
            for profile in self.profiles
            if any(_normalized(alias) in normalized for alias in profile.aliases)
        ]
        if len(matched) > 1:
            names = [f"{item.board_id}@{item.revision}" for item in matched]
            raise BoardProfileError(f"scenario matches several board profiles: {names}")
        return matched[0] if matched else None

    def planning_context(self, scenario: str) -> dict[str, Any]:
        normalized = _normalized(scenario)
        matches = [p for p in self.profiles if any(_normalized(alias) in normalized for alias in p.aliases)]
        if len(matches) > 1:
            return {"status": "multiple", "selected": None,
                    "candidates": [p.describe() for p in matches],
                    "detail": "Lead chooses the requested devices; several boards may participate in one job."}
        selected = matches[0] if matches else None
        if selected is not None:
            return {"status": "profiled", "selected": selected.describe()}
        return {
            "status": "profile_required",
            "selected": None,
            "required_stages": list(_PHASES),
            "profile_location": "QKD_PROJ/boards/<board-id>/<revision>/profile.yaml",
            "detail": (
                "Research the exact board/revision and create a digest-bound profile before "
                "upload or measurement. Offline architecture and adapter implementation may proceed."
            ),
        }

    @property
    def digest(self) -> str:
        payload = [item.describe() for item in self.profiles]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
