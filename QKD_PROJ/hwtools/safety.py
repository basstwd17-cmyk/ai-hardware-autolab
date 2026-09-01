"""Contract enforcement for hardware tools.

This lives in layer 3, not layer 2, on purpose. If the gate sat in the
orchestrator, a new orchestrator -- or an agent calling a tool directly --
would bypass it. Every tool takes a SafetyGate and calls it before touching
hardware, so there is no path to the board that skips the contract.

Sources of truth:
  contracts/board-map.yaml       what the board actually is
  contracts/gate*-test-spec.yaml what is allowed, forbidden, and how to judge
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"

# Values that mean "a human has not confirmed this yet". Any tool that depends
# on such a field must refuse to run rather than guess.
UNVERIFIED = {None, "", "UNVERIFIED", "TBD", "unknown"}

# Operations that only read or observe. Allowed while the gate is still in
# document review, because they cannot change the board's state.
READ_ONLY_OPERATIONS = {
    "diamond_build",
    "firmware_build",
    "power_off_continuity_check",
    "readback_or_program_status",
    "scope_identify",
}


class SafetyViolation(RuntimeError):
    """Raised instead of touching hardware. Never catch this to retry."""


@dataclass(frozen=True)
class Contracts:
    board_map: dict[str, Any]
    test_spec: dict[str, Any]
    board_map_path: Path
    test_spec_path: Path

    @classmethod
    def load(
        cls,
        test_spec: str,
        contracts_dir: Path = CONTRACTS_DIR,
        *,
        project_root: Path | None = None,
    ) -> "Contracts":
        spec_path = contracts_dir / test_spec
        if not spec_path.is_file():
            raise FileNotFoundError(f"Contract not found: {spec_path}")
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        profile = spec.get("board_profile") if isinstance(spec, dict) else None
        board_reference = profile.get("board_map") if isinstance(profile, dict) else None
        if board_reference:
            root = Path(project_root or contracts_dir.parent.parent).resolve()
            board_path = (root / str(board_reference)).resolve()
            try:
                board_path.relative_to(root)
            except ValueError as exc:
                raise FileNotFoundError("Contract board_map escapes the project root") from exc
        else:
            board_path = contracts_dir / "board-map.yaml"
        if not board_path.is_file():
            raise FileNotFoundError(f"Contract not found: {board_path}")
        return cls(
            board_map=yaml.safe_load(board_path.read_text(encoding="utf-8")),
            test_spec=spec,
            board_map_path=board_path,
            test_spec_path=spec_path,
        )


def _walk(data: Any, dotted: str) -> Any:
    """Follow a dotted path like 'scope.model' through nested mappings."""
    node = data
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            raise SafetyViolation(f"Contract has no field '{dotted}'")
        node = node[key]
    return node


class SafetyGate:
    def __init__(self, contracts: Contracts, allow_hardware: bool = False) -> None:
        self.contracts = contracts
        # Default False encodes the current approval state recorded in
        # GATE1_REVIEW.md: DOCUMENTS ONLY. Turning this on is a human decision,
        # made once per session at the entry point -- never inside a tool.
        self.allow_hardware = allow_hardware
        self._spec = contracts.test_spec
        self._forbidden = set(self._spec.get("forbidden_operations") or [])
        self._allowed = set(self._spec.get("allowed_operations") or [])

    # -- operation permission ------------------------------------------------

    def require(self, operation: str) -> None:
        """Gate a named operation against the test spec. Call before acting."""
        if operation in self._forbidden:
            raise SafetyViolation(
                f"'{operation}' is listed in forbidden_operations of "
                f"{self.contracts.test_spec_path.name}"
            )
        if self._allowed and operation not in self._allowed:
            raise SafetyViolation(
                f"'{operation}' is not in allowed_operations of "
                f"{self.contracts.test_spec_path.name}. Add it to the contract "
                f"and have a human review the change first."
            )
        if operation not in READ_ONLY_OPERATIONS and not self.allow_hardware:
            raise SafetyViolation(
                f"'{operation}' would drive hardware but this run is in "
                f"documents-only mode. Pass allow_hardware=True explicitly, and "
                f"only after the approval checklist in GATE1_REVIEW.md is signed."
            )

    # -- data completeness ---------------------------------------------------

    def require_verified(self, *dotted_paths: str) -> dict[str, Any]:
        """Refuse to run when a value the tool depends on is still a placeholder.

        This is the rule from ARCHITECTURE.md: a model must not guess a value
        that a human has not confirmed. Blocking here is cheaper than a wrong
        probe on a TQFP144 pin.
        """
        resolved: dict[str, Any] = {}
        missing: list[str] = []
        for dotted in dotted_paths:
            value = _walk(self.contracts.board_map, dotted)
            if isinstance(value, str):
                comparable: Any = value.strip()
            else:
                comparable = value
            if comparable in UNVERIFIED:
                missing.append(dotted)
            else:
                resolved[dotted] = value
        if missing:
            raise SafetyViolation(
                "Unverified board-map fields block this operation: "
                + ", ".join(missing)
                + f". Confirm them against the schematic and datasheet, then "
                f"update {self.contracts.board_map_path.name}."
            )
        return resolved

    def require_approved(self, *dotted_paths: str) -> None:
        """Refuse when a contract field explicitly says approved: false."""
        for dotted in dotted_paths:
            if _walk(self.contracts.board_map, dotted) is not True:
                raise SafetyViolation(
                    f"'{dotted}' is not approved in "
                    f"{self.contracts.board_map_path.name}"
                )

    # -- thresholds ----------------------------------------------------------

    def pass_criteria(self) -> dict[str, Any]:
        """Thresholds a tool judges against. Deterministic, never model output."""
        return dict(self._spec.get("pass_criteria") or {})

    def stop_conditions(self) -> list[str]:
        return list(self._spec.get("stop_conditions") or [])

    def describe(self) -> dict[str, Any]:
        return {
            "test_id": self._spec.get("test_id"),
            "status": self._spec.get("status"),
            "board_map": self.contracts.board_map_path.name,
            "test_spec": self.contracts.test_spec_path.name,
            "allow_hardware": self.allow_hardware,
            "allowed_operations": sorted(self._allowed),
            "forbidden_operations": sorted(self._forbidden),
        }
