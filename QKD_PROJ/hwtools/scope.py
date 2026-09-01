"""measure_scope() -- the layer 3 entry point for any oscilloscope measurement.

This function does not know what is being measured. It enforces the contract,
connects, hands control to a recipe, and turns whatever the recipe produced
into a ToolResult with full provenance. Swapping the measurement means swapping
the recipe, not editing this file.

    measure_scope(recipe=jitter_on_dbg0, params={"n": 200}, gate=gate, record=record)
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .instruments import RTM3004, Waveform
from .record import RunRecord
from .result import FAIL, ToolResult
from .safety import SafetyGate, SafetyViolation
from .scpi import ScpiError

# Values a recipe must record for its result to be reproducible. "How many
# samples is enough" is the recipe's judgement, but the number it chose is not
# optional -- without it the same measurement cannot be repeated.
REQUIRED_MEASUREMENT_KEYS = ("sample_count", "retrieval_path")


class MissingRecord(RuntimeError):
    """A record the test spec named by title is absent from the measurements."""


def _missing_records(
    measurements: dict[str, Any], required: "tuple[str, ...] | list[str]"
) -> list[str]:
    """Names from the contract that nothing in the measurements satisfies.

    Matches a bare name against a channel-prefixed key too, so a contract can
    ask for `probe_attenuation` and be satisfied by `CH3_probe_attenuation`.
    """
    keys = set(measurements)
    missing = []
    for name in required:
        if name in keys or any(k.endswith(f"_{name}") for k in keys):
            continue
        missing.append(name)
    return missing


@dataclass
class RecipeOutcome:
    """What a recipe hands back. Numbers only -- no interpretation."""

    measurements: dict[str, Any] = field(default_factory=dict)
    verdict: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Channels the recipe probed as logic outputs, checked for 50 ohm loading.
    probed_channels: tuple[int, ...] = ()
    # Raw captures used to reach the verdict. The fixed layer serialises these
    # into the RunRecord so a screenshot is never the only evidence.
    waveforms: tuple[Waveform, ...] = ()


class Recipe(Protocol):
    def __call__(
        self, scope: RTM3004, params: dict[str, Any], gate: SafetyGate
    ) -> RecipeOutcome: ...


def measure_scope(
    recipe: Recipe,
    params: dict[str, Any],
    gate: SafetyGate,
    record: RunRecord | None = None,
    *,
    operation: str = "oscilloscope_capture",
    screenshot: bool = True,
    mock: bool = False,
    mock_responses: dict[str, str] | None = None,
) -> ToolResult:
    started = time.monotonic()
    name = getattr(recipe, "__name__", "recipe")
    result = ToolResult(tool="scope", action=name)
    result.measurements["recipe"] = name
    result.measurements["params"] = dict(params)

    try:
        gate.require(operation)
        resolved = gate.require_verified(
            "scope.model", "scope.resource", "scope.host", "scope.port"
        )
    except SafetyViolation as exc:
        result.commands = []
        return _finish(result, started, record, failure=str(exc))

    host, port = str(resolved["scope.host"]), int(resolved["scope.port"])
    try:
        scope = RTM3004(host=host, port=port, mock=mock, mock_responses=mock_responses)
    except OSError as exc:
        # A cable or an instrument that is off is one of the most common
        # failures, so it has to arrive as a result like any other. Raising
        # here would break the promise that this function always returns one.
        return _finish(
            result,
            started,
            record,
            failure=(
                f"Cannot reach the scope at {host}:{port} ({exc}). Check the "
                f"link and that the address matches board-map.yaml."
            ),
        )

    try:
        identity = scope.identify()
        result.versions.update(identity.as_versions())
        if identity.model != resolved["scope.model"]:
            raise ScpiError(
                f"Connected instrument is {identity.model}, but the contract "
                f"says {resolved['scope.model']}. Refusing to attribute a "
                f"measurement to the wrong instrument."
            )

        outcome = recipe(scope, params, gate)

        # Recorded here rather than left to the recipe: probe attenuation and
        # coupling scale and qualify every voltage the recipe reports, and the
        # instrument changes them on its own when a probe is plugged in. A
        # rewritten recipe cannot drop them by forgetting.
        for channel in outcome.probed_channels:
            result.measurements.update(scope.front_end(channel).as_measurements())

        # A recipe is free to choose how it probes, but not to load the DUT.
        if outcome.probed_channels:
            scope.assert_high_impedance(*outcome.probed_channels)

        scope.check_errors(f"recipe {name}")

        result.measurements.update(outcome.measurements)
        result.verdict = outcome.verdict
        for warning in outcome.warnings:
            result.warn(warning)

        # Checked against result.measurements, which already includes the
        # front-end facts, so a contract can require probe_attenuation without
        # every recipe having to report it itself.
        required = gate.pass_criteria().get("required_records")
        missing = _missing_records(
            result.measurements, required or REQUIRED_MEASUREMENT_KEYS
        )
        if missing and required:
            # The contract asked for these by name, so their absence is a
            # failed test, not a note.
            raise MissingRecord(
                f"The test spec requires {', '.join(missing)} in the "
                f"measurements, and recipe '{name}' did not produce them. "
                f"The reported numbers could not be reproduced from this record."
            )
        if missing:
            result.warn(
                f"Recipe '{name}' did not record {', '.join(missing)}; this "
                f"measurement cannot be reproduced from the record alone."
            )

        if record is not None:
            for waveform in outcome.waveforms:
                path = _save_waveform_csv(
                    record.dir / f"{name}_CH{waveform.channel}.csv", waveform
                )
                result.add_artifact(path, kind="waveform_csv", relative_to=record.dir)

        if screenshot and record is not None:
            path = scope.save_screenshot(record.dir / f"{name}.png")
            result.add_artifact(path, kind="screenshot", relative_to=record.dir)

        return _finish(result, started, record)

    except Exception as exc:  # noqa: BLE001 - every failure must reach SAFE state
        scope.emergency_stop()
        return _finish(result, started, record, failure=f"{type(exc).__name__}: {exc}")
    finally:
        result.commands = scope.scpi.commands()
        scope.close()


def _save_waveform_csv(path: Path, waveform: Waveform) -> Path:
    """Persist the exact samples and their time axis used by a recipe."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    times = waveform.times_s()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("time_s", "voltage_v"))
        writer.writerows(zip(times, waveform.values_v, strict=True))
    return path


def _finish(
    result: ToolResult,
    started: float,
    record: RunRecord | None,
    failure: str | None = None,
) -> ToolResult:
    result.duration_s = round(time.monotonic() - started, 3)
    if failure is not None:
        result.ok = False
        result.error = failure
        result.verdict = FAIL
    if record is not None:
        record.add(result)
    return result
