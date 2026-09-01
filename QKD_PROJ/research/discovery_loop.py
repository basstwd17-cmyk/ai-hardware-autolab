"""One durable discovery action per daemon tick.

The older :mod:`discovery` pipeline is useful as a synchronous library seam.
This loop is the daemon form: its phase is derived only from handoffs, it
spends at most one model turn per ``advance`` call, and a process restart loses
no position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from QKD_PROJ.agents.schema import AgentDefinition, load_all, validate_handoff
from QKD_PROJ.capabilities.extension import derive_extension_plan
from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator import loop as L
from QKD_PROJ.orchestrator.jobs import JobLedger

from .architecture import ArchitectureDecision, ContractDraft, ContractReview
from .discovery import AgentRunner, KnowledgeIngestor
from .models import ResearchPlan, ResearchValidationError, SourcePacket, scenario_sha256
from .mission import ChangeAnalysis, MissionPlan, SEARCH_POLICIES


WORKFLOW = "discovery"
MISSION_WORKFLOW = "mission"
DISCOVERY_GATE = "__discovery__"
MISSION_GATE = "__mission__"

MISSION_PLAN = "mission_plan"
RESEARCH_PLAN = "research_plan"
RESEARCH = "research"
CHANGE = "change_analysis"
ARCHITECTURE = "architecture"
DRAFT = "contract_draft"
REVIEW = "contract_review"
DECIDE = "decide"
RETRY_HANDOFF = "retry_handoff"
HANDOFF_REJECTED_EVENT = "workflow.handoff.rejected"


class DiscoveryLoopError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoveryPhase:
    name: str
    handoff_kind: str | None = None


class DiscoveryLoop:
    """Advance a contract-gap job from question to reviewed proposal."""

    def __init__(
        self,
        ledger: JobLedger,
        runner: AgentRunner,
        *,
        agents: Sequence[AgentDefinition] | None = None,
        knowledge_ingestor: KnowledgeIngestor | None = None,
        knowledge_scope: KnowledgeScope | None = None,
        classify_failure: Callable[[BaseException], str] = L.default_failure_classifier,
        max_handoff_retries: int = 2,
    ) -> None:
        self.ledger = ledger
        self.runner = runner
        self.agents = {item.name: item for item in (agents or load_all())}
        self.knowledge_ingestor = knowledge_ingestor
        self.knowledge_scope = knowledge_scope
        self.classify_failure = classify_failure
        if max_handoff_retries < 0:
            raise DiscoveryLoopError("max_handoff_retries cannot be negative")
        self.max_handoff_retries = max_handoff_retries
        for name in (
            "mission-architect",
            "change-analyst",
            "system-architect",
            "researcher",
            "architecture-selector",
            "contract-drafter",
            "contract-reviewer",
        ):
            if name not in self.agents:
                raise DiscoveryLoopError(f"missing discovery agent definition: {name}")
        if self.knowledge_ingestor is not None and self.knowledge_scope is None:
            raise DiscoveryLoopError("knowledge ingestion requires an explicit KnowledgeScope")

    def owns(self, job_id: str) -> bool:
        brief = self._brief(job_id)
        resources = brief.get("resources") if isinstance(brief, dict) else None
        return isinstance(resources, dict) and resources.get("workflow") in (
            WORKFLOW,
            MISSION_WORKFLOW,
        )

    def _is_mission(self, job_id: str) -> bool:
        resources = self._brief(job_id).get("resources")
        return isinstance(resources, dict) and resources.get("workflow") == MISSION_WORKFLOW

    def phase(self, job_id: str) -> DiscoveryPhase:
        job = self.ledger.job(job_id)
        if job.state == J.DONE:
            return DiscoveryPhase(L.FINISHED)
        if job.state in (J.BLOCKED, J.AWAITING_APPROVAL):
            return DiscoveryPhase(L.BLOCKED)
        mission = self._is_mission(job_id)
        if mission and not self.ledger.handoffs(job_id, "MissionPlan", 0):
            return DiscoveryPhase(MISSION_PLAN, "MissionPlan")
        if not mission and not self.ledger.handoffs(job_id, "ResearchPlan", 0):
            return DiscoveryPhase(RESEARCH_PLAN, "ResearchPlan")
        if not self.ledger.handoffs(job_id, "SourcePacket", 0):
            return DiscoveryPhase(RESEARCH, "SourcePacket")
        mission_plan = self._mission(job_id) if mission else None
        # There is no honest "change from baseline" for a new board or design
        # with no baseline contract.  Do not spend an agent turn manufacturing
        # one merely because the old workflow was linear.
        if (
            mission_plan is not None
            and mission_plan.baseline_contract is not None
            and not self.ledger.handoffs(job_id, "ChangeAnalysis", 0)
        ):
            return DiscoveryPhase(CHANGE, "ChangeAnalysis")
        decisions = self.ledger.handoffs(job_id, "ArchitectureDecision", 0)
        if not decisions:
            return DiscoveryPhase(ARCHITECTURE, "ArchitectureDecision")
        decision = ArchitectureDecision.parse(decisions[-1].payload)
        if decision.status != "selected":
            return DiscoveryPhase(DECIDE)
        if self._board_gap(job_id) is not None:
            # Research may identify an architecture for a new board, but a
            # contract cannot honestly name build/upload tools until the
            # board profile binds them.  End with an explicit onboarding gap
            # instead of asking the drafter to invent an adapter.
            return DiscoveryPhase(DECIDE)
        # A sourced investigation can conclude that the exact reviewed
        # baseline remains the right authority.  In that case a new LLM draft
        # would only duplicate (and potentially weaken) the existing contract.
        if mission_plan is not None and mission_plan.contract_action == "reuse":
            return DiscoveryPhase(DECIDE)
        if not self.ledger.handoffs(job_id, "ContractDraft", 0):
            return DiscoveryPhase(DRAFT, "ContractDraft")
        if not self.ledger.handoffs(job_id, "ContractReview", 0):
            return DiscoveryPhase(REVIEW, "ContractReview")
        return DiscoveryPhase(DECIDE)

    def advance(self, job_id: str) -> L.Advance:
        job = self.ledger.job(job_id)
        if not self.owns(job_id):
            raise DiscoveryLoopError(f"job {job_id} is not a discovery workflow")
        phase = self.phase(job_id)
        if phase.name == L.FINISHED:
            return L.Advance(
                L.FINISHED,
                job_id,
                0,
                f"discovery job is done: {job.outcome}",
                outcome=job.outcome,
            )
        if phase.name == L.BLOCKED:
            return L.Advance(L.BLOCKED, job_id, 0, f"job is {job.state}", blocked=True)
        if job.state == J.PENDING:
            self.ledger.transition(job_id, J.RUNNING, reason="discovery started")
            job = self.ledger.job(job_id)
        if phase.name == MISSION_PLAN:
            return self._mission_plan(job)
        if phase.name == RESEARCH_PLAN:
            return self._research_plan(job)
        if phase.name == RESEARCH:
            return self._research(job)
        if phase.name == CHANGE:
            return self._change(job)
        if phase.name == ARCHITECTURE:
            return self._architecture(job)
        if phase.name == DRAFT:
            return self._draft(job)
        if phase.name == REVIEW:
            return self._review(job)
        return self._finish(job)

    def _research_plan(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        task: dict[str, Any] = {
            "kind": "DiscoveryBrief",
            "job_id": job.job_id,
            "scenario": scenario,
            "scenario_sha256": digest,
            "resources": resources,
            "steering": self._steering(job.job_id),
        }
        context = resources.get("knowledge_context")
        if isinstance(context, dict):
            task["knowledge_context"] = context
        result = self._call(
            job,
            "system-architect",
            task,
            lambda value: ResearchPlan.parse(value),
            "forming the research plan",
        )
        if isinstance(result, L.Advance):
            return result
        if result.scenario_sha256 != digest:
            return self._invalid(job, "ResearchPlan belongs to a different scenario")
        return self._record(job, result.kind, result.agent, result.to_dict(), RESEARCH_PLAN)

    def _mission_plan(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        available = {
            str(item)
            for item in resources.get("available_execution_agents", [])
            if isinstance(item, str)
        }
        available_skills = {
            str(item)
            for item in resources.get("available_execution_skills", [])
            if isinstance(item, str)
        }
        catalog_rows = resources.get("available_capabilities")
        available_capabilities = {
            str(item["id"])
            for item in catalog_rows
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        } if isinstance(catalog_rows, list) else None
        deferred_rows = resources.get("deferred_hardware_capabilities")
        available_deferred_hardware = {
            str(item["id"])
            for item in deferred_rows
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        } if isinstance(deferred_rows, list) else None
        context = resources.get("knowledge_context")
        known_claims = _claim_ids(context)
        selected = resources.get("selected_contract")
        selected_name = selected.get("name") if isinstance(selected, dict) else None
        search_policy = str(resources.get("search_policy") or "refresh")
        if search_policy not in SEARCH_POLICIES:
            return self._invalid(job, f"unknown mission search policy {search_policy!r}")
        task: dict[str, Any] = {
            "kind": "MissionBrief",
            "job_id": job.job_id,
            "scenario": scenario,
            "scenario_sha256": digest,
            "search_policy": search_policy,
            "resources": resources,
            "knowledge_context": context,
            "available_execution_agents": sorted(available),
            "available_execution_skills": sorted(available_skills),
            "available_capabilities": catalog_rows if isinstance(catalog_rows, list) else [],
            "deferred_hardware_capabilities": (
                deferred_rows if isinstance(deferred_rows, list) else []
            ),
            "steering": self._steering(job.job_id),
        }
        result = self._call(
            job,
            "mission-architect",
            task,
            lambda value: MissionPlan.parse(
                value,
                available_agents=available,
                available_skills=available_skills,
                available_capability_ids=available_capabilities,
                available_hardware_capability_ids=available_deferred_hardware,
                known_claim_ids=known_claims,
                selected_contract=selected_name if isinstance(selected_name, str) else None,
                required_search_policy=search_policy,
            ),
            "planning the mission",
        )
        if isinstance(result, L.Advance):
            return result
        if result.scenario_sha256 != digest:
            return self._invalid(job, "MissionPlan belongs to a different scenario")
        recorded = self._record(job, result.kind, result.agent, result.to_dict(), MISSION_PLAN)
        try:
            extension = derive_extension_plan(result, resources)
        except ValueError as exc:
            return self._invalid(job, f"MissionPlan extension is invalid: {exc}")
        self.ledger.append_event(job.job_id, "capability.plan", extension.describe(), "platform")
        return recorded

    def _research(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        plan = self._plan(job.job_id)
        mission = self._mission(job.job_id) if self._is_mission(job.job_id) else None
        if mission is not None:
            problem = self._ingest_mission(job, mission)
            if problem is not None:
                return problem
        result = self._call(
            job,
            "researcher",
            {
                "kind": "ResearchTask",
                "job_id": job.job_id,
                "scenario": scenario,
                "scenario_sha256": digest,
                "research_plan": plan.to_dict(),
                **({"mission_plan": mission.to_dict()} if mission is not None else {}),
                "resources": resources,
                "steering": self._steering(job.job_id),
            },
            lambda value: SourcePacket.parse(value, plan=plan),
            "collecting sources",
        )
        if isinstance(result, L.Advance):
            return result
        return self._record(job, result.kind, result.agent, result.to_dict(), RESEARCH)

    def _change(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        mission = self._mission(job.job_id)
        plan = mission.as_research_plan()
        packet = self._packet(job.job_id, plan)
        problem = self._ingest_sources(job, packet)
        if problem is not None:
            return problem
        result = self._call(
            job,
            "change-analyst",
            {
                "kind": "ChangeTask",
                "job_id": job.job_id,
                "scenario": scenario,
                "scenario_sha256": digest,
                "mission_plan": mission.to_dict(),
                "source_packet": packet.to_dict(),
                "knowledge_context": resources.get("knowledge_context"),
                "selected_contract": resources.get("selected_contract"),
                "steering": self._steering(job.job_id),
            },
            lambda value: ChangeAnalysis.parse(value, mission=mission, packet=packet),
            "comparing the sourced change with the baseline",
        )
        if isinstance(result, L.Advance):
            return result
        return self._record(job, result.kind, result.agent, result.to_dict(), CHANGE)

    def _architecture(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        plan = self._plan(job.job_id)
        packet = self._packet(job.job_id, plan)
        mission = self._mission(job.job_id) if self._is_mission(job.job_id) else None
        analysis = (
            self._analysis(job.job_id, mission, packet)
            if mission is not None
            and self.ledger.handoffs(job.job_id, "ChangeAnalysis", 0)
            else None
        )
        problem = self._ingest_sources(job, packet)
        if problem is not None:
            return problem
        if analysis is not None:
            problem = self._ingest_change(job, analysis)
            if problem is not None:
                return problem
        result = self._call(
            job,
            "architecture-selector",
            {
                "kind": "ArchitectureTask",
                "job_id": job.job_id,
                "scenario": scenario,
                "scenario_sha256": digest,
                "research_plan": plan.to_dict(),
                "source_packet": packet.to_dict(),
                **({"mission_plan": mission.to_dict()} if mission is not None else {}),
                **({"change_analysis": analysis.to_dict()} if analysis is not None else {}),
                "knowledge_context": resources.get("knowledge_context"),
                "steering": self._steering(job.job_id),
            },
            lambda value: ArchitectureDecision.parse(value, plan=plan, packet=packet),
            "selecting an architecture",
        )
        if isinstance(result, L.Advance):
            return result
        return self._record(job, result.kind, result.agent, result.to_dict(), ARCHITECTURE)

    def _draft(self, job: J.Job) -> L.Advance:
        scenario, digest, resources = self._inputs(job.job_id)
        plan = self._plan(job.job_id)
        packet = self._packet(job.job_id, plan)
        decision = self._decision(job.job_id, plan, packet)
        problem = self._ingest_named(job, "ingest_architecture_decision", decision)
        if problem is not None:
            return problem
        mission = self._mission(job.job_id) if self._is_mission(job.job_id) else None
        analysis = (
            self._analysis(job.job_id, mission, packet)
            if mission is not None
            and self.ledger.handoffs(job.job_id, "ChangeAnalysis", 0)
            else None
        )
        available_agents = (
            set(mission.execution_agents) if mission is not None else set(self.agents)
        )
        available_tools = {
            str(item) for item in resources.get("available_tools", []) if isinstance(item, str)
        }
        result = self._call(
            job,
            "contract-drafter",
            {
                "kind": "ContractDraftTask",
                "job_id": job.job_id,
                "scenario": scenario,
                "scenario_sha256": digest,
                "architecture_decision": decision.to_dict(),
                "source_packet": packet.to_dict(),
                **({"mission_plan": mission.to_dict()} if mission is not None else {}),
                **({"change_analysis": analysis.to_dict()} if analysis is not None else {}),
                "available_agents": sorted(available_agents),
                "available_tools": sorted(available_tools),
                "steering": self._steering(job.job_id),
            },
            lambda value: ContractDraft.parse(
                value,
                decision=decision,
                packet=packet,
                available_agents=available_agents,
                available_tools=available_tools,
                available_skills=(set(mission.execution_skills) if mission is not None else None),
            ),
            "drafting a contract proposal",
        )
        if isinstance(result, L.Advance):
            return result
        return self._record(job, result.kind, result.agent, result.to_dict(), DRAFT)

    def _review(self, job: J.Job) -> L.Advance:
        plan = self._plan(job.job_id)
        packet = self._packet(job.job_id, plan)
        decision = self._decision(job.job_id, plan, packet)
        mission = self._mission(job.job_id) if self._is_mission(job.job_id) else None
        analysis = (
            self._analysis(job.job_id, mission, packet)
            if mission is not None
            and self.ledger.handoffs(job.job_id, "ChangeAnalysis", 0)
            else None
        )
        draft = ContractDraft.parse(
            self.ledger.handoffs(job.job_id, "ContractDraft", 0)[-1].payload,
            decision=decision,
            packet=packet,
        )
        problem = self._ingest_named(job, "ingest_contract_draft", draft)
        if problem is not None:
            return problem
        result = self._call(
            job,
            "contract-reviewer",
            {
                "kind": "ContractReviewTask",
                "job_id": job.job_id,
                "architecture_decision": decision.to_dict(),
                "source_packet": packet.to_dict(),
                "contract_draft": draft.to_dict(),
                **({"mission_plan": mission.to_dict()} if mission is not None else {}),
                **({"change_analysis": analysis.to_dict()} if analysis is not None else {}),
                "draft_sha256": draft.sha256,
                "steering": self._steering(job.job_id),
            },
            lambda value: ContractReview.parse(value, draft=draft),
            "reviewing the contract proposal",
        )
        if isinstance(result, L.Advance):
            return result
        return self._record(job, result.kind, result.agent, result.to_dict(), REVIEW)

    def _finish(self, job: J.Job) -> L.Advance:
        decisions = self.ledger.handoffs(job.job_id, "ArchitectureDecision", 0)
        decision = ArchitectureDecision.parse(decisions[-1].payload)
        reviews = self.ledger.handoffs(job.job_id, "ContractReview", 0)
        if decision.status != "selected":
            reason = f"architecture ended as {decision.status}; research or scope must change"
        elif self._board_gap(job.job_id) is not None:
            reason = self._board_gap(job.job_id) or "board adapter is not ready"
        elif self._is_mission(job.job_id) and self._mission(job.job_id).contract_action == "reuse":
            reason = "the sourced architecture selected the existing reviewed contract"
        elif reviews:
            review = ContractReview.parse(reviews[-1].payload)
            problem = self._ingest_named(job, "ingest_contract_review", review)
            if problem is not None:
                return problem
            reason = (
                "contract proposal is ready for a person's review"
                if review.outcome == "ready_for_human_review"
                else f"contract proposal review ended as {review.outcome}"
            )
        else:
            reason = "discovery stopped before a contract proposal was reviewed"
        closed = self.ledger.transition(job.job_id, J.DONE, "contract_gap", reason=reason)
        return L.Advance(
            L.FINISHED,
            job.job_id,
            0,
            reason,
            outcome=closed.outcome,
        )

    def _board_gap(self, job_id: str) -> str | None:
        resources = self._brief(job_id).get("resources")
        if not isinstance(resources, dict):
            return None
        development = resources.get("board_development")
        if not isinstance(development, dict):
            return None
        if development.get("status") == "profile_required":
            return (
                "board_profile_required: research completed, but the exact board/revision "
                "still needs a digest-bound build, upload and scope adapter profile"
            )
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
        if not gaps:
            return None
        return (
            f"board_adapter_required: {selected.get('board_id')}@{selected.get('revision')} "
            f"still needs reviewed adapters for {', '.join(gaps)}"
        )

    def _call(self, job: J.Job, agent_name: str, task: dict[str, Any], parser: Any, what: str):
        agent = self.agents[agent_name]
        rejections = self._handoff_rejections(job.job_id, agent_name, what)
        if rejections:
            task = dict(task)
            task["handoff_repair"] = {
                "attempt": len(rejections) + 1,
                "validation_error": rejections[-1].payload.get("detail"),
                "instruction": (
                    "Return the same handoff again, correcting only the schema or linkage "
                    "error. Do not weaken criteria or invent evidence."
                ),
            }
        try:
            envelope = self.runner(agent, task)
        except Exception as exc:  # runtime failure cannot become a design conclusion
            return self._block(job, self.classify_failure(exc), f"{agent_name} could not run while {what}: {exc}")
        if not isinstance(envelope, dict):
            return self._invalid(
                job,
                f"{agent_name} returned {type(envelope).__name__}, not an object",
                retry_key=(agent_name, what),
            )
        stamped = dict(envelope)
        stamped.setdefault("kind", agent.emits[0])
        stamped.setdefault("agent", agent.name)
        if stamped.get("kind") != agent.emits[0] or stamped.get("agent") != agent.name:
            return self._invalid(
                job,
                f"{agent_name} misattributed its {agent.emits[0]} handoff",
                retry_key=(agent_name, what),
            )
        try:
            validate_handoff(stamped)
            return parser(stamped)
        except (ResearchValidationError, ValueError) as exc:
            return self._invalid(
                job,
                f"{agent_name} returned an invalid handoff while {what}: {exc}",
                retry_key=(agent_name, what),
            )

    def _record(
        self,
        job: J.Job,
        kind: str,
        agent: str,
        payload: dict[str, Any],
        action: str,
    ) -> L.Advance:
        handoff = self.ledger.record_handoff(
            job.job_id, kind, agent, payload, iteration=0
        )
        return L.Advance(action, job.job_id, 0, f"recorded {kind}", handoff.handoff_id)

    def _block(self, job: J.Job, code: str, detail: str) -> L.Advance:
        reason = f"{code}: {detail}"[:4_000]
        self.ledger.transition(job.job_id, J.BLOCKED, reason=reason)
        return L.Advance(
            L.BLOCKED,
            job.job_id,
            0,
            reason,
            blocked=True,
            reason_code=code,
        )

    def _invalid(
        self,
        job: J.Job,
        detail: str,
        retry_key: tuple[str, str] | None = None,
    ) -> L.Advance:
        if retry_key is not None:
            agent_name, what = retry_key
            attempt = len(self._handoff_rejections(job.job_id, agent_name, what)) + 1
            self.ledger.append_event(
                job.job_id,
                HANDOFF_REJECTED_EVENT,
                {
                    "agent": agent_name,
                    "phase": what,
                    "attempt": attempt,
                    "detail": detail[:4_000],
                },
                "platform",
            )
            if attempt <= self.max_handoff_retries:
                return L.Advance(
                    RETRY_HANDOFF,
                    job.job_id,
                    0,
                    f"invalid handoff; correction attempt {attempt}/{self.max_handoff_retries}",
                    reason_code=L.BLOCKED_INVALID,
                )
        return self._block(job, L.BLOCKED_INVALID, detail)

    def _handoff_rejections(
        self, job_id: str, agent_name: str, what: str
    ) -> list[J.Event]:
        rows: list[J.Event] = []
        cursor = 0
        while True:
            page = self.ledger.events_since(cursor, job_id, 500)
            if not page:
                return rows
            for event in page:
                if (
                    event.kind == HANDOFF_REJECTED_EVENT
                    and event.payload.get("agent") == agent_name
                    and event.payload.get("phase") == what
                ):
                    rows.append(event)
            cursor = page[-1].event_id

    def _brief(self, job_id: str) -> dict[str, Any]:
        rows = self.ledger.handoffs(job_id, "Brief", 0)
        return rows[-1].payload if rows else {}

    def _inputs(self, job_id: str) -> tuple[str, str, dict[str, Any]]:
        brief = self._brief(job_id)
        scenario = str(brief.get("scenario") or "")
        resources = brief.get("resources")
        if not isinstance(resources, dict):
            raise DiscoveryLoopError("discovery Brief.resources must be an object")
        digest = scenario_sha256(scenario)
        expected = resources.get("scenario_sha256")
        if expected != digest:
            raise DiscoveryLoopError("discovery Brief scenario digest does not match its text")
        return scenario, digest, resources

    def _plan(self, job_id: str) -> ResearchPlan:
        if self._is_mission(job_id):
            return self._mission(job_id).as_research_plan()
        return ResearchPlan.parse(self.ledger.handoffs(job_id, "ResearchPlan", 0)[-1].payload)

    def _mission(self, job_id: str) -> MissionPlan:
        return MissionPlan.parse(
            self.ledger.handoffs(job_id, "MissionPlan", 0)[-1].payload
        )

    def _packet(self, job_id: str, plan: ResearchPlan) -> SourcePacket:
        return SourcePacket.parse(
            self.ledger.handoffs(job_id, "SourcePacket", 0)[-1].payload,
            plan=plan,
        )

    def _decision(
        self, job_id: str, plan: ResearchPlan, packet: SourcePacket
    ) -> ArchitectureDecision:
        return ArchitectureDecision.parse(
            self.ledger.handoffs(job_id, "ArchitectureDecision", 0)[-1].payload,
            plan=plan,
            packet=packet,
        )

    def _analysis(
        self, job_id: str, mission: MissionPlan, packet: SourcePacket
    ) -> ChangeAnalysis:
        return ChangeAnalysis.parse(
            self.ledger.handoffs(job_id, "ChangeAnalysis", 0)[-1].payload,
            mission=mission,
            packet=packet,
        )

    def _ingest_mission(self, job: J.Job, mission: MissionPlan) -> L.Advance | None:
        method = getattr(self.knowledge_ingestor, "ingest_mission_plan", None)
        scope = self._knowledge_scope(job.job_id)
        if method is None or scope is None:
            return None
        try:
            method(mission, scope, source_job_id=job.job_id)
        except Exception as exc:
            return self._block(job, "knowledge_ingestion", str(exc))
        return None

    def _ingest_sources(self, job: J.Job, packet: SourcePacket) -> L.Advance | None:
        scope = self._knowledge_scope(job.job_id)
        if self.knowledge_ingestor is None or scope is None:
            return None
        try:
            self.knowledge_ingestor.ingest(
                packet, scope, source_job_id=job.job_id
            )
        except Exception as exc:
            return self._block(job, "knowledge_ingestion", str(exc))
        return None

    def _ingest_change(
        self, job: J.Job, analysis: ChangeAnalysis
    ) -> L.Advance | None:
        method = getattr(self.knowledge_ingestor, "ingest_change_analysis", None)
        scope = self._knowledge_scope(job.job_id)
        if method is None or scope is None:
            return None
        try:
            method(analysis, scope, source_job_id=job.job_id)
        except Exception as exc:
            return self._block(job, "knowledge_ingestion", str(exc))
        return None

    def _ingest_named(self, job: J.Job, method_name: str, envelope: Any) -> L.Advance | None:
        method = getattr(self.knowledge_ingestor, method_name, None)
        scope = self._knowledge_scope(job.job_id)
        if method is None or scope is None:
            return None
        try:
            method(envelope, scope, source_job_id=job.job_id)
        except Exception as exc:
            return self._block(job, "knowledge_ingestion", str(exc))
        return None

    def _knowledge_scope(self, job_id: str) -> KnowledgeScope | None:
        resources = self._brief(job_id).get("resources")
        raw = resources.get("knowledge_scope") if isinstance(resources, dict) else None
        if isinstance(raw, dict):
            return KnowledgeScope.parse(raw)
        return self.knowledge_scope

    def _steering(self, job_id: str) -> list[dict[str, Any]]:
        from QKD_PROJ.orchestrator.control import pending_steers

        return pending_steers(self.ledger, job_id)


def start_discovery(
    ledger: JobLedger,
    objective: str,
    scenario: str,
    *,
    resources: dict[str, Any] | None = None,
) -> J.Job:
    digest = scenario_sha256(scenario)
    merged = dict(resources or {})
    merged.update({"workflow": WORKFLOW, "scenario_sha256": digest})
    brief = {
        "kind": "Brief",
        "agent": "human",
        "scenario": scenario,
        "resources": merged,
    }
    validate_handoff(brief)
    return ledger.create_job(DISCOVERY_GATE, objective, brief=brief)


def start_mission(
    ledger: JobLedger,
    objective: str,
    scenario: str,
    *,
    resources: dict[str, Any] | None = None,
) -> J.Job:
    digest = scenario_sha256(scenario)
    merged = dict(resources or {})
    merged.update({"workflow": MISSION_WORKFLOW, "scenario_sha256": digest})
    brief = {
        "kind": "Brief",
        "agent": "human",
        "scenario": scenario,
        "resources": merged,
    }
    validate_handoff(brief)
    return ledger.create_job(MISSION_GATE, objective, brief=brief)


def _claim_ids(context: Any) -> set[str]:
    if not isinstance(context, dict) or not isinstance(context.get("claims"), list):
        return set()
    return {
        str(item["claim_id"])
        for item in context["claims"]
        if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
    }
