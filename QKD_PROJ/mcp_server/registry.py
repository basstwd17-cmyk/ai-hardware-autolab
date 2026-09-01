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
    # None means the historical profile: every tool the `allow_hardware` flag
    # permits. A named role narrows that to one row of the matrix, and the two
    # locks compose -- a role that lists board tools still gets none of them
    # unless the flag was also given.
    role: str | None = None

    def __post_init__(self) -> None:
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

    _contract_path(policy, contract)
    return SafetyGate(
        Contracts.load(
            contract,
            contracts_dir=policy.contracts_dir,
            project_root=policy.project_root,
        ),
        allow_hardware=policy.allow_hardware,
    )


def _record(policy: ServerPolicy, gate, args: dict[str, Any], default: str):
    from QKD_PROJ.hwtools import RunRecord

    name = args.get("record_name") or default
    if not RUN_NAME.fullmatch(str(name)):
        raise ToolError(f"record_name must match {RUN_NAME.pattern}")
    return RunRecord(str(name), gate, runs_dir=policy.runs_dir)


def _result_payload(record, result) -> dict[str, Any]:
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
    for path in sorted(policy.contracts_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            entries.append({"file": path.name, "error": f"unparsable: {exc}"})
            continue
        entries.append(
            {
                "file": path.name,
                "test_id": data.get("test_id"),
                "status": data.get("status"),
                "allowed_operations": data.get("allowed_operations"),
                "forbidden_operations": data.get("forbidden_operations"),
            }
        )
    return {"contracts": entries, "policy": policy.describe()}


def _read_contract(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    path = _contract_path(policy, _string(args, "name"))
    return {"file": path.name, "text": _clip(path.read_text(encoding="utf-8"))}


def _list_runs(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    limit = int(args.get("limit") or 25)
    if not policy.runs_dir.is_dir():
        return {"runs": [], "note": f"{policy.runs_dir} does not exist yet"}
    directories = sorted((p for p in policy.runs_dir.iterdir() if p.is_dir()), reverse=True)
    runs = []
    for directory in directories[: max(1, limit)]:
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
    return {"runs": runs, "total": len(directories)}


def _read_run(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    name = _string(args, "run")
    if not RUN_NAME.fullmatch(name):
        raise ToolError("run must be a plain run directory name")
    directory = policy.runs_dir / name
    if not directory.is_dir():
        raise ToolError(f"No such run: {name}")
    payload: dict[str, Any] = {"run": name, "files": sorted(p.name for p in directory.iterdir())}
    for key, filename in (("status", "run_status.json"), ("manifest", "manifest.json")):
        path = directory / filename
        if path.is_file():
            try:
                payload[key] = json.loads(_clip(path.read_text(encoding="utf-8")))
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
    if policy.role is not None and stage.agent != policy.role:
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
    return _result_payload(record, scan_jtag(gate, record=record))


def _program_cpld_sram(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import program_cpld_sram, scan_jtag

    gate = _gate(policy, _string(args, "contract"))
    xcf_name = _string(args, "program_xcf")
    if not re.fullmatch(r"^[A-Za-z0-9._-]+\.xcf$", xcf_name):
        raise ToolError("program_xcf must be a plain .xcf file name in contracts/")
    xcf = policy.contracts_dir / xcf_name
    if not xcf.is_file():
        raise ToolError(f"No such XCF: {xcf_name}")

    record = _record(policy, gate, args, "mcp_sram_program")
    # The chain is verified in the same run that programs it. A scan from an
    # earlier session proves nothing about the cable that is attached now.
    scan = scan_jtag(gate, record=record)
    if not scan.ok:
        return _result_payload(record, scan)
    result = program_cpld_sram(gate, scan, program_xcf=xcf, record=record)
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


def _run_bbb(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import run_bbb

    gate = _gate(policy, _string(args, "contract"))
    script = _string(args, "script")

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
    return _result_payload(record, run_bbb(gate, script, record))


def _deploy_pru(policy: ServerPolicy, args: dict[str, Any]) -> dict[str, Any]:
    from QKD_PROJ.hwtools import deploy_pru

    gate = _gate(policy, _string(args, "contract"))
    record = _record(policy, gate, args, "mcp_deploy_pru")
    return _result_payload(record, deploy_pru(gate, record))


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
                "List every contract in contracts/ with its test_id, status and the "
                "operations it allows or forbids. Start here: every other tool takes "
                "a contract name."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
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
                "Runs are the evidence chain: every tool call writes one."
            ),
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                "additionalProperties": False,
            },
            handler=bind(_list_runs),
        ),
        Tool(
            name="read_run",
            description="Return run_status.json and manifest.json for one run directory.",
            input_schema={
                "type": "object",
                "properties": {"run": {"type": "string"}},
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
                    "Run one allowlisted diagnostic script on the BeagleBone Black over "
                    "SSH and record stdout, stderr, exit code and the script hash. Only "
                    "scripts listed in the contract, matched by content hash, will run."
                ),
                input_schema=_schema(
                    {
                        "contract": _CONTRACT_ARG,
                        "script": {
                            "type": "string",
                            "description": "Allowlist key from the contract's scripts map",
                        },
                    },
                    ["contract", "script"],
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
    return [t for t in tools if policy.permits_tool(t.name)]


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
