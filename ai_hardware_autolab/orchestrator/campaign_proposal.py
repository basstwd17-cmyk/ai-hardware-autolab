"""Build one reviewable campaign from a completed hardware mission.

The model does not author this YAML.  The proposal is derived from the exact
scenario, installed contract, digest-bound board profile and typed hardware
steps already recorded in the ledger.  Approval writes those bytes create-only
and activates them through the same CampaignController used by CLI and UI.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from ai_hardware_autolab.boards.profiles import BoardProfile, BoardProfileError
from ai_hardware_autolab.hwtools.result import sha256_file
from ai_hardware_autolab.orchestrator.campaign import CampaignController
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.pagination import last_event


# These are driver effects, not a board/task routing table. Lead chooses the
# method; the existing driver owns the checks for the action it really sends.
NATIVE_OPERATIONS = {
    "scan_jtag": ("jtag_scan",),
    "program_cpld_sram": ("jtag_scan", "sram_fast_program"),
    "deploy_pru": ("pru_deploy",),
    "run_bbb": ("bbb_ssh_command",),
}


class CampaignProposalError(RuntimeError):
    pass


@dataclass(frozen=True)
class CampaignProposal:
    job_id: str
    contract: str
    contract_sha256: str
    scenario_sha256: str
    board: dict[str, Any] | None
    hardware_safety: dict[str, Any]
    permissions: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]
    max_iterations: int
    max_auto_approvals: int
    duration_hours: int
    review_sha256: str
    boundaries: tuple[dict[str, Any], ...] = ()
    authorization_scope: str = "scenario"

    @property
    def ready(self) -> bool:
        return not self.blockers and bool(self.permissions)

    def describe(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "contract": self.contract,
            "contract_sha256": self.contract_sha256,
            "scenario_sha256": self.scenario_sha256,
            "board": self.board,
            "hardware_safety": dict(self.hardware_safety),
            "permissions": [dict(item) for item in self.permissions],
            "blockers": list(self.blockers),
            "ready": self.ready,
            "max_iterations": self.max_iterations,
            "max_auto_approvals": self.max_auto_approvals,
            "duration_hours": self.duration_hours,
            "review_sha256": self.review_sha256,
            "boundaries": [dict(item) for item in self.boundaries],
            "authorization_scope": self.authorization_scope,
        }


class CampaignProposalService:
    def __init__(
        self,
        ledger: JobLedger,
        controller: CampaignController,
        *,
        project_root: Path,
        contracts_dir: Path,
        campaigns_dir: Path,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.ledger = ledger
        self.controller = controller
        self.project_root = Path(project_root).resolve()
        self.contracts_dir = Path(contracts_dir).resolve()
        self.campaigns_dir = Path(campaigns_dir).resolve()
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat())

    def candidates(self) -> list[dict[str, Any]]:
        """Live review targets, not old result cards. This does not grant approval."""
        candidates = []
        for job in self.ledger.jobs():
            if job.gate != "__mission__":
                continue
            briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
            resources = briefs[0].payload.get("resources", {}) if len(briefs) == 1 else {}
            if resources.get("execution_mode") != "hardware":
                continue
            simple = resources.get("workflow") == "simple-mission"
            if simple and job.state == "done":
                continue
            if not simple and (job.state != "done" or job.outcome != "contract_gap"):
                continue
            plans = self.ledger.handoffs(job.job_id, "Plan", job.iteration) if simple else []
            steps = plans[-1].payload.get("steps", []) if plans else []
            has_hardware = not simple or any(
                isinstance(step, dict) and step.get("touches_hardware") is True
                for step in steps
            )
            blocked = last_event(self.ledger, job.job_id, lambda e: e.kind == "job.blocked")
            detail = str(blocked.payload.get("reason", "")) if blocked and job.state == "blocked" else ""
            candidates.append({
                "job_id": job.job_id,
                "state": job.state,
                "objective": job.objective,
                "preview_available": has_hardware,
                "detail": detail or (
                    "하드웨어 실행 범위를 검토할 수 있습니다."
                    if has_hardware else
                    "Lead의 하드웨어 실행 계획이 아직 없습니다. 개발·조사가 끝난 뒤 범위를 검토할 수 있습니다."
                ),
                "revision": f"{job.updated_at}:{plans[-1].sha256 if plans else ''}",
            })
        return sorted(candidates, key=lambda item: not item["preview_available"])

    def preview(
        self,
        job_id: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
        authorization_scope: str = "scenario",
    ) -> CampaignProposal:
        if authorization_scope not in ("scenario", "scope_session"):
            raise CampaignProposalError("authorization_scope must be scenario or scope_session")
        if not 1 <= max_iterations <= 64:
            raise CampaignProposalError("max_iterations must be 1..64")
        if not 1 <= max_auto_approvals <= 256:
            raise CampaignProposalError("max_auto_approvals must be 1..256")
        if not 1 <= duration_hours <= 72:
            raise CampaignProposalError("duration_hours must be 1..72")
        job = self.ledger.job(job_id)
        if job.gate != "__mission__":
            raise CampaignProposalError(f"{job_id} is not a mission")
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        if len(briefs) != 1:
            raise CampaignProposalError("mission needs exactly one durable Brief")
        brief = briefs[0].payload
        resources = brief.get("resources") if isinstance(brief, dict) else None
        if not isinstance(resources, dict) or resources.get("execution_mode") != "hardware":
            raise CampaignProposalError("one-click campaign approval is only for hardware missions")
        simple = resources.get("workflow") == "simple-mission"
        if simple:
            if job.state not in ("pending", "running", "awaiting_approval", "blocked"):
                raise CampaignProposalError(
                    f"{job_id} is not waiting for a three-agent hardware campaign"
                )
        elif job.state != "done" or job.outcome != "contract_gap":
            raise CampaignProposalError(
                f"{job_id} is not a completed mission awaiting execution"
            )
        scenario_digest = str(resources.get("scenario_sha256") or "")
        scenario = str(brief.get("scenario") or "").strip()
        actual_scenario_digest = hashlib.sha256(scenario.encode("utf-8")).hexdigest()
        if scenario_digest != actual_scenario_digest:
            raise CampaignProposalError(
                "mission scenario digest differs from its durable Brief"
            )
        contract, digest = self._contract(job_id, resources)
        contract_path = self.contracts_dir / contract
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise CampaignProposalError("installed contract is not an object")

        blockers: list[str] = []
        if simple:
            plans = self.ledger.handoffs(job_id, "Plan", job.iteration)
            steps = plans[-1].payload.get("steps") if plans else None
        else:
            orchestration = data.get("orchestration")
            steps = orchestration.get("steps") if isinstance(orchestration, dict) else None
        if not isinstance(steps, list):
            blockers.append(
                "Lead has not produced a machine-readable hardware plan yet"
                if simple
                else "contract has no machine-readable orchestration steps"
            )
            steps = []
        permissions: list[dict[str, Any]] = []
        boundaries: dict[tuple[str, str], dict[str, Any]] = {}
        for step in steps:
            if not isinstance(step, dict) or step.get("touches_hardware") is not True:
                continue
            name = step.get("contract") or contract
            expected = step.get("contract_sha256") or (digest if name == contract else None)
            try:
                boundary_data, boundary_digest = self._load_boundary(name, expected)
            except CampaignProposalError as exc:
                blockers.append(f"{step.get('id')}: {exc}")
                continue
            local: list[str] = []
            profile, board = None, None
            hardware_safety = dict(boundary_data.get("hardware_safety") or {})
            tool = step.get("tool")
            permission = None
            try:
                if tool == "measure_scope":
                    from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate
                    gate = SafetyGate(Contracts.load(name, self.contracts_dir, project_root=self.project_root), allow_hardware=True)
                    hardware_safety = gate.instrument_scope()
                    permission = self._instrument_permission(name, boundary_digest, step, max_auto_approvals)
                elif tool in NATIVE_OPERATIONS:
                    permission = self._native_permission(name, boundary_digest, step, local, max_auto_approvals)
                    if permission.get("device"):
                        hardware_safety = {**hardware_safety, "devices": [permission["device"]]}
                else:
                    # Profile-backed tools consume stages; native tools do not.
                    hardware_safety = self._hardware_safety(boundary_data, local)
                    profile, board = self._profile(boundary_data, local)
                    if tool == "measure_board_scope" and not hardware_safety.get("scope_channels"):
                        local.append("hardware_safety has no reviewed scope channel mapping")
                    permission = self._permission(name, boundary_digest, step, steps, profile, local, max_auto_approvals)
            except (OSError, ValueError, RuntimeError) as exc:
                local.append(str(exc))
            blockers.extend(f"{name}: {problem}" for problem in local)
            if any(b["contract"] == name and b["contract_sha256"] != boundary_digest for b in boundaries.values()):
                blockers.append(f"{name}: conflicting contract bindings")
            # One shared document can describe several device effects. Do not
            # let the last step overwrite the scope/BBB safety shown for review.
            boundaries[(name, tool)] = {"contract": name, "contract_sha256": boundary_digest,
                                      "tool": tool, "board": board, "hardware_safety": hardware_safety}
            if permission is not None:
                # One permission per device/role/operation. Step IDs may change
                # when Lead replans; they are not the hardware authority.
                identity = (name, permission["role"], permission["tool"])
                if not any((p["contract"], p["role"], p["tool"]) == identity for p in permissions):
                    permissions.append(permission)
        if not permissions:
            blockers.append("contract exposes no supported hardware permission")
        if authorization_scope == "scope_session" and any(
            p["tool"] != "measure_scope" or p["artifact_mode"] != "pinned" for p in permissions
        ):
            blockers.append("새 작업에서 재사용하는 승인은 스코프 단독 측정 범위만 지원합니다.")

        blockers = list(dict.fromkeys(blockers))
        first = next(iter(boundaries.values()), {})
        board, hardware_safety = first.get("board"), first.get("hardware_safety", {})
        review = {
            "job_id": job_id,
            "contract": contract,
            "contract_sha256": digest,
            "scenario_sha256": scenario_digest,
            "board": board,
            "hardware_safety": hardware_safety,
            "permissions": permissions,
            "boundaries": list(boundaries.values()),
            "blockers": blockers,
            "max_iterations": max_iterations,
            "max_auto_approvals": max_auto_approvals,
            "duration_hours": duration_hours,
            "authorization_scope": authorization_scope,
        }
        review_sha = hashlib.sha256(
            yaml.safe_dump(review, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CampaignProposal(
            job_id,
            contract,
            digest,
            scenario_digest,
            board,
            hardware_safety,
            tuple(permissions),
            tuple(blockers),
            max_iterations,
            max_auto_approvals,
            duration_hours,
            review_sha,
            tuple(boundaries.values()),
            authorization_scope,
        )

    def _load_boundary(self, name: str, expected: str | None) -> tuple[dict[str, Any], str]:
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".yaml"):
            raise CampaignProposalError("hardware boundary must be a plain YAML filename")
        path = (self.contracts_dir / name).resolve()
        if path.parent != self.contracts_dir or not path.is_file():
            raise CampaignProposalError(f"missing hardware boundary {name}")
        digest = sha256_file(path)
        if digest != expected:
            raise CampaignProposalError(f"hardware boundary {name} is unbound or changed; replan before review")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise CampaignProposalError("hardware boundary must be an object")
        return data, digest

    def _native_permission(self, contract, digest, step, blockers, maximum, *, prepared_bound=None):
        from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate
        bound = prepared_bound or Contracts.load(contract, self.contracts_dir, project_root=self.project_root)
        gate = SafetyGate(bound, allow_hardware=True)
        tool = step["tool"]
        operations = NATIVE_OPERATIONS[tool]
        if tool == "run_bbb" and (
            (step.get("tool_args") or {}).get("identity_only") is True
            or set(bound.test_spec.get("allowed_operations") or []) == {"bbb_identify"}
        ):
            operations = ("bbb_identify",)
        for operation in operations:
            if operation not in (bound.test_spec.get("allowed_operations") or []):
                blockers.append(f"{tool}: explicitly allow {operation} in the device boundary")
            gate.require(operation)
        device = {}
        if tool in {"run_bbb", "deploy_pru"}:
            from ai_hardware_autolab.hwtools.ssh import bbb_endpoint, validate_bbb_endpoint
            # The map is already digest-bound. Reusing its endpoint does not
            # require Lead to copy the same fields into every new contract.
            device = (bound.test_spec.get("bbb") or {}).get("target")
            if device is None:
                device = bbb_endpoint(bound.board_map, env={})
            if (not isinstance(device, dict) or not isinstance(device.get("host"), str) or not device["host"].strip()
                    or device["host"].upper() in {"UNVERIFIED", "UNKNOWN", "TBD"}
                    or not isinstance(device.get("user"), str) or not device["user"].strip()
                    or device["user"].upper() in {"UNVERIFIED", "UNKNOWN", "TBD"}
                    or type(device.get("port")) is not int or not 1 <= device["port"] <= 65535):
                blockers.append("BBB access needs its actual bbb.target host/user/port, not an unrelated BoardProfile")
                device = {}
            else:
                # Explicit targets still constrain the actual session. For a
                # legacy target-only contract, its execution check remains in MCP.
                actual = bbb_endpoint(bound.board_map)
                if actual["host"] is not None and actual["user"] is not None:
                    validate_bbb_endpoint(actual, device)
                device = {"model": "BBB SSH", **{key: device[key] for key in ("host", "user", "port")}}
        artifacts = {
            bound.test_spec_path.resolve().relative_to(self.project_root).as_posix(): digest,
            bound.board_map_path.resolve().relative_to(self.project_root).as_posix(): sha256_file(bound.board_map_path),
        }
        if tool == "program_cpld_sram":
            from ai_hardware_autolab.hwtools.sram import program_input_artifacts
            xcf_name = (step.get("tool_args") or {}).get("program_xcf")
            if not isinstance(xcf_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.xcf", xcf_name):
                raise ValueError("program_cpld_sram needs a concrete program_xcf filename before approval")
            artifacts.update(program_input_artifacts(gate, self.contracts_dir / xcf_name))
        # Native drivers check their own connection/pins/image policy again at
        # use time. No unrelated profile stage is an approval prerequisite.
        return {"id": f"device-{digest[:12]}-{tool}", "contract": contract, "contract_sha256": digest,
                "role": step.get("agent"), "tool": tool, "max_approvals": maximum,
                "max_executions": 1, "phase": "device", "stage_id": None,
                "artifact_mode": "pinned", "artifacts": artifacts, "artifact_kinds": [], "producer_tools": [], "device": device}

    def _instrument_permission(self, contract: str, digest: str, step: dict[str, Any], maximum: int) -> dict[str, Any]:
        from ai_hardware_autolab.hwtools.safety import Contracts
        bound = Contracts.load(contract, self.contracts_dir, project_root=self.project_root)
        # Pin the actual connection settings as well as the approved safety
        # document. A later board-map edit must invalidate the authorization.
        artifacts = {}
        for path in (bound.test_spec_path, bound.board_map_path):
            reference = path.resolve().relative_to(self.project_root).as_posix()
            artifacts[reference] = sha256_file(path)
        return {"id": f"{step['id']}-measure_scope", "contract": contract,
                "contract_sha256": digest, "role": step.get("agent"), "tool": "measure_scope",
                "max_approvals": maximum, "max_executions": 1, "phase": "measure", "stage_id": None,
                "artifact_mode": "pinned", "artifacts": artifacts, "artifact_kinds": [], "producer_tools": []}

    @staticmethod
    def _hardware_safety(
        contract: dict[str, Any], blockers: list[str]
    ) -> dict[str, Any]:
        """Return the electrical envelope a person is actually reviewing.

        A contract digest does bind these bytes, but a digest is not a useful
        review surface.  One-click unattended hardware execution therefore
        requires the structured envelope and carries it in the review digest.
        """
        safety = contract.get("hardware_safety")
        if not isinstance(safety, dict):
            blockers.append(
                "contract has no structured hardware_safety envelope; amend it before unattended execution"
            )
            return {}
        limits = safety.get("limits")
        pins = safety.get("pin_assignments")
        stops = safety.get("stop_conditions")
        if not isinstance(limits, list) or not limits:
            blockers.append("hardware_safety has no electrical limits")
        else:
            parameters = {
                str(item.get("parameter") or "").lower()
                for item in limits
                if isinstance(item, dict)
            }
            if not {"voltage", "current"} <= parameters:
                blockers.append("hardware_safety must include voltage and current limits")
        if not isinstance(pins, list) or not pins:
            blockers.append("hardware_safety has no reviewed pin assignments")
        if not isinstance(stops, list) or not stops:
            blockers.append("hardware_safety has no stop conditions")
        return dict(safety)

    def approve(
        self,
        job_id: str,
        approved_by: str,
        expected_review_sha256: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
        authorization_scope: str = "scenario",
    ) -> dict[str, Any]:
        actor = approved_by.strip()
        if not actor:
            raise CampaignProposalError("approval must name who reviewed the scope")
        proposal = self.preview(
            job_id,
            max_iterations=max_iterations,
            max_auto_approvals=max_auto_approvals,
            duration_hours=duration_hours,
            authorization_scope=authorization_scope,
        )
        if proposal.review_sha256 != expected_review_sha256:
            raise CampaignProposalError(
                "campaign review changed after it was displayed; refresh and review it again"
            )
        if not proposal.ready:
            raise CampaignProposalError("campaign has blockers: " + "; ".join(proposal.blockers))
        if not self.controller.hardware_enabled:
            raise CampaignProposalError(
                "campaign approval requires the hardware daemon profile"
            )
        expires = self._expires(proposal.duration_hours)
        campaign_id = "mission-" + re.sub(r"[^a-zA-Z0-9_-]", "-", job_id)[-32:]
        permissions = [self._policy_permission(item) for item in proposal.permissions]
        policy = {
            "schema_version": 2,
            "campaign_id": campaign_id,
            "approved_by": actor,
            "expires_at": expires,
            "scenario_sha256": proposal.scenario_sha256,
            "max_auto_approvals": proposal.max_auto_approvals,
            "max_iterations": proposal.max_iterations,
            "permissions": permissions,
            "authorization_scope": proposal.authorization_scope,
        }
        content = yaml.safe_dump(policy, sort_keys=False).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        filename = f"{campaign_id}-{proposal.review_sha256[:10]}.yaml"
        target = self.campaigns_dir / filename
        self._install_create_only(target, content)
        activated = self.controller.activate(job_id, filename, digest, actor)
        return {
            "proposal": proposal.describe(),
            "filename": filename,
            "sha256": digest,
            "campaign": activated,
        }

    def _contract(self, job_id: str, resources: dict[str, Any]) -> tuple[str, str]:
        promoted = last_event(
            self.ledger,
            job_id,
            lambda event: event.kind == "contract.promoted",
        )
        source = promoted.payload if promoted is not None else resources.get("selected_contract")
        plans = self.ledger.handoffs(job_id, "Plan", self.ledger.job(job_id).iteration)
        current = plans[-1].payload.get("compiled_contract") if plans else None
        if isinstance(current, dict):
            source = current
        if not isinstance(source, dict):
            raise CampaignProposalError("mission has no installed or selected contract")
        name = source.get("contract") or source.get("name")
        digest = source.get("contract_sha256") or source.get("sha256")
        if not isinstance(name, str) or Path(name).name != name:
            raise CampaignProposalError("mission contract is not a plain filename")
        path = (self.contracts_dir / name).resolve()
        if path.parent != self.contracts_dir or not path.is_file():
            raise CampaignProposalError(f"mission contract is missing: {name}")
        actual = sha256_file(path)
        if actual != digest:
            raise CampaignProposalError(
                f"mission contract drifted: expected {digest}, found {actual}"
            )
        return name, actual

    def _profile(
        self, contract: dict[str, Any], blockers: list[str]
    ) -> tuple[BoardProfile | None, dict[str, Any] | None]:
        binding = contract.get("board_profile")
        if not isinstance(binding, dict):
            blockers.append("contract has no digest-bound board profile")
            return None, None
        reference = binding.get("path")
        expected = binding.get("sha256")
        if not isinstance(reference, str) or not isinstance(expected, str):
            blockers.append("board profile binding is incomplete")
            return None, None
        path = (self.project_root / reference).resolve()
        try:
            profile = BoardProfile.load(path, project_root=self.project_root)
        except BoardProfileError as exc:
            blockers.append(str(exc))
            return None, None
        if profile.sha256 != expected:
            blockers.append("board profile changed after contract review")
        return profile, {
            "board_id": profile.board_id,
            "revision": profile.revision,
            "title": profile.title,
            "profile": reference,
            "profile_sha256": profile.sha256,
        }

    def _permission(
        self,
        contract: str,
        digest: str,
        step: dict[str, Any],
        all_steps: list[Any],
        profile: BoardProfile | None,
        blockers: list[str],
        max_approvals: int,
    ) -> dict[str, Any] | None:
        step_id = str(step.get("id") or "")
        role = str(step.get("agent") or "")
        tool = str(step.get("tool") or "")
        args = step.get("tool_args")
        stage_id = args.get("stage_id") if isinstance(args, dict) else None
        stage = next(
            (item for item in profile.stages if item.stage_id == stage_id),
            None,
        ) if profile is not None and isinstance(stage_id, str) else None
        if profile is not None and stage is None and not isinstance(stage_id, str):
            compatible = [
                item
                for item in profile.stages
                if item.status == "ready"
                and item.tool == tool
                and (
                    item.agent == role
                    or (
                        role == "builder"
                        and item.agent in {
                            "design-builder", "firmware-builder",
                            "board-operator", "measurement",
                        }
                    )
                )
            ]
            if len(compatible) == 1:
                stage = compatible[0]
                stage_id = stage.stage_id
        if profile is None:
            blockers.append(f"{step_id}: no reviewed board profile is available")
            return None
        if stage is None:
            blockers.append(f"{step_id}: board profile has no stage {stage_id!r}")
            return None
        if stage.status != "ready":
            blockers.append(f"{step_id}: {stage.phase} still requires a board adapter")
            return None
        builder_stage_agents = {
            "design-builder",
            "firmware-builder",
            "board-operator",
            "measurement",
        }
        role_matches = stage.agent == role or (
            role == "builder" and stage.agent in builder_stage_agents
        )
        if stage.tool != tool or not role_matches:
            blockers.append(
                f"{step_id}: contract agent/tool differs from the reviewed board stage"
            )
            return None
        base = {
            "id": f"{step_id}-{tool}",
            "contract": contract,
            "contract_sha256": digest,
            "role": role,
            "tool": tool,
            "max_approvals": max_approvals,
            "max_executions": 1,
            "stage_id": stage_id,
            "phase": stage.phase if stage is not None else None,
        }
        if stage is not None and stage.phase == "upload":
            producers: list[str] = []
            for candidate in profile.stages:
                if candidate.status != "ready" or candidate.phase not in ("design", "build"):
                    continue
                if candidate.tool == "build_cpld":
                    producers.append("cpld.build")
                elif candidate.tool == "run_board_build":
                    producers.append(f"board_adapter.{candidate.stage_id}")
            producers.extend(
                f"codex_workspace.{candidate.get('id')}"
                for candidate in all_steps
                if isinstance(candidate, dict)
                and candidate.get("tool") == "codex_workspace"
                and isinstance(candidate.get("id"), str)
            )
            if not producers:
                blockers.append(f"{step_id}: upload has no reviewed build producer")
                return None
            return {
                **base,
                "artifact_mode": "run_manifest",
                "artifacts": {},
                "artifact_kinds": [
                    "board_build", "board_design", "workspace_output",
                    "bitstream", "pad_report",
                ],
                "producer_tools": sorted(set(producers)),
            }
        if stage is not None and stage.phase in ("upload", "measure"):
            reference = profile.path.relative_to(self.project_root).as_posix()
            return {
                **base,
                "artifact_mode": "pinned",
                "artifacts": {reference: profile.sha256},
                "artifact_kinds": [],
                "producer_tools": [],
            }
        blockers.append(f"{step_id}: automatic campaign policy does not support {tool!r}")
        return None

    @staticmethod
    def _policy_permission(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value[key]
            for key in (
                "id", "contract", "contract_sha256", "role", "tool", "artifacts",
                "artifact_mode", "artifact_kinds", "producer_tools",
                "max_approvals", "max_executions",
            )
        }

    def _expires(self, hours: int) -> str:
        raw = self.now()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (parsed + timedelta(hours=hours)).isoformat()

    @staticmethod
    def _install_create_only(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_file() and target.read_bytes() == content:
                return
            raise CampaignProposalError(f"refusing to overwrite campaign {target.name}")
        descriptor, temporary = tempfile.mkstemp(prefix=".campaign-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)
        except FileExistsError as exc:
            raise CampaignProposalError(f"campaign appeared concurrently: {target.name}") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
