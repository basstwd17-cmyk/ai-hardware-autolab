"""measure_scope() -- the layer 3 entry point for any oscilloscope measurement.

This function does not know what is being measured. It enforces the contract,
connects, hands control to a recipe, and turns whatever the recipe produced
into a ToolResult with full provenance. Swapping the measurement means swapping
the recipe, not editing this file.

    measure_scope(recipe=jitter_on_dbg0, params={"n": 200}, gate=gate, record=record)
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .instruments import RTM3004, Waveform
from .record import RunRecord
from .result import FAIL, ToolResult
from .safety import SafetyGate, SafetyViolation
from .measurement_stats import EdgePolicy, waveform_statistics
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


def identify_scope(gate: SafetyGate, record: RunRecord | None = None) -> ToolResult:
    """Read identity only, including on failure: no STOP, reset or acquisition."""
    from .result import PASS
    started = time.monotonic()
    result = ToolResult(tool="scope", action="identify")
    result.measurements["identity_only"] = True
    result.measurements["diagnostic_complete"] = False
    scope, failure = None, None
    phase = "authorization"
    try:
        gate.require("scope_identify")
        phase = "configuration"
        resolved = gate.require_verified("scope.model", "scope.host", "scope.port")
        result.measurements["target"] = {"host": str(resolved["scope.host"]), "port": int(resolved["scope.port"])}
        phase = "tcp_connect"
        scope = RTM3004(host=str(resolved["scope.host"]), port=int(resolved["scope.port"]))
        phase = "scpi_identification"
        raw = scope.scpi.query("*IDN?")
        result.measurements["identification_response"] = raw
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) < 4 or not parts[1]:
            raise ScpiError("Malformed *IDN? response")
        manufacturer, model, serial, firmware = parts[:4]
        phase = "model_validation"
        if model != resolved["scope.model"]:
            raise ScpiError(f"Expected {resolved['scope.model']}, received {model}")
        phase = "option_query"
        options = scope.scpi.query("*OPT?")
        result.measurements["options_response"] = options
        from .instruments.rtm3004 import ScopeIdentity
        identity = ScopeIdentity(manufacturer, model, serial, firmware,
                                tuple(o.strip() for o in options.split(",") if o.strip()), raw)
        result.versions.update(identity.as_versions())
        result.measurements["identity_verified"] = True
        result.measurements["diagnostic_complete"] = True
        result.verdict = PASS
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        result.measurements.update(failure_stage=phase, failure_error=failure,
            diagnostic_complete=(phase in {"tcp_connect", "scpi_identification", "model_validation", "option_query"}
                                 and isinstance(exc, (OSError, ScpiError))))
    finally:
        if scope is not None:
            try:
                result.commands = scope.scpi.commands()
            finally:
                try:
                    scope.close()
                except Exception as exc:
                    failure = failure or f"{type(exc).__name__}: {exc}"
                    result.measurements.update(diagnostic_complete=False, cleanup_error=str(exc))
    return _finish(result, started, record, failure=failure)


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
        # This driver opens a TCP connection, not a VISA resource. Requiring
        # an unused resource string made otherwise valid device plans fail.
        resolved = gate.require_verified("scope.model", "scope.host", "scope.port")
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

        saved_waveforms: list[Waveform] = []
        channel_captures: dict[int, int] = {}

        def preserve_waveform(waveform: Waveform) -> None:
            if record is None or any(waveform is saved for saved in saved_waveforms):
                return
            count = channel_captures.get(waveform.channel, 0) + 1
            suffix = "" if count == 1 else f"_{count}"
            path = _save_waveform_csv(
                record.dir / f"{name}_CH{waveform.channel}{suffix}.csv", waveform
            )
            result.add_artifact(path, kind="waveform_csv", relative_to=record.dir)
            saved_waveforms.append(waveform)
            channel_captures[waveform.channel] = count

        scope.waveform_sink = preserve_waveform
        outcome = recipe(scope, params, gate)
        # Also preserve waveforms returned by recipes using another acquisition
        # mechanism, before statistics/front-end validation can fail.
        for waveform in outcome.waveforms:
            preserve_waveform(waveform)
        # Recipe metadata is useful, but cannot overwrite statistics recomputed
        # from actual samples or instrument front-end readbacks below.
        result.measurements.update(outcome.measurements)

        # Every saved waveform gets the same deterministic statistics. Recipes
        # may choose the threshold/direction, but cannot substitute prose for
        # sample interval, voltage or edge timing evidence.
        edge_policy = EdgePolicy(
            threshold_v=(
                float(params["edge_threshold_v"])
                if isinstance(params.get("edge_threshold_v"), (int, float))
                and not isinstance(params.get("edge_threshold_v"), bool)
                else None
            ),
            hysteresis_v=(
                float(params.get("edge_hysteresis_v", 0.0))
                if isinstance(params.get("edge_hysteresis_v", 0.0), (int, float))
                and not isinstance(params.get("edge_hysteresis_v", 0.0), bool)
                else 0.0
            ),
            direction=str(params.get("edge_direction") or "rising").lower(),
        )
        waveform_rows: list[dict[str, Any]] = []
        statistics_level = params.get("statistics_level", "basic")
        if statistics_level not in {"basic", "full"}:
            raise ValueError("statistics_level must be basic or full")
        for waveform in outcome.waveforms:
            stats = waveform_statistics(waveform, policy=edge_policy,
                                        include_distribution=statistics_level == "full")
            waveform_rows.append({"channel": waveform.channel, **stats})
            result.measurements.update(
                {f"CH{waveform.channel}_{key}": value for key, value in stats.items()}
            )
            # The electrical gate consumes these long-established aliases.
            # Bind them to the samples as well, not to recipe-authored numbers.
            result.measurements.update({f"CH{waveform.channel}_min_v": stats["min_voltage_v"],
                                        f"CH{waveform.channel}_max_v": stats["max_voltage_v"]})
        if len(waveform_rows) == 1:
            result.measurements.update(
                {key: value for key, value in waveform_rows[0].items() if key != "channel"}
            )
        if waveform_rows:
            result.measurements["waveform_statistics"] = waveform_rows

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

        if gate.contracts.test_spec.get("status") == "proposed_hardware_boundary":
            gate.require_measured_voltage(result.measurements)
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

        if record is not None and waveform_rows:
            # One deterministic calculation, reusable by Builder/Verifier.
            # The original samples and hashes remain independently inspectable.
            analysis = {
                "schema_version": 1, "source": "host waveform_statistics",
                "method": "min/max over recorded volts; Vpp=max-min; period=mean same-polarity edge interval; frequency=1/period",
                "edge_policy": {"threshold_v": edge_policy.threshold_v,
                                "hysteresis_v": edge_policy.hysteresis_v,
                                "direction": edge_policy.direction},
                "waveforms": waveform_rows,
                "statistics_level": statistics_level,
            }
            # ToolResult serializes artifact dataclasses in to_dict().
            analysis["artifacts"] = [a for a in result.to_dict()["artifacts"] if a["kind"] == "waveform_csv"]
            path = record.dir / "waveform_analysis.json"
            path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
            result.add_artifact(path, kind="waveform_analysis", relative_to=record.dir)

        if screenshot and record is not None:
            path = scope.save_screenshot(record.dir / f"{name}.png")
            result.add_artifact(path, kind="screenshot", relative_to=record.dir)

        return _finish(result, started, record)

    except Exception as exc:  # noqa: BLE001 - every failure must reach SAFE state
        operations = set(gate.contracts.test_spec.get("allowed_operations") or [])
        if operations and operations <= {"scope_identify", "oscilloscope_capture"}:
            # A passive capture does not authorize changing generator outputs.
            try:
                scope.stop()
            except Exception as stop_error:
                result.warnings.append(f"Scope stop could not be confirmed: {stop_error}")
        else:
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
