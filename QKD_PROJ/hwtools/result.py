"""Structured return values for every hardware tool call.

Layer 3 tools do not communicate through console text. Every tool returns a
ToolResult that layer 2 (the orchestrator) and layer 1 (the AI agent) can read
without parsing prose. This is what makes the AI on top replaceable: swap the
agent and the contract with the hardware is unchanged.

Rule: if a fact is needed to reproduce a measurement, it belongs in here --
not in a log line.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
WARNING = "WARNING"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Artifact:
    """A file produced by a tool, pinned by content hash."""

    path: str
    kind: str
    sha256: str
    size_bytes: int

    @classmethod
    def of(cls, path: Path, kind: str, relative_to: Path | None = None) -> "Artifact":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact does not exist: {path}")
        shown = str(path.relative_to(relative_to)) if relative_to else str(path)
        return cls(
            path=shown.replace("\\", "/"),
            kind=kind,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
        )


@dataclass
class ToolResult:
    """The single return type of every layer 3 function."""

    tool: str
    action: str
    ok: bool = True
    started_at: str = field(default_factory=utc_now)
    duration_s: float = 0.0

    # Exactly what was sent to the outside world: argv lines for subprocesses,
    # SCPI lines for instruments. Enough to replay the run by hand.
    commands: list[str] = field(default_factory=list)

    # Deterministic numbers. Layer 3 measures; layer 1 interprets.
    measurements: dict[str, Any] = field(default_factory=dict)

    # PASS / FAIL / WARNING against thresholds from the test spec, or None when
    # the tool does not judge. Never set this from an AI model.
    verdict: str | None = None

    artifacts: list[Artifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    # Identity of whatever produced the result: Diamond version, *IDN? string,
    # firmware build hash. Without this a number cannot be reproduced later.
    versions: dict[str, str] = field(default_factory=dict)

    def add_artifact(self, path: Path, kind: str, relative_to: Path | None = None) -> Artifact:
        artifact = Artifact.of(path, kind, relative_to)
        self.artifacts.append(artifact)
        return artifact

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def fail(self, message: str) -> "ToolResult":
        self.ok = False
        self.error = message
        self.verdict = FAIL
        return self

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["artifacts"] = [asdict(a) for a in self.artifacts]
        return data

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def __str__(self) -> str:
        head = f"[{'ok' if self.ok else 'FAILED'}] {self.tool}.{self.action}"
        if self.verdict:
            head += f" -> {self.verdict}"
        if self.error:
            head += f": {self.error}"
        return head
