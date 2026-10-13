"""Web/daemon integration, without translating prose back into legacy Plans.

Model turns remain in the isolated development workspace. Hardware requests are
executed separately by a host-owned adapter after scenario and safety checks.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import threading
import uuid

from .conversation_flow import ConversationFlow
from .conversation_codex import ConversationCodexRuntime, ledger_event_sink
from .model_selection import selection_for


def prepare_intake(scenario, engine, **_unused):
    from .scenario_preflight import PreflightError
    if not isinstance(scenario,str) or not scenario.strip(): raise PreflightError("a scenario is required")
    return ({"status":"ready", "reason_code":"single_workflow", "detail":"Lead → Builder → Verifier로 프로젝트를 개발합니다.",
             "scenario":scenario, "scenario_sha256":hashlib.sha256(scenario.strip().encode()).hexdigest(),
             "execution_mode":"offline", "workflow":"conversation", "roles":["lead-agent","builder","verifier"], "approval_required":False}, {"intake_version":1})


class WorkspaceConversationRuntime:
    def __init__(self, ledger, manager, config, *, factory=ConversationCodexRuntime,
                 dry_run=False, role_models=None, knowledge_retriever=None, knowledge_scope=None):
        self.ledger, self.manager, self.config = ledger, manager, config
        self.factory, self.dry_run = factory, dry_run
        self.role_models = role_models or {}
        self.knowledge_retriever, self.knowledge_scope = knowledge_retriever, knowledge_scope
        self._active = {}
        self._lock = threading.Lock()

    def __call__(self, role, context):
        if self.dry_run:
            raise RuntimeError("dry-run: no model or device was executed")
        agent = "lead-agent" if role == "lead" else role
        choice = selection_for(context.get("inputs"), agent)
        config = replace(self.config, model=choice.get("model", self.role_models.get(agent)),
                         effort=choice.get("effort"))
        runtime = self.factory(config, event_sink=ledger_event_sink(self.ledger))
        job_id = context["job_id"]
        if self.knowledge_retriever is not None and self.knowledge_scope is not None:
            try:
                board = (context.get("inputs") or {}).get("selected_board")
                project = (context.get("inputs") or {}).get("project") or {}
                scope = replace(self.knowledge_scope, project_id=project.get("project_id", self.knowledge_scope.project_id),
                                hardware_id=board["board_id"] if board else None,
                                revision=board["revision"] if board else None)
                knowledge = self.knowledge_retriever.retrieve(
                    context['objective'][:2000], scope, limit=8).to_dict()
                store = getattr(self.knowledge_retriever, 'store', None)
                knowledge['unverified_notes'] = ([claim.to_dict() for claim in
                    store.search(context['objective'][:2000], scope=scope, statuses=('proposed',), limit=6)]
                    if store is not None else [])
                context = {**context, 'knowledge_context': knowledge}
                self.ledger.append_event(job_id, 'conversation.knowledge.loaded',
                                         {'claim_ids': [c['claim_id'] for c in knowledge['claims']]}, role)
            except Exception as exc:
                # Optional prior knowledge is not an entrance ticket to coding.
                self.ledger.append_event(job_id, 'conversation.knowledge.unavailable',
                                         {'error_type': type(exc).__name__}, role)
        task = {"job_id": job_id, "step_id": str(context["attempt_id"]),
                "resources": {"development_mode": "autonomous-v1"}}
        task["project"] = (context.get("inputs") or {}).get("project")
        workspace = self.manager(task)
        with self._lock:
            self._active[job_id] = runtime
        failure = None
        try:
            if str(workspace.resolve()) != context["workspace"]:
                raise ValueError("recorded workspace does not match the job snapshot")
            # A cancel can arrive while the source snapshot/audit is prepared.
            if self.ledger.job(job_id).state != "running":
                raise RuntimeError("job stopped before runtime dispatch")
            return runtime(role, context)
        except BaseException as exc:
            failure = exc
            raise
        finally:
            with self._lock:
                self._active.pop(job_id, None)
            try:
                audit = self.manager.audit(task, agent)
                self.ledger.append_event(job_id, "conversation.workspace.audited", audit, role)
            except Exception as exc:
                self.ledger.append_event(job_id, "conversation.workspace.audit_failed",
                                         {"error_type": type(exc).__name__, "error": str(exc)}, role)
                if failure is None:
                    raise

    def cancel(self, job_id):
        with self._lock:
            runtime = self._active.get(job_id)
        if runtime is not None:
            return runtime.cancel(job_id)
        return False



class WebConversationFlow(ConversationFlow):
    def __init__(self, ledger, runtime, manager):
        super().__init__(ledger, runtime)
        self.manager = manager

    def submit(self, objective, scenario, scenario_sha256, search_policy, selected_models,
               *, execution_mode="offline", device_tools=None, device_catalog=None, intake=None):
        # The host allocates identity before copying source. It does not import
        # historical contracts/approvals or invent executable tool parameters.
        job_id = "job_" + uuid.uuid4().hex[:12]
        project = (intake or {}).get("project")
        workspace = (self.manager.prepare_project(job_id, project) if hasattr(self.manager, "prepare_project")
                     else self.manager.prepare(job_id))
        web_search = getattr(getattr(self.runtime, "config", None), "web_search", "live")
        limitations = ["changes require human review before merge"]
        if web_search == "disabled":
            limitations.append("external research disabled by host configuration")
        if execution_mode == "offline":
            limitations.insert(0, "offline only")
        return self.create(objective, workspace, job_id=job_id, execution_mode=execution_mode, inputs={
            "scenario": scenario, "scenario_sha256": scenario_sha256,
            "model_selection": selected_models, "search_policy": search_policy,
            "available_sources": "Current project source, documentation, retrieved knowledge, and native web research when enabled.",
            "research": {"web_search": web_search, "source_preference": "official project documentation, primary references and manufacturer datasheets when relevant",
                         "source_notes": "Carry document title, URL and relevant findings in sources; reuse research_sources across turns."},
            "limitations": limitations,
            **(intake or {}),
        })

    def steer(self, job_id, message, sent_by, *, prepare=None):
        # Feedback cannot reopen cancelled jobs or replay an interrupted turn.
        events = self._events(job_id)
        replies = [e for e in events if e.kind == "conversation.turn.completed"]
        starts = [e for e in events if e.kind == "conversation.turn.started"]
        question = (self.ledger.job(job_id).state == "blocked" and replies and starts
                    and replies[-1].payload["turn_id"] == starts[-1].event_id
                    and replies[-1].payload["reply"]["status"] == "needs_user")
        self.feedback(job_id, message, actor=sent_by)
        feedback = [e for e in self._events(job_id) if e.kind == "conversation.feedback"]
        if question:
            self.ledger.transition(job_id, "running", reason="user answered; return context to Lead")
        event = feedback[-1]
        return {"event_id": event.event_id, "pending": [{"message": message, "by": sent_by}],
                "will_be_delivered": self.ledger.job(job_id).state in {"running", "pending"}}
