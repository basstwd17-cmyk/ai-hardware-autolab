"""Gate 1 GPIO measurement entry point with physical-probe safety checks."""

from __future__ import annotations

from typing import Any

from .record import RunRecord
from .recipes.gate1_gpio_toggle import gate1_gpio_toggle
from .result import ToolResult
from .safety import SafetyGate, SafetyViolation
from .scope import measure_scope


def measure_gate1_gpio(
    gate: SafetyGate,
    record: RunRecord | None = None,
    *,
    params: dict[str, Any] | None = None,
    screenshot: bool = True,
    mock: bool = False,
    mock_responses: dict[str, str] | None = None,
) -> ToolResult:
    """Capture ``dbg0`` only after both probe approvals and channel checks pass."""
    resolved_params = dict(params or {})
    try:
        gate.require("oscilloscope_capture")
        gate.require_approved("signals.gate1.proposed_probe.approved")

        output = gate.contracts.test_spec["test_design"]["output"]
        if output.get("physical_access_approved") is not True:
            raise SafetyViolation(
                "'test_design.output.physical_access_approved' is not approved "
                f"in {gate.contracts.test_spec_path.name}"
            )

        board_probe = gate.contracts.board_map["signals"]["gate1"]["proposed_probe"]
        if board_probe.get("signal") != output.get("name"):
            raise SafetyViolation("Board map and test spec name different Gate 1 probe signals.")
        if int(board_probe.get("cpld_pin")) != int(output.get("cpld_pin")):
            raise SafetyViolation("Board map and test spec name different Gate 1 CPLD pins.")
        if board_probe.get("schematic_connection") != "none":
            raise SafetyViolation("Gate 1 probe pin is no longer isolated on the schematic.")

        channel = _channel_number(resolved_params.get("channel", "CH3"))
        allowed = {
            _channel_number(value)
            for value in gate.contracts.board_map["scope"]["gate1_probe_constraint"][
                "allowed_channels"
            ]
        }
        if channel not in allowed:
            shown = ", ".join(f"CH{number}" for number in sorted(allowed))
            raise SafetyViolation(
                f"CH{channel} is not approved for Gate 1 probing; use {shown}."
            )
        resolved_params["channel"] = channel
    except (KeyError, TypeError, ValueError, SafetyViolation) as exc:
        result = ToolResult(tool="scope", action="gate1_gpio_toggle")
        result.commands = []
        result.fail(str(exc))
        if record is not None:
            record.add(result)
        return result

    return measure_scope(
        gate1_gpio_toggle,
        resolved_params,
        gate,
        record=record,
        operation="oscilloscope_capture",
        screenshot=screenshot,
        mock=mock,
        mock_responses=mock_responses,
    )


def _channel_number(value: Any) -> int:
    shown = str(value).strip().upper()
    if shown.startswith("CH"):
        shown = shown[2:]
    channel = int(shown)
    if channel < 1 or channel > 4:
        raise ValueError(f"Unknown RTM3004 channel: {value!r}")
    return channel
