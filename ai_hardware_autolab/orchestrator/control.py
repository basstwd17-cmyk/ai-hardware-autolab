"""Local project/model commands and scenario/knowledge access."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Callable
from ai_hardware_autolab.knowledge.models import KnowledgeScope, KnowledgeValidationError
from ai_hardware_autolab.knowledge.retriever import KnowledgeRetriever
from ai_hardware_autolab.knowledge.store import KnowledgeStore, KnowledgeStoreError
from . import daemon as D, jobs as J
from .jobs import JobLedger, JobLedgerError
from .pagination import iter_events
from .remote_summary import build_remote_snapshot
from .scenario_preflight import PreflightError, ScenarioPreflight
from .model_selection import ModelCatalog
SEARCH_POLICIES = ("refresh", "always")
STEER="control.steer"
CANCEL="control.cancel"
PROTOCOL_VERSION=1
class ControlError(ValueError): pass
@dataclass
class ControlPlane:
    ledger: JobLedger
    daemon: D.Daemon | None = None
    on_shutdown: Callable[[], None] | None = None
    preflight_engine: ScenarioPreflight | None = None
    knowledge_store: KnowledgeStore | None = None
    knowledge_retriever: KnowledgeRetriever | None = None
    default_knowledge_scope: KnowledgeScope | None = None
    project_registry: Any | None = None
    model_catalog: ModelCatalog = field(default_factory=ModelCatalog)
    def __post_init__(self) -> None:
        if self.knowledge_retriever is None and self.knowledge_store is not None:
            self.knowledge_retriever = KnowledgeRetriever(self.knowledge_store)

    def _max_iterations(self) -> int | None:
        """The loop's ceiling, when there is a loop to ask."""
        loop = getattr(self.daemon, "loop", None)
        policy = getattr(loop, "policy", None)
        return getattr(policy, "max_iterations", None)

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
            # ``JobLedger.resume`` intentionally treats every RUNNING row as a
            # possible crash remnant because the ledger cannot see a process.
            # The live control plane can: the scheduler publishes the one job
            # whose turn it currently owns. Do not tell the UI that healthy
            # live work was abandoned by a previous daemon.
            state["interrupted"] = bool(
                state.get("interrupted") and not self.daemon.is_active(job_id)
            )
        state["pending_steers"] = pending_steers(self.ledger, job_id)
        state["will_deliver_steering"] = will_deliver_steering(
            self.ledger, job_id, self._max_iterations()
        )
        conversation = getattr(getattr(self.daemon, "loop", None), "conversation", None)
        if conversation is not None and conversation.owns(job_id):
            phase = conversation.phase(job_id)
            state["interrupted"] = phase.action == "interrupted"
            state["workflow"] = "conversation"
            state["phase"] = phase.action
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

    def mission(
        self, objective: str, scenario: str, scenario_sha256: str,
        search_policy: str = "refresh", *, model_selection=None, hardware_selection=None, project_id=None,
    ) -> dict[str, Any]:
        """All new scenarios enter one conversation; old jobs keep their Brief."""
        from .conversation_service import prepare_intake
        try:
            selected_models = self.model_catalog.validate(model_selection)
            if search_policy not in SEARCH_POLICIES:
                raise PreflightError(f"search_policy must be one of {list(SEARCH_POLICIES)}")
            engine = self._preflight_engine()
            engine.verify_scenario(scenario, scenario_sha256)
            if not scenario.strip() or not objective.strip():
                raise PreflightError("a scenario and objective are required")
            conversation = getattr(getattr(self.daemon, "loop", None), "conversation", None)
            if conversation is None:
                raise ControlError("신규 작업에는 대화 실행기가 필요합니다. 작업공간이 설정된 데몬으로 시작해주세요.")
            project = self.project_registry.resolve(project_id) if self.project_registry is not None else None
            if project_id is not None and project is None:
                raise ControlError("this daemon has no project registry")
            if hardware_selection is not None:
                raise PreflightError("bundled hardware adapters are not part of this platform")
            preview, inputs = prepare_intake(scenario, engine)
            if project is not None:
                inputs["project"] = project
                preview["project"] = project
            # A repeated submission reuses only a live conversation; never
            # reinterpret an old simple/discovery job as the new workflow.
            for existing in self.ledger.jobs():
                if existing.state == J.DONE or not conversation.owns(existing.job_id):
                    continue
                brief = conversation._brief(existing.job_id)
                old = brief.get("inputs") or {}
                if (old.get("scenario_sha256") == scenario_sha256
                        and old.get("model_selection") == selected_models
                        and old.get("hardware_selection") == inputs.get("hardware_selection")
                        and old.get("hardware_profile") == inputs.get("hardware_profile")
                        and old.get("search_policy") == search_policy
                        and old.get("project") == inputs.get("project")):
                    return {"job": existing.describe(), "preflight": preview, "reused": True,
                            "workflow": "conversation", "execution_mode": brief["execution_mode"]}
            job = conversation.submit(objective, scenario, scenario_sha256,
                search_policy, selected_models, execution_mode=preview["execution_mode"], intake=inputs)
            return {"job": job.describe(), "preflight": preview, "reused": False,
                    "workflow": "conversation", "execution_mode": preview["execution_mode"]}
        except (J.JobLedgerError, PreflightError, ValueError) as exc:
            raise ControlError(str(exc)) from exc

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
        resumed = False
        conversation = getattr(getattr(self.daemon, "loop", None), "conversation", None)
        if conversation is not None and conversation.owns(approval.job_id):
            resumed = False
        return {"approval": approval.describe(), "job_resumed": resumed}

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

    def _cmd_models_list(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"provider": "codex", "models": self.model_catalog.models()}
        except ValueError as exc:
            raise ControlError(str(exc)) from exc

    def _cmd_projects_catalog(self, request):
        if self.project_registry is None:
            raise ControlError("project connections are not configured")
        return self.project_registry.catalog()

    def _cmd_projects_register(self, request):
        if self.project_registry is None:
            raise ControlError("project connections are not configured")
        try:
            return {"project": self.project_registry.register(request.get("project"))}
        except (ValueError, OSError) as exc:
            raise ControlError(str(exc)) from exc

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
        return {"preflight": self.preflight(_required(request, "scenario"), hardware_selection=request.get("hardware_selection"))}

    def _cmd_mission(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.mission(
            _required(request, "objective"),
            _required(request, "scenario"),
            _required(request, "scenario_sha256"),
            str(request.get("search_policy", "refresh")),
            model_selection=request.get("model_selection"),
                        project_id=request.get("project_id"),
        )

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

    def preflight(self, scenario, **_unused):
        from .conversation_service import prepare_intake
        return prepare_intake(scenario, self._preflight_engine())[0]
    def steer(self, job_id, message, sent_by):
        if not isinstance(message,str) or not message.strip() or not isinstance(sent_by,str) or not sent_by.strip():
            raise ControlError("message and sender are required")
        if self.ledger.job(job_id).state == J.DONE: raise ControlError("job finished; there is nobody to steer")
        conversation = getattr(getattr(self.daemon,"loop",None),"conversation",None)
        if conversation is None or not conversation.owns(job_id): raise ControlError("unsupported scenario workflow")
        return conversation.steer(job_id,message.strip(),sent_by)

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

def pending_steers(ledger, job_id):
    events = list(iter_events(ledger,job_id))
    through = max((e.payload.get("feedback_through",0) for e in events if e.kind=="conversation.turn.completed"),default=0)
    return [{"message":e.payload.get("message"),"by":e.payload.get("by"),"event_id":e.event_id} for e in events if e.kind=="conversation.feedback" and e.event_id>through]
def will_deliver_steering(ledger, job_id, max_iterations=None):
    return ledger.job(job_id).state != J.DONE
