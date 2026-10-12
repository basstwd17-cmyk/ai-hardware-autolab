"""The default three-agent mission: Lead -> Builder -> Verifier.

The detailed discovery workflow remains readable for historical jobs. New
user scenarios take this shorter route and use the normal execution ledger,
approval gate and evidence reducer directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_hardware_autolab.agents.schema import validate_handoff
from ai_hardware_autolab.boards.profiles import BoardCatalog, BoardProfileError
from ai_hardware_autolab.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
from ai_hardware_autolab.capabilities.matcher import match_capabilities
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.research.discovery_loop import MISSION_GATE
from ai_hardware_autolab.research.models import scenario_sha256
from ai_hardware_autolab.orchestrator.mission_contract import MissionContract
from ai_hardware_autolab.orchestrator.source_context import source_context
from ai_hardware_autolab.knowledge.models import KnowledgeScope
from ai_hardware_autolab.knowledge.retriever import KnowledgeRetriever
from .development import MODE, autonomous


SIMPLE_MISSION_WORKFLOW = "simple-mission"


class SimpleMissionResourceRefresher:
    """Re-read deterministic catalogs between Lead iterations.

    A Builder may add and test a new board profile in iteration 1.  Once its
    verified workspace delta is safely published, iteration 2 must see that
    profile without asking the user to submit the same scenario again.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        knowledge_retriever: KnowledgeRetriever | None = None,
        default_knowledge_scope: KnowledgeScope | None = None,
        defer_agent_knowledge: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.knowledge_retriever = knowledge_retriever
        self.default_knowledge_scope = default_knowledge_scope
        self.defer_agent_knowledge = defer_agent_knowledge

    def __call__(self, scenario: str, resources: dict[str, Any]) -> dict[str, Any]:
        refreshed = dict(resources)
        refreshed["source_context"] = source_context(refreshed)
        mode = str(refreshed.get("execution_mode") or "offline")
        try:
            boards = BoardCatalog.load_default(self.project_root)
        except BoardProfileError as exc:
            refreshed["board_refresh_error"] = str(exc)
            return refreshed
        board = boards.planning_context(scenario)
        refreshed["board_development"] = board
        refreshed["board_catalog_sha256"] = boards.digest
        refreshed["source_context"] = source_context(refreshed)

        readable = {
            str(tool)
            for tool in refreshed.get("available_tools") or []
            if isinstance(tool, str) and tool.startswith(("read_", "list_"))
        }
        tools = readable | {"codex_workspace"}
        selected = board.get("selected")
        if isinstance(selected, dict):
            tools.update(
                str(stage["tool"])
                for stage in selected.get("stages") or []
                if isinstance(stage, dict)
                and stage.get("status") == "ready"
                and isinstance(stage.get("tool"), str)
                and (mode == "hardware" or stage.get("mode") != "hardware")
            )
            scope = dict(refreshed.get("knowledge_scope") or {})
            scope.update({
                "hardware_id": selected.get("board_id"),
                "revision": selected.get("revision"),
            })
            refreshed["knowledge_scope"] = scope
        refreshed["available_tools"] = sorted(tools)
        if autonomous(refreshed):
            # Catalog matches are useful context, not a device-selection gate.
            refreshed["available_tools"] = list(resources.get("available_tools") or ["codex_workspace"])
            board["advisory_only"] = True
            board["profile_required"] = False

        source = refreshed.get("capability_catalog_source")
        if isinstance(source, str) and source:
            try:
                catalog = CapabilityCatalog.load(
                    Path(source), project_root=self.project_root,
                    allow_partial=autonomous(refreshed),
                )
            except (CapabilityCatalogError, OSError) as exc:
                if autonomous(refreshed):
                    refreshed["available_capabilities"] = []
                    refreshed["capability_candidates"] = []
                    refreshed["capability_catalog_warning"] = str(exc)
                # Legacy ContractCompiler still performs its strict recheck.
            else:
                refreshed.pop("capability_catalog_warning", None)
                if catalog.withheld:
                    refreshed["capability_catalog_warning"] = "; ".join(
                        f"{identifier} withheld: {reason}" for identifier, reason in catalog.withheld
                    )
                refreshed["capability_catalog_sha256"] = catalog.digest
                refreshed["available_capabilities"] = catalog.for_mode(mode)
                rows = catalog.for_planning(mode)
                refreshed["capability_candidates"] = match_capabilities(
                    scenario,
                    rows,
                    board=selected if isinstance(selected, dict) else None,
                )
        if self.knowledge_retriever is not None and not (self.defer_agent_knowledge and autonomous(refreshed)):
            scope_data = refreshed.get("knowledge_scope")
            try:
                scope = (
                    KnowledgeScope.parse(scope_data)
                    if isinstance(scope_data, dict)
                    else self.default_knowledge_scope or KnowledgeScope()
                )
                refreshed["knowledge_context"] = self.knowledge_retriever.retrieve(
                    scenario,
                    scope,
                    limit=16,
                ).to_dict()
                refreshed["knowledge_scope"] = scope.to_dict()
                refreshed.pop("knowledge_refresh_error", None)
            except Exception as exc:  # noqa: BLE001 - context failure is observable, not authority
                # Knowledge is planning context, never execution authority. A
                # stale or unavailable map is made visible but does not erase
                # deterministic board/capability discovery performed above.
                refreshed["knowledge_refresh_error"] = f"{type(exc).__name__}: {exc}"
        return refreshed


def hardware_completion_gap(
    _job: J.Job,
    plan: dict[str, Any],
    resources: dict[str, Any],
) -> str | None:
    """A hardware scenario cannot finish on profile/code preparation alone."""
    if autonomous(resources):
        return None if plan.get("requirements_complete") is True else "Lead reports remaining scenario requirements"
    if resources.get("execution_mode") != "hardware":
        return None
    if any(
        isinstance(step, dict) and step.get("touches_hardware") is True
        for step in plan.get("steps") or []
    ):
        return None
    return "offline preparation passed, but the approved hardware stage has not run yet"


def start_simple_mission(
    ledger: JobLedger,
    objective: str,
    scenario: str,
    *,
    resources: dict[str, Any] | None = None,
) -> tuple[J.Job, bool]:
    """Create one mission, or return the existing non-terminal duplicate."""

    digest = scenario_sha256(scenario)
    for job in ledger.jobs():
        if job.state == J.DONE:
            continue
        briefs = ledger.handoffs(job.job_id, "Brief", 0)
        if not briefs:
            continue
        existing = briefs[-1].payload.get("resources")
        if (
            isinstance(existing, dict)
            and existing.get("workflow") == SIMPLE_MISSION_WORKFLOW
            and existing.get("scenario_sha256") == digest
        ):
            if existing.get("model_selection", {}) != (resources or {}).get("model_selection", {}):
                raise ValueError(
                    "같은 시나리오의 작업이 이미 열려 있고 모델 설정이 다릅니다. "
                    "기존 작업을 마치거나 취소한 뒤 새 모델로 시작하세요."
                )
            return job, True

    merged = dict(resources or {})
    execution_mode = str(merged.get("execution_mode") or "offline")
    mission = MissionContract.draft(scenario, objective, execution_mode)
    merged.update(
        {
            "workflow": SIMPLE_MISSION_WORKFLOW,
            "development_mode": merged.get("development_mode", MODE),
            "scenario_sha256": digest,
            "mission_id": mission.mission_id,
            "mission_contract": mission.describe(),
            "agent_flow": ["lead-agent", "builder", "verifier"],
            "lead_search_budget": int(merged.get("lead_search_budget", 12)),
        }
    )
    brief = {
        "kind": "Brief",
        "agent": "human",
        "scenario": scenario,
        "resources": merged,
    }
    merged["source_context"] = source_context(merged)
    validate_handoff(brief)
    return ledger.create_job(MISSION_GATE, objective, brief=brief), False


def owns_simple_mission(ledger: JobLedger, job_id: str) -> bool:
    briefs = ledger.handoffs(job_id, "Brief", 0)
    if not briefs:
        return False
    resources = briefs[-1].payload.get("resources")
    return isinstance(resources, dict) and resources.get("workflow") == SIMPLE_MISSION_WORKFLOW
