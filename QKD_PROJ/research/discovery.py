"""Durable two-agent discovery: scenario -> ResearchPlan -> SourcePacket."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from QKD_PROJ.agents.schema import AgentDefinition, load_all
from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.knowledge.models import KnowledgeScope

from .models import ResearchPlan, ResearchValidationError, SourcePacket, scenario_sha256

if TYPE_CHECKING:
    from QKD_PROJ.knowledge.ingest import KnowledgeIngestResult
    from QKD_PROJ.knowledge.retriever import KnowledgeContext


class DiscoveryError(RuntimeError):
    pass


AgentRunner = Callable[[AgentDefinition, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class DiscoveryResult:
    plan: ResearchPlan
    sources: SourcePacket
    resumed_plan: bool
    resumed_sources: bool
    knowledge_claim_ids: tuple[str, ...] = ()


class KnowledgeIngestor(Protocol):
    def ingest(
        self,
        packet: SourcePacket,
        scope: KnowledgeScope,
        *,
        source_job_id: str | None = None,
    ) -> KnowledgeIngestResult: ...


class KnowledgeContextRetriever(Protocol):
    def retrieve(self, query: str, scope: KnowledgeScope, **kwargs: Any) -> KnowledgeContext: ...


class DiscoveryPipeline:
    """Advance discovery from ledger state, never from model memory.

    A crash after the architect returns does not spend another architect turn:
    the next call reloads the recorded ResearchPlan and starts the researcher.
    Likewise a completed SourcePacket is returned without contacting either
    model again.
    """

    def __init__(
        self,
        ledger: JobLedger,
        runner: AgentRunner,
        *,
        agents: Sequence[AgentDefinition] | None = None,
        knowledge_ingestor: KnowledgeIngestor | None = None,
        knowledge_retriever: KnowledgeContextRetriever | None = None,
    ) -> None:
        self.ledger = ledger
        self.runner = runner
        definitions = list(agents or load_all())
        self._agents = {agent.name: agent for agent in definitions}
        for required in ("system-architect", "researcher"):
            if required not in self._agents:
                raise DiscoveryError(f"missing discovery agent definition: {required}")
        self.knowledge_ingestor = knowledge_ingestor
        self.knowledge_retriever = knowledge_retriever

    def run(
        self,
        job_id: str,
        scenario: str,
        *,
        resources: Sequence[str] = (),
        scope: KnowledgeScope | None = None,
    ) -> DiscoveryResult:
        job = self.ledger.job(job_id)
        if job.iteration != 0:
            raise DiscoveryError("discovery must finish before the development loop starts")
        digest = scenario_sha256(scenario)
        normalized_resources = self._resources(resources)
        if (
            self.knowledge_ingestor is not None or self.knowledge_retriever is not None
        ) and scope is None:
            raise DiscoveryError("a knowledge-enabled discovery run requires an explicit scope")
        knowledge_context = None
        if self.knowledge_retriever is not None and scope is not None:
            knowledge_context = self.knowledge_retriever.retrieve(scenario, scope).to_dict()

        recorded_plans = self.ledger.handoffs(job_id, "ResearchPlan", iteration=0)
        resumed_plan = bool(recorded_plans)
        if recorded_plans:
            try:
                plan = ResearchPlan.parse(recorded_plans[0].payload)
            except ResearchValidationError as exc:
                raise DiscoveryError(f"recorded ResearchPlan is invalid: {exc}") from exc
            if plan.scenario_sha256 != digest:
                raise DiscoveryError("recorded ResearchPlan belongs to a different scenario")
        else:
            task = {
                "kind": "DiscoveryBrief",
                "job_id": job_id,
                "gate": job.gate,
                "scenario": scenario,
                "scenario_sha256": digest,
                "resources": normalized_resources,
            }
            if knowledge_context is not None:
                task["knowledge_context"] = knowledge_context
            envelope = self.runner(
                self._agents["system-architect"],
                task,
            )
            try:
                plan = ResearchPlan.parse(envelope)
            except ResearchValidationError as exc:
                raise DiscoveryError(f"system architect returned an invalid plan: {exc}") from exc
            if plan.agent != "system-architect" or plan.scenario_sha256 != digest:
                raise DiscoveryError("system architect misattributed the ResearchPlan")
            self.ledger.record_handoff(
                job_id, "ResearchPlan", "system-architect", plan.to_dict(), iteration=0
            )

        recorded_packets = self.ledger.handoffs(job_id, "SourcePacket", iteration=0)
        resumed_sources = bool(recorded_packets)
        if recorded_packets:
            try:
                packet = SourcePacket.parse(recorded_packets[0].payload, plan=plan)
            except ResearchValidationError as exc:
                raise DiscoveryError(f"recorded SourcePacket is invalid: {exc}") from exc
        else:
            envelope = self.runner(
                self._agents["researcher"],
                {
                    "kind": "ResearchTask",
                    "job_id": job_id,
                    "gate": job.gate,
                    "scenario": scenario,
                    "research_plan": plan.to_dict(),
                    "resources": normalized_resources,
                },
            )
            try:
                packet = SourcePacket.parse(envelope, plan=plan)
            except ResearchValidationError as exc:
                raise DiscoveryError(f"researcher returned an invalid packet: {exc}") from exc
            if packet.agent != "researcher":
                raise DiscoveryError("researcher misattributed the SourcePacket")
            self.ledger.record_handoff(
                job_id, "SourcePacket", "researcher", packet.to_dict(), iteration=0
            )

        knowledge_claim_ids: tuple[str, ...] = ()
        if self.knowledge_ingestor is not None and scope is not None:
            try:
                ingested = self.knowledge_ingestor.ingest(
                    packet, scope, source_job_id=job_id
                )
            except Exception as exc:
                # SourcePacket is already durable.  A retry starts here and
                # spends no agent turn, so a broken knowledge projection does
                # not force the researcher to recreate its evidence.
                raise DiscoveryError(f"knowledge ingestion failed: {exc}") from exc
            knowledge_claim_ids = tuple(claim.claim_id for claim in ingested.claims)

        return DiscoveryResult(
            plan,
            packet,
            resumed_plan,
            resumed_sources,
            knowledge_claim_ids,
        )

    @staticmethod
    def _resources(values: Sequence[str]) -> list[str]:
        if isinstance(values, (str, bytes)) or len(values) > 64:
            raise DiscoveryError("resources must be a sequence of at most 64 paths or labels")
        result: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 1_000:
                raise DiscoveryError("every discovery resource must be a non-empty string")
            result.append(value.strip())
        return result
