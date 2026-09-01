"""Durably connect a completed mission to its executable child job.

The discovery and execution loops intentionally remain separate.  This module
owns the narrow bridge between them.  It does not ask an LLM what should run:
it replays the reviewed mission handoffs, resolves either the exact baseline
contract or the deterministic compiled proposal, and creates a child job with
a stable id.  Stable ids plus append-only link events make crash recovery
idempotent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.contract_promotion import (
    ContractPromotionService,
    PromotionError,
)
from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.orchestrator.loop import start
from QKD_PROJ.research.architecture import ArchitectureDecision, ContractReview
from QKD_PROJ.research.discovery_loop import MISSION_GATE, MISSION_WORKFLOW
from QKD_PROJ.research.mission import MissionPlan


CONTINUED_EVENT = "workflow.continued"
PARENT_EVENT = "workflow.parent"
WAITING_EVENT = "workflow.continuation.waiting"
FAILED_EVENT = "workflow.continuation.failed"


@dataclass(frozen=True)
class ContinuationResult:
    parent_job_id: str
    status: str
    contract: str | None = None
    contract_sha256: str | None = None
    child_job_id: str | None = None
    detail: str = ""

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


class MissionContinuation:
    """Create exactly one execution child for one completed mission."""

    def __init__(
        self,
        ledger: JobLedger,
        contracts_dir: Path,
        *,
        campaign_controller: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.contracts_dir = Path(contracts_dir).resolve()
        self.promoter = ContractPromotionService(ledger, self.contracts_dir)
        self.campaign_controller = campaign_controller

    def continue_job(self, parent_job_id: str) -> ContinuationResult:
        existing = self._existing(parent_job_id)
        if existing is not None:
            return existing

        job = self.ledger.job(parent_job_id)
        if job.gate != MISSION_GATE:
            return ContinuationResult(parent_job_id, "not_a_mission")
        if job.state != J.DONE or job.outcome != "contract_gap":
            return ContinuationResult(
                parent_job_id,
                "not_ready",
                detail=f"mission is {job.state}/{job.outcome}",
            )

        brief_rows = self.ledger.handoffs(parent_job_id, "Brief", 0)
        mission_rows = self.ledger.handoffs(parent_job_id, "MissionPlan", 0)
        decision_rows = self.ledger.handoffs(parent_job_id, "ArchitectureDecision", 0)
        if len(brief_rows) != 1 or len(mission_rows) != 1 or len(decision_rows) != 1:
            return self._failed(parent_job_id, "mission handoff chain is incomplete or ambiguous")
        brief = brief_rows[0].payload
        resources = brief.get("resources") if isinstance(brief, dict) else None
        if not isinstance(resources, dict) or resources.get("workflow") != MISSION_WORKFLOW:
            return self._failed(parent_job_id, "Brief does not name the mission workflow")
        try:
            mission = MissionPlan.parse(mission_rows[0].payload)
            decision = ArchitectureDecision.parse(decision_rows[0].payload)
        except ValueError as exc:
            return self._failed(parent_job_id, f"mission handoff validation failed: {exc}")
        if decision.status != "selected":
            return ContinuationResult(
                parent_job_id,
                "research_required",
                detail=f"architecture ended as {decision.status}",
            )

        board_gap = self._board_gap(resources)
        if board_gap is not None:
            return self._waiting(parent_job_id, board_gap, None, None)

        try:
            if mission.contract_action == "reuse":
                contract, digest = self._baseline(resources, mission)
            else:
                reviews = self.ledger.handoffs(parent_job_id, "ContractReview", 0)
                if len(reviews) != 1:
                    return self._failed(parent_job_id, "reviewed contract proposal is missing")
                review = ContractReview.parse(reviews[0].payload)
                if review.outcome != "ready_for_human_review":
                    return ContinuationResult(
                        parent_job_id,
                        "review_changes_required",
                        detail=f"contract review ended as {review.outcome}",
                    )
                mode = str(resources.get("execution_mode") or "offline")
                proposal = (
                    self.promoter.apply_reviewed_offline(parent_job_id)
                    if mode == "offline"
                    else self.promoter.apply_reviewed(parent_job_id)
                )
                contract, digest = proposal.filename, proposal.contract_sha256
        except (PromotionError, JobLedgerError, OSError, ValueError) as exc:
            return self._failed(parent_job_id, str(exc))

        mode = str(resources.get("execution_mode") or "offline")
        if mode == "hardware" and not self._campaign_allows(
            parent_job_id, contract, digest, mission.scenario_sha256
        ):
            return self._waiting(
                parent_job_id,
                f"hardware contract {contract} is not covered by the active campaign",
                contract,
                digest,
            )
        return self._create_child(parent_job_id, brief, mission, contract, digest, mode)

    @staticmethod
    def _board_gap(resources: dict[str, Any]) -> str | None:
        development = resources.get("board_development")
        if not isinstance(development, dict):
            return None
        if development.get("status") == "profile_required":
            return "the exact board/revision needs a reviewed adapter profile"
        selected = development.get("selected")
        if not isinstance(selected, dict):
            return None
        mode = str(resources.get("execution_mode") or "offline")
        gaps = [
            str(stage.get("phase"))
            for stage in selected.get("stages", [])
            if isinstance(stage, dict)
            and stage.get("status") == "adapter_required"
            and (mode == "hardware" or stage.get("mode") != "hardware")
        ]
        if gaps:
            return "board profile still needs adapters for " + ", ".join(gaps)
        return None

    def recover(self) -> list[str]:
        notes: list[str] = []
        for job in self.ledger.jobs(state=J.DONE):
            if job.gate != MISSION_GATE or job.outcome != "contract_gap":
                continue
            result = self.continue_job(job.job_id)
            if result.status in ("created", "existing"):
                notes.append(
                    f"{job.job_id} continues as {result.child_job_id} under {result.contract}"
                )
        return notes

    def _baseline(self, resources: dict[str, Any], mission: MissionPlan) -> tuple[str, str]:
        selected = resources.get("selected_contract")
        if not isinstance(selected, dict):
            raise PromotionError("reuse mission has no preflight-selected contract")
        name = selected.get("name")
        digest = selected.get("sha256")
        if name != mission.baseline_contract or not isinstance(digest, str):
            raise PromotionError("reuse mission changed the selected contract identity")
        if Path(str(name)).name != name:
            raise PromotionError("selected contract is not a plain filename")
        path = (self.contracts_dir / str(name)).resolve()
        if path.parent != self.contracts_dir or not path.is_file():
            raise PromotionError(f"selected contract is missing: {name}")
        actual = sha256_file(path)
        if actual != digest:
            raise PromotionError(
                f"selected contract changed after mission intake: expected {digest}, found {actual}"
            )
        return str(name), digest

    def _create_child(
        self,
        parent_job_id: str,
        brief: dict[str, Any],
        mission: MissionPlan,
        contract: str,
        digest: str,
        mode: str,
    ) -> ContinuationResult:
        child_id = "job_" + hashlib.sha256(
            f"mission-execution:{parent_job_id}".encode("utf-8")
        ).hexdigest()[:12]
        scenario = str(brief.get("scenario") or "")
        resources = dict(brief.get("resources") or {})
        resources.update(
            {
                "workflow": "execution",
                "parent_job_id": parent_job_id,
                "selected_contract": {"name": contract, "sha256": digest},
                "execution_mode": mode,
                "approval_required": mode == "hardware",
                "roles": list(mission.execution_agents),
                "skills": list(mission.execution_skills),
                "mission_plan_sha256": self.ledger.handoffs(
                    parent_job_id, "MissionPlan", 0
                )[0].sha256,
            }
        )
        try:
            child = start(
                self.ledger,
                contract,
                self.ledger.job(parent_job_id).objective,
                scenario=scenario,
                resources=resources,
                job_id=child_id,
            )
        except JobLedgerError as exc:
            if "already exists" not in str(exc):
                raise
            child = self.ledger.job(child_id)
            child_briefs = self.ledger.handoffs(child_id, "Brief", 0)
            if (
                child.gate != contract
                or len(child_briefs) != 1
                or child_briefs[0].payload.get("scenario") != scenario
            ):
                raise PromotionError("deterministic continuation child id collided") from exc

        self._append_once(
            parent_job_id,
            CONTINUED_EVENT,
            {
                "child_job_id": child.job_id,
                "contract": contract,
                "contract_sha256": digest,
                "scenario_sha256": mission.scenario_sha256,
            },
        )
        self._append_once(
            child.job_id,
            PARENT_EVENT,
            {
                "parent_job_id": parent_job_id,
                "contract": contract,
                "contract_sha256": digest,
            },
        )
        return ContinuationResult(
            parent_job_id,
            "created",
            contract,
            digest,
            child.job_id,
            "execution job created from the reviewed mission",
        )

    def _campaign_allows(
        self, parent_job_id: str, contract: str, digest: str, scenario_digest: str
    ) -> bool:
        controller = self.campaign_controller
        policy = getattr(controller, "active_policy", None)
        return bool(
            policy is not None
            and getattr(controller, "active_job_id", None) == parent_job_id
            and policy.scenario_sha256 == scenario_digest
            and any(
                item.contract == contract and item.contract_sha256 == digest
                for item in policy.permissions
            )
        )

    def _existing(self, parent_job_id: str) -> ContinuationResult | None:
        rows = [
            event
            for event in self._events(parent_job_id)
            if event.kind == CONTINUED_EVENT
        ]
        if not rows:
            return None
        payload = rows[-1].payload
        return ContinuationResult(
            parent_job_id,
            "existing",
            str(payload.get("contract") or "") or None,
            str(payload.get("contract_sha256") or "") or None,
            str(payload.get("child_job_id") or "") or None,
            "continuation already exists",
        )

    def _waiting(
        self,
        parent_job_id: str,
        detail: str,
        contract: str | None = None,
        digest: str | None = None,
    ) -> ContinuationResult:
        self._append_once(
            parent_job_id,
            WAITING_EVENT,
            {"detail": detail, "contract": contract, "contract_sha256": digest},
        )
        status = "waiting_campaign" if contract is not None else "board_profile_required"
        return ContinuationResult(parent_job_id, status, contract, digest, detail=detail)

    def _failed(self, parent_job_id: str, detail: str) -> ContinuationResult:
        self._append_once(parent_job_id, FAILED_EVENT, {"detail": detail})
        return ContinuationResult(parent_job_id, "failed", detail=detail)

    def _append_once(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if any(event.kind == kind and event.payload == payload for event in self._events(job_id)):
            return
        self.ledger.append_event(job_id, kind, payload, "platform")

    def _events(self, job_id: str) -> list[J.Event]:
        rows: list[J.Event] = []
        cursor = 0
        while True:
            page = self.ledger.events_since(cursor, job_id, 500)
            if not page:
                return rows
            rows.extend(page)
            cursor = page[-1].event_id
