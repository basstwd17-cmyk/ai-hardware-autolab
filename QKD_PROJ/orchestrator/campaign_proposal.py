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

from QKD_PROJ.boards.profiles import BoardProfile, BoardProfileError
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator.campaign import CampaignController
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.pagination import last_event


class CampaignProposalError(RuntimeError):
    pass


@dataclass(frozen=True)
class CampaignProposal:
    job_id: str
    contract: str
    contract_sha256: str
    scenario_sha256: str
    board: dict[str, Any] | None
    permissions: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]
    max_iterations: int
    max_auto_approvals: int
    duration_hours: int
    review_sha256: str

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
            "permissions": [dict(item) for item in self.permissions],
            "blockers": list(self.blockers),
            "ready": self.ready,
            "max_iterations": self.max_iterations,
            "max_auto_approvals": self.max_auto_approvals,
            "duration_hours": self.duration_hours,
            "review_sha256": self.review_sha256,
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

    def preview(
        self,
        job_id: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
    ) -> CampaignProposal:
        if not 1 <= max_iterations <= 64:
            raise CampaignProposalError("max_iterations must be 1..64")
        if not 1 <= max_auto_approvals <= 256:
            raise CampaignProposalError("max_auto_approvals must be 1..256")
        if not 1 <= duration_hours <= 72:
            raise CampaignProposalError("duration_hours must be 1..72")
        job = self.ledger.job(job_id)
        if job.gate != "__mission__" or job.state != "done" or job.outcome != "contract_gap":
            raise CampaignProposalError(
                f"{job_id} is not a completed mission awaiting execution"
            )
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        if len(briefs) != 1:
            raise CampaignProposalError("mission needs exactly one durable Brief")
        brief = briefs[0].payload
        resources = brief.get("resources") if isinstance(brief, dict) else None
        if not isinstance(resources, dict) or resources.get("execution_mode") != "hardware":
            raise CampaignProposalError("one-click campaign approval is only for hardware missions")
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
        profile, board = self._profile(data, blockers)
        orchestration = data.get("orchestration")
        steps = orchestration.get("steps") if isinstance(orchestration, dict) else None
        if not isinstance(steps, list):
            blockers.append("contract has no machine-readable orchestration steps")
            steps = []
        permissions: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("touches_hardware") is not True:
                continue
            permission = self._permission(
                contract, digest, step, profile, blockers, max_auto_approvals
            )
            if permission is not None:
                permissions.append(permission)
        if not permissions:
            blockers.append("contract exposes no supported hardware permission")

        blockers = list(dict.fromkeys(blockers))
        review = {
            "job_id": job_id,
            "contract": contract,
            "contract_sha256": digest,
            "scenario_sha256": scenario_digest,
            "board": board,
            "permissions": permissions,
            "blockers": blockers,
            "max_iterations": max_iterations,
            "max_auto_approvals": max_auto_approvals,
            "duration_hours": duration_hours,
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
            tuple(permissions),
            tuple(blockers),
            max_iterations,
            max_auto_approvals,
            duration_hours,
            review_sha,
        )

    def approve(
        self,
        job_id: str,
        approved_by: str,
        expected_review_sha256: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
    ) -> dict[str, Any]:
        actor = approved_by.strip()
        if not actor:
            raise CampaignProposalError("approval must name who reviewed the scope")
        proposal = self.preview(
            job_id,
            max_iterations=max_iterations,
            max_auto_approvals=max_auto_approvals,
            duration_hours=duration_hours,
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
        if profile is None:
            blockers.append(f"{step_id}: no reviewed board profile is available")
            return None
        if stage is None:
            blockers.append(f"{step_id}: board profile has no stage {stage_id!r}")
            return None
        if stage.status != "ready":
            blockers.append(f"{step_id}: {stage.phase} still requires a board adapter")
            return None
        if stage.tool != tool or stage.agent != role:
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
        if tool == "run_board_stage" and stage is not None and stage.phase == "upload":
            producers = [
                f"board_adapter.{candidate.stage_id}"
                for candidate in profile.stages
                if candidate.status == "ready" and candidate.phase in ("design", "build")
            ]
            if not producers:
                blockers.append(f"{step_id}: upload has no reviewed build producer")
                return None
            return {
                **base,
                "artifact_mode": "run_manifest",
                "artifacts": {},
                "artifact_kinds": ["board_build", "board_design"],
                "producer_tools": producers,
            }
        if tool in ("run_board_stage", "measure_board_scope") and stage is not None:
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
