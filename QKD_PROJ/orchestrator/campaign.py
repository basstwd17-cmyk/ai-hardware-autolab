"""Deterministic, pre-authorised campaigns for bounded unattended hardware work.

An agent never grants itself authority here.  A person reviews a campaign
file, launches the daemon with that file's exact SHA-256, and the evaluator
may then decide individual ledger approval requests only while every
machine-checkable bound still matches.

The campaign is intentionally narrower than a free-form safety policy.  This
first layer enforces facts the platform can prove without touching the board:

* exact scenario and contract digests;
* an exact role, MCP tool, and artifact set;
* campaign, permission, iteration, and per-approval execution ceilings; and
* an absolute expiry time.

Voltage, current, pin direction, and scope limits belong in the signed
contract and in later deterministic device interlocks.  Text in this file is
not treated as an enforceable electrical limit merely because it sounds like
one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from QKD_PROJ.agents.schema import AgentDefinition
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.mcp_server.registry import CONTRACT_NAME, ROLE_TOOLS
from QKD_PROJ.orchestrator.jobs import Approval, Job, JobLedger
from QKD_PROJ.orchestrator.pagination import iter_events

SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
TOP_LEVEL_KEYS = {
    "schema_version",
    "campaign_id",
    "approved_by",
    "expires_at",
    "scenario_sha256",
    "max_auto_approvals",
    "max_iterations",
    "permissions",
}
PERMISSION_KEYS = {
    "id",
    "contract",
    "contract_sha256",
    "role",
    "tool",
    "artifacts",
    "max_approvals",
    "max_executions",
    "artifact_mode",
    "artifact_kinds",
    "producer_tools",
}


class CampaignPolicyError(RuntimeError):
    """A campaign is invalid, drifted, or cannot answer safely."""


@dataclass(frozen=True)
class CampaignPermission:
    permission_id: str
    contract: str
    contract_sha256: str
    role: str
    tool: str
    artifacts: dict[str, str]
    max_approvals: int
    max_executions: int
    artifact_mode: str = "pinned"
    artifact_kinds: tuple[str, ...] = ()
    producer_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoApprovalEvaluation:
    allowed: bool
    reason: str
    campaign_id: str
    campaign_sha256: str
    permission_id: str | None = None
    artifacts: dict[str, str] | None = None
    max_executions: int = 1
    expires_at: str | None = None
    artifact_mode: str = "pinned"
    lineage_runs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignPolicy:
    schema_version: int
    campaign_id: str
    approved_by: str
    expires_at: str
    scenario_sha256: str
    max_auto_approvals: int
    max_iterations: int
    permissions: tuple[CampaignPermission, ...]
    sha256: str
    contracts_dir: Path
    project_root: Path

    @classmethod
    def load(
        cls,
        path: Path,
        expected_sha256: str,
        *,
        contracts_dir: Path,
        project_root: Path,
    ) -> "CampaignPolicy":
        path = Path(path)
        contracts_dir = Path(contracts_dir).resolve()
        project_root = Path(project_root).resolve()
        if path.suffix.lower() not in (".yaml", ".yml"):
            raise CampaignPolicyError("campaign must be a YAML file")
        if not path.is_file():
            raise CampaignPolicyError(f"campaign does not exist: {path}")
        if not SHA256.fullmatch(str(expected_sha256)):
            raise CampaignPolicyError("campaign_sha256 must be a full SHA-256 digest")
        actual = sha256_file(path)
        if actual.lower() != expected_sha256.lower():
            raise CampaignPolicyError(
                f"campaign changed after approval: expected {expected_sha256}, found {actual}"
            )
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CampaignPolicyError(f"campaign is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise CampaignPolicyError("campaign root must be an object")
        unknown = set(data) - TOP_LEVEL_KEYS
        if unknown:
            raise CampaignPolicyError(f"unknown campaign fields: {', '.join(sorted(unknown))}")
        schema_version = data.get("schema_version")
        if schema_version not in (1, 2):
            raise CampaignPolicyError("campaign schema_version must be 1 or 2")

        campaign_id = _plain(data.get("campaign_id"), "campaign_id")
        approved_by = _plain(data.get("approved_by"), "approved_by")
        expires_at = _timestamp(data.get("expires_at"), "expires_at")
        scenario_sha256 = _digest(data.get("scenario_sha256"), "scenario_sha256")
        max_auto_approvals = _positive(data.get("max_auto_approvals"), "max_auto_approvals")
        max_iterations = _positive(data.get("max_iterations"), "max_iterations")

        raw_permissions = data.get("permissions")
        if not isinstance(raw_permissions, list) or not raw_permissions:
            raise CampaignPolicyError("campaign permissions must be a non-empty list")
        permissions = tuple(
            _permission(raw, contracts_dir, project_root, schema_version)
            for raw in raw_permissions
        )
        ids = [permission.permission_id for permission in permissions]
        if len(ids) != len(set(ids)):
            raise CampaignPolicyError("campaign permission ids must be unique")
        selectors = [(p.contract, p.role, p.tool) for p in permissions]
        if len(selectors) != len(set(selectors)):
            raise CampaignPolicyError(
                "campaign may not define two permissions for the same contract, role, and tool"
            )
        return cls(
            schema_version=schema_version,
            campaign_id=campaign_id,
            approved_by=approved_by,
            expires_at=expires_at,
            scenario_sha256=scenario_sha256.lower(),
            max_auto_approvals=max_auto_approvals,
            max_iterations=max_iterations,
            permissions=permissions,
            sha256=actual,
            contracts_dir=contracts_dir,
            project_root=project_root,
        )

    def permission_for(
        self, contract: str, role: str, tool: str
    ) -> CampaignPermission | None:
        return next(
            (
                permission
                for permission in self.permissions
                if (permission.contract, permission.role, permission.tool)
                == (contract, role, tool)
            ),
            None,
        )


class CampaignAutoApprover:
    """Evaluate and record one approval request against an active campaign."""

    def __init__(
        self,
        ledger: JobLedger,
        policy: CampaignPolicy,
        *,
        now: Callable[[], str] | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self.ledger = ledger
        self.policy = policy
        self.now = now or _utc_now
        self.runs_dir = Path(runs_dir or (policy.project_root / "QKD_PROJ" / "runs")).resolve()

    def evaluate(
        self, job: Job, step: dict[str, Any], agent: AgentDefinition
    ) -> AutoApprovalEvaluation:
        prefix = {
            "campaign_id": self.policy.campaign_id,
            "campaign_sha256": self.policy.sha256,
            "expires_at": self.policy.expires_at,
        }

        def refuse(reason: str) -> AutoApprovalEvaluation:
            return AutoApprovalEvaluation(False, reason, **prefix)

        if _parse_time(self.now()) >= _parse_time(self.policy.expires_at):
            return refuse(f"campaign expired at {self.policy.expires_at}")
        if job.iteration > self.policy.max_iterations:
            return refuse(
                f"iteration {job.iteration} exceeds campaign maximum {self.policy.max_iterations}"
            )
        scenario = self._scenario(job.job_id)
        actual_scenario = hashlib.sha256(scenario.strip().encode("utf-8")).hexdigest()
        if actual_scenario != self.policy.scenario_sha256:
            return refuse(
                f"job scenario hashes to {actual_scenario}, not the approved scenario"
            )

        contract = str(step.get("contract") or job.gate)
        tool = str(step.get("tool") or "")
        permission = self.policy.permission_for(contract, agent.name, tool)
        if permission is None:
            return refuse(
                f"no campaign permission for contract={contract!r}, role={agent.name!r}, "
                f"tool={tool!r}"
            )
        if not agent.touches_hardware:
            return refuse(f"role {agent.name!r} is not declared hardware-touching")
        if tool not in agent.mcp_tools or tool not in ROLE_TOOLS.get(agent.name, frozenset()):
            return refuse(f"tool {tool!r} is outside role {agent.name!r}")

        contract_path = self.policy.contracts_dir / contract
        if not contract_path.is_file():
            return refuse(f"approved contract is missing: {contract}")
        actual_contract = sha256_file(contract_path)
        if actual_contract != permission.contract_sha256:
            return refuse(
                f"contract {contract} drifted: expected {permission.contract_sha256}, "
                f"found {actual_contract}"
            )

        requested_artifacts = step.get("artifacts") or {}
        if not isinstance(requested_artifacts, dict):
            return refuse("step artifacts must be an object")
        approved_artifacts: dict[str, str]
        lineage_runs: tuple[str, ...] = ()
        if permission.artifact_mode == "pinned":
            if requested_artifacts and requested_artifacts != permission.artifacts:
                return refuse("step artifacts differ from the campaign's exact artifact set")
            for reference, digest in permission.artifacts.items():
                problem = _artifact_problem(self.policy.project_root, reference, digest)
                if problem:
                    return refuse(f"campaign artifact {reference} {problem}")
            approved_artifacts = dict(permission.artifacts)
        else:
            if not requested_artifacts:
                return refuse("a run-manifest permission requires this iteration's artifacts")
            normalized: dict[str, str] = {}
            for reference, digest in requested_artifacts.items():
                if not isinstance(reference, str) or not isinstance(digest, str):
                    return refuse("step artifacts must map paths to SHA-256 strings")
                if not SHA256.fullmatch(digest):
                    return refuse(f"step artifact {reference!r} has no full SHA-256 digest")
                normalized[reference] = digest.lower()
            problem, lineage_runs = self._derived_artifact_problem(permission, normalized)
            if problem:
                return refuse(problem)
            approved_artifacts = normalized

        total, per_permission = self._grant_counts(permission.permission_id)
        if total >= self.policy.max_auto_approvals:
            return refuse(
                f"campaign used all {self.policy.max_auto_approvals} automatic approvals"
            )
        if per_permission >= permission.max_approvals:
            return refuse(
                f"permission {permission.permission_id} used all "
                f"{permission.max_approvals} automatic approvals"
            )
        return AutoApprovalEvaluation(
            True,
            "all signed campaign bounds match",
            permission_id=permission.permission_id,
            artifacts=approved_artifacts,
            max_executions=permission.max_executions,
            artifact_mode=permission.artifact_mode,
            lineage_runs=lineage_runs,
            **prefix,
        )

    def apply(
        self,
        approval: Approval,
        evaluation: AutoApprovalEvaluation,
    ) -> Approval:
        actor = f"policy:{self.policy.campaign_id}"
        self.ledger.append_event(
            approval.job_id,
            "campaign.approval.evaluated",
            {
                "campaign_id": self.policy.campaign_id,
                "campaign_sha256": self.policy.sha256,
                "permission_id": evaluation.permission_id,
                "approval_id": approval.approval_id,
                "allowed": evaluation.allowed,
                "reason": evaluation.reason,
                "artifact_mode": evaluation.artifact_mode,
                "artifacts": dict(evaluation.artifacts or {}),
                "lineage_runs": list(evaluation.lineage_runs),
            },
            actor,
        )
        if not evaluation.allowed:
            return approval
        return self.ledger.decide_approval(
            approval.approval_id,
            True,
            actor,
            note=(
                f"pre-authorised campaign {self.policy.campaign_id}, reviewed by "
                f"{self.policy.approved_by}: {evaluation.reason}"
            ),
            bundle_sha256=self.policy.sha256,
            now=self.now(),
        )

    def _scenario(self, job_id: str) -> str:
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        if not briefs:
            return ""
        return str(briefs[-1].payload.get("scenario") or "")

    def _derived_artifact_problem(
        self,
        permission: CampaignPermission,
        artifacts: dict[str, str],
    ) -> tuple[str | None, tuple[str, ...]]:
        """Admit a changing artifact only through a PASS run manifest.

        The campaign does not approve arbitrary future bytes.  It approves a
        producer and artifact kind under an immutable contract.  Every actual
        byte sequence is then pinned into its own ledger approval before the
        hardware tool sees it.
        """
        runs: set[str] = set()
        for reference, expected in artifacts.items():
            path = _resolve(self.policy.project_root, reference)
            if path is None or not path.is_file():
                return (f"derived artifact {reference} is not a file inside the project", ())
            try:
                relative = path.relative_to(self.runs_dir)
            except ValueError:
                return (f"derived artifact {reference} is outside the immutable runs directory", ())
            if len(relative.parts) < 2:
                return (f"derived artifact {reference} names no run directory", ())
            run_name = relative.parts[0]
            run_dir = self.runs_dir / run_name
            manifest_path = run_dir / "manifest.json"
            if not manifest_path.is_file():
                return (f"derived artifact {reference} has no run manifest", ())
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return (f"derived artifact {reference} has an unreadable run manifest: {exc}", ())
            if not isinstance(manifest, dict) or manifest.get("verdict") != "PASS":
                return (f"derived artifact {reference} did not come from a PASS run", ())
            contract = manifest.get("contract")
            if not isinstance(contract, dict) or contract.get("test_spec") != permission.contract:
                return (
                    f"derived artifact {reference} was not built under {permission.contract}",
                    (),
                )
            producer_ok = False
            for step in manifest.get("steps") or []:
                if not isinstance(step, dict) or step.get("verdict") != "PASS":
                    continue
                selector = f"{step.get('tool', '')}.{step.get('action', '')}"
                if selector in permission.producer_tools:
                    producer_ok = True
                    break
            if not producer_ok:
                return (
                    f"derived artifact {reference} has no approved PASS producer "
                    f"{list(permission.producer_tools)}",
                    (),
                )
            artifact_path = path.relative_to(run_dir).as_posix()
            manifest_artifact = next(
                (
                    row
                    for row in manifest.get("artifacts") or []
                    if isinstance(row, dict) and row.get("path") == artifact_path
                ),
                None,
            )
            if manifest_artifact is None:
                return (f"derived artifact {reference} is absent from its run manifest", ())
            kind = str(manifest_artifact.get("kind") or "")
            if kind not in permission.artifact_kinds:
                return (
                    f"derived artifact {reference} has kind {kind!r}, outside "
                    f"{list(permission.artifact_kinds)}",
                    (),
                )
            manifest_digest = str(manifest_artifact.get("sha256") or "").lower()
            if manifest_digest != expected:
                return (f"derived artifact {reference} digest differs from its run manifest", ())
            actual = sha256_file(path)
            if actual != expected:
                return (f"derived artifact {reference} now hashes to {actual} instead of {expected}", ())
            runs.add(run_name)
        return None, tuple(sorted(runs))

    def _grant_counts(self, permission_id: str) -> tuple[int, int]:
        total = 0
        selected = 0
        for event in iter_events(self.ledger):
            if event.kind != "campaign.approval.evaluated":
                continue
            payload = event.payload
            if payload.get("campaign_sha256") != self.policy.sha256 or not payload.get("allowed"):
                continue
            total += 1
            if payload.get("permission_id") == permission_id:
                selected += 1
        return total, selected


class CampaignController:
    """One digest-bound campaign that may run without repeated human clicks.

    Activation is a control-plane action tied to the scenario job.  The
    resulting event is sufficient to restore the same authority after a daemon
    restart; the YAML digest, contract digests and expiry are all rechecked.
    """

    def __init__(
        self,
        ledger: JobLedger,
        execution_loop: Any,
        hardware_gate: Any,
        *,
        campaigns_dir: Path,
        contracts_dir: Path,
        project_root: Path,
        runs_dir: Path,
        hardware_enabled: bool,
        router: Any | None = None,
        initial_policy: CampaignPolicy | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.ledger = ledger
        self.execution_loop = execution_loop
        self.hardware_gate = hardware_gate
        self.campaigns_dir = Path(campaigns_dir).resolve()
        self.contracts_dir = Path(contracts_dir).resolve()
        self.project_root = Path(project_root).resolve()
        self.runs_dir = Path(runs_dir).resolve()
        self.hardware_enabled = hardware_enabled
        self.router = router
        self.now = now or _utc_now
        self.active_policy: CampaignPolicy | None = None
        self.active_job_id: str | None = None
        self.active_filename: str | None = None
        self.activated_by: str | None = None
        self.activated_at: str | None = None
        if initial_policy is not None:
            self._install(initial_policy)

    def catalog(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.campaigns_dir.is_dir():
            return rows
        for path in sorted(self.campaigns_dir.glob("*.y*ml")):
            digest = sha256_file(path)
            try:
                policy = self._load(path.name, digest)
            except CampaignPolicyError as exc:
                rows.append({"filename": path.name, "sha256": digest, "valid": False, "error": str(exc)})
            else:
                rows.append({"filename": path.name, "sha256": digest, "valid": True, "campaign": self._policy_description(policy)})
        return rows

    def preview(self, filename: str, sha256: str) -> dict[str, Any]:
        return self._policy_description(self._load(filename, sha256))

    def activate(
        self,
        job_id: str,
        filename: str,
        sha256: str,
        approved_by: str,
    ) -> dict[str, Any]:
        if not self.hardware_enabled:
            raise CampaignPolicyError("campaign activation requires the hardware daemon profile")
        if not approved_by.strip():
            raise CampaignPolicyError("campaign activation must name who approved it")
        job = self.ledger.job(job_id)
        if job.state == "done" and not (
            job.gate == "__mission__" and job.outcome == "contract_gap"
        ):
            raise CampaignPolicyError(f"job {job_id} is already done")
        policy = self._load(filename, sha256)
        if _parse_time(self.now()) >= _parse_time(policy.expires_at):
            raise CampaignPolicyError(f"campaign expired at {policy.expires_at}")
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        scenario = str(briefs[-1].payload.get("scenario") or "") if briefs else ""
        actual = hashlib.sha256(scenario.strip().encode("utf-8")).hexdigest()
        if actual != policy.scenario_sha256:
            raise CampaignPolicyError(
                f"job scenario hashes to {actual}, not the campaign's {policy.scenario_sha256}"
            )
        if self.active_policy is not None and self.active_policy.sha256 != policy.sha256:
            raise CampaignPolicyError(
                f"campaign {self.active_policy.campaign_id} is already active; deactivate it first"
            )
        self._install(policy)
        self.active_job_id = job_id
        self.active_filename = filename
        self.activated_by = approved_by.strip()
        self.activated_at = self.now()
        self.ledger.append_event(
            job_id,
            "campaign.activated",
            {
                "filename": filename,
                "campaign_id": policy.campaign_id,
                "campaign_sha256": policy.sha256,
                "approved_by": self.activated_by,
                "expires_at": policy.expires_at,
                "schema_version": policy.schema_version,
            },
            self.activated_by,
        )
        return self.status()

    def deactivate(self, requested_by: str, reason: str = "") -> dict[str, Any]:
        if self.active_policy is None or self.active_job_id is None:
            raise CampaignPolicyError("no campaign is active")
        if not requested_by.strip():
            raise CampaignPolicyError("campaign deactivation must name who requested it")
        job_id = self.active_job_id
        policy = self.active_policy
        self.ledger.append_event(
            job_id,
            "campaign.deactivated",
            {
                "campaign_id": policy.campaign_id,
                "campaign_sha256": policy.sha256,
                "reason": reason.strip(),
            },
            requested_by.strip(),
        )
        self._clear()
        return self.status()

    def recover(self) -> dict[str, Any]:
        latest = None
        for event in iter_events(self.ledger):
            if event.kind in ("campaign.activated", "campaign.deactivated"):
                latest = event
        if latest is None or latest.kind == "campaign.deactivated":
            return self.status()
        payload = latest.payload
        try:
            policy = self._load(str(payload.get("filename") or ""), str(payload.get("campaign_sha256") or ""))
            if _parse_time(self.now()) >= _parse_time(policy.expires_at):
                return self.status()
            job = self.ledger.job(latest.job_id)
            if job.state == "done":
                return self.status()
        except Exception:  # noqa: BLE001 - recovery of authority must fail closed
            return self.status()
        self._install(policy)
        self.active_job_id = latest.job_id
        self.active_filename = str(payload.get("filename"))
        self.activated_by = str(payload.get("approved_by") or latest.agent or "")
        self.activated_at = latest.at
        return self.status()

    def status(self) -> dict[str, Any]:
        policy = self.active_policy
        if policy is None:
            return {"active": False, "hardware_enabled": self.hardware_enabled}
        used = 0
        permission_used: dict[str, int] = {}
        for event in iter_events(self.ledger):
            if event.kind != "campaign.approval.evaluated":
                continue
            if event.payload.get("campaign_sha256") != policy.sha256 or not event.payload.get("allowed"):
                continue
            used += 1
            key = str(event.payload.get("permission_id") or "")
            permission_used[key] = permission_used.get(key, 0) + 1
        result = self._policy_description(policy)
        result.update({
            "active": True,
            "hardware_enabled": self.hardware_enabled,
            "job_id": self.active_job_id,
            "filename": self.active_filename,
            "activated_by": self.activated_by,
            "activated_at": self.activated_at,
            "auto_approvals_used": used,
            "auto_approvals_remaining": max(0, policy.max_auto_approvals - used),
            "permission_usage": permission_used,
        })
        return result

    def _load(self, filename: str, digest: str) -> CampaignPolicy:
        if Path(filename).name != filename or not filename.endswith((".yaml", ".yml")):
            raise CampaignPolicyError("campaign must be one plain YAML filename")
        return CampaignPolicy.load(
            self.campaigns_dir / filename,
            digest,
            contracts_dir=self.contracts_dir,
            project_root=self.project_root,
        )

    def _install(self, policy: CampaignPolicy) -> None:
        approver = CampaignAutoApprover(
            self.ledger, policy, now=self.now, runs_dir=self.runs_dir
        )
        self.execution_loop.auto_approver = approver
        if self.router is not None:
            self.router.auto_approver = approver
        self.hardware_gate.active_campaign_sha256 = policy.sha256
        self.hardware_gate.active_campaign_contracts = {
            permission.contract: permission.contract_sha256
            for permission in policy.permissions
        }
        self.active_policy = policy

    def _clear(self) -> None:
        self.execution_loop.auto_approver = None
        if self.router is not None:
            self.router.auto_approver = None
        self.hardware_gate.active_campaign_sha256 = None
        self.hardware_gate.active_campaign_contracts = {}
        self.active_policy = None
        self.active_job_id = None
        self.active_filename = None
        self.activated_by = None
        self.activated_at = None

    @staticmethod
    def _policy_description(policy: CampaignPolicy) -> dict[str, Any]:
        return {
            "campaign_id": policy.campaign_id,
            "schema_version": policy.schema_version,
            "sha256": policy.sha256,
            "approved_by": policy.approved_by,
            "expires_at": policy.expires_at,
            "max_auto_approvals": policy.max_auto_approvals,
            "max_iterations": policy.max_iterations,
            "permissions": [
                {
                    "id": item.permission_id,
                    "contract": item.contract,
                    "contract_sha256": item.contract_sha256,
                    "role": item.role,
                    "tool": item.tool,
                    "artifact_mode": item.artifact_mode,
                    "artifact_kinds": list(item.artifact_kinds),
                    "producer_tools": list(item.producer_tools),
                    "max_approvals": item.max_approvals,
                    "max_executions": item.max_executions,
                    "pinned_artifacts": dict(item.artifacts),
                }
                for item in policy.permissions
            ],
        }


def _permission(
    raw: Any,
    contracts_dir: Path,
    project_root: Path,
    schema_version: int,
) -> CampaignPermission:
    if not isinstance(raw, dict):
        raise CampaignPolicyError("each campaign permission must be an object")
    unknown = set(raw) - PERMISSION_KEYS
    if unknown:
        raise CampaignPolicyError(f"unknown permission fields: {', '.join(sorted(unknown))}")
    permission_id = _plain(raw.get("id"), "permission.id")
    contract = _plain(raw.get("contract"), f"permission {permission_id} contract")
    if not CONTRACT_NAME.fullmatch(contract):
        raise CampaignPolicyError(f"permission {permission_id} contract is not a plain YAML name")
    contract_sha256 = _digest(
        raw.get("contract_sha256"), f"permission {permission_id} contract_sha256"
    ).lower()
    role = _plain(raw.get("role"), f"permission {permission_id} role")
    tool = _plain(raw.get("tool"), f"permission {permission_id} tool")
    if role not in ROLE_TOOLS:
        raise CampaignPolicyError(f"permission {permission_id} names unknown role {role!r}")
    if tool not in ROLE_TOOLS[role]:
        raise CampaignPolicyError(
            f"permission {permission_id} gives {role!r} a tool outside its role: {tool!r}"
        )
    contract_path = contracts_dir / contract
    if not contract_path.is_file():
        raise CampaignPolicyError(f"permission {permission_id} contract is missing: {contract}")

    if schema_version == 1 and any(
        key in raw for key in ("artifact_mode", "artifact_kinds", "producer_tools")
    ):
        raise CampaignPolicyError(
            f"permission {permission_id} uses v2 artifact policy fields in a v1 campaign"
        )
    artifact_mode = (
        "pinned"
        if schema_version == 1
        else _plain(raw.get("artifact_mode"), f"permission {permission_id} artifact_mode")
    )
    if artifact_mode not in ("pinned", "run_manifest"):
        raise CampaignPolicyError(
            f"permission {permission_id} artifact_mode must be pinned or run_manifest"
        )
    raw_artifacts = raw.get("artifacts", {})
    if not isinstance(raw_artifacts, dict):
        raise CampaignPolicyError(f"permission {permission_id} artifacts must be an object")
    if artifact_mode == "pinned" and not raw_artifacts:
        raise CampaignPolicyError(f"permission {permission_id} needs at least one artifact digest")
    if artifact_mode == "run_manifest" and raw_artifacts:
        raise CampaignPolicyError(
            f"permission {permission_id} run_manifest artifacts are supplied by each iteration"
        )
    artifacts: dict[str, str] = {}
    for reference, digest in raw_artifacts.items():
        reference = _plain(reference, f"permission {permission_id} artifact path")
        artifacts[reference] = _digest(
            digest, f"permission {permission_id} artifact {reference}"
        ).lower()
        if _resolve(project_root, reference) is None:
            raise CampaignPolicyError(
                f"permission {permission_id} artifact escapes the project: {reference}"
            )
    artifact_kinds = (
        _string_list(raw.get("artifact_kinds"), f"permission {permission_id} artifact_kinds")
        if artifact_mode == "run_manifest"
        else ()
    )
    producer_tools = (
        _string_list(raw.get("producer_tools"), f"permission {permission_id} producer_tools")
        if artifact_mode == "run_manifest"
        else ()
    )
    if artifact_mode == "run_manifest" and (not artifact_kinds or not producer_tools):
        raise CampaignPolicyError(
            f"permission {permission_id} run_manifest needs artifact_kinds and producer_tools"
        )
    return CampaignPermission(
        permission_id=permission_id,
        contract=contract,
        contract_sha256=contract_sha256,
        role=role,
        tool=tool,
        artifacts=artifacts,
        max_approvals=_positive(
            raw.get("max_approvals"), f"permission {permission_id} max_approvals"
        ),
        max_executions=_positive(
            raw.get("max_executions"), f"permission {permission_id} max_executions"
        ),
        artifact_mode=artifact_mode,
        artifact_kinds=artifact_kinds,
        producer_tools=producer_tools,
    )


def _plain(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CampaignPolicyError(f"{field} must be a non-empty string")
    return value.strip()


def _digest(value: Any, field: str) -> str:
    value = _plain(value, field)
    if not SHA256.fullmatch(value):
        raise CampaignPolicyError(f"{field} must be a full SHA-256 digest")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CampaignPolicyError(f"{field} must be a positive integer")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CampaignPolicyError(f"{field} must be a non-empty list")
    result = tuple(_plain(item, field) for item in value)
    if len(result) != len(set(result)):
        raise CampaignPolicyError(f"{field} must not contain duplicates")
    return result


def _timestamp(value: Any, field: str) -> str:
    value = _plain(value, field)
    moment = _parse_time(value)
    if moment.utcoffset() is None:
        raise CampaignPolicyError(f"{field} must include a timezone")
    return value


def _parse_time(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignPolicyError(f"invalid ISO-8601 timestamp: {value}") from exc
    if moment.tzinfo is None:
        raise CampaignPolicyError(f"timestamp must include a timezone: {value}")
    return moment


def _resolve(project_root: Path, reference: str) -> Path | None:
    candidate = (project_root / reference).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return None
    return candidate


def _artifact_problem(project_root: Path, reference: str, expected: str) -> str | None:
    path = _resolve(project_root, reference)
    if path is None:
        return "cannot be located inside the project"
    if not path.is_file():
        return "is not on disk"
    actual = sha256_file(path)
    if actual != expected:
        return f"now hashes to {actual} instead of {expected}"
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
