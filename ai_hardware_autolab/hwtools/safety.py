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
    # Local source work is an offline effect, not a hardware effect.  The
    # per-job workspace and its write-scope audit are the authority for it.
    "workspace_development",
    "diamond_build",
    "firmware_build",
    "power_off_continuity_check",
    "readback_or_program_status",
    "scope_identify",
    "bbb_identify",
}

SCOPE_OPERATIONS = {"scope_identify", "oscilloscope_capture"}


def _high_impedance(value: Any) -> bool:
    return str(value or "").lower().replace(" ", "").replace("_", "") in {
        "1mohm", "1mω", "1m", "high-z", "highz",
    }


def _signal_key(value: str) -> str:
    """Normalize conventional bus labels, never guess a physical pin mapping."""
    key = value.strip().upper()
    bare = key.removeprefix("SPI.")
    if bare in {"CS", "CLK", "SCLK", "MOSI", "MISO"}:
        return "CLK" if bare == "SCLK" else bare
    return key


class SafetyViolation(RuntimeError):
    """Raised instead of touching hardware. Never catch this to retry."""


@dataclass(frozen=True)
class Contracts:
    board_map: dict[str, Any]
    test_spec: dict[str, Any]
    board_map_path: Path
    test_spec_path: Path
    project_root: Path | None = None

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
        return cls.from_spec(spec, spec_path, project_root=project_root)

    @classmethod
    def from_spec(cls, spec: dict[str, Any], spec_path: Path, *, project_root: Path | None = None) -> "Contracts":
        """Resolve supplied contract bytes before installation; never grants access."""
        contracts_dir = spec_path.parent
        profile = spec.get("board_profile") if isinstance(spec, dict) else None
        # A connection map does not need a board/stage catalog around it.
        board_reference = spec.get("device_map") or (profile.get("board_map") if isinstance(profile, dict) else None)
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
            project_root=root if board_reference else contracts_dir.parent.parent,
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
    def __init__(self, contracts: Contracts, allow_hardware: bool = False, *,
                 delegated_operation: str | None = None) -> None:
        self.contracts = contracts
        # Default False encodes the current approval state recorded in
        # GATE1_REVIEW.md: DOCUMENTS ONLY. Turning this on is a human decision,
        # made once per session at the entry point -- never inside a tool.
        self.allow_hardware = allow_hardware
        # Host-only dispatch context, never read from agent-authored YAML or
        # tool arguments. The launcher/one-attempt broker owns this authority.
        self.delegated_operation = delegated_operation
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
        if operation not in READ_ONLY_OPERATIONS and self.allow_hardware:
            self.require_generated_hardware_safety(operation)

    def require_generated_hardware_safety(self, operation: str | None = None) -> None:
        """Generated hardware authority must carry cited electrical bounds.

        Legacy reviewed contracts keep their existing review path. A contract
        created by an agent, however, may not reach hardware with only prose
        saying it is safe: the exact voltage/current limits, pin directions
        and stop conditions are part of the bytes a person campaign-approves.
        """
        if self._spec.get("status") == "proposed_hardware_boundary":
            operations = set(self._spec.get("allowed_operations") or [])
            if operation in SCOPE_OPERATIONS or (operation is None and operations and operations <= SCOPE_OPERATIONS):
                self.require_instrument_boundary()
                return
        if self._spec.get("status") not in {"reviewed_generated_hardware", "proposed_hardware_boundary"}:
            return
        safety = self._spec.get("hardware_safety")
        if not isinstance(safety, dict):
            raise SafetyViolation("generated hardware contract has no hardware_safety envelope")
        limits = safety.get("limits")
        pins = safety.get("pin_assignments")
        stops = safety.get("stop_conditions")
        if not isinstance(limits, list) or not limits:
            raise SafetyViolation("generated hardware contract has no electrical limits")
        parameters = {
            str(item.get("parameter") or "").lower()
            for item in limits
            if isinstance(item, dict)
        }
        # Scanning a JTAG chain drives its interface but does not enable a
        # supply rail. Do not ask for an unrelated board supply-current limit.
        required = {"voltage"} if operation == "jtag_scan" else {"voltage", "current"}
        if not required <= parameters:
            raise SafetyViolation("generated hardware contract lacks voltage/current bounds")
        if not isinstance(pins, list) or not pins:
            raise SafetyViolation("generated hardware contract has no pin assignments")
        if not isinstance(stops, list) or not stops:
            raise SafetyViolation("generated hardware contract has no stop conditions")

    def instrument_scope(self) -> dict[str, Any]:
        """Read a scope permission without inventing board/programming inputs.

        Existing hand-reviewed scope contracts remain usable. New proposals
        still require explicit input bounds. This normalizes data, not tasks.
        """
        # A Lead may keep one integrated multi-device plan/configuration. This
        # view describes ONLY the scope; it neither grants the other operations
        # nor requires their board-programming inputs for a passive measurement.
        operations = self._allowed & SCOPE_OPERATIONS
        if not operations:
            raise SafetyViolation("contract has no passive scope operation")
        if operations & self._forbidden:
            raise SafetyViolation("scope operation is explicitly forbidden")
        if operations == {"scope_identify"}:
            # Identification never selects a channel or changes an input. The
            # endpoint remains bound by the contract/map and scenario approval.
            self.require("scope_identify")
            resolved = self.require_verified("scope.model", "scope.host", "scope.port")
            device = {key: resolved[f"scope.{key}"] for key in ("model", "host", "port")}
            declared = (self._spec.get("hardware_safety") or {}).get("devices")
            if declared is not None:
                self._scope_device(declared)
            return {"devices": [device], "scope_channels": [], "identity_only": True,
                    "stop_conditions": ["close connection on identification failure"]}
        if self._spec.get("hardware_safety") is not None:
            self.require_instrument_boundary()
            safety = self._spec["hardware_safety"]
            return {"devices": [self._scope_device(safety.get("devices"))],
                    "scope_channels": safety["scope_channels"],
                    "stop_conditions": safety["stop_conditions"]}
        if self._spec.get("status") != "active":
            raise SafetyViolation("new instrument boundary needs explicit input bounds")
        source = self._spec.get("signal_source") or {}
        channel = source.get("channel")
        termination = (self._spec.get("pass_criteria") or {}).get("required_termination")
        stops = self._spec.get("stop_conditions")
        if type(channel) is not int or channel not in range(1, 5) or termination != "1_Mohm" or not stops:
            raise SafetyViolation("existing scope contract needs its channel, 1 Mohm input and stop conditions")
        resolved = self.require_verified("scope.model", "scope.host", "scope.port")
        model = (self._spec.get("instrument") or {}).get("model")
        if model != resolved["scope.model"]:
            raise SafetyViolation("existing contract instrument differs from the configured scope")
        return {"devices": [{key: resolved[f"scope.{key}"] for key in ("model", "host", "port")}],
                "scope_channels": [{"channel": channel, "signal": str(source.get("terminal") or source.get("connection") or ""),
                                    "termination": termination, "source": self.contracts.test_spec_path.name}],
                "stop_conditions": list(stops), "legacy_reviewed_boundary": True}

    def _scope_device(self, devices: Any) -> dict[str, Any]:
        resolved = self.require_verified("scope.model", "scope.host", "scope.port")
        expected = {key: resolved[f"scope.{key}"] for key in ("model", "host", "port")}
        if not isinstance(devices, list) or not devices or any(not isinstance(d, dict) for d in devices):
            raise SafetyViolation("instrument boundary must identify exactly one scope")
        candidates = [d for d in devices if d.get("kind", d.get("type")) in {"scope", "oscilloscope"}
                      or d.get("model") == expected["model"] or d.get("host") == expected["host"]]
        if len(devices) == 1:
            candidates = devices
        if len(candidates) != 1 or any(candidates[0].get(k) != v for k, v in expected.items()):
            raise SafetyViolation("instrument boundary differs from the actual configured scope endpoint")
        return expected

    def require_instrument_boundary(self) -> None:
        """Passive input configuration needs instrument bounds, not a DUT board.

        This is a tool effect boundary, not scenario classification. It never
        enables an output, power supply, programmer or board communication.
        """
        import math
        safety = self._spec.get("hardware_safety")
        stops = safety.get("stop_conditions") if isinstance(safety, dict) else None
        if not isinstance(stops, list) or not stops or any(not isinstance(item, str) or not item.strip() for item in stops):
            raise SafetyViolation("instrument boundary needs stop conditions")
        self._scope_device(safety.get("devices"))
        channels = safety.get("scope_channels")
        if not isinstance(channels, list) or not channels:
            raise SafetyViolation("instrument boundary needs scope channels and input voltage bounds")
        seen = set()
        for row in channels:
            if not isinstance(row, dict):
                raise SafetyViolation("invalid scope channel")
            ch, limit = row.get("channel"), row.get("max_abs_input_v")
            if type(ch) is not int or ch not in range(1, 5) or ch in seen:
                raise SafetyViolation("scope channel must be unique and in 1..4")
            seen.add(ch)
            if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
                raise SafetyViolation("scope channel needs a finite positive max_abs_input_v")
            if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("source", "signal")):
                raise SafetyViolation("scope input bound needs its source and connected signal")
            if not _high_impedance(row.get("termination")):
                raise SafetyViolation("generic passive capture supports only 1 Mohm input")

    def require_scope_capture(self, params: dict[str, Any]) -> None:
        """Bind a generated contract's reviewed channel map to one capture.

        The scope recipe already forces a 1 Mohm DC input.  This check adds
        the part that cannot live in the recipe: which campaign-reviewed DUT
        signal may be connected to each channel.  Legacy hand-reviewed
        contracts keep their existing path.
        """
        safety = self._spec.get("hardware_safety")
        if not isinstance(safety, dict) or not safety.get("scope_channels"):
            return
        if self._spec.get("status") in {"reviewed_generated_hardware", "proposed_hardware_boundary"}:
            self.require_generated_hardware_safety("oscilloscope_capture")
        else:
            self.require_instrument_boundary()
        reviewed = safety.get("scope_channels")
        if not isinstance(reviewed, list) or not reviewed:
            raise SafetyViolation(
                "generated hardware contract has no reviewed scope_channels mapping"
            )
        expected: dict[int, tuple[str, str]] = {}
        for index, row in enumerate(reviewed):
            if not isinstance(row, dict):
                raise SafetyViolation(f"hardware_safety.scope_channels[{index}] is not an object")
            channel = row.get("channel")
            signal = row.get("signal")
            termination = str(row.get("termination") or "").lower().replace(" ", "")
            if (
                not isinstance(channel, int)
                or isinstance(channel, bool)
                or not 1 <= channel <= 4
                or not isinstance(signal, str)
                or not signal.strip()
            ):
                raise SafetyViolation(
                    f"hardware_safety.scope_channels[{index}] has an invalid channel/signal"
                )
            if not _high_impedance(termination):
                raise SafetyViolation(
                    f"scope CH{channel} must be reviewed for 1 Mohm/high-Z termination"
                )
            if channel in expected:
                raise SafetyViolation("hardware_safety scope channels must be unique")
            expected[channel] = (signal.strip(), termination)

        requested = params.get("channels")
        if not isinstance(requested, list) or not requested:
            raise SafetyViolation("scope capture has no channel mapping")
        actual: dict[int, str] = {}
        for row in requested:
            if not isinstance(row, dict):
                raise SafetyViolation("scope capture channel mapping is invalid")
            channel, signal = row.get("channel"), row.get("signal")
            if type(channel) is not int or channel not in range(1, 5) or channel in actual or not isinstance(signal, str):
                raise SafetyViolation("scope capture channel mapping is invalid")
            actual[channel] = signal.strip()
        expected_signals = {channel: signal for channel, (signal, _) in expected.items()}
        if not actual or any(channel not in expected_signals or _signal_key(expected_signals[channel]) != _signal_key(signal)
                             for channel, signal in actual.items()):
            raise SafetyViolation(
                f"scope capture channels {actual} differ from reviewed mapping {expected_signals}"
            )

    def require_measured_voltage(self, measurements: dict[str, Any]) -> None:
        """Trip when a captured waveform exceeds a reviewed voltage maximum."""
        safety = self._spec.get("hardware_safety")
        if (isinstance(safety, dict) and safety.get("scope_channels")
                and "oscilloscope_capture" in self._allowed):
            import math
            self.require_instrument_boundary()
            observed_channels = 0
            for row in safety["scope_channels"]:
                if not any(f"CH{row['channel']}_{suffix}" in measurements for suffix in ("min_v", "max_v")):
                    continue  # approved channels need not all be measured each time
                observed_channels += 1
                for suffix in ("min_v", "max_v"):
                    value = measurements.get(f"CH{row['channel']}_{suffix}")
                    if (type(value) not in (int, float) or not math.isfinite(value)
                            or abs(value) > row["max_abs_input_v"]):
                        raise SafetyViolation(f"CH{row['channel']} voltage is absent, invalid or outside its approved input bound")
            if not observed_channels:
                raise SafetyViolation("capture returned no approved channel voltage measurements")
            return
        if self._spec.get("status") not in {"reviewed_generated_hardware", "proposed_hardware_boundary"}:
            return
        safety = self._spec.get("hardware_safety") or {}
        maxima = [
            _volts(item.get("maximum"), item.get("unit"))
            for item in safety.get("limits") or []
            if isinstance(item, dict)
            and str(item.get("parameter") or "").strip().lower() == "voltage"
        ]
        maxima = [value for value in maxima if value is not None]
        if not maxima:
            raise SafetyViolation("generated hardware contract has no usable voltage limit")
        maximum = min(maxima)
        observed: list[tuple[str, float]] = []
        for key, value in measurements.items():
            if not (key.endswith("_min_v") or key.endswith("_max_v")):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                observed.append((key, abs(float(value))))
        if not observed:
            raise SafetyViolation("scope capture returned no measured voltage extrema")
        exceeded = [(key, value) for key, value in observed if value > maximum]
        if exceeded:
            detail = ", ".join(f"{key}={value:.6g} V" for key, value in exceeded)
            raise SafetyViolation(
                f"measured voltage exceeded the reviewed {maximum:.6g} V limit: {detail}"
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
        direct = self._spec.get("stop_conditions")
        safety = self._spec.get("hardware_safety")
        nested = safety.get("stop_conditions") if isinstance(safety, dict) else None
        return list(direct or nested or [])

    def describe(self) -> dict[str, Any]:
        return {
            "test_id": self._spec.get("test_id"),
            "status": self._spec.get("status"),
            "board_map": self.contracts.board_map_path.name,
            "test_spec": self.contracts.test_spec_path.name,
            "allow_hardware": self.allow_hardware,
            "delegated_operation": self.delegated_operation,
            "allowed_operations": sorted(self._allowed),
            "forbidden_operations": sorted(self._forbidden),
        }


def _volts(value: Any, unit: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    scale = {
        "v": 1.0,
        "volt": 1.0,
        "volts": 1.0,
        "mv": 1e-3,
        "millivolt": 1e-3,
        "millivolts": 1e-3,
    }.get(str(unit or "").strip().lower())
    return float(value) * scale if scale is not None and value >= 0 else None
