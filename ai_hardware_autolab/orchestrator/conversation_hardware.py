"""Host-owned device bridge for the single scenario workflow.

Agents choose needed connection checks; Builder may execute artifacts it created in
the job workspace.  The installed board profile supplies physical connection
details only: endpoints, wiring, device identity and electrical limits.  It is
not an application-script or bitstream allowlist.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import threading

from ai_hardware_autolab.boards.hardware import HardwareProfile
from ai_hardware_autolab.boards.connections import HardwareConnections
from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_tools
from ai_hardware_autolab.mcp_server.protocol import DevicePreparationError, ToolError

from .conversation_flow import CONNECTION_TOOL, conversation_brief
from .jobs import JobLedger
from ai_hardware_autolab.boards.scope_setup import declared_connections

TOOLS_BY_DEVICE_KIND = {
    "scope": {"measure_scope"},
    "bbb": {"run_bbb", "deploy_pru"},
    "cpld": {"scan_jtag", "program_cpld_sram"},
}


@dataclass(frozen=True)
class ConversationHardwareConfig:
    project_root: Path
    runs_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_root", Path(self.project_root).resolve(strict=True))
        object.__setattr__(self, "runs_dir", Path(self.runs_dir).resolve())


def device_tool_catalog(profile: HardwareProfile | None = None) -> list[dict[str, Any]]:
    """Describe tools supported by the physical devices in the active setup."""
    if isinstance(profile, (set, frozenset, list, tuple)):
        kinds = set(profile)
        visible = set().union(*(TOOLS_BY_DEVICE_KIND.get(str(kind), set()) for kind in profile))
        profile = None
    else:
        kinds = profile.device_kinds if profile is not None else set(TOOLS_BY_DEVICE_KIND)
        visible = (set().union(*(TOOLS_BY_DEVICE_KIND.get(kind, set())
                                for kind in profile.device_kinds))
                   if profile is not None else set().union(*TOOLS_BY_DEVICE_KIND.values()))
    policy = ServerPolicy(role="builder", allow_hardware=True)
    rows = []
    for tool in build_tools(policy):
        if not tool.touches_hardware or tool.name not in visible:
            continue
        schema = dict(tool.input_schema)
        properties = dict(schema.get("properties") or {})
        properties.pop("contract", None)
        properties.pop("record_name", None)
        if tool.name == "run_bbb" and "script" in properties:
            properties["script"] = {
                **properties["script"],
                "description": "Path to a script created or edited inside this job workspace",
            }
        if tool.name == "program_cpld_sram" and "program_xcf" in properties:
            properties["program_xcf"] = {
                **properties["program_xcf"],
                "description": "Path to an SRAM-only XCF inside this job workspace",
            }
        hidden = {"contract", "record_name"}
        required = [key for key in schema.get("required", []) if key not in hidden]
        rows.append({
            "name": tool.name,
            "description": ({
                "program_cpld_sram": "Scan the connected JTAG device, then program a job-workspace XCF and bitstream into volatile SRAM. No candidate registration or separate approval is needed.",
                "run_bbb": "Run a job-workspace Python (.py, python3) or Bash script on the connected BBB and record its hash, stdout, stderr and exit code. No script registration is needed. Use identity_only=true for identification.",
                "deploy_pru": "Upload and build this job workspace's BeagleBone-Black sources, then restart the installed PRU service and verify its response.",
            }.get(tool.name, tool.description)),
            "approval": "automatic",
            "input_schema": {**schema, "properties": properties, "required": required},
        })
    if profile is not None and profile.data.get("schema_version") == 2 and "serial" in kinds:
        rows.append({"name": "serial_exchange", "approval": "automatic",
            "description": "Read or write bounded bytes on a selected board's serial port (8N1). Not a firmware programmer or proof of board identity. Use the board protocol from its documentation. Opening a serial port may reset some boards.",
            "input_schema": {"type": "object", "additionalProperties": False,
                "properties": {"device": {"type": "string", "enum": [d["id"] for d in profile.data["devices"] if d["kind"] == "serial"]},
                    "baudrate": {"type": "integer", "minimum": 300, "maximum": 4000000},
                    "write_hex": {"type": "string", "maxLength": 8192},
                    "read_bytes": {"type": "integer", "minimum": 0, "maximum": 4096},
                    "timeout_s": {"type": "number", "minimum": 0.05, "maximum": 5}}, "required": ["device"]}})
    if kinds:
        rows.append({
            "name": CONNECTION_TOOL,
            "description": "Check only the devices needed for the scenario. Reuse timestamped hardware_connections from previous turns without calling again; call again when a fresh observation is needed. Identity checks do not program, measure waveforms or detect wiring.",
            "approval": "automatic",
            "input_schema": {"type": "object", "additionalProperties": False,
                "properties": {"devices": {"type": "array", "minItems": 1, "uniqueItems": True,
                    "items": {"type": "string", "enum": sorted(profile.device_selectors if profile is not None else kinds)}}}, "required": ["devices"]},
        })
    return rows


class ConversationHardwareExecutor:
    def __init__(self, ledger: JobLedger, profile: HardwareProfile,
                 config: ConversationHardwareConfig,
                 *, tool_builder: Callable[[ServerPolicy], list] = build_tools):
        self.ledger = ledger
        self.profile = profile
        self.config = config
        self.tool_builder = tool_builder
        self.connections = HardwareConnections(profile, config.runs_dir)
        self.cancelled = None

    def _check_active(self, job_id):
        if self.cancelled is not None and self.cancelled(job_id):
            raise DevicePreparationError("job stopped before device dispatch")

    def _require_job_profile(self, job_id, tool_name):
        brief = conversation_brief(self.ledger, job_id)
        inputs = brief.get("inputs") or {}
        if brief.get("execution_mode") != "hardware" or (inputs.get("hardware_profile") or {}).get("sha256") != self.profile.sha256:
            raise ValueError("device call needs this job's selected hardware setup")
        if tool_name not in inputs.get("device_tools", []):
            raise ValueError("device tool is unavailable for this job")
        self.profile.verify_current()

    def check_connections(self, job_id, arguments):
        brief = conversation_brief(self.ledger, job_id)
        selected = (brief.get("inputs") or {}).get("hardware_profile") or {}
        if brief.get("execution_mode") != "hardware" or selected.get("sha256") != self.profile.sha256:
            raise ValueError("connection check needs this job's installed hardware profile")
        if CONNECTION_TOOL not in (brief.get("inputs") or {}).get("device_tools", []):
            raise ValueError("connection check tool is unavailable for this job")
        if not isinstance(arguments, dict) or set(arguments) != {"devices"}:
            raise ValueError("connection check needs only a devices list")
        self.connections.selected_devices(arguments["devices"])
        return self.connections.check(arguments["devices"],
                                      cancelled=lambda: self.ledger.job(job_id).state != "running")

    def _prepare(self, job_id: str, request: dict[str, Any]):
        brief = conversation_brief(self.ledger, job_id)
        if brief.get("execution_mode") != "hardware":
            raise ValueError("offline conversation cannot execute a device tool")
        inputs = brief.get("inputs") or {}
        selected = inputs.get("hardware_profile")
        if not isinstance(selected, dict) or selected.get("sha256") != self.profile.sha256:
            raise ValueError("job hardware profile is missing or changed")

        tool_name = str(request.get("tool") or "")
        if tool_name not in set(inputs.get("device_tools") or ()):
            raise ValueError("requested device tool is unavailable for the connected device kinds")
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("device arguments must be an object")
        public = next((row for row in device_tool_catalog(self.profile) if row["name"] == tool_name), None)
        if public is None:
            raise ValueError("requested device tool is unavailable")
        schema = public["input_schema"]
        if set(arguments) - set(schema.get("properties", {})):
            raise ValueError("device arguments contain unknown fields")
        if any(key not in arguments for key in schema.get("required", [])):
            raise ValueError("device arguments are missing required fields")
        types = {"string": str, "object": dict, "boolean": bool, "array": list}
        for key, value in arguments.items():
            expected = types.get(schema.get("properties", {}).get(key, {}).get("type"))
            if expected is not None and not isinstance(value, expected):
                raise ValueError(f"device argument {key!r} has the wrong type")
        if self.profile.data.get("schema_version") == 2 and tool_name == "measure_scope" and not arguments.get("identity_only"):
            declared = {r["channel"] for r in declared_connections(self.ledger.scope_connection_events(self.profile.sha256))}
            requested = (arguments.get("params") or {}).get("channels", [])
            if not isinstance(requested, list) or any(not isinstance(row, dict) for row in requested):
                raise ValueError("scope channels must be a list of channel objects")
            if not requested or any(type(row.get("channel")) is not int or row["channel"] not in declared for row in requested):
                raise ValueError("scope wiring for this board is unknown; request the user's channel-to-signal mapping (e.g. CH1=PWM)")

        self.ledger.append_event(job_id, "hardware.policy.automatic", {
            "tool": tool_name,
            "profile": self.profile.label,
            "reason": "submitted_hardware_scenario",
            "request_sha256": self.profile.request_digest(tool_name, arguments),
        }, "platform")

        bound_arguments = {**arguments, "contract": self.profile.label}
        workspace = Path(str(brief["workspace"])).resolve(strict=True)
        policy = ServerPolicy(
            role="builder",
            allow_hardware=True,
            project_root=workspace,
            device_root=self.config.project_root,
            runs_dir=self.config.runs_dir,
            hardware_tool=tool_name,
            hardware_contract=self.profile.label,
            hardware_contract_sha256=self.profile.sha256,
            hardware_profile_path=self.profile.path,
            hardware_profile_sha256=self.profile.sha256,
            scenario_workspace=workspace,
            scope_connections=tuple(declared_connections(self.ledger.scope_connection_events(self.profile.sha256))),
        )
        tool = next((item for item in self.tool_builder(policy)
                     if item.name == tool_name and item.touches_hardware), None)
        if tool is None:
            raise ValueError("requested device tool is unavailable at the execution boundary")
        return tool, bound_arguments

    def __call__(self, job_id: str, request_turn_id: int,
                 request: dict[str, Any]) -> dict[str, Any]:
        if request.get("tool") == "serial_exchange":
            from ai_hardware_autolab.hwtools.local_board import serial_exchange
            try:
                self._require_job_profile(job_id, "serial_exchange")
            except ValueError as exc:
                raise DevicePreparationError(str(exc)) from exc
            with self.connections.device_lock:
                self._check_active(job_id)
                return serial_exchange(self.profile, request.get("arguments"), self.config.runs_dir)
        if request.get("tool") == CONNECTION_TOOL:
            try:
                inventory = self.check_connections(job_id, request.get("arguments"))
            except ValueError as exc:
                raise DevicePreparationError(str(exc)) from exc
            return {"tool": CONNECTION_TOOL, "allowed": True, "approval_required": False,
                    "inventory": inventory, "evidence_kind": "connection_identity",
                    "result": {"ok": all(row["status"] in {"connected", "present"} for row in inventory["devices"]),
                               "measurements": {"identity_only": True}}}
        try:
            tool, bound_arguments = self._prepare(job_id, request)
        except (ValueError, OSError, ToolError) as exc:
            raise DevicePreparationError(str(exc)) from exc
        # Never convert arbitrary handler exceptions into permission to retry:
        # the handler may already have contacted hardware.
        with self.connections.device_lock:
            self._check_active(job_id)
            payload = tool.handler(bound_arguments)
        result = payload.get("result") if isinstance(payload, dict) else None
        measurements = result.get("measurements") if isinstance(result, dict) else {}
        return {
            "tool": tool.name, "allowed": not bool(payload.get("execution_blocked")),
            "approval_required": False,
            "hardware_contacted": bool(payload.get("hardware_contacted",
                (measurements or {}).get("hardware_contacted", True))),
            "run": payload.get("run") if isinstance(payload, dict) else None,
            "run_verdict": payload.get("run_verdict") if isinstance(payload, dict) else None,
            "result": result if isinstance(result, dict) else {},
            **({"error": payload.get("summary"), "repair_target": payload.get("repair_target")}
               if payload.get("execution_blocked") else {}),
        }


class SelectedHardwareExecutor:
    """Dispatch by the immutable job selection, sharing one physical-device lock."""
    def __init__(self, ledger, registry, config):
        self.ledger, self.registry, self.config = ledger, registry, config
        self._lock = threading.RLock()
        self.device_lock = threading.RLock()
        self._executors = {}

    def for_profile(self, profile):
        with self._lock:
            key = (profile.board_id, profile.revision, profile.sha256)
            if key not in self._executors:
                executor = ConversationHardwareExecutor(self.ledger, profile, self.config)
                executor.connections.device_lock = self.device_lock
                executor.cancelled = lambda job_id: self.ledger.job(job_id).state != "running"
                self._executors[key] = executor
            return self._executors[key]

    def connections_for(self, selected=None):
        profile = self.registry.resolve(selected)
        if profile is None:
            raise ValueError("select a registered hardware setup")
        return self.for_profile(profile).connections

    def __call__(self, job_id, request_turn_id, request):
        brief = conversation_brief(self.ledger, job_id)
        binding = (brief.get("inputs") or {}).get("hardware_profile") or {}
        if brief.get("execution_mode") != "hardware" or not binding:
            raise DevicePreparationError("this job has no hardware execution setup")
        try:
            profile = self.registry.resolve({"board_id": binding.get("board_id"), "revision": binding.get("revision")})
            if profile.sha256 != binding.get("sha256"):
                raise ValueError("this job's setup changed; submit a new job with the updated revision")
        except (ValueError, OSError) as exc:
            raise DevicePreparationError(str(exc)) from exc
        return self.for_profile(profile)(job_id, request_turn_id, request)
