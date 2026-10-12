"""What the MCP server exposes, and to whom.

Two rules shape this module.

**The tool list is the first safety boundary.** A tool that can drive the board
is not registered at all unless the server was started with
``--allow-hardware``. An agent cannot ask for a tool it cannot see, and it
cannot flip that flag: it is a launch argument, decided once by a person, which
is exactly where the architecture already puts that decision. ``SafetyGate``
still enforces the contract underneath, so this is a second lock, not a
replacement.

**Tools take a contract name, not a path.** Every hardware call names a file in
``contracts/``; the tool derives sources, devices and permitted pin-outs from
it. A model that could pass arbitrary paths could aim a programmer at any file
on disk, and the contract would stop describing what happened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .protocol import Prompt, Resource, Tool, ToolError

QKD_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = QKD_ROOT.parent
CONTRACTS_DIR = QKD_ROOT / "contracts"
RUNS_DIR = QKD_ROOT / "runs"
SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"

CONTRACT_NAME = re.compile(r"^[A-Za-z0-9._-]+\.yaml$")
RUN_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_TEXT_BYTES = 200_000


# -- what each role may do ---------------------------------------------------
#
# One matrix, in one place. The agent definitions already say which agent may
# call which tool; this is where that becomes something a caller cannot talk
# its way past, because a tool outside the row is not registered at all.
#
# Role names are the agent names on purpose. Two vocabularies for the same six
# actors is one vocabulary too many, and the pair that would drift is exactly
# the pair that decides who may touch the board.

READ = "read"
MUTATE = "mutate"
EFFECTS = (READ, MUTATE)

_READ_ONLY_TOOLS = frozenset({"list_contracts", "read_contract", "list_runs", "read_run"})

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "lead-agent": _READ_ONLY_TOOLS,
    "builder": _READ_ONLY_TOOLS
    | {
        "build_cpld",
        "run_board_build",
        "scan_jtag",
        "program_cpld_sram",
        "deploy_pru",
        "run_bbb",
        "run_board_stage",
        "measure_gate1_gpio",
        "measure_board_scope",
        "measure_scope",
    },
    "mission-architect": _READ_ONLY_TOOLS,
    "change-analyst": _READ_ONLY_TOOLS,
    "architecture-selector": _READ_ONLY_TOOLS,
    "system-architect": _READ_ONLY_TOOLS,
    "researcher": _READ_ONLY_TOOLS,
    "contract-drafter": _READ_ONLY_TOOLS,
    "gate-architect": _READ_ONLY_TOOLS,
    "design-builder": _READ_ONLY_TOOLS | {"build_cpld"},
    "firmware-builder": _READ_ONLY_TOOLS | {"run_board_build"},
    # The only role that may change the state of the board.
    "board-operator": _READ_ONLY_TOOLS
    | {"scan_jtag", "program_cpld_sram", "deploy_pru", "run_bbb", "run_board_stage"},
    # Observes. It shares run_bbb with the operator, which is why the effect
    # column below exists: the tool is the same, the scripts it may run are not.
    "measurement": _READ_ONLY_TOOLS
    | {"measure_gate1_gpio", "measure_board_scope", "run_bbb", "run_board_stage"},
    "contract-reviewer": _READ_ONLY_TOOLS,
    "verifier": _READ_ONLY_TOOLS,
    "reporter": _READ_ONLY_TOOLS,
}

# Which script effects a role may run through `run_bbb`.
ROLE_EFFECTS: dict[str, frozenset[str]] = {
    "lead-agent": frozenset({READ}),
    "builder": frozenset({READ, MUTATE}),
    "mission-architect": frozenset({READ}),
    "change-analyst": frozenset({READ}),
    "architecture-selector": frozenset({READ}),
    "system-architect": frozenset({READ}),
    "researcher": frozenset({READ}),
    "contract-drafter": frozenset({READ}),
    "gate-architect": frozenset({READ}),
    "design-builder": frozenset({READ}),
    "firmware-builder": frozenset({READ}),
    "board-operator": frozenset({READ, MUTATE}),
    "measurement": frozenset({READ}),
    "contract-reviewer": frozenset({READ}),
    "verifier": frozenset({READ}),
    "reporter": frozenset({READ}),
}


@dataclass(frozen=True)
class ServerPolicy:
    """Decided at launch by a person, never by a caller."""

    allow_hardware: bool = False
    contracts_dir: Path = CONTRACTS_DIR
    runs_dir: Path = RUNS_DIR
    project_root: Path = PROJECT_ROOT
    device_root: Path | None = None
    # None means the historical profile: every tool the `allow_hardware` flag
    # permits. A named role narrows that to one row of the matrix, and the two
    # locks compose -- a role that lists board tools still gets none of them
    # unless the flag was also given.
    role: str | None = None
    hardware_tool: str | None = None
    hardware_contract: str | None = None
    hardware_contract_sha256: str | None = None
    hardware_profile_path: Path | None = None
    hardware_profile_sha256: str | None = None
    workspace_recipe: str | None = None
    workspace_recipe_sha256: str | None = None
    scenario_workspace: Path | None = None
    # Host-supplied operator declarations for this job, never model tool arguments.
    scope_connections: tuple = ()

    def __post_init__(self) -> None:
        binding = (self.hardware_tool, self.hardware_contract, self.hardware_contract_sha256)
        if any(binding) and not all(binding):
            raise ToolError("hardware turn binding needs tool, contract and digest together")
        profile_binding = (self.hardware_profile_path, self.hardware_profile_sha256)
        if any(profile_binding) and not all(profile_binding):
            raise ToolError("hardware profile binding needs path and digest together")
        if (self.workspace_recipe or self.workspace_recipe_sha256) and (
            not self.workspace_recipe or not self.workspace_recipe_sha256 or not self.hardware_tool
        ):
            raise ToolError("workspace recipe requires a bound hardware turn and source digest")
        if self.scenario_workspace is not None:
            workspace = Path(self.scenario_workspace).resolve(strict=True)
            object.__setattr__(self, "scenario_workspace", workspace)
        if self.role is not None and self.role not in ROLE_TOOLS:
            raise ToolError(
                f"Unknown role {self.role!r}. Known roles: {', '.join(sorted(ROLE_TOOLS))}."
            )

    def permits_tool(self, name: str) -> bool:
        return self.role is None or name in ROLE_TOOLS[self.role]

    def allowed_effects(self) -> frozenset[str]:
        """Which script effects this launch may run. Unroled servers may run any."""
        if self.role is None:
            return frozenset(EFFECTS)
        return ROLE_EFFECTS[self.role]

    def describe(self) -> dict[str, Any]:
        return {
            "allow_hardware": self.allow_hardware,
            "role": self.role,
            "allowed_effects": sorted(self.allowed_effects()),
            "contracts_dir": str(self.contracts_dir),
            "runs_dir": str(self.runs_dir),
        }


# -- argument helpers --------------------------------------------------------


def _string(args: dict[str, Any], key: str, required: bool = True) -> str:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"Missing required argument: {key}")
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"Argument {key} must be a non-empty string")
    return value.strip()


def _contract_path(policy: ServerPolicy, name: str) -> Path:
    if not CONTRACT_NAME.fullmatch(name):
        raise ToolError(
            f"Contract must be a plain .yaml file name, got {name!r}. "
            "Paths are not accepted."
        )
    path = policy.contracts_dir / name
    if not path.is_file():
        available = ", ".join(sorted(p.name for p in policy.contracts_dir.glob("*.yaml")))
        raise ToolError(f"No such contract: {name}. Available: {available}")
    return path


def _gate(policy: ServerPolicy, contract: str):
    from QKD_PROJ.hwtools import Contracts, SafetyGate
    from QKD_PROJ.hwtools.result import sha256_file

    if policy.hardware_profile_path is not None:
        from QKD_PROJ.boards.hardware import HardwareProfile
        from QKD_PROJ.hwtools.result import sha256_file
        path = Path(policy.hardware_profile_path).resolve()
        if sha256_file(path) != policy.hardware_profile_sha256:
            raise ToolError("installed hardware profile changed after job intake")
        profile = HardwareProfile.load(
            policy.device_root or policy.project_root,
            path.parent.parent.name,
            path.parent.name,
        )
        if contract != profile.label:
            raise ToolError("device call names a different hardware profile")
        return profile.compatibility_gate(
            scope_connections=policy.scope_connections,
            delegated_operation="sram_fast_program" if policy.hardware_tool == "program_cpld_sram" else None
        )
    path = _contract_path(policy, contract)
    delegated = None
    if policy.allow_hardware and policy.hardware_tool == "program_cpld_sram":
        if contract != policy.hardware_contract or sha256_file(path) != policy.hardware_contract_sha256:
            raise ToolError("SRAM boundary differs from the authorized dispatch")
        delegated = "sram_fast_program"
    return SafetyGate(
        Contracts.load(
            contract,
            contracts_dir=policy.contracts_dir,
            project_root=policy.device_root or policy.project_root,
        ),
        allow_hardware=policy.allow_hardware,
        delegated_operation=delegated,
    )


def _record(policy: ServerPolicy, gate, args: dict[str, Any], default: str):
    from QKD_PROJ.hwtools import RunRecord

    name = args.get("record_name") or default
    if not RUN_NAME.fullmatch(str(name)):
        raise ToolError(f"record_name must match {RUN_NAME.pattern}")
    return RunRecord(str(name), gate, runs_dir=policy.runs_dir)


def _result_payload(record, result) -> dict[str, Any]:
    result.measurements["execution_completed"] = bool(result.ok)
    verdict = record.finish(result.verdict)
    return {
        "run": record.dir.name,
        "run_dir": str(record.dir),
        "run_verdict": verdict,
        "result": result.to_dict(),
    }


def _clip(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES:
        return text
    return encoded[:MAX_TEXT_BYTES].decode("utf-8", "ignore") + "\n...[truncated]"


# -- offline handlers --------------------------------------------------------


def _list_contracts(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    entries = []
    include_unready = args.get("include_unready", False)
    if type(include_unready) is not bool:
        raise ToolError("include_unready must be a JSON boolean")
    omitted = 0
    for path in sorted(policy.contracts_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            entries.append({"file": path.name, "error": f"unparsable: {exc}"})
            continue
        readiness = _contract_readiness(policy, path, data)
        if readiness["issues"] and not include_unready:
            omitted += 1
            continue
        entries.append(
            {
                "file": path.name,
                "test_id": data.get("test_id"),
                "status": data.get("status"),
                "allowed_operations": data.get("allowed_operations"),
                "forbidden_operations": data.get("forbidden_operations"),
                "reuse": readiness,
            }
        )
    return {"contracts": entries, "policy": policy.describe(), "omitted_unready": omitted,
            "note": "Listing is not approval or hardware verification. include_unready=true shows incomplete proposals for repair."}


def _contract_readiness(policy, path, data):
    """Cheap read-only structural check; actual source bytes are checked at dispatch preparation."""
    issues = []
    if isinstance(data, dict) and data.get("status") == "proposed_hardware_boundary":
        from dataclasses import replace
        from QKD_PROJ.hwtools.safety import Contracts, SafetyGate, READ_ONLY_OPERATIONS
        from QKD_PROJ.orchestrator.boundary_normalization import normalize_boundary
        try:
            normalized = normalize_boundary(data)
            bound = Contracts.load(path.name, policy.contracts_dir, project_root=policy.project_root)
            gate = SafetyGate(replace(bound, test_spec=normalized), allow_hardware=True)
            for operation in normalized.get("allowed_operations", []):
                if operation not in READ_ONLY_OPERATIONS:
                    gate.require(operation)
            if "sram_fast_program" in normalized.get("allowed_operations", []):
                if not isinstance(normalized.get("sram_program_candidate"), dict):
                    issues.append("missing executable sram_program_candidate; reuse verified source metadata, not a path/hash-only draft")
        except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
            issues.append(str(exc))
    return {"status": "needs_preparation" if issues else "not_flagged", "issues": issues}


def _read_contract(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    path = _contract_path(policy, _string(args, "name"))
    raw = path.read_text(encoding="utf-8")
    return {"file": path.name, "text": _clip(raw),
            "reuse": _contract_readiness(policy, path, yaml.safe_load(raw) or {})}


def _list_runs(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 25)
    query = _string(args, "query", required=False).casefold()
    if not policy.runs_dir.is_dir():
        return {"runs": [], "note": f"{policy.runs_dir} does not exist yet"}
    directories = sorted((p for p in policy.runs_dir.iterdir() if p.is_dir()), reverse=True)
    matching = [p for p in directories if query in p.name.casefold()]
    runs = []
    for directory in matching[: max(1, limit)]:
        status_path = directory / "run_status.json"
        verdict = None
        steps = None
        if status_path.is_file():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                verdict = status.get("verdict")
                steps = [f"{s.get('tool')}.{s.get('action')}" for s in status.get("steps", [])]
            except json.JSONDecodeError:
                verdict = "unreadable run_status.json"
        runs.append({"run": directory.name, "verdict": verdict, "steps": steps})
    return {"runs": runs, "total": len(directories), "matched_total": len(matching)}


def _read_run(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools.evidence_view import run_view
    name = _string(args, "run")
    detail = args.get("detail", "summary")
    if detail not in ("summary", "full"):
        raise ToolError("detail must be summary or full")
    if not RUN_NAME.fullmatch(name):
        raise ToolError("run must be a plain run directory name")
    directory = policy.runs_dir / name
    if not directory.is_dir():
        raise ToolError(f"No such run: {name}")
    payload: dict[str, Any] = {"run": name, "files": sorted(p.name for p in directory.iterdir()),
                               "detail": detail}
    for key, filename in (("status", "run_status.json"), ("manifest", "manifest.json")):
        path = directory / filename
        if path.is_file():
            try:
                # Never clip JSON before parsing: a long valid manifest used to
                # become an 'unparsable' error, provoking repeated shell reads.
                data = json.loads(path.read_text(encoding="utf-8"))
                if detail == "summary" and isinstance(data, dict):
                    data, omitted = run_view(data)
                    payload["omitted" if key == "manifest" else "status_omitted"] = omitted
                    payload["full_evidence"] = {"path": str(path), "read_run_detail": "full",
                        "note": "A summary is not a complete command/per-round audit. Read full details for a specific discrepancy."}
                payload[key] = data
            except json.JSONDecodeError as exc:
                payload[key] = {"error": f"unparsable: {exc}"}
    return payload


def _build_cpld(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools.cpld import BuildError, CpldProject, IoExpectation, build_cpld

    contract = _string(args, "contract")
    gate = _gate(policy, contract)
    spec = gate.contracts.test_spec
    design = spec.get("test_design") or {}
    sources = design.get("sources") or {}
    if not design.get("top") or not sources.get("hdl") or not sources.get("constraints"):
        raise ToolError(
            f"{contract} has no test_design.top / sources.hdl / sources.constraints, "
            "so the project cannot be derived from it. Add them to the contract "
            "rather than passing paths here."
        )

    hdl = policy.project_root / str(sources["hdl"])
    lpf = policy.project_root / str(sources["constraints"])
    for path in (hdl, lpf):
        if not path.is_file():
            raise ToolError(f"Source named by the contract does not exist: {path}")

    expected = args.get("expected_io")
    if expected is not None:
        if not isinstance(expected, dict) or not isinstance(expected.get("ports"), dict):
            raise ToolError("expected_io must be an object with a 'ports' map")
        io = IoExpectation(
            ports={str(k): int(v) for k, v in expected["ports"].items()},
            io_type=expected.get("io_type"),
            drive_ma=int(expected["drive_ma"]) if expected.get("drive_ma") else None,
            slew=expected.get("slew"),
        )
    else:
        try:
            io = IoExpectation.from_contract(gate)
        except BuildError as exc:
            raise ToolError(
                f"{exc} Pass expected_io explicitly, or add test_design.output to "
                "the contract. Building without a pin check is not offered."
            ) from exc

    project = CpldProject(
        directory=hdl.parent,
        name=str(design["top"]),
        top=str(design["top"]),
        device=str(gate.contracts.board_map["cpld"]["part"]),
        hdl_sources=(hdl,),
        constraint_file=lpf,
        # A frequency preference is only required when the contract describes a
        # clock. Demanding one for a combinational design would fail a build
        # that is correct.
        requires_frequency_constraint=bool(design.get("clock")),
    )
    record = _record(policy, gate, args, f"mcp_build_{project.name}")
    result = build_cpld(project, gate, record=record, expected_io=io)
    return _result_payload(record, result)


def _run_board_adapter(
    policy: ServerPolicy, args: dict[str, Any], *, hardware: bool
) -> dict[str, Any]:
    from QKD_PROJ.hwtools.board_adapter import run_profile_stage

    contract, stage_id, gate, profile, stage = _bound_profile_stage(policy, args)
    expected_tool = "run_board_stage" if hardware else "run_board_build"
    if stage.tool != expected_tool:
        raise ToolError(
            f"stage {stage_id} belongs to {stage.tool!r}, not {expected_tool!r}"
        )
    record = _record(policy, gate, args, f"mcp_board_{stage_id}")
    result = run_profile_stage(
        profile,
        stage_id,
        gate,
        record=record,
        project_root=policy.project_root,
    )
    return _result_payload(record, result)


def _bound_profile_stage(policy: ServerPolicy, args: dict[str, Any]):
    from QKD_PROJ.boards.profiles import BoardProfile

    contract = _string(args, "contract")
    stage_id = _string(args, "stage_id")
    gate = _gate(policy, contract)
    binding = gate.contracts.test_spec.get("board_profile")
    if not isinstance(binding, dict):
        raise ToolError(f"{contract} has no board_profile binding")
    reference = _string(binding, "path")
    expected = _string(binding, "sha256")
    profile_path = (policy.project_root / reference).resolve()
    try:
        profile_path.relative_to(policy.project_root.resolve())
    except ValueError as exc:
        raise ToolError("board profile escapes the project root") from exc
    profile = BoardProfile.load(profile_path, project_root=policy.project_root)
    if profile.sha256 != expected:
        raise ToolError(
            f"board profile changed after contract review: expected {expected}, "
            f"found {profile.sha256}"
        )
    stage = next((item for item in profile.stages if item.stage_id == stage_id), None)
    if stage is None:
        raise ToolError(f"board profile has no stage {stage_id!r}")
    legacy_builder_roles = {
        "design-builder", "firmware-builder", "board-operator", "measurement"
    }
    role_matches = (
        policy.role is None
        or stage.agent == policy.role
        or (policy.role == "builder" and stage.agent in legacy_builder_roles)
    )
    if not role_matches:
        raise ToolError(
            f"stage {stage_id} belongs to agent {stage.agent!r}, not role {policy.role!r}"
        )
    return contract, stage_id, gate, profile, stage


def _run_board_build(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    return _run_board_adapter(policy, args, hardware=False)


def _run_board_stage(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    return _run_board_adapter(policy, args, hardware=True)


def _measure_board_scope(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools.recipes import profile_channel_capture
    from QKD_PROJ.hwtools.scope import measure_scope

    _, stage_id, gate, _, stage = _bound_profile_stage(policy, args)
    if stage.tool != "measure_board_scope" or stage.phase != "measure":
        raise ToolError(f"stage {stage_id} is not a profiled scope measurement")
    record = _record(policy, gate, args, f"mcp_scope_{stage_id}")
    result = measure_scope(
        profile_channel_capture,
        dict(stage.parameters),
        gate,
        record=record,
    )
    return _result_payload(record, result)


# -- hardware handlers -------------------------------------------------------


def _scan_jtag(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import scan_jtag

    gate = _gate(policy, _string(args, "contract"))
    record = _record(policy, gate, args, "mcp_jtag_scan")
    xcf = (gate.contracts.test_spec.get("jtag") or {}).get("scan_xcf")
    kwargs = {"xcf_path": (policy.device_root or policy.project_root) / str(xcf)} if xcf else {}
    return _result_payload(record, scan_jtag(gate, record=record, **kwargs))


def _program_cpld_sram(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import program_cpld_sram, scan_jtag

    gate = _gate(policy, _string(args, "contract"))
    scan_kwargs: dict[str, Any] = {}
    if policy.scenario_workspace is not None:
        workspace = policy.scenario_workspace
        xcf = (workspace / _string(args, "program_xcf")).resolve()
        if not xcf.is_relative_to(workspace):
            raise ToolError("program_xcf must stay inside the job workspace")
        jtag = gate.contracts.test_spec.get("jtag") or {}
        scan_xcf_value = jtag.get("scan_xcf")
        if not isinstance(scan_xcf_value, str):
            raise ToolError("hardware connection has no JTAG scan configuration")
        device_root = (policy.device_root or policy.project_root).resolve()
        scan_xcf = (device_root / scan_xcf_value).resolve()
        if not scan_xcf.is_relative_to(device_root) or not scan_xcf.is_file():
            raise ToolError("hardware JTAG scan configuration is missing or invalid")
        scan_kwargs["xcf_path"] = scan_xcf
    elif policy.hardware_profile_path is not None:
        jtag = gate.contracts.test_spec.get("jtag") or {}
        xcf_value = jtag.get("program_xcf")
        scan_xcf_value = jtag.get("scan_xcf")
        if not isinstance(xcf_value, str):
            raise ToolError("hardware profile has no pinned SRAM program XCF")
        if not isinstance(scan_xcf_value, str):
            raise ToolError("hardware profile has no pinned JTAG scan XCF")
        device_root = (policy.device_root or policy.project_root).resolve()
        xcf = (device_root / xcf_value).resolve()
        scan_xcf = (device_root / scan_xcf_value).resolve()
        if not xcf.is_relative_to(device_root) or not scan_xcf.is_relative_to(device_root):
            raise ToolError("hardware profile JTAG inputs escape the device root")
        if not scan_xcf.is_file():
            raise ToolError(f"No such scan XCF: {scan_xcf}")
        scan_kwargs["xcf_path"] = scan_xcf
    else:
        xcf_name = _string(args, "program_xcf")
        if not re.fullmatch(r"^[A-Za-z0-9._-]+\.xcf$", xcf_name):
            raise ToolError("program_xcf must be a plain .xcf file name in contracts/")
        xcf = policy.contracts_dir / xcf_name
    if not xcf.is_file():
        raise ToolError(f"No such XCF: {xcf}")

    # Local input defects must be found before the first JTAG scan as well as
    # before the program command. This is the same check used before approval.
    from QKD_PROJ.hwtools.sram import program_input_artifacts, workspace_program_input_artifacts
    if policy.scenario_workspace is not None:
        workspace_program_input_artifacts(gate, xcf, policy.scenario_workspace)
    else:
        program_input_artifacts(gate, xcf)

    record = _record(policy, gate, args, "mcp_sram_program")
    # The chain is verified in the same run that programs it. A scan from an
    # earlier session proves nothing about the cable that is attached now.
    scan = scan_jtag(gate, record=record, **scan_kwargs)
    if not scan.ok:
        return _result_payload(record, scan)
    program_kwargs = {"program_xcf": xcf, "record": record}
    if policy.scenario_workspace is not None:
        program_kwargs["workspace_root"] = policy.scenario_workspace
    result = program_cpld_sram(gate, scan, **program_kwargs)
    payload = _result_payload(record, result)
    payload["scan"] = scan.to_dict()
    return payload


def _measure_gate1_gpio(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import measure_gate1_gpio

    gate = _gate(policy, _string(args, "contract"))
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object")
    record = _record(policy, gate, args, "mcp_gate1_gpio")
    result = measure_gate1_gpio(gate, record=record, params=params)
    return _result_payload(record, result)


def _measure_scope(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools.scope import measure_scope
    from QKD_PROJ.hwtools.recipes.profile_channel_capture import profile_channel_capture
    gate = _gate(policy, _string(args, "contract"))
    if _identity_only(args):
        if args.get("params") or policy.workspace_recipe:
            raise ToolError("identity_only cannot include capture params or a workspace recipe")
        from QKD_PROJ.hwtools.scope import identify_scope
        record = _record(policy, gate, args, "mcp_scope_identify")
        return _result_payload(record, identify_scope(gate, record))
    boundary = gate.instrument_scope()
    params = args.get("params")
    if not isinstance(params, dict):
        raise ToolError("params must describe the requested channel capture")
    if not policy.workspace_recipe:
        gate.require_scope_capture(params)
    record = _record(policy, gate, args, "mcp_scope_capture")
    from QKD_PROJ.hwtools.workspace_recipe import load_recipe, bounded_recipe, RecipeSources
    from QKD_PROJ.hwtools.result import ToolResult, FAIL
    recipe = profile_channel_capture
    sources = RecipeSources(record)
    if policy.workspace_recipe:
        try:
            recipe, content = load_recipe(policy.project_root, policy.workspace_recipe, str(policy.workspace_recipe_sha256),
                                          source_sink=sources)
            source = record.dir / "executed_recipe.py"
            source.write_bytes(content)
        except Exception as exc:
            result = ToolResult(tool="scope", action="prepare_recipe", ok=False, verdict=FAIL, error=str(exc))
            sources.attach(result)
            record.add(result)
            return _result_payload(record, result)
    recipe = bounded_recipe(recipe, {row["channel"] for row in boundary["scope_channels"]})
    result = measure_scope(recipe, params, gate, record)
    if policy.scope_connections:
        result.measurements["operator_scope_connections"] = list(policy.scope_connections)
    if policy.workspace_recipe:
        result.measurements["executed_entrypoint"] = policy.workspace_recipe
        result.measurements["executed_source_sha256"] = policy.workspace_recipe_sha256
        result.add_artifact(source, "executed-source", relative_to=record.dir)
        sources.attach(result)
        # measure_scope already recorded the result; update the manifest with
        # source provenance in the same step, not a second measurement.
        record.write()
    return _result_payload(record, result)


def script_effect(entry: Any) -> str:
    """Classify one allowlisted script as `read` or `mutate`.

    The contract's ``effect`` field is a sentence a person wrote, and a useful
    one -- "temporarily muxes P9_17/18/21 as GPIO then restores exact state"
    says more than any enum. It cannot be enforced, so ``effect_class`` carries
    the machine-readable half beside it rather than replacing it.

    **A missing classification means `mutate`.** Failing closed is the whole
    point: a script nobody has classified is a script nobody has thought about,
    and the role that must not change the board is exactly the one that would
    otherwise inherit the benefit of the doubt.

    Note also what does *not* count as read-only. A script that changes a
    setting and restores it afterwards is a mutation: the restore is a second
    change rather than the absence of a first, and a process that dies between
    them leaves the board altered.
    """
    if not isinstance(entry, dict):
        return MUTATE
    declared = str(entry.get("effect_class") or "").strip().lower()
    return declared if declared in EFFECTS else MUTATE


def _identity_only(args: dict[str, Any]) -> bool:
    value = args.get("identity_only", False)
    if type(value) is not bool:
        raise ToolError("identity_only must be a JSON boolean")
    return value


def _run_bbb(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import run_bbb

    gate = _gate(policy, _string(args, "contract"))
    _check_bbb_target(policy, gate)
    if _identity_only(args):
        if args.get("script") or policy.workspace_recipe:
            raise ToolError("identity_only cannot include a script or workspace recipe")
        from QKD_PROJ.hwtools.bbb import identify_bbb
        record = _record(policy, gate, args, "mcp_bbb_identify")
        return _result_payload(record, identify_bbb(gate, record))
    script = _string(args, "script")

    source_path = None
    if policy.scenario_workspace is not None:
        source_path = (policy.scenario_workspace / script).resolve()
        if not source_path.is_relative_to(policy.scenario_workspace):
            raise ToolError("BBB script must stay inside the job workspace")
        if not source_path.is_file():
            raise ToolError(f"No such workspace script: {script}")
    else:
        allowlist = gate.contracts.test_spec.get("scripts")
        entry = allowlist.get(script) if isinstance(allowlist, dict) else None
        effect = script_effect(entry)
        permitted = policy.allowed_effects()
        if effect not in permitted:
            raise ToolError(
                f"Script '{script}' is classified '{effect}' and this server was launched as "
                f"role '{policy.role}', which may run {sorted(permitted)} scripts only. "
                + (
                    "The contract does not classify it, so it counts as 'mutate' until a person "
                    "reviews it and adds effect_class."
                    if not (isinstance(entry, dict) and entry.get("effect_class"))
                    else "Run it from a board-operator server, with the approval that needs."
                )
            )

    record = _record(policy, gate, args, f"mcp_bbb_{re.sub(r'[^A-Za-z0-9_]+', '_', script)}")
    return _result_payload(record, run_bbb(
        gate, script, record, repo_root=policy.project_root, source_path=source_path,
    ))


def _deploy_pru(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import deploy_pru

    gate = _gate(policy, _string(args, "contract"))
    _check_bbb_target(policy, gate)
    record = _record(policy, gate, args, "mcp_deploy_pru")
    return _result_payload(record, deploy_pru(gate, record, repo_root=policy.project_root))


def _check_bbb_target(policy, gate):
    """Credentials remain local; an environment change cannot choose a board."""
    if not policy.hardware_contract:
        return  # historical manually configured CLI; not an automatic Campaign
    from QKD_PROJ.hwtools.ssh import SshError, SshTarget, bbb_endpoint
    expected = (gate.contracts.test_spec.get("bbb") or {}).get("target")
    if expected is None:
        expected = bbb_endpoint(gate.contracts.board_map, env={})
    try:
        SshTarget.from_env(board_map=gate.contracts.board_map, expected=expected)
    except SshError as exc:
        raise ToolError(str(exc)) from exc


def _validate_scenario_inputs(policy, tool_name, args):
    """Read-only input checks, before dispatch can contact any device."""
    root = policy.scenario_workspace
    gate = _gate(policy, str(policy.hardware_contract))
    if tool_name == "run_bbb" and not _identity_only(args):
        source = (root / _string(args, "script")).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            raise ToolError("script must name an existing file inside this job workspace")
    elif tool_name == "program_cpld_sram":
        from QKD_PROJ.hwtools.sram import workspace_program_input_artifacts
        xcf = (root / _string(args, "program_xcf")).resolve()
        if not xcf.is_relative_to(root):
            raise ToolError("program_xcf must stay inside this job workspace")
        workspace_program_input_artifacts(gate, xcf, root)
    elif tool_name == "measure_scope" and not _identity_only(args):
        gate.require_scope_capture(args.get("params") or {})
    elif tool_name == "deploy_pru":
        from QKD_PROJ.hwtools.bbb import _plan_uploads
        config = gate.contracts.test_spec.get("bbb") or {}
        repo = (root / str(config.get("repo_root") or "")).resolve()
        if not repo.is_relative_to(root) or not repo.is_dir():
            raise ToolError("PRU sources must exist inside this job workspace")
        uploads = _plan_uploads(config, repo, str(config.get("remote_root") or ""))
        if any(not local.resolve().is_relative_to(root) for local, _ in uploads):
            raise ToolError("PRU upload source escapes this job workspace")


# -- schemas -----------------------------------------------------------------

_CONTRACT_ARG = {
    "type": "string",
    "description": "File name in contracts/, for example gate1-test-spec.yaml",
}
_RECORD_ARG = {
    "type": "string",
    "description": "Optional run directory label. A RunRecord is always written.",
}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**properties, "record_name": _RECORD_ARG},
        "required": required,
        "additionalProperties": False,
    }


def build_tools(policy: ServerPolicy) -> list[Tool]:
    """Offline tools always; board-touching tools only when a person allowed it."""

    def bind(handler):
        return lambda args: handler(policy, args)

    tools = [
        Tool(
            name="list_contracts",
            description=(
                "List reusable contract references and their operations. Incomplete generated "
                "proposals are omitted by default; include_unready=true exposes them for repair. "
                "A listed reference is not approval or proof of current hardware state."
            ),
            input_schema={"type": "object", "properties": {"include_unready": {"type": "boolean"}}, "additionalProperties": False},
            handler=bind(_list_contracts),
        ),
        Tool(
            name="read_contract",
            description="Return the full YAML text of one contract.",
            input_schema={
                "type": "object",
                "properties": {"name": _CONTRACT_ARG},
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=bind(_read_contract),
        ),
        Tool(
            name="list_runs",
            description=(
                "List run directories newest first with their verdict and steps. "
                "Use query for a literal run-name fragment (e.g. spi) and a small limit first; "
                "do not fetch a large recent list when an exact relevant run is already known."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200},
                               "query": {"type": "string", "description": "Case-insensitive literal run-name fragment"}},
                "additionalProperties": False,
            },
            handler=bind(_list_runs),
        ),
        Tool(
            name="read_run",
            description="Read one run's results, errors and artifact hashes. Default summary marks shortened command/per-round lists; use detail=full for a specific evidence gap. Never remeasure just to obtain a report.",
            input_schema={
                "type": "object",
                "properties": {"run": {"type": "string"}, "detail": {"type": "string", "enum": ["summary", "full"], "default": "summary"}},
                "required": ["run"],
                "additionalProperties": False,
            },
            handler=bind(_read_run),
        ),
        Tool(
            name="build_cpld",
            description=(
                "Build the design named by a contract with Lattice Diamond and check "
                "the result against it: DRC, timing, and the placed pin-out. Touches "
                "no hardware. Fails rather than building without a pin check."
            ),
            input_schema=_schema(
                {
                    "contract": _CONTRACT_ARG,
                    "expected_io": {
                        "type": "object",
                        "description": (
                            "Required only when the contract has no test_design.output. "
                            "ports maps port name to pin number."
                        ),
                        "properties": {
                            "ports": {"type": "object"},
                            "io_type": {"type": "string"},
                            "drive_ma": {"type": "integer"},
                            "slew": {"type": "string"},
                        },
                        "required": ["ports"],
                    },
                },
                ["contract"],
            ),
            handler=bind(_build_cpld),
        ),
        Tool(
            name="run_board_build",
            description=(
                "Run one exact offline build argv from a digest-bound board profile. "
                "The agent supplies only the contract and stage id; declared artifacts "
                "are copied and hashed into the run record."
            ),
            input_schema=_schema(
                {"contract": _CONTRACT_ARG, "stage_id": {"type": "string"}},
                ["contract", "stage_id"],
            ),
            handler=bind(_run_board_build),
        ),
    ]

    if not policy.allow_hardware:
        return [t for t in tools if policy.permits_tool(t.name)]

    tools.extend(
        [
            Tool(
                name="measure_scope",
                description="Capture 1..4 passive scope inputs with params; or identity_only=true to query identity without channels, settings, waveform, screenshot or STOP. Identity requires scope_identify in the boundary. No DUT board profile required.",
                input_schema=_schema({"contract": _CONTRACT_ARG, "params": {"type": "object"},
                                      "identity_only": {"type": "boolean"}}, ["contract"]),
                handler=bind(_measure_scope),
                touches_hardware=True,
            ),
            Tool(
                name="scan_jtag",
                description=(
                    "Verify the JTAG chain and IDCODE with Diamond pgrcmd. Reads the "
                    "device; does not program it."
                ),
                input_schema=_schema({"contract": _CONTRACT_ARG}, ["contract"]),
                handler=bind(_scan_jtag),
                touches_hardware=True,
            ),
            Tool(
                name="program_cpld_sram",
                description=(
                    "Scan the chain, then program one volatile SRAM image named by the "
                    "contract's approved candidate. Refuses any XCF that names another "
                    "file, and any non-SRAM operation. Consumes an approval."
                ),
                input_schema=_schema(
                    {
                        "contract": _CONTRACT_ARG,
                        "program_xcf": {
                            "type": "string",
                            "description": "File name of the program XCF in contracts/",
                        },
                    },
                    ["contract", "program_xcf"],
                ),
                handler=bind(_program_cpld_sram),
                touches_hardware=True,
            ),
            Tool(
                name="measure_gate1_gpio",
                description=(
                    "Capture the isolated Gate 1 debug output on the RTM3004 after the "
                    "probe approvals and channel checks in the contract pass."
                ),
                input_schema=_schema(
                    {"contract": _CONTRACT_ARG, "params": {"type": "object"}},
                    ["contract"],
                ),
                handler=bind(_measure_gate1_gpio),
                touches_hardware=True,
            ),
            Tool(
                name="measure_board_scope",
                description=(
                    "Capture one to four RTM3004 channels using only channel, scale, "
                    "timebase and trigger settings bound into the reviewed board profile."
                ),
                input_schema=_schema(
                    {"contract": _CONTRACT_ARG, "stage_id": {"type": "string"}},
                    ["contract", "stage_id"],
                ),
                handler=bind(_measure_board_scope),
                touches_hardware=True,
            ),
            Tool(
                name="run_bbb",
                description=(
                    "Run one script on the BeagleBone Black over SSH and record stdout, "
                    "stderr, exit code and the script hash. Conversation jobs may use a "
                    "path inside their isolated workspace. Historical callers use the "
                    "contract script list. "
                    "Alternatively identity_only=true authenticates and reads BBB model/kernel once; "
                    "no script/upload/GPIO and no script allowlist needed. Requires bbb_identify."
                ),
                input_schema=_schema(
                    {
                        "contract": _CONTRACT_ARG,
                        "identity_only": {"type": "boolean"},
                        "script": {
                            "type": "string",
                            "description": "Allowlist key from the contract's scripts map",
                        },
                    },
                    ["contract"],
                ),
                handler=bind(_run_bbb),
                touches_hardware=True,
            ),
            Tool(
                name="deploy_pru",
                description=(
                    "Upload, build, install and restart the PRU firmware on the BBB, then "
                    "prove it answers VERSION and PING. Reports the stage that failed."
                ),
                input_schema=_schema({"contract": _CONTRACT_ARG}, ["contract"]),
                handler=bind(_deploy_pru),
                touches_hardware=True,
            ),
            Tool(
                name="run_board_stage",
                description=(
                    "Run one exact upload or measurement adapter argv from the "
                    "digest-bound board profile. Requires hardware mode and approval."
                ),
                input_schema=_schema(
                    {"contract": _CONTRACT_ARG, "stage_id": {"type": "string"}},
                    ["contract", "stage_id"],
                ),
                handler=bind(_run_board_stage),
                touches_hardware=True,
            ),
        ]
    )
    # The role filter is applied last, over both halves. A tool outside the
    # role's row is not registered, so no prompt can reach it -- the same lock
    # the --allow-hardware flag already provides, one notch finer.
    selected = [t for t in tools if policy.permits_tool(t.name)]
    if policy.hardware_tool is None:
        return selected
    # One already-authorized attempt. Enforce the actual MCP arguments here;
    # runtime approval prompts do not necessarily include them or happen at all.
    from dataclasses import replace
    from QKD_PROJ.hwtools.result import sha256_file
    remaining = [1]
    from QKD_PROJ.orchestrator.execution_sources import SourceSnapshot, source_block
    installed_sources = SourceSnapshot(policy.device_root or PROJECT_ROOT, policy.hardware_tool)
    workspace_sources = SourceSnapshot(policy.project_root, policy.hardware_tool)

    def guarded(tool):
        def validate(args):
            if args.get("contract") != policy.hardware_contract:
                raise ToolError("device call names a different hardware boundary")
            if policy.hardware_profile_path is not None:
                if sha256_file(Path(policy.hardware_profile_path)) != policy.hardware_profile_sha256:
                    raise ToolError("hardware profile changed after authorization")
            else:
                path = _contract_path(policy, str(policy.hardware_contract))
                if sha256_file(path) != policy.hardware_contract_sha256:
                    raise ToolError("hardware boundary changed after authorization")
            # A malformed argument has not attempted a device effect. Return
            # corrective feedback without consuming the authorized attempt.
            schema = tool.input_schema
            for key in schema.get("required", []):
                if key not in args:
                    raise ToolError(f"{tool.name} needs argument {key!r}; correct the call")
            types = {"string": str, "object": dict, "array": list, "boolean": bool}
            for key, value in args.items():
                expected_type = types.get(schema.get("properties", {}).get(key, {}).get("type"))
                if expected_type is not None and not isinstance(value, expected_type):
                    raise ToolError(f"{tool.name} argument {key!r} has the wrong type; correct the call")
            if tool.name in {"run_bbb", "deploy_pru"}:
                _check_bbb_target(policy, _gate(policy, str(policy.hardware_contract)))
            if tool.name in {"run_bbb", "measure_scope"}:
                identity = _identity_only(args)
                other = "script" if tool.name == "run_bbb" else "params"
                if identity and (args.get(other) or policy.workspace_recipe):
                    raise ToolError("identity_only cannot include executable/capture configuration")
                if not identity and other not in args:
                    raise ToolError(f"{tool.name} needs {other} unless identity_only=true; correct the call")
        def dispatch(args):
            if policy.scenario_workspace is not None:
                from .protocol import DevicePreparationError
                try:
                    validate(args)
                    _validate_scenario_inputs(policy, tool.name, args)
                except Exception as exc:
                    raise DevicePreparationError(str(exc)) from exc
            else:
                validate(args)
            if remaining[0] <= 0:
                raise ToolError("authorized device attempt already consumed; report the result before another step")
            changes = installed_sources.changes(tool.name) + workspace_sources.changes(tool.name)
            if changes:
                return {**source_block(changes, policy.project_root), 'blocked_tool': tool.name}
            remaining[0] -= 1
            return tool.handler(args)
        def invoke(args):
            if policy.workspace_recipe and tool.name != "measure_scope":
                from QKD_PROJ.hwtools.workspace_recipe import execute_device_recipe
                return execute_device_recipe(policy, args, dispatch, validate=validate)
            return dispatch(args)
        return invoke

    return [replace(t, handler=guarded(t)) if t.touches_hardware else t
            for t in selected if not t.touches_hardware or t.name == policy.hardware_tool]


# -- prompts and resources ---------------------------------------------------


def _skill_files(policy: ServerPolicy) -> list[Path]:
    skills = policy.project_root / ".claude" / "skills"
    if not skills.is_dir():
        return []
    return sorted(skills.glob("*/SKILL.md"))


def _skill_meta(path: Path) -> tuple[str, str, str]:
    """Return (name, description, body) from a SKILL.md with YAML frontmatter."""
    text = path.read_text(encoding="utf-8")
    name, description = path.parent.name, ""
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        front, marker, body = rest.partition("\n---")
        if marker:
            try:
                data = yaml.safe_load(front) or {}
                name = str(data.get("name") or name)
                description = str(data.get("description") or "")
            except yaml.YAMLError:
                body = text
            return name, description, body.lstrip("\n")
    return name, description, text


def _agent_prompts(policy: ServerPolicy) -> list[Prompt]:
    """Agent definitions, offered to whichever runtime asked for them.

    A runtime that can be handed a per-agent tool allowlist (Claude Code) gets
    one from ``agents/export.py``. A runtime that cannot (Codex) still needs
    the definition itself, and fetching it over the same MCP connection that
    carries the tools means the two cannot drift apart on disk.

    The board-touching agents are withheld unless the server was started with
    ``--allow-hardware``. This mirrors the tool list exactly: on a locked
    server their tools do not exist, so handing out a prompt that instructs an
    agent to call them would only produce a confident failure.
    """
    try:
        from QKD_PROJ.agents.export import render_codex_prompt
        from QKD_PROJ.agents.schema import ValidationError, load_all
    except ImportError:  # pragma: no cover - agents/ is optional at runtime
        return []

    try:
        agents = load_all(policy.project_root / "QKD_PROJ" / "agents")
    except (ValidationError, OSError):
        # A malformed definition must not take the tool server down with it.
        return []

    prompts = []
    for agent in agents:
        if agent.touches_hardware and not policy.allow_hardware:
            continue
        # A roled server serves that role's definition and no other. Handing a
        # measurement server the board-operator prompt would instruct an agent
        # to call tools this launch does not register -- a confident failure,
        # for the same reason the hardware prompts are withheld above.
        if policy.role is not None and agent.name != policy.role:
            continue
        prompts.append(
            Prompt(
                name=f"agent-{agent.name}",
                description=f"Agent ({agent.role}): {agent.description}",
                loader=(lambda body: (lambda args: body))(render_codex_prompt(agent)),
            )
        )
    return prompts


def build_prompts(policy: ServerPolicy) -> list[Prompt]:
    """Skills are portable playbooks: frontmatter plus markdown, no runtime API.

    Exposing them as MCP prompts is what makes the same procedural knowledge
    available to Codex, Claude Code or anything else that speaks MCP. Agent
    definitions ride the same channel for the same reason.
    """
    prompts = []
    for path in _skill_files(policy):
        name, description, body = _skill_meta(path)
        prompts.append(
            Prompt(
                name=name,
                description=description or f"Skill: {name}",
                loader=(lambda body: (lambda args: body))(body),
            )
        )
    return prompts + _agent_prompts(policy)


def build_resources(policy: ServerPolicy) -> list[Resource]:
    resources: list[Resource] = []
    for path in sorted(policy.contracts_dir.glob("*.yaml")):
        resources.append(
            Resource(
                uri=f"contract://{path.name}",
                name=path.name,
                description="Contract: what is allowed, forbidden, and how to judge",
                mime_type="application/yaml",
                loader=(lambda p: (lambda: _clip(p.read_text(encoding="utf-8"))))(path),
            )
        )
    for filename, description in (
        ("STATUS.md", "Where the work stopped and what is next"),
        ("README.md", "Three-layer architecture and the rules at each boundary"),
    ):
        path = policy.project_root / "QKD_PROJ" / filename
        if path.is_file():
            resources.append(
                Resource(
                    uri=f"doc://{filename}",
                    name=filename,
                    description=description,
                    mime_type="text/markdown",
                    loader=(lambda p: (lambda: _clip(p.read_text(encoding="utf-8"))))(path),
                )
            )
    return resources


INSTRUCTIONS = """RTASE_V1 hardware tools.

Layer 3 of a three-layer design: these tools are deterministic and each returns
a ToolResult recorded under runs/. You decide what to do; the tools decide
whether it is allowed.

Rules that are not negotiable here:
- Every call names a contract in contracts/. Read it first (list_contracts,
  read_contract). The contract, not this conversation, says what is permitted.
- Do not interpret a PASS/FAIL yourself when a tool already judged it. Layer 3
  judges against thresholds; you interpret why and decide what to try next.
- Never assert a threshold that no datasheet or contract supports.
- If a tool refuses, read the refusal. It is a contract statement, not a bug to
  work around.
"""
