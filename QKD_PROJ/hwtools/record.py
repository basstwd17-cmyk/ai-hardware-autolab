"""Run records: one directory per test run, hashed and self-describing.

A measurement that cannot be traced back to the exact source, contract and
instrument state that produced it is not evidence. This module is what turns a
sequence of ToolResults into that evidence.

Layout:
  runs/<UTC timestamp>_<name>/
    manifest.json     contract snapshot, tool results, artifact hashes
    run_status.json   final verdict, short enough to poll
    <artifacts>       bitstreams, logs, waveform CSV, screenshots
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .result import FAIL, PASS, WARNING, ToolResult, utc_now
from .safety import SafetyGate

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


def _utc_run_stamp() -> str:
    """Human-readable UTC prefix shared by every run directory."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class RunRecord:
    def __init__(self, name: str, gate: SafetyGate, runs_dir: Path = RUNS_DIR) -> None:
        stamp = _utc_run_stamp()
        self.name = name
        self.gate = gate
        runs_dir = Path(runs_dir)

        # mkdir(exist_ok=False) is the arbiter, rather than a preceding
        # exists() check. Two orchestrators can choose a directory at the same
        # time; only the atomic create tells us which one won. Keep the old,
        # readable name for the first run and add a deterministic suffix only
        # when that exact second/name pair is already occupied.
        collision = 0
        while True:
            suffix = "" if collision == 0 else f"_{collision:03d}"
            candidate = runs_dir / f"{stamp}_{name}{suffix}"
            try:
                candidate.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                collision += 1
                continue
            self.dir = candidate
            break
        self.started_at = utc_now()
        self.results: list[ToolResult] = []
        self.verdict: str | None = None

    # -- collecting ----------------------------------------------------------

    def add(self, result: ToolResult) -> ToolResult:
        self.results.append(result)
        self.write()  # flush after every step so a crash still leaves evidence
        return result

    def adopt(self, source: Path, kind: str) -> Path:
        """Copy an external file (bitstream, build log) into the run directory.

        Tools reference artifacts by their in-run path so the record stays valid
        even after the source tree moves or is rebuilt.
        """
        source = Path(source)
        target = self.dir / source.name
        shutil.copy2(source, target)
        return target

    # -- verdict -------------------------------------------------------------

    def finish(self, verdict: str | None = None) -> str:
        """Close the run. Verdict defaults to the worst of the step verdicts."""
        if verdict is None:
            if any(r.verdict == FAIL or not r.ok for r in self.results):
                verdict = FAIL
            elif any(r.verdict == WARNING or r.warnings for r in self.results):
                verdict = WARNING
            elif self.results and all(r.verdict == PASS for r in self.results):
                verdict = PASS
            else:
                verdict = "INCOMPLETE"
        self.verdict = verdict
        self.write()
        return verdict

    # -- persistence ---------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        return {
            "run": self.name,
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "verdict": self.verdict,
            "contract": self.gate.describe(),
            "pass_criteria": self.gate.pass_criteria(),
            "steps": [r.to_dict() for r in self.results],
            "artifacts": [
                asdict(a) for r in self.results for a in r.artifacts
            ],
        }

    def status(self) -> dict[str, Any]:
        return {
            "run": self.name,
            "verdict": self.verdict,
            "updated_at": utc_now(),
            "steps": [
                {
                    "tool": r.tool,
                    "action": r.action,
                    "ok": r.ok,
                    "verdict": r.verdict,
                    "error": r.error,
                }
                for r in self.results
            ],
        }

    def write(self) -> None:
        (self.dir / "manifest.json").write_text(
            json.dumps(self.manifest(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (self.dir / "run_status.json").write_text(
            json.dumps(self.status(), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def __str__(self) -> str:
        return f"{self.name} -> {self.verdict or 'running'} ({self.dir})"
