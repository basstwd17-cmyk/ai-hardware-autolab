"""Compile a governed three-agent Plan into immutable contract YAML.

Lead proposes typed data. This service validates it against intake-time
capabilities and board context, renders the bytes deterministically and
installs them create-only. Installing a hardware contract grants no hardware
access; the execution loop still requires a digest-bound Campaign approval.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_hardware_autolab.agents.schema import ValidationError, validate_plan_governance
from ai_hardware_autolab.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
from ai_hardware_autolab.hwtools.safety import READ_ONLY_OPERATIONS, SafetyViolation
from ai_hardware_autolab.hwtools.sram import SramProgramError
from .development import autonomous, normalize_plan, validate_development_plan
from .boundary_normalization import normalize_boundary
from .candidate_reference import resolve_candidate_reference


COMPILER_VERSION = 1
COMPILED_EVENT = "contract.compiled"
SELECTED_EVENT = "capability.selected"
_SLUG = re.compile(r"[^a-z0-9]+")


class ContractCompileError(ValueError):
    """The proposed contract is ambiguous, stale, or outside known bounds."""


@dataclass(frozen=True)
class CompiledPlan:
    plan: dict[str, Any]
    filename: str
    sha256: str
    action: str
    installed: bool
    selected_capabilities: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def describe(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "action": self.action,
            "installed": self.installed,
            "selected_capabilities": [dict(item) for item in self.selected_capabilities],
        }


class ContractCompiler:
    def __init__(self, contracts_dir: Path, *, project_root: Path) -> None:
        self.contracts_dir = Path(contracts_dir).resolve()
        self.project_root = Path(project_root).resolve()

    def compile(
        self,
        plan: dict[str, Any],
        *,
        job_id: str,
        iteration: int,
        scenario_sha256: str,
        resources: dict[str, Any],
    ) -> CompiledPlan:
        if autonomous(resources):
            try:
                return self._compile_development(plan, resources)
            except (SafetyViolation, SramProgramError, OSError, yaml.YAMLError, ValueError) as exc:
                # Invalid device configuration is feedback to Lead before any
                # approval or device call, not an unhandled daemon failure.
                raise ContractCompileError(f"repair hardware configuration: {exc}") from exc
        rows = resources.get("available_capabilities")
        capabilities = rows if isinstance(rows, list) else []
        capabilities = self._recheck_catalog(resources, capabilities)
        if not re.fullmatch(r"[0-9a-f]{64}", scenario_sha256):
            raise ContractCompileError("scenario_sha256 must be a lowercase SHA-256 digest")
        try:
            validate_plan_governance(plan, available_capabilities=capabilities)
        except ValidationError as exc:
            raise ContractCompileError(str(exc)) from exc

        selected = self._selected(plan, capabilities)
        action = str(plan["contract_action"])
        if action == "reuse":
            filename, digest = self._reuse_contract(plan, selected, resources)
            governed = self._bind_contract(plan, filename, digest, action)
            return CompiledPlan(governed, filename, digest, action, False, tuple(selected))

        spec = dict(plan["contract_spec"])
        hardware = any(bool(step.get("touches_hardware")) for step in plan.get("steps", []))
        self._validate_operations(spec, resources, capabilities, hardware=hardware)
        parents = self._parent_contracts(selected, resources) if action == "amend" else []
        if action == "amend" and not parents:
            raise ContractCompileError("contract_action 'amend' needs a digest-bound parent contract")

        identity = {
            "scenario_sha256": scenario_sha256,
            "contract_action": action,
            "contract_spec": spec,
            "implementation": plan["implementation"],
            "steps": plan.get("steps", []),
            "parents": parents,
        }
        identity_sha = _json_sha256(identity)
        slug = _SLUG.sub("-", str(spec["objective"]).lower()).strip("-")[:28] or "scenario"
        filename = f"auto-{slug}-{identity_sha[:12]}.yaml"
        inherited = self._amended_contract_base(parents) if action == "amend" else {}
        contract = {
            **inherited,
            "schema_version": 1,
            "test_id": filename[:-5],
            "status": "reviewed_generated_hardware" if hardware else "reviewed_generated_offline",
            "purpose": [str(spec["objective"]).strip()],
            "allowed_operations": list(spec["allowed_operations"]),
            "forbidden_operations": list(spec["forbidden_operations"]),
            "generated_from": {
                "compiler_version": COMPILER_VERSION,
                "job_id": job_id,
                "mission_id": str(resources.get("mission_id") or job_id),
                "iteration": iteration,
                "scenario_sha256": scenario_sha256,
                "plan_identity_sha256": identity_sha,
                "contract_action": action,
                "parent_contracts": parents,
                "base_capabilities": [dict(item) for item in selected],
            },
            "orchestration": {
                "agent_flow": ["lead-agent", "builder", "verifier"],
                "write_scope": list(plan["implementation"]["write_scope"]),
                "steps": [dict(step) for step in plan.get("steps", [])],
            },
            "pass_criteria": {
                "conditions": [dict(item) for item in spec["pass_criteria"]],
                "evidence_required": list(spec["evidence_required"]),
            },
        }
        if hardware:
            contract["hardware_safety"] = dict(spec["hardware_safety"])
        board = resources.get("board_development")
        selected_board = board.get("selected") if isinstance(board, dict) else None
        if isinstance(selected_board, dict):
            contract["board_profile"] = {
                "board_id": selected_board.get("board_id"),
                "revision": selected_board.get("revision"),
                "path": selected_board.get("profile"),
                "sha256": selected_board.get("profile_sha256"),
                "board_map": selected_board.get("board_map"),
            }
        content = yaml.safe_dump(
            contract, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        target = self.contracts_dir / filename
        installed = self._install_create_only(target, content, digest)
        governed = self._bind_contract(plan, filename, digest, action)
        return CompiledPlan(governed, filename, digest, action, installed, tuple(selected))

    def _compile_development(self, plan: dict[str, Any], resources: dict[str, Any]) -> CompiledPlan:
        """No coding contract. Bind only contracts actually consumed by tools.

        Safety proposals may be created by Lead, but installation is not approval.
        Their identity excludes implementation steps, filenames and success tests.
        """
        result = normalize_plan(plan, resources)
        validate_development_plan(result, resources, available_capabilities=[])
        proposals = result.get("hardware_boundaries", {})
        if not isinstance(proposals, dict):
            raise ContractCompileError("hardware_boundaries must map boundary names to proposals")
        proposals = dict(proposals)
        if isinstance(result.get("hardware_contract"), dict):
            proposals["hardware_boundary"] = result["hardware_contract"]
        installed = False
        pending: dict[str, bytes] = {}

        def contract_bytes(path: Path) -> bytes:
            return pending[path.name] if path.name in pending else path.read_bytes()

        def prepared_bound(path: Path):
            from ai_hardware_autolab.hwtools.safety import Contracts
            return Contracts.from_spec(yaml.safe_load(contract_bytes(path)) or {}, path, project_root=self.project_root)

        for boundary_id, proposal in proposals.items():
            if not isinstance(boundary_id, str) or not boundary_id or not isinstance(proposal, dict):
                raise ContractCompileError("each hardware boundary needs a name and an object")
            proposal = resolve_candidate_reference(normalize_boundary(proposal), self.contracts_dir)
            # Keep executable device configuration supplied by Lead intact. Do
            # not manufacture safety limits or claim a human reviewed them.
            safety = proposal.get("hardware_safety")
            stops = safety.get("stop_conditions") if isinstance(safety, dict) else None
            if not isinstance(stops, list) or not stops or any(not isinstance(item, str) or not item.strip() for item in stops):
                raise ContractCompileError("hardware_contract needs its safety boundary and stop conditions")
            operations = proposal.get("allowed_operations")
            if not isinstance(operations, list) or not operations or any(not isinstance(item, str) or not item.strip() for item in operations):
                raise ContractCompileError("hardware_contract needs the requested device operations")
            document = copy.deepcopy(proposal)
            document.pop("orchestration", None)
            document.pop("pass_criteria", None)
            document["status"] = "proposed_hardware_boundary"
            content = yaml.safe_dump(document, sort_keys=True, allow_unicode=True).encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            filename = f"hardware-{digest[:20]}.yaml"
            pending[filename] = content
            for step in result["steps"]:
                if step.get("touches_hardware") and step.get("contract") == f"${boundary_id}":
                    step["contract"] = filename
        bindings = []
        normalized_references = {}
        for step in result["steps"]:
            if step.get("tool") == "codex_workspace":
                continue
            name = str(step.get("contract") or "")
            path = self.contracts_dir / name if name in pending else self._contract_path(name)
            original_digest = hashlib.sha256(contract_bytes(path)).hexdigest()
            if step.get("contract_sha256") and step["contract_sha256"] != original_digest:
                raise ContractCompileError(f"contract changed before preparation: {path.name}")
            # Reused generated proposals go through precisely the same shape
            # conversion as newly authored ones. Keep historical bytes intact.
            if path.name not in normalized_references:
                source = yaml.safe_load(contract_bytes(path)) or {}
                if not isinstance(source, dict):
                    raise ContractCompileError(f"contract YAML must be an object: {path.name}")
                normalized = resolve_candidate_reference(normalize_boundary(source), self.contracts_dir) if source.get("status") == "proposed_hardware_boundary" else source
                derived = path
                if normalized != source:
                    normalized["normalized_from"] = {"name": path.name, "sha256": original_digest}
                    raw = yaml.safe_dump(normalized, sort_keys=True, allow_unicode=True).encode("utf-8")
                    derived_digest = hashlib.sha256(raw).hexdigest()
                    derived = self.contracts_dir / f"hardware-{derived_digest[:20]}.yaml"
                    pending[derived.name] = raw
                normalized_references[path.name] = derived
            path = normalized_references[path.name]
            step["contract"] = path.name
            step["contract_sha256"] = hashlib.sha256(contract_bytes(path)).hexdigest()
            if step.get("touches_hardware"):
                if step.get("tool") == "measure_scope":
                    from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate
                    from ai_hardware_autolab.hwtools.result import sha256_file
                    bound = prepared_bound(path)
                    SafetyGate(bound, allow_hardware=True).instrument_scope()
                    # The approval signs device configuration, not a nonexistent
                    # programming image. Freeze this BEFORE a person clicks yes.
                    step["artifacts"] = {
                        bound.test_spec_path.resolve().relative_to(self.project_root).as_posix(): step["contract_sha256"],
                        bound.board_map_path.resolve().relative_to(self.project_root).as_posix(): sha256_file(bound.board_map_path),
                    }
                else:
                    from .campaign_proposal import CampaignProposalService, NATIVE_OPERATIONS
                    if step.get("tool") in NATIVE_OPERATIONS:
                        proposer = CampaignProposalService(None, None, project_root=self.project_root,
                            contracts_dir=self.contracts_dir, campaigns_dir=self.project_root / "ai_hardware_autolab/campaigns")
                        blockers = []
                        permission = proposer._native_permission(path.name, step["contract_sha256"], step, blockers, 1,
                                                                prepared_bound=prepared_bound(path))
                        if blockers:
                            raise ContractCompileError("; ".join(blockers))
                        # Preserve and check any explicit caller pins, never silently replace them.
                        artifacts = dict(step.get("artifacts") or {})
                        for ref, digest in permission["artifacts"].items():
                            if ref in artifacts and artifacts[ref] != digest:
                                raise ContractCompileError(f"step artifact differs from actual input: {ref}")
                            artifacts[ref] = digest
                        step["artifacts"] = artifacts
                binding = {"name": path.name, "sha256": step["contract_sha256"], "action": "hardware_boundary"}
                if binding not in bindings:
                    bindings.append(binding)
        # Validation errors above leave no generated drafts on disk. Store only
        # referenced, prepared documents; preserve all existing files verbatim.
        referenced = {s.get("contract") for s in result["steps"]}
        for filename, raw in pending.items():
            if filename in referenced:
                installed = self._install_create_only(self.contracts_dir / filename, raw,
                    hashlib.sha256(raw).hexdigest()) or installed
        result["hardware_contracts"] = bindings
        if bindings:
            result["compiled_contract"] = bindings[0]
        else:
            result.pop("compiled_contract", None)
        first = bindings[0] if bindings else {}
        return CompiledPlan(result, first.get("name", ""), first.get("sha256", ""),
                            "hardware_boundary" if bindings else "workspace", installed)

    def _amended_contract_base(self, parents: list[dict[str, str]]) -> dict[str, Any]:
        """Carry executable adapter configuration out of one reviewed parent.

        Earlier code recorded only the parent digest and then rendered a fresh
        generic document.  That made an amended BBB contract lose its ``bbb``
        and ``scripts`` sections, so the very hardware tool the Plan selected
        could no longer read its configuration.  Multiple parents are not
        merged by guessing; Lead must choose one executable baseline.
        """
        if len(parents) != 1:
            raise ContractCompileError(
                "contract_action 'amend' needs exactly one executable parent contract"
            )
        parent = parents[0]
        path = self._contract_path(parent["name"])
        actual = _file_sha256(path)
        if actual != parent["sha256"]:
            raise ContractCompileError(
                f"parent contract {parent['name']} drifted before amendment"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ContractCompileError(f"parent contract {parent['name']} is not an object")
        inherited = copy.deepcopy(raw)
        for key in (
            "schema_version",
            "test_id",
            "status",
            "purpose",
            "allowed_operations",
            "forbidden_operations",
            "generated_from",
            "orchestration",
            "pass_criteria",
            "hardware_safety",
            "board_profile",
        ):
            inherited.pop(key, None)
        return inherited

    def _reuse_contract(
        self,
        plan: dict[str, Any],
        selected: list[dict[str, Any]],
        resources: dict[str, Any],
    ) -> tuple[str, str]:
        names = {str(step.get("contract") or "") for step in plan.get("steps", [])}
        names.discard("")
        if len(names) != 1:
            raise ContractCompileError("contract reuse requires every step to name one contract")
        filename = next(iter(names))
        allowed = {
            str(contract.get("name"))
            for item in selected
            for contract in item.get("contracts", [])
            if isinstance(contract, dict)
        }
        selected_contract = resources.get("selected_contract")
        if isinstance(selected_contract, dict) and isinstance(selected_contract.get("name"), str):
            allowed.add(str(selected_contract["name"]))
        if filename not in allowed:
            raise ContractCompileError(f"reused contract {filename!r} is not bound to the selected input")
        path = self._contract_path(filename)
        return filename, _file_sha256(path)

    def _selected(
        self, plan: dict[str, Any], capabilities: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        catalog = {str(row.get("id")): row for row in capabilities if isinstance(row, dict)}
        selected: list[dict[str, Any]] = []
        for reference in plan["implementation"]["base_capabilities"]:
            row = catalog[str(reference["id"])]
            selected.append({
                "id": str(reference["id"]),
                "version": str(reference["version"]),
                "sha256": str(reference["catalog_sha256"]),
                "contracts": [dict(item) for item in row.get("contracts", []) if isinstance(item, dict)],
                "runs": list(row.get("runs") or []),
                "assets": [dict(item) for item in row.get("assets", []) if isinstance(item, dict)],
            })
        return selected

    def _recheck_catalog(
        self, resources: dict[str, Any], offered: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        source = resources.get("capability_catalog_source")
        if not isinstance(source, str) or not source:
            return offered
        path = Path(source).resolve()
        try:
            path.relative_to(self.project_root)
        except ValueError as exc:
            raise ContractCompileError("capability catalog source escapes the project") from exc
        try:
            current = CapabilityCatalog.load(path, project_root=self.project_root)
        except CapabilityCatalogError as exc:
            raise ContractCompileError(f"capability catalog no longer validates: {exc}") from exc
        expected = resources.get("capability_catalog_sha256")
        if not isinstance(expected, str) or current.digest != expected:
            raise ContractCompileError("capability catalog changed after mission intake")
        current_rows = {row["id"]: row for row in current.for_planning(
            str(resources.get("execution_mode") or "offline")
        )}
        result: list[dict[str, Any]] = []
        for offered_row in offered:
            identifier = offered_row.get("id")
            fresh = current_rows.get(identifier)
            if fresh is None or fresh.get("sha256") != offered_row.get("sha256"):
                raise ContractCompileError(
                    f"offered capability {identifier!r} changed after mission intake"
                )
            result.append({
                **fresh,
                **{key: offered_row[key] for key in ("match_score", "match_reasons") if key in offered_row},
            })
        return result

    def _validate_operations(
        self,
        spec: dict[str, Any],
        resources: dict[str, Any],
        capabilities: list[dict[str, Any]],
        *,
        hardware: bool,
    ) -> None:
        known = set(READ_ONLY_OPERATIONS)
        for row in capabilities:
            known.update(str(item) for item in row.get("operations", []) if isinstance(item, str))
        selected = resources.get("selected_contract")
        if isinstance(selected, dict):
            path = self.contracts_dir / str(selected.get("name") or "")
            if path.is_file():
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    known.update(str(item) for item in raw.get("allowed_operations", []) if isinstance(item, str))
        board = resources.get("board_development")
        selected_board = board.get("selected") if isinstance(board, dict) else None
        if isinstance(selected_board, dict):
            phase_operation = {
                "design": "firmware_build",
                "build": "firmware_build",
                "upload": "board_upload",
                "measure": "oscilloscope_capture",
                "verify": "readback_or_program_status",
            }
            known.update(
                phase_operation[str(stage.get("phase"))]
                for stage in selected_board.get("stages") or []
                if isinstance(stage, dict)
                and stage.get("status") == "ready"
                and str(stage.get("phase")) in phase_operation
                and (hardware or stage.get("mode") != "hardware")
            )
        unknown = set(spec["allowed_operations"]) - known
        if unknown:
            raise ContractCompileError(
                "contract proposes operations absent from verified capabilities/profile: "
                + ", ".join(sorted(unknown))
            )
        if not hardware:
            unsafe = set(spec["allowed_operations"]) - READ_ONLY_OPERATIONS
            if unsafe:
                raise ContractCompileError(
                    "offline contract may not authorize hardware operations: "
                    + ", ".join(sorted(unsafe))
                )

    def _parent_contracts(
        self, selected: list[dict[str, Any]], resources: dict[str, Any]
    ) -> list[dict[str, str]]:
        parents: dict[str, str] = {}
        for item in selected:
            for contract in item.get("contracts", []):
                name = str(contract.get("name") or "")
                if name:
                    parents[name] = str(contract.get("sha256") or _file_sha256(self._contract_path(name)))
        current = resources.get("selected_contract")
        if isinstance(current, dict) and isinstance(current.get("name"), str):
            name = str(current["name"])
            parents[name] = str(current.get("sha256") or _file_sha256(self._contract_path(name)))
        return [{"name": name, "sha256": parents[name]} for name in sorted(parents)]

    def _contract_path(self, filename: str) -> Path:
        if Path(filename).name != filename or not filename.endswith(".yaml"):
            raise ContractCompileError(f"contract must be a plain YAML filename: {filename!r}")
        path = (self.contracts_dir / filename).resolve()
        if path.parent != self.contracts_dir or not path.is_file():
            raise ContractCompileError(f"contract is missing: {filename}")
        return path

    @staticmethod
    def _bind_contract(
        plan: dict[str, Any], filename: str, digest: str, action: str
    ) -> dict[str, Any]:
        result = dict(plan)
        result["steps"] = [{**step, "contract": filename} for step in plan.get("steps", [])]
        result["compiled_contract"] = {
            "name": filename,
            "sha256": digest,
            "action": action,
        }
        return result

    def _install_create_only(self, target: Path, content: bytes, digest: str) -> bool:
        self.contracts_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or _file_sha256(target) != digest:
                raise ContractCompileError(f"refusing to overwrite existing contract {target.name}")
            return False
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.contracts_dir, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if _file_sha256(target) != digest:
                    raise ContractCompileError(
                        f"contract {target.name} appeared concurrently with different bytes"
                    )
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
