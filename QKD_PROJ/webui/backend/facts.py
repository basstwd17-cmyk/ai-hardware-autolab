"""Details the event stream references but does not carry.

The ledger records a contract by **name** -- ``job.created`` carries
``gate``, which ``ControlPlane.start`` has already forced to equal the
preflight-selected contract's file name. It does not record that contract's
digest, its ``test_id`` or its ``allowed_operations``, because a ledger row is
a record of what happened, not a copy of the file it happened against.

The graph wants those. So they are looked up here, from the files themselves,
behind an interface the graph is given rather than one it imports. Two reasons
that boundary is worth the extra type:

* **Determinism.** ``graph.py`` stays a pure function of (frames, facts). A
  test hands it a ``FrozenFacts`` and gets the same graph every run, with no
  contracts directory in sight.
* **Honesty about provenance.** An edge derived from a file carries
  ``source_event_id: None``. Nothing in the graph can claim a ledger cursor for
  something a ledger row never said.

A name that resolves to nothing is not an error and not a reason to drop the
node. It stays in the graph as an unresolved reference -- the ledger did name
it -- and gains its details if the file appears later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

QKD_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACTS = QKD_ROOT / "contracts"
DEFAULT_RUNS = QKD_ROOT / "runs"


@dataclass(frozen=True)
class ContractFacts:
    """What a contract file says about itself. Browser-safe: no paths."""

    name: str
    sha256: str
    gate: str | None = None
    status: str | None = None
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunFacts:
    """What a run's manifest says. Browser-safe: no paths."""

    run_id: str
    contract: str | None = None
    gate: str | None = None
    verdict: str | None = None
    bitstreams: tuple[str, ...] = ()


class Facts(Protocol):
    """Everything the graph may ask about something it only has a name for."""

    def contract(self, name: str) -> ContractFacts | None: ...

    def run(self, run_id: str) -> RunFacts | None: ...


class NoFacts:
    """Resolves nothing. Every reference stays unresolved, and that is legal."""

    def contract(self, name: str) -> ContractFacts | None:
        return None

    def run(self, run_id: str) -> RunFacts | None:
        return None


@dataclass
class FrozenFacts:
    """A fixed answer set. For tests, and for a snapshot that must not drift."""

    contracts: dict[str, ContractFacts] = field(default_factory=dict)
    runs: dict[str, RunFacts] = field(default_factory=dict)

    def contract(self, name: str) -> ContractFacts | None:
        return self.contracts.get(name)

    def run(self, run_id: str) -> RunFacts | None:
        return self.runs.get(run_id)


@dataclass
class FileFacts:
    """Reads ``contracts/*.yaml`` and ``runs/*/manifest.json``, once each.

    Cached because a graph rebuild from cursor 0 asks about the same contract
    once per job, and because a file that changes underneath a running graph
    would make the same replay produce two different answers.
    """

    contracts_dir: Path = DEFAULT_CONTRACTS
    runs_dir: Path = DEFAULT_RUNS
    _contracts: dict[str, ContractFacts | None] = field(default_factory=dict, init=False)
    _runs: dict[str, RunFacts | None] = field(default_factory=dict, init=False)

    def contract(self, name: str) -> ContractFacts | None:
        if name not in self._contracts:
            self._contracts[name] = self._read_contract(name)
        return self._contracts[name]

    def run(self, run_id: str) -> RunFacts | None:
        if run_id not in self._runs:
            self._runs[run_id] = self._read_run(run_id)
        return self._runs[run_id]

    # -- reading ----------------------------------------------------------

    def _read_contract(self, name: str) -> ContractFacts | None:
        path = _within(self.contracts_dir, name)
        if path is None or not path.is_file():
            return None
        raw = path.read_bytes()
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return ContractFacts(
            name=name,
            # The same digest ``ScenarioPreflight.verify_contract`` compares
            # against: the file's bytes, not a canonicalised re-serialisation.
            sha256=hashlib.sha256(raw).hexdigest(),
            gate=_string(data.get("test_id")),
            status=_string(data.get("status")),
            operations=tuple(sorted(_strings(data.get("allowed_operations")))),
        )

    def _read_run(self, run_id: str) -> RunFacts | None:
        directory = _within(self.runs_dir, run_id)
        if directory is None:
            return None
        manifest = directory / "manifest.json"
        if not manifest.is_file():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        contract = data.get("contract") if isinstance(data.get("contract"), dict) else {}
        bitstreams = sorted(
            {
                digest
                for artifact in data.get("artifacts") or []
                if isinstance(artifact, dict) and artifact.get("kind") == "bitstream"
                for digest in [_string(artifact.get("sha256"))]
                if digest
            }
        )
        return RunFacts(
            run_id=run_id,
            contract=_string(contract.get("test_spec")),
            gate=_string(contract.get("test_id")),
            verdict=_string(data.get("verdict")),
            bitstreams=tuple(bitstreams),
        )


def _within(root: Path, name: str) -> Path | None:
    """``root / name``, or nothing if ``name`` tries to leave ``root``.

    The names come from ledger rows and from artifact paths, which are not
    attacker-controlled -- but they are *data*, and a lookup that resolves
    ``../../`` is a file read this module was never asked to do.
    """
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]
