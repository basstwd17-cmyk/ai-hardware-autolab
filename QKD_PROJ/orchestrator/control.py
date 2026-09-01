"""The commands a person can send a running daemon.

One request object in, one response object out, no transport in sight. The
named pipe in ``service.py`` carries these; a test calls ``handle()`` directly;
a UI will do the same over whatever it ends up speaking. Keeping the command
surface transport-free is what makes all three the same code path.

Every command that changes anything changes it **in the ledger**, because the
daemon that acts on it may not be the process that received it -- and almost
certainly is not the process that will still be running an hour later when a
person finally answers an approval.

Three commands change state, and each has a rule that is not obvious:

**approve** never decides on the daemon's behalf. It records what a person
said. The gate in ``daemon.py`` reads that decision back and re-hashes the
artifacts before a board-touching call goes through, so approving here does not
authorise anything by itself.

**cancel** withdraws the job's outstanding requests before closing it. A
granted approval left behind on a cancelled job is a signature with nothing
attached to it, and the next thing to quote it would be quoting a decision
nobody made about it.

**steer** is delivered, not stored as a setting. A steering message is written
to the log, and the loop hands the agent every steer it has not seen yet.
Delivery is derived from the log rather than marked in a column -- but only the
handoffs an agent *that was given the steering* produced count as proof. See
`pending_steers` for why that distinction is not pedantic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from QKD_PROJ.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
from QKD_PROJ.boards.profiles import BoardCatalog, BoardProfileError
from QKD_PROJ.knowledge.models import KnowledgeScope, KnowledgeValidationError
from QKD_PROJ.knowledge.retriever import KnowledgeRetriever
from QKD_PROJ.knowledge.store import KnowledgeStore, KnowledgeStoreError
from QKD_PROJ.orchestrator import daemon as D
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator import loop as L
from QKD_PROJ.orchestrator.contract_promotion import (
    ContractPromotionService,
    PromotionError,
)
from QKD_PROJ.orchestrator.campaign import CampaignController, CampaignPolicyError
from QKD_PROJ.orchestrator.campaign_proposal import (
    CampaignProposalError,
    CampaignProposalService,
)
from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.orchestrator.pagination import iter_events, tail_since
from QKD_PROJ.orchestrator.remote_summary import build_remote_snapshot
from QKD_PROJ.orchestrator.scenario_preflight import PreflightError, ScenarioPreflight
from QKD_PROJ.research.discovery_loop import MISSION_GATE, start_discovery, start_mission
from QKD_PROJ.research.mission import SEARCH_POLICIES

# Control events. Namespaced away from the ledger's own kinds for the same
# reason the runtime's are: a reader must be able to tell who said a thing.
NAMESPACE = "control."
STEER = NAMESPACE + "steer"
CANCEL = NAMESPACE + "cancel"
REVISION_REQUESTED = NAMESPACE + "revision.requested"
REVISION_OF = "workflow.revision"

PROTOCOL_VERSION = 1


class ControlError(ValueError):
    """The request was understood and refused, with a reason a caller can show."""


@dataclass
class ControlPlane:
    """Commands, answered from the ledger."""

    ledger: JobLedger
    daemon: D.Daemon | None = None
    on_shutdown: Callable[[], None] | None = None
    preflight_engine: ScenarioPreflight | None = None
    knowledge_store: KnowledgeStore | None = None
    knowledge_retriever: KnowledgeRetriever | None = None
    default_knowledge_scope: KnowledgeScope | None = None
    contract_promoter: ContractPromotionService | None = None
    campaign_controller: CampaignController | None = None
    campaign_proposer: CampaignProposalService | None = None
    capability_catalog: CapabilityCatalog | None = None
    board_catalog: BoardCatalog | None = None

    def __post_init__(self) -> None:
        if self.knowledge_retriever is None and self.knowledge_store is not None:
            self.knowledge_retriever = KnowledgeRetriever(self.knowledge_store)

    def _max_iterations(self) -> int | None:
        """The loop's ceiling, when there is a loop to ask."""
        loop = getattr(self.daemon, "loop", None)
        policy = getattr(loop, "policy", None)
        return getattr(policy, "max_iterations", None)

    # -- reading ---------------------------------------------------------

    def status(self, now: str | None = None) -> dict[str, Any]:
        if self.daemon is None:
            return {
                "cursor": self.ledger.latest_event_id(),
                "jobs": [j.describe() for j in self.ledger.jobs()],
                "inbox": [a.describe() for a in self.ledger.pending_approvals(now=now)],
            }
        return self.daemon.status(now)

    def job(self, job_id: str) -> dict[str, Any]:
        state = self.ledger.resume(job_id)
        if self.daemon is not None:
            state["waiting"] = self.daemon.waiting_for(job_id).describe()
        state["pending_steers"] = pending_steers(self.ledger, job_id)
        state["will_deliver_steering"] = will_deliver_steering(
            self.ledger, job_id, self._max_iterations()
        )
        return state

    def handoffs(self, job_id: str) -> dict[str, Any]:
        """The durable envelopes for one job, in ledger order.

        The live event stream deliberately carries only a handoff digest: it
        must stay cheap enough to animate and replay.  Reading the result of a
        selected local job is a different question.  This command leaves the
        ledger untouched and returns the envelopes it already owns, so the UI
        can show what each specialist actually handed to the next one.
        """
        # Make a missing job an ordinary control-plane refusal instead of
        # returning an indistinguishable empty list.
        self.ledger.resume(job_id)
        return {"handoffs": [handoff.describe() for handoff in self.ledger.handoffs(job_id)]}

    def events(self, since: int = 0, job_id: str | None = None, limit: int = 500) -> dict[str, Any]:
        """The replay cursor. `since` is an `events.event_id`, not a runtime's own."""
        events = self.ledger.events_since(since, job_id, limit)
        return {
            "events": [e.describe() for e in events],
            "cursor": events[-1].event_id if events else since,
            "latest": self.ledger.latest_event_id(),
        }

    def inbox(self, now: str | None = None) -> dict[str, Any]:
        return {"approvals": [a.describe() for a in self.ledger.pending_approvals(now=now)]}

    def remote_snapshot(self, now: str | None = None, limit: int = 24) -> dict[str, Any]:
        """A bounded, path-free phone projection derived from durable records."""
        phase = getattr(getattr(self.daemon, "loop", None), "phase", None)
        snapshot = build_remote_snapshot(
            self.ledger,
            self.status(now),
            limit=limit,
            phase_for=phase if callable(phase) else None,
        )
        for card in snapshot["jobs"]:
            job_id = card["job_id"]
            pending = pending_steers(self.ledger, job_id)
            card["feedback"] = {
                "pending_count": len(pending),
                "will_be_delivered": will_deliver_steering(
                    self.ledger, job_id, self._max_iterations()
                ),
                "latest": {
                    "message": str(pending[-1].get("message") or "")[:600],
                    "by": str(pending[-1].get("by") or "")[:100],
                    "event_id": pending[-1].get("event_id"),
                } if pending else None,
            }
        return snapshot

    def campaign_catalog(self) -> dict[str, Any]:
        return {"campaigns": self._campaign_controller().catalog()}

    def campaign_status(self) -> dict[str, Any]:
        return self._campaign_controller().status()

    def campaign_preview(self, filename: str, sha256: str) -> dict[str, Any]:
        try:
            return self._campaign_controller().preview(filename, sha256)
        except CampaignPolicyError as exc:
            raise ControlError(str(exc)) from exc

    def campaign_activate(
        self, job_id: str, filename: str, sha256: str, approved_by: str
    ) -> dict[str, Any]:
        try:
            result = self._campaign_controller().activate(job_id, filename, sha256, approved_by)
            router = getattr(self.daemon, "loop", None)
            continue_completed = getattr(router, "continue_completed", None)
            if callable(continue_completed):
                continuation = continue_completed(job_id)
                if continuation is not None:
                    result = dict(result)
                    result["continuation"] = continuation.describe()
            return result
        except CampaignPolicyError as exc:
            raise ControlError(str(exc)) from exc

    def campaign_deactivate(self, requested_by: str, reason: str = "") -> dict[str, Any]:
        try:
            return self._campaign_controller().deactivate(requested_by, reason)
        except CampaignPolicyError as exc:
            raise ControlError(str(exc)) from exc

    def campaign_bundle_preview(
        self,
        job_id: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
    ) -> dict[str, Any]:
        try:
            return self._campaign_proposer().preview(
                job_id,
                max_iterations=max_iterations,
                max_auto_approvals=max_auto_approvals,
                duration_hours=duration_hours,
            ).describe()
        except CampaignProposalError as exc:
            raise ControlError(str(exc)) from exc

    def campaign_bundle_approve(
        self,
        job_id: str,
        approved_by: str,
        review_sha256: str,
        *,
        max_iterations: int = 8,
        max_auto_approvals: int = 24,
        duration_hours: int = 12,
        comment: str = "",
    ) -> dict[str, Any]:
        try:
            result = self._campaign_proposer().approve(
                job_id,
                approved_by,
                review_sha256,
                max_iterations=max_iterations,
                max_auto_approvals=max_auto_approvals,
                duration_hours=duration_hours,
            )
            router = getattr(self.daemon, "loop", None)
            continue_completed = getattr(router, "continue_completed", None)
            if callable(continue_completed):
                continuation = continue_completed(job_id)
                if continuation is not None:
                    result["continuation"] = continuation.describe()
                    child_job_id = continuation.child_job_id
                    if comment.strip() and child_job_id:
                        result["steering"] = self.steer(
                            child_job_id, comment, approved_by
                        )
            return result
        except (CampaignProposalError, CampaignPolicyError) as exc:
            raise ControlError(str(exc)) from exc

    def knowledge_claim(self, claim_id: str) -> dict[str, Any]:
        claim = self._knowledge_store().claim(claim_id)
        if claim is None:
            raise ControlError(f"knowledge claim does not exist: {claim_id}")
        return claim.to_dict()

    def knowledge_search(
        self,
        query: str,
        scope: KnowledgeScope,
        limit: int = 20,
    ) -> dict[str, Any]:
        context = self._knowledge_retriever().retrieve(query, scope, limit=limit)
        return context.to_dict()

    def knowledge_neighbors(
        self,
        entity_key: str,
        scope: KnowledgeScope,
        limit: int = 20,
    ) -> dict[str, Any]:
        claims = self._knowledge_store().neighbors(entity_key, scope=scope, limit=limit)
        return {
            "entity": entity_key,
            "scope": scope.to_dict(),
            "claims": [claim.to_dict() for claim in claims],
        }

    def knowledge_events(self, since: int = 0, limit: int = 500) -> dict[str, Any]:
        store = self._knowledge_store()
        events = store.events_since(since, limit)
        return {
            "events": [event.to_dict() for event in events],
            "cursor": events[-1].event_id if events else since,
            "latest": store.latest_event_id(),
        }

    # -- changing --------------------------------------------------------

    def preflight(self, scenario: str) -> dict[str, Any]:
        try:
            return self._preflight_engine().inspect(scenario)
        except PreflightError as exc:
            raise ControlError(str(exc)) from exc

    def start(
        self,
        gate: str,
        objective: str,
        scenario: str = "",
        contract_sha256: str = "",
        scenario_sha256: str = "",
    ) -> dict[str, Any]:
        """Open a durable job from the same boundary used by every other command.

        The CLI used to create jobs by opening the SQLite ledger directly.  A
        long-lived office cannot do that safely: it must use the daemon's turn
        lock so creation cannot race the scheduler, and so the resulting Brief
        is the first durable handoff rather than UI-only state.
        """
        try:
            if not contract_sha256 or not scenario_sha256:
                raise PreflightError(
                    "job.start requires contract_sha256 and scenario_sha256 from scenario.preflight"
                )

            engine = self._preflight_engine()
            engine.verify_scenario(scenario, scenario_sha256)
            preview = engine.inspect(scenario)
            if preview.get("status") != "ready":
                raise PreflightError(
                    f"scenario preflight is {preview.get('status')}: {preview.get('reason_code')}"
                )
            selected = preview.get("selected_contract")
            if not isinstance(selected, dict) or not isinstance(selected.get("name"), str):
                raise PreflightError("ready preflight returned no selected contract")
            selected_name = str(selected["name"])
            if gate != selected_name:
                raise PreflightError(
                    f"job.start gate {gate!r} does not match preflight selection {selected_name!r}"
                )
            if str(selected.get("sha256", "")).lower() != contract_sha256.lower():
                raise PreflightError(
                    f"contract_sha256 does not match the preflight selection {selected_name}"
                )
            if str(preview.get("scenario_sha256", "")).lower() != scenario_sha256.lower():
                raise PreflightError("scenario_sha256 does not match the repeated preflight")

            engine.verify_contract(selected_name, contract_sha256)
            resources = {
                "selected_contract": selected,
                "execution_mode": preview.get("execution_mode"),
                "approval_required": preview.get("approval_required"),
                "roles": list(preview.get("roles") or []),
                "skills": list(preview.get("skills") or []),
                "expected_artifacts": list(preview.get("expected_artifacts") or []),
            }
            if self.default_knowledge_scope is not None:
                resources["knowledge_scope"] = self.default_knowledge_scope.to_dict()
            if self.knowledge_retriever is not None:
                scope = self.default_knowledge_scope or KnowledgeScope()
                resources["knowledge_context"] = self.knowledge_retriever.retrieve(
                    scenario, scope, limit=16
                ).to_dict()
            job = L.start(
                self.ledger,
                selected_name,
                objective,
                scenario=scenario,
                resources=resources,
            )
        except (
            J.JobLedgerError,
            L.LoopError,
            PreflightError,
            KnowledgeStoreError,
            ValueError,
        ) as exc:
            raise ControlError(str(exc)) from exc
        return {"job": job.describe()}

    def discover(
        self,
        objective: str,
        scenario: str,
        scenario_sha256: str,
    ) -> dict[str, Any]:
        """Open a contract-gap workflow without pretending it has authority.

        A ready scenario belongs in ``start``.  This command is only for the
        deterministic preflight result ``contract_gap``; discovery may produce
        a reviewed proposal, but never installs it under ``contracts/``.
        """
        try:
            engine = self._preflight_engine()
            engine.verify_scenario(scenario, scenario_sha256)
            preview = engine.inspect(scenario)
            if preview.get("status") != "contract_gap":
                raise PreflightError(
                    "scenario already has a reviewed contract; use job.start with the "
                    "preflight-selected contract"
                )
            resources: dict[str, Any] = {
                "preflight": preview,
                "execution_mode": preview.get("execution_mode"),
                "available_tools": sorted(
                    {
                        tool
                        for agent in engine.agents
                        for tool in agent.mcp_tools
                    }
                ),
            }
            if self.default_knowledge_scope is not None:
                resources["knowledge_scope"] = self.default_knowledge_scope.to_dict()
            if self.knowledge_retriever is not None:
                scope = self.default_knowledge_scope or KnowledgeScope()
                resources["knowledge_context"] = self.knowledge_retriever.retrieve(
                    scenario, scope, limit=16
                ).to_dict()
            job = start_discovery(
                self.ledger,
                objective,
                scenario,
                resources=resources,
            )
        except (
            J.JobLedgerError,
            PreflightError,
            KnowledgeStoreError,
            CapabilityCatalogError,
            ValueError,
        ) as exc:
            raise ControlError(str(exc)) from exc
        return {"job": job.describe(), "preflight": preview}

    def mission(
        self,
        objective: str,
        scenario: str,
        scenario_sha256: str,
        search_policy: str = "refresh",
    ) -> dict[str, Any]:
        """Open the common front door for new work, improvements and repairs.

        Unlike ``discover``, a mission accepts both a reviewed baseline and a
        contract gap.  The selected contract and knowledge are inputs to the
        first agent, never authority to execute: the workflow stops at a
        reviewed contract proposal.
        """

        try:
            if search_policy not in SEARCH_POLICIES:
                raise PreflightError(
                    f"search_policy must be one of {list(SEARCH_POLICIES)}"
                )
            engine = self._preflight_engine()
            engine.verify_scenario(scenario, scenario_sha256)
            preview = engine.inspect(scenario)
            selected = preview.get("selected_contract")
            mode = preview.get("execution_mode")
            board_context = (
                self.board_catalog.planning_context(scenario)
                if self.board_catalog is not None
                else {
                    "status": "profile_required",
                    "selected": None,
                    "required_stages": [
                        "research", "design", "build", "upload", "measure", "verify"
                    ],
                }
            )
            reason_code = str(preview.get("reason_code") or "")
            candidates = [
                {
                    key: candidate[key]
                    for key in ("name", "test_id", "status", "sha256")
                    if key in candidate
                }
                for candidate in preview.get("candidates", [])
                if isinstance(candidate, dict)
            ]
            if isinstance(selected, dict):
                contract_resolution = {
                    "mode": "reuse_or_amend",
                    "reason": reason_code,
                    "parents": [
                        {
                            key: selected[key]
                            for key in ("name", "test_id", "status", "sha256")
                            if key in selected
                        }
                    ],
                    "requires_human_approval": mode == "hardware",
                }
            else:
                contract_resolution = {
                    "mode": "derive",
                    "reason": reason_code,
                    "parents": candidates if reason_code == "ambiguous_contract" else [],
                    # The submitted scenario is the authority for offline file
                    # work. Hardware still needs a campaign or a human grant.
                    "requires_human_approval": mode == "hardware",
                }
            selected_board = board_context.get("selected")
            if isinstance(selected_board, dict):
                requested_roles: set[str] = set()
                for stage in selected_board.get("stages", []):
                    if not isinstance(stage, dict) or stage.get("status") != "ready":
                        continue
                    if stage.get("mode") == "hardware" and mode != "hardware":
                        continue
                    if isinstance(stage.get("agent"), str):
                        requested_roles.add(stage["agent"])
            elif self.board_catalog is not None:
                # A generic preflight role is not a board adapter.  Until the
                # exact board is profiled, discovery may research and select
                # an architecture but execution gets no vendor-specific
                # builder/operator by similarity of names.
                requested_roles = set()
            else:
                # Compatibility for callers that intentionally construct a
                # control plane without the board catalog. In the real
                # service the catalog is always wired; library users retain
                # the historical preflight routing.
                requested_roles = {
                    str(item)
                    for item in preview.get("roles", [])
                    if isinstance(item, str)
                }
            requested_roles.update({"verifier", "reporter"})
            execution_agents = sorted(
                agent.name
                for agent in engine.agents
                if agent.role in ("build", "operate", "measure", "verify", "report")
                and agent.name in requested_roles
                and (mode == "hardware" or not agent.touches_hardware)
            )
            if not execution_agents:
                raise PreflightError("no execution agents are available for this mission scope")
            agent_tools = {
                tool
                for agent in engine.agents
                if agent.name in execution_agents
                for tool in agent.mcp_tools
            }
            if isinstance(selected_board, dict):
                profile_tools = {
                    str(stage["tool"])
                    for stage in selected_board.get("stages", [])
                    if isinstance(stage, dict)
                    and stage.get("status") == "ready"
                    and isinstance(stage.get("tool"), str)
                    and (mode == "hardware" or stage.get("mode") != "hardware")
                }
                # Read/list tools remain useful to every role.  Effectful
                # tools must appear in the selected board profile as well as
                # the role definition.
                agent_tools = {
                    tool
                    for tool in agent_tools
                    if tool.startswith(("read_", "list_")) or tool in profile_tools
                }
            elif self.board_catalog is not None:
                # An unprofiled board may be researched, but no board-specific
                # build/upload/measurement adapter is inferred from prose.
                agent_tools = {
                    tool for tool in agent_tools if tool.startswith(("read_", "list_"))
                }
            resources: dict[str, Any] = {
                "preflight": preview,
                "selected_contract": selected if isinstance(selected, dict) else None,
                "contract_resolution": contract_resolution,
                "execution_mode": mode,
                "approval_required": preview.get("approval_required"),
                "search_policy": search_policy,
                "available_execution_agents": execution_agents,
                "board_development": board_context,
                "board_catalog_sha256": (
                    self.board_catalog.digest if self.board_catalog is not None else None
                ),
                "available_execution_skills": sorted(
                    {
                        skill
                        for agent in engine.agents
                        if agent.name in execution_agents
                        for skill in agent.skills
                    }
                ),
                "available_tools": sorted(agent_tools),
            }
            if self.capability_catalog is not None and mode in ("offline", "hardware"):
                resources["capability_catalog_sha256"] = self.capability_catalog.digest
                resources["available_capabilities"] = self.capability_catalog.for_mode(mode)
                planning_rows = self.capability_catalog.for_planning(mode)
                resources["deferred_hardware_capabilities"] = [
                    row for row in planning_rows
                    if row.get("mode") == "hardware" and not row.get("available_now")
                ]
            selected_scope = self.default_knowledge_scope
            if isinstance(selected_board, dict):
                base = (
                    self.default_knowledge_scope.to_dict()
                    if self.default_knowledge_scope is not None
                    else {}
                )
                base.update(
                    {
                        "hardware_id": selected_board.get("board_id"),
                        "revision": selected_board.get("revision"),
                    }
                )
                selected_scope = KnowledgeScope.parse(base)
            if selected_scope is not None:
                resources["knowledge_scope"] = selected_scope.to_dict()
            if self.knowledge_retriever is not None:
                scope = selected_scope or KnowledgeScope()
                resources["knowledge_context"] = self.knowledge_retriever.retrieve(
                    scenario, scope, limit=32
                ).to_dict()
            job = start_mission(
                self.ledger,
                objective,
                scenario,
                resources=resources,
            )
        except (
            J.JobLedgerError,
            PreflightError,
            KnowledgeStoreError,
            CapabilityCatalogError,
            BoardProfileError,
            ValueError,
        ) as exc:
            raise ControlError(str(exc)) from exc
        return {"job": job.describe(), "preflight": preview}

    def promotion_preview(self, job_id: str) -> dict[str, Any]:
        """Compile the exact candidate a person would approve, without writing it."""

        try:
            return self._contract_promoter().preview(job_id).describe()
        except (PromotionError, JobLedgerError) as exc:
            raise ControlError(str(exc)) from exc

    def promotion_request(self, job_id: str, requested_by: str) -> dict[str, Any]:
        """Put the exact candidate digests in the existing durable approval inbox."""

        try:
            proposal, approval = self._contract_promoter().request(
                job_id, requested_by=requested_by
            )
        except (PromotionError, JobLedgerError) as exc:
            raise ControlError(str(exc)) from exc
        return {"proposal": proposal.describe(), "approval": approval.describe()}

    def promotion_apply(self, approval_id: str) -> dict[str, Any]:
        """Install only bytes covered by an already-granted human decision."""

        try:
            proposal = self._contract_promoter().apply(approval_id)
        except (PromotionError, JobLedgerError) as exc:
            raise ControlError(str(exc)) from exc
        return {"proposal": proposal.describe(), "installed": True}

    def _preflight_engine(self) -> ScenarioPreflight:
        if self.preflight_engine is None:
            self.preflight_engine = ScenarioPreflight()
        return self.preflight_engine

    def _knowledge_store(self) -> KnowledgeStore:
        if self.knowledge_store is None:
            raise ControlError("the knowledge store is not configured on this daemon")
        return self.knowledge_store

    def _knowledge_retriever(self) -> KnowledgeRetriever:
        if self.knowledge_retriever is None:
            raise ControlError("the knowledge retriever is not configured on this daemon")
        return self.knowledge_retriever

    def _contract_promoter(self) -> ContractPromotionService:
        if self.contract_promoter is None:
            self.contract_promoter = ContractPromotionService(
                self.ledger, self._preflight_engine().contracts_dir
            )
        return self.contract_promoter

    def approve(
        self,
        approval_id: str,
        granted: bool,
        decided_by: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Record what a person decided. It authorises nothing on its own.

        The hardware gate reads this back and re-hashes the artifacts before
        any board-touching call, so a grant here is a decision, not a key.
        """
        if not decided_by.strip():
            raise ControlError("an approval decision must name who made it")
        try:
            approval = self.ledger.decide_approval(
                approval_id, granted, decided_by, note=note
            )
        except JobLedgerError as exc:
            raise ControlError(str(exc)) from exc
        return {"approval": approval.describe()}

    def cancel(self, job_id: str, cancelled_by: str, reason: str = "") -> dict[str, Any]:
        """Stop a job for good, taking its outstanding requests with it."""
        if not cancelled_by.strip():
            raise ControlError("a cancellation must name who asked for it")
        try:
            job = self.ledger.job(job_id)
        except JobLedgerError as exc:
            raise ControlError(str(exc)) from exc
        if job.state == J.DONE:
            raise ControlError(f"job {job_id} already finished as {job.outcome}")

        withdrawn = []
        for approval in job_approvals(self.ledger, job_id):
            if approval.state not in (J.APPROVAL_PENDING, J.APPROVAL_GRANTED):
                continue
            self.ledger.withdraw_approval(
                approval.approval_id, cancelled_by, f"job cancelled: {reason}".strip()
            )
            withdrawn.append(approval.approval_id)

        self.ledger.append_event(
            job_id, CANCEL, {"by": cancelled_by, "reason": reason, "withdrawn": withdrawn},
            cancelled_by,
        )
        closed = self.ledger.transition(
            job_id,
            J.DONE,
            "cancelled",
            reason=f"cancelled by {cancelled_by}: {reason}".strip(),
            agent=cancelled_by,
        )
        return {"job": closed.describe(), "withdrawn": withdrawn}

    def steer(self, job_id: str, message: str, sent_by: str) -> dict[str, Any]:
        """Leave a message for whichever agent runs next on this job."""
        if not message.strip():
            raise ControlError("a steering message with no content steers nothing")
        if not sent_by.strip():
            raise ControlError("a steering message must name who sent it")
        try:
            job = self.ledger.job(job_id)
        except JobLedgerError as exc:
            raise ControlError(str(exc)) from exc
        if job.state == J.DONE:
            raise ControlError(f"job {job_id} finished as {job.outcome}; there is nobody to steer")

        event = self.ledger.append_event(
            job_id, STEER, {"message": message.strip(), "by": sent_by}, sent_by
        )
        pending = pending_steers(self.ledger, job_id)
        return {
            "event_id": event.event_id,
            "pending": pending,
            # Whether an agent will actually be given this. A job that is
            # blocked or awaiting a person still has agents ahead of it; one
            # whose current iteration already has a terminal verdict does not,
            # and the message will reach the report as context instead of the
            # next agent as direction.
            "will_be_delivered": will_deliver_steering(
                self.ledger, job_id, self._max_iterations()
            ),
        }

    def revise_mission(
        self, job_id: str, message: str, requested_by: str
    ) -> dict[str, Any]:
        """Open a new mission whose first input is feedback on a completed one.

        A revision is not steering: there is nobody left to steer after the
        mission has produced its reviewed contract.  It also must not mutate
        the signed review in place.  The new Brief therefore preserves the
        old scenario and resources, names its parent, and carries the human's
        requested change as a new durable input.
        """
        if not message.strip():
            raise ControlError("a revision request must say what should change")
        if not requested_by.strip():
            raise ControlError("a revision request must name who requested it")
        try:
            source = self.ledger.job(job_id)
        except JobLedgerError as exc:
            raise ControlError(str(exc)) from exc
        if source.gate != MISSION_GATE:
            raise ControlError(f"job {job_id} is not a mission")
        if source.state != J.DONE:
            raise ControlError(
                f"job {job_id} is still {source.state}; steer it instead of revising it"
            )
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        if len(briefs) != 1:
            raise ControlError("the source mission has no unambiguous Brief")
        brief = briefs[0].payload
        scenario = brief.get("scenario")
        resources = brief.get("resources")
        if not isinstance(scenario, str) or not isinstance(resources, dict):
            raise ControlError("the source mission Brief is malformed")
        revised_resources = dict(resources)
        revised_resources["revision_of"] = job_id
        revised_resources["review_feedback"] = {
            "message": message.strip(),
            "by": requested_by.strip(),
        }
        try:
            revised = start_mission(
                self.ledger,
                f"Revise: {source.objective}",
                scenario,
                resources=revised_resources,
            )
        except (JobLedgerError, ValueError) as exc:
            raise ControlError(str(exc)) from exc
        self.ledger.append_event(
            job_id,
            REVISION_REQUESTED,
            {
                "revision_job_id": revised.job_id,
                "message": message.strip(),
                "by": requested_by.strip(),
            },
            requested_by.strip(),
        )
        self.ledger.append_event(
            revised.job_id,
            REVISION_OF,
            {"parent_job_id": job_id},
            requested_by.strip(),
        )
        return {"job": revised.describe(), "revision_of": job_id}

    # -- dispatch --------------------------------------------------------

    def handle(self, request: Any) -> dict[str, Any]:
        """One request object to one response object. Never raises at a caller."""
        if not isinstance(request, dict):
            return _error("a request must be a JSON object")
        command = request.get("command")
        if not isinstance(command, str):
            return _error("a request must name a command")

        try:
            handler = getattr(self, f"_cmd_{command.replace('-', '_')}", None)
            if handler is None:
                known = sorted(
                    name[5:] for name in dir(self) if name.startswith("_cmd_")
                )
                return _error(f"unknown command {command!r}. Known: {', '.join(known)}")
            payload = handler(request)
        except ControlError as exc:
            return _error(str(exc))
        except JobLedgerError as exc:
            return _error(str(exc))
        except Exception as exc:  # noqa: BLE001 - a control plane must answer, not die
            return _error(f"{type(exc).__name__}: {exc}")
        return {"ok": True, "version": PROTOCOL_VERSION, **payload}

    def _cmd_ping(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True, "cursor": self.ledger.latest_event_id()}

    def _cmd_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"status": self.status(request.get("now"))}

    def _cmd_job(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"job": self.job(_required(request, "job_id"))}

    def _cmd_handoffs(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.handoffs(_required(request, "job_id"))

    def _cmd_events(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.events(
            int(request.get("since", 0)),
            request.get("job_id"),
            int(request.get("limit", 500)),
        )

    def _cmd_inbox(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.inbox(request.get("now"))

    def _cmd_remote_summary(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"snapshot": self.remote_snapshot(
            request.get("now"),
            _integer(request, "limit", 24, maximum=100),
        )}

    def _cmd_campaign_catalog(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.campaign_catalog()

    def _cmd_campaign_status(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"campaign": self.campaign_status()}

    def _cmd_campaign_preview(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"campaign": self.campaign_preview(
            _required(request, "filename"), _required(request, "sha256")
        )}

    def _cmd_campaign_activate(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"campaign": self.campaign_activate(
            _required(request, "job_id"),
            _required(request, "filename"),
            _required(request, "sha256"),
            _required(request, "approved_by"),
        )}

    def _cmd_campaign_deactivate(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"campaign": self.campaign_deactivate(
            _required(request, "requested_by"), str(request.get("reason", ""))
        )}

    def _cmd_campaign_bundle_preview(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"proposal": self.campaign_bundle_preview(
            _required(request, "job_id"),
            max_iterations=_integer(request, "max_iterations", 8, maximum=64),
            max_auto_approvals=_integer(
                request, "max_auto_approvals", 24, maximum=256
            ),
            duration_hours=_integer(request, "duration_hours", 12, maximum=72),
        )}

    def _cmd_campaign_bundle_approve(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.campaign_bundle_approve(
            _required(request, "job_id"),
            _required(request, "approved_by"),
            _required(request, "review_sha256"),
            max_iterations=_integer(request, "max_iterations", 8, maximum=64),
            max_auto_approvals=_integer(
                request, "max_auto_approvals", 24, maximum=256
            ),
            duration_hours=_integer(request, "duration_hours", 12, maximum=72),
            comment=str(request.get("comment", "")),
        )

    def _cmd_job_mission_revise(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.revise_mission(
            _required(request, "job_id"),
            _required(request, "message"),
            _required(request, "requested_by"),
        )

    def _cmd_knowledge_claim(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"claim": self.knowledge_claim(_required(request, "claim_id"))}

    def _cmd_knowledge_search(self, request: dict[str, Any]) -> dict[str, Any]:
        query = request.get("query", "")
        if not isinstance(query, str):
            raise ControlError("query must be a string")
        return {
            "context": self.knowledge_search(
                query,
                self._knowledge_scope(request.get("scope")),
                _integer(request, "limit", 20, maximum=64),
            )
        }

    def _cmd_knowledge_neighbors(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "neighbors": self.knowledge_neighbors(
                _required(request, "entity_key"),
                self._knowledge_scope(request.get("scope")),
                _integer(request, "limit", 20, maximum=100),
            )
        }

    def _cmd_knowledge_events(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "knowledge": self.knowledge_events(
                _integer(request, "since", 0, minimum=0, maximum=2**63 - 1),
                _integer(request, "limit", 500, maximum=10_000),
            )
        }

    def _knowledge_scope(self, value: Any) -> KnowledgeScope:
        if value is None:
            return self.default_knowledge_scope or KnowledgeScope()
        try:
            return KnowledgeScope.parse(value)
        except KnowledgeValidationError as exc:
            raise ControlError(str(exc)) from exc

    def _cmd_preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"preflight": self.preflight(_required(request, "scenario"))}

    def _cmd_start(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.start(
            _required(request, "gate"),
            _required(request, "objective"),
            _required(request, "scenario"),
            str(request.get("contract_sha256", "")),
            str(request.get("scenario_sha256", "")),
        )

    def _cmd_discover(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.discover(
            _required(request, "objective"),
            _required(request, "scenario"),
            _required(request, "scenario_sha256"),
        )

    def _cmd_mission(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.mission(
            _required(request, "objective"),
            _required(request, "scenario"),
            _required(request, "scenario_sha256"),
            str(request.get("search_policy", "refresh")),
        )

    def _cmd_contract_promotion_preview(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"proposal": self.promotion_preview(_required(request, "job_id"))}

    def _cmd_contract_promotion_request(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.promotion_request(
            _required(request, "job_id"),
            _required(request, "requested_by"),
        )

    def _cmd_contract_promotion_apply(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.promotion_apply(_required(request, "approval_id"))

    def _cmd_approve(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.approve(
            _required(request, "approval_id"),
            _boolean(request, "granted"),
            _required(request, "decided_by"),
            str(request.get("note", "")),
        )

    def _cmd_cancel(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.cancel(
            _required(request, "job_id"),
            _required(request, "cancelled_by"),
            str(request.get("reason", "")),
        )

    def _cmd_steer(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.steer(
            _required(request, "job_id"),
            _required(request, "message"),
            _required(request, "sent_by"),
        )

    def _cmd_shutdown(self, request: dict[str, Any]) -> dict[str, Any]:
        if self.on_shutdown is None:
            raise ControlError("this control plane is not attached to a service")
        self.on_shutdown()
        return {"stopping": True}

    def _campaign_controller(self) -> CampaignController:
        if self.campaign_controller is None:
            raise ControlError("campaign control is not configured in this daemon")
        return self.campaign_controller

    def _campaign_proposer(self) -> CampaignProposalService:
        if self.campaign_proposer is not None:
            return self.campaign_proposer
        controller = self._campaign_controller()
        self.campaign_proposer = CampaignProposalService(
            self.ledger,
            controller,
            project_root=controller.project_root,
            contracts_dir=controller.contracts_dir,
            campaigns_dir=controller.campaigns_dir,
            now=controller.now,
        )
        return self.campaign_proposer


def _boolean(request: dict[str, Any], key: str) -> bool:
    """A JSON boolean, or nothing.

    `bool(request.get(key))` accepted the string "false" as True, because every
    non-empty Python string is truthy. A UI that sent `"granted": "false"` --
    or a hand-written curl -- would have recorded a grant against a decline.
    An approval is the one field in this protocol where a coercion is not worth
    the convenience, so only a real boolean is taken.
    """
    value = request.get(key)
    if not isinstance(value, bool):
        raise ControlError(
            f"{key} must be a JSON boolean (true/false), got {type(value).__name__} "
            f"{value!r}. Strings are not coerced here: an approval decided by a "
            "coercion is not a decision."
        )
    return value


def _required(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ControlError(f"{key} is required and must be a non-empty string")
    return value.strip()


def _integer(
    request: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int,
) -> int:
    value = request.get(key, default)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ControlError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "version": PROTOCOL_VERSION, "error": message}


# -- steering delivery -------------------------------------------------------


# Handoffs whose author was handed the pending steering. Only these are proof a
# message was seen.
#
# The first version cleared on any `handoff.*`, which was wrong in a way that
# lost a person's words silently. A Verdict is written by `EvidenceReducer` --
# deterministic code that never reads a task, let alone a steering list -- so a
# steer sent while the loop was between "every step reported" and "verdict
# recorded" was marked delivered by a handoff nobody read it into. The message
# vanished, and the next agent planned as though it had never been sent.
#
# `handoff.Brief` is the human's own opening statement and predates any steer.
# `handoff.Report` is deliberately excluded too, for a different reason: the
# reporter is not given steering at all (see `undelivered_steers`).
DELIVERING_HANDOFFS = ("handoff.Plan", "handoff.StepResult")


def _is_delivery(event: Any) -> bool:
    return event.kind in DELIVERING_HANDOFFS


def pending_steers(ledger: JobLedger, job_id: str) -> list[dict[str, Any]]:
    """Steering messages no agent has been given yet.

    Derived from the log rather than a delivered-flag column, because two
    records of one fact part company the first time a process dies between
    them. The boundary is a handoff from an agent that *received* the steering
    -- a Plan or a StepResult -- not any handoff at all.

    Paged rather than capped: the first version read the oldest ten thousand
    events, so on a job past that bound the newest steer was invisible while
    older ones looked pending forever.
    """
    return [
        {"message": e.payload.get("message"), "by": e.payload.get("by"), "event_id": e.event_id}
        for e in tail_since(ledger, job_id, _is_delivery)
        if e.kind == STEER
    ]


# Verdict outcomes after which no further agent takes direction. `open` is the
# only one that sends the loop round again; the rest go to `_decide`, which
# finishes the job. The reporter runs in between and is deliberately not given
# steering, so a message arriving now reaches the record and nobody's plan.
TERMINAL_VERDICTS = ("satisfied", "exhausted", "primary_run_missing")


def will_deliver_steering(
    ledger: JobLedger, job_id: str, max_iterations: int | None = None
) -> bool:
    """Will an agent actually be handed a steer sent right now?

    Saying "delivered" when the loop has already decided is worse than saying
    nothing: a person walks away believing they redirected something that was
    already on its way to a report.

    Three ways the answer is no. The job is finished. The current iteration has
    a terminal verdict, so the only agent left is the reporter. Or the loop has
    reached its iteration ceiling, which ends it even on an `open` verdict --
    that last one is only knowable when the caller passes the ceiling in, which
    is why it is a parameter rather than a guess.
    """
    job = ledger.job(job_id)
    if job.state == J.DONE:
        return False

    verdicts = ledger.handoffs(job_id, "Verdict", job.iteration)
    if not verdicts:
        return True

    outcome = str(verdicts[-1].payload.get("outcome", ""))
    if outcome in TERMINAL_VERDICTS:
        return False
    if max_iterations is not None and job.iteration >= max_iterations:
        return False
    return True


def job_approvals(ledger: JobLedger, job_id: str) -> list[J.Approval]:
    """Every approval this job ever raised, in any state.

    Read through the log because the frozen ledger exposes pending requests and
    single lookups but no per-job listing of decided ones. Paged, for the same
    reason everything else here is.
    """
    seen: dict[str, J.Approval] = {}
    for event in iter_events(ledger, job_id):
        approval_id = event.payload.get("approval_id")
        if event.kind.startswith("approval.") and approval_id:
            seen[str(approval_id)] = ledger.approval(str(approval_id))
    return list(seen.values())


def undelivered_steers(ledger: JobLedger, job_id: str) -> list[dict[str, Any]]:
    """What a person said that no agent will now act on.

    The reporter is **not** given steering as instruction. A report describes
    what happened, and letting a message shape it after the evidence is in is
    the softening its own definition forbids. But the message was said, by a
    person, on the record -- so it goes to the reporter as something to write
    down under its own heading, and the loop marks it as context rather than
    direction.
    """
    return pending_steers(ledger, job_id)
