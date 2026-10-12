"""plan -> execute -> reduce -> record -> decide, one step at a time.

The loop is written as a function that does **one thing and returns**, not a
``while`` that runs to completion. ``advance()`` reads the ledger, works out
what has not happened yet, does that, and stops. Continuing across a
rate-limit window, a closed editor or a reboot is then just calling it again --
there is no in-memory position to lose, because there is no in-memory position.

That shape falls out of a rule the rest of the platform already follows: state
that only exists inside a process does not survive the process. So the phase is
**derived**, never stored:

    no Plan for this iteration          -> plan
    Plan has steps with no StepResult   -> execute the first of them
    every step reported, no Verdict     -> reduce and record
    Verdict recorded                    -> decide

A second column would be a second thing that can disagree with the handoffs.

Three boundaries this module does not cross.

**It never edits the ledger's schema.** Durable research progress is appended
through the public ``JobLedger`` API; state transitions and handoffs still use
their dedicated ledger methods. What a runtime
observes -- command execution, file changes, MCP calls, sub-agent activity --
is the adapter's vocabulary, and the adapter writes it. A "step dispatched"
event would be convenient here and it is not this module's to invent: the
resumable signal is the *absence* of a StepResult, which is derived and cannot
drift.

**It never decides whether a condition holds.** ``EvidenceReducer`` does that
from run records. The loop's job at the reduce phase is only to bind each
condition to the run the step actually produced, which is the one fact the
plan could not know in advance.

**A model never grants an approval.** A step whose agent touches hardware files
a durable request. By default it waits for a person. An optional deterministic
campaign policy may decide that request from a campaign digest a person
activated at daemon launch; the agent cannot edit or expand that policy.
Whether the agent touches hardware is read from its definition rather than
listed here, so the permission has one source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from ai_hardware_autolab.agents.schema import (
    AgentDefinition,
    ValidationError,
    load_all,
    validate_handoff,
    validate_plan_governance,
)
from ai_hardware_autolab.orchestrator.contract_compiler import (
    COMPILED_EVENT,
    SELECTED_EVENT,
    ContractCompileError,
    ContractCompiler,
)
from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator.evidence import Condition, Reduction, reduce_gate
from ai_hardware_autolab.orchestrator.jobs import JobLedger, JobLedgerError
from ai_hardware_autolab.orchestrator.clarification import USER_INPUT, pending_clarification, plan_question
from ai_hardware_autolab.orchestrator.pagination import iter_events
from ai_hardware_autolab.orchestrator.development import autonomous, normalize_plan, validate_development_plan
from ai_hardware_autolab.orchestrator.research_budget import (
    RESEARCH_PROGRESS,
    ResearchBudgetPolicy,
    ResearchBudgetState,
)

# Phases, in the order they run. Names match the handoff each produces, except
# `decide`, which produces a transition.
PLAN = "plan"
EXECUTE = "execute"
REDUCE = "reduce"
DECIDE = "decide"
REPORT = "report"
BLOCKED = "blocked"
FINISHED = "finished"

# What one call to advance() did.
ACTIONS = (PLAN, EXECUTE, REDUCE, DECIDE, REPORT, BLOCKED, FINISHED)

# The strike rule stops a loop that keeps failing the same way. This stops one
# that keeps failing in new ways -- which the strike rule cannot see, because
# every signature is different and the count never builds.
DEFAULT_MAX_ITERATIONS = 8

# Why a job stopped, in a form something other than a person can read. Both of
# these produce `job.blocked`; they are not the same problem. A runtime that
# died says nothing about the gate. A malformed envelope says the agent
# answered and the answer was unusable -- which is a defect in the agent or its
# runner, and is fixed somewhere else entirely.
BLOCKED_RUNTIME = "runtime_failure"
BLOCKED_INVALID = "invalid_handoff"

# A third kind, supplied by whoever knows how to recognise it. A rate limit is
# a runtime failure that will pass on its own, and a scheduler needs to tell it
# from one that will not -- so the classification is a policy the caller
# provides rather than a guess made here.
BLOCKED_RATE_LIMIT = "rate_limited"


def default_failure_classifier(exc: BaseException) -> str:
    """Every runtime failure is permanent until something says otherwise."""
    return BLOCKED_RUNTIME

# Which envelope each role is supposed to return. The loop knows this; the
# agent is not asked to declare it, and an agent that declares a different one
# is disagreeing with the plan rather than informing it.
KIND_BY_ROLE = {
    "plan": "Plan",
    "build": "StepResult",
    "operate": "StepResult",
    "measure": "StepResult",
    "verify": "Verdict",
    "report": "Report",
}


class LoopError(RuntimeError):
    """The loop will not take the next step, and why."""


class AgentRunner(Protocol):
    """Whatever actually runs an agent and returns its envelope.

    The seam between this loop and a runtime. An implementation spawns
    ``codex exec`` or ``claude -p``, hands over the agent definition and the
    task, and returns the JSON envelope the agent produced. It may log whatever
    runtime detail it observes; none of that vocabulary belongs in here.

    Raising is a legitimate answer. A runner that cannot reach its runtime
    should say so rather than fabricate an envelope, and the loop turns that
    into a blocked job rather than a failed condition -- the board told us
    nothing, so nothing may be concluded about it.
    """

    def __call__(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]: ...


class AutoApprovalPolicy(Protocol):
    """A deterministic authority activated outside the agent runtime."""

    def evaluate(self, job: J.Job, step: dict[str, Any], agent: AgentDefinition) -> Any: ...

    def apply(self, approval: J.Approval, evaluation: Any) -> J.Approval: ...


@dataclass(frozen=True)
class Advance:
    """One step of the loop, and what it changed."""

    action: str
    job_id: str
    iteration: int
    detail: str
    handoff_id: str | None = None
    approval_id: str | None = None
    outcome: str | None = None
    blocked: bool = False
    # `runtime_failure` or `invalid_handoff` when blocked. A caller deciding
    # what to retry needs to tell those apart without reading English.
    reason_code: str | None = None

    @property
    def done(self) -> bool:
        return self.action == FINISHED

    def describe(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class LoopPolicy:
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    strikes: int = J.DEFAULT_STRIKES
    report_on_finish: bool = True


# -- binding conditions to the runs that answered them -----------------------


def conditions_for(
    plan: dict[str, Any], step_results: dict[str, dict[str, Any]]
) -> list[Condition]:
    """Attach each advance condition to the run its step actually produced.

    A plan is written before anything runs, so its conditions name a field and
    an expectation but no run -- that is the one thing the architect could not
    know. Binding them is this loop's whole contribution to the verdict.

    Two cases are deliberately not resolved by choosing:

    * A step that never reported leaves its conditions with no run, which the
      reducer reports as ``evidence_missing``. Dropping them instead would let
      a gate close on the steps that happened to finish.
    * A step that produced several runs, with a condition that does not say
      which, raises. Picking the first would produce a defensible-looking
      number out of an under-specified plan.
    """
    conditions: list[Condition] = []
    for index, step in enumerate(plan.get("steps") or []):
        if not isinstance(step, dict):
            raise LoopError(f"Plan.steps[{index}] is not an object")
        step_id = str(step.get("id", ""))
        result = step_results.get(step_id)
        runs = list(result.get("runs") or []) if result else []

        for spec in step.get("advance_when") or []:
            if not isinstance(spec, dict):
                raise LoopError(f"advance_when entry of step {step_id} is not an object")
            named = spec.get("run")
            if named:
                run = str(named)
            elif len(runs) == 1:
                run = runs[0]
            elif not runs:
                run = None
            else:
                raise LoopError(
                    f"step {step_id} produced {len(runs)} runs and the condition "
                    f"{spec.get('field')!r} does not say which one it means. Name the run in "
                    "the condition rather than letting the loop pick."
                )
            conditions.append(
                Condition(
                    field=str(spec["field"]),
                    expect=spec["expect"],
                    source=str(spec.get("source", "")),
                    run=run,
                )
            )
    if not conditions:
        raise LoopError(
            "the plan carries no advance conditions, so nothing about this gate can be decided"
        )
    return conditions


def corroboration_for(plan: dict[str, Any]) -> list[dict[str, Any]]:
    entries = plan.get("corroboration") or []
    return [e for e in entries if isinstance(e, dict)]


# -- the loop ----------------------------------------------------------------


class Loop:
    """Drives one job forward, one call at a time."""

    def __init__(
        self,
        ledger: JobLedger,
        runner: AgentRunner,
        *,
        runs_dir: Path | None = None,
        policy: LoopPolicy | None = None,
        agents: Sequence[AgentDefinition] | None = None,
        classify_failure: Callable[[BaseException], str] = default_failure_classifier,
        auto_approver: AutoApprovalPolicy | None = None,
        scenario_approver: AutoApprovalPolicy | None = None,
        planner_agent: str = "gate-architect",
        verifier_agent: str = "verifier",
        reporter_agent: str = "reporter",
        require_plan_governance: bool = False,
        contract_compiler: ContractCompiler | None = None,
        repair_invalid_plans: bool = False,
        reasoning_verifier: bool = False,
        resource_refresher: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        completion_guard: Callable[[J.Job, dict[str, Any], dict[str, Any]], str | None] | None = None,
        knowledge_ingestor: Any | None = None,
        agent_knowledge: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        research_budget_policy: ResearchBudgetPolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.runner = runner
        self.runs_dir = Path(runs_dir) if runs_dir else None
        self.policy = policy or LoopPolicy()
        self.agents = {a.name: a for a in (agents if agents is not None else load_all())}
        # How a runtime failure is named. The loop knows a call failed; only the
        # caller knows whether this runtime's failures come back on their own.
        self.classify_failure = classify_failure
        self.auto_approver = auto_approver
        self.scenario_approver = scenario_approver
        self.planner_agent = planner_agent
        self.verifier_agent = verifier_agent
        self.reporter_agent = reporter_agent
        self.require_plan_governance = require_plan_governance
        self.contract_compiler = contract_compiler
        self.repair_invalid_plans = repair_invalid_plans
        self.reasoning_verifier = reasoning_verifier
        self.resource_refresher = resource_refresher
        self.completion_guard = completion_guard
        self.knowledge_ingestor = knowledge_ingestor
        self.agent_knowledge = agent_knowledge
        self.research_budget_policy = research_budget_policy or ResearchBudgetPolicy()
        # A campaign is authority for one exact job, so its iteration ceiling
        # must not become a process-wide mutable setting.  Keep the normal
        # daemon policy as the default and bind reviewed overrides by job id.
        self._campaign_iteration_limits: dict[str, int] = {}

    def set_campaign_iteration_limit(self, job_id: str, limit: int | None) -> None:
        """Bind or clear the reviewed iteration ceiling for one campaign job."""
        if limit is None:
            self._campaign_iteration_limits.pop(job_id, None)
            return
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("campaign iteration limit must be a positive integer")
        self._campaign_iteration_limits[job_id] = limit

    def iteration_limit(self, job_id: str) -> int:
        """The active campaign wins for its job; other jobs keep daemon policy."""
        return self._campaign_iteration_limits.get(job_id, self.policy.max_iterations)

    # -- reading where we are ------------------------------------------------

    def agent(self, name: str) -> AgentDefinition:
        try:
            return self.agents[name]
        except KeyError:
            raise LoopError(
                f"the plan names agent {name!r}, which has no definition. Known: "
                f"{', '.join(sorted(self.agents))}"
            ) from None

    def latest_plan(self, job_id: str, iteration: int) -> dict[str, Any] | None:
        plans = self.ledger.handoffs(job_id, "Plan", iteration)
        return plans[-1].payload if plans else None

    def step_results(self, job_id: str, iteration: int) -> dict[str, dict[str, Any]]:
        return {
            handoff.step_id: handoff.payload
            for handoff in self.ledger.handoffs(job_id, "StepResult", iteration)
        }

    def phase(self, job_id: str) -> str:
        """What has not happened yet for this iteration. Entirely derived."""
        job = self.ledger.job(job_id)
        if job.state == J.DONE:
            return FINISHED
        from .recovery import blocked_step, unfinished_dispatch
        if blocked_step(self.ledger, job_id) or unfinished_dispatch(self.ledger, job_id):
            return BLOCKED
        if job.state in (J.AWAITING_APPROVAL, J.BLOCKED):
            return BLOCKED
        if job.iteration == 0:
            return PLAN
        plan = self.latest_plan(job_id, job.iteration)
        if plan is None:
            return PLAN
        if plan_question(plan):
            return BLOCKED
        reported = self.step_results(job_id, job.iteration)
        brief = self.ledger.handoffs(job_id, "Brief", 0)
        # Phase selection needs only the persisted mode, not board/catalog
        # hashing and knowledge retrieval on every scheduler/UI poll.
        resources = brief[-1].payload.get("resources", {}) if brief else {}
        if autonomous(resources) and any(row.get("status") == "failed" for row in reported.values()):
            return DECIDE if self.ledger.handoffs(job_id, "Verdict", job.iteration) else REDUCE
        for step in plan.get("steps") or []:
            if str(step.get("id", "")) not in reported:
                return EXECUTE
        if not self.ledger.handoffs(job_id, "Verdict", job.iteration):
            return REDUCE
        return DECIDE

    # -- one step ------------------------------------------------------------

    def advance(self, job_id: str) -> Advance:
        """Do the next thing this job needs, then return.

        Nothing here loops. A caller that wants several steps calls again, and
        a caller that has run out of budget simply stops -- the ledger holds
        everything needed to pick it back up.
        """
        job = self.ledger.job(job_id)
        phase = self.phase(job_id)

        if phase == FINISHED:
            return Advance(FINISHED, job_id, job.iteration, f"job is done: {job.outcome}",
                           outcome=job.outcome)
        if phase == BLOCKED:
            return self._blocked(job)
        if phase == PLAN:
            return self._plan(job)
        if phase == EXECUTE:
            return self._execute(job)
        if phase == REDUCE:
            return self._reduce(job)
        return self._decide(job)

    def run(self, job_id: str, max_steps: int = 50) -> list[Advance]:
        """Call advance() until the job finishes, blocks, or the budget runs out.

        ``max_steps`` is a guard on this call, not on the job: stopping here
        leaves the job exactly where it was, and the next call continues.
        """
        history: list[Advance] = []
        for _ in range(max_steps):
            step = self.advance(job_id)
            history.append(step)
            if step.done or step.blocked:
                break
        return history

    # -- phases --------------------------------------------------------------

    def _blocked(self, job: J.Job) -> Advance:
        from .recovery import blocked_step, unfinished_dispatch
        stopped = blocked_step(self.ledger, job.job_id)
        dispatch = unfinished_dispatch(self.ledger, job.job_id)
        if stopped or dispatch:
            code = (stopped.payload.get('repair_target') or 'hardware_state_unknown') if stopped else 'hardware_state_unknown'
            detail = str(stopped.payload.get('summary') or 'Unresolved blocked step') if stopped else 'Device dispatch has no recorded outcome; reconcile before retry'
            if job.state != J.BLOCKED:
                return self._block(job, code, detail)
            return Advance(BLOCKED, job.job_id, job.iteration, detail, blocked=True, reason_code=code)
        question = pending_clarification(self.ledger, job.job_id)
        if question is not None:
            if question.answers:
                if job.iteration >= self.iteration_limit(job.job_id):
                    return self._finish(job, "exhausted", "iteration ceiling reached before re-planning after user input")
                if job.state == J.BLOCKED:
                    self.ledger.transition(job.job_id, J.RUNNING, reason="user input received; re-plan")
                # Retain the immutable question Plan. After a crash either this
                # question still exists or the next iteration has no Plan yet.
                iteration = self.ledger.begin_iteration(job.job_id)
                return Advance(PLAN, job.job_id, iteration, "user input received; next turn re-plans")
            if job.state == J.RUNNING:
                return self._block(job, USER_INPUT, question.question)
            return Advance(BLOCKED, job.job_id, job.iteration,
                           f"{USER_INPUT}: {question.question}", blocked=True, reason_code=USER_INPUT)
        pending = self.ledger.pending_approvals(job.job_id)
        if job.state == J.AWAITING_APPROVAL and not pending:
            # Every request was answered. Whether they were granted is checked
            # when the step is dispatched; the loop only resumes here.
            self.ledger.transition(job.job_id, J.RUNNING, reason="approval queue is empty")
            return Advance(EXECUTE, job.job_id, job.iteration, "approvals answered; resuming")
        detail = (
            f"waiting on {len(pending)} approval(s): "
            + ", ".join(a.approval_id for a in pending)
            if pending
            else f"job is {job.state}"
        )
        return Advance(
            BLOCKED,
            job.job_id,
            job.iteration,
            detail,
            approval_id=pending[0].approval_id if pending else None,
            blocked=True,
        )

    def _plan(self, job: J.Job) -> Advance:
        if job.state == J.PENDING:
            self.ledger.transition(job.job_id, J.RUNNING, reason="first plan")
        ceiling = self.iteration_limit(job.job_id)
        if job.iteration > ceiling:
            return self._finish(
                job,
                "exhausted",
                f"reached the {ceiling}-iteration ceiling without converging",
            )

        # phase() only routes here when the current iteration has no plan, so
        # the counter is advanced exactly once: at the start, and by _decide()
        # when it sends the loop round again.
        iteration = job.iteration or self.ledger.begin_iteration(job.job_id)
        architect = self.agent(self.planner_agent)
        briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
        brief = briefs[-1].payload if briefs else {}
        resources = self._resources(brief)
        task = {
            "kind": "Brief" if iteration == 1 else "Verdict",
            "job_id": job.job_id,
            "gate": job.gate,
            "mission_id": str(resources.get("mission_id") or job.gate),
            "objective": job.objective,
            # The objective is a short title. The durable Brief is the person's
            # actual direction and must reach the architect intact; otherwise
            # a UI-derived title silently becomes the whole assignment.
            "scenario": brief.get("scenario", job.objective),
            "resources": resources,
            "iteration": iteration,
            "strikes": self.ledger.strikes(job.job_id),
            "previous_verdict": self._previous_verdict(job.job_id, iteration),
            "previous_verifier_review": self._previous_verifier_review(
                job.job_id, iteration
            ),
            # The reducer says which conditions lack evidence; the StepResult
            # says why the tool produced none. Omitting the latter made the
            # architect repeat an identical build after a pre-run tool error.
            "previous_step_results": self._previous_step_results(
                job.job_id, iteration
            ),
            "steering": self._steering(job.job_id),
        }
        previous_plans = [h for h in self.ledger.handoffs(job.job_id, "Plan") if h.iteration < iteration]
        if previous_plans:
            previous = previous_plans[-1]
            # Keep the engineering context across model calls. Configuration
            # proposals are already immutable files referenced by the compiled
            # steps; do not resend their full YAML or the entire research dump.
            task["previous_plan"] = self._agent_plan(previous.payload)
            task["previous_plan"]["iteration"] = previous.iteration
        if previous_plans and plan_question(previous_plans[-1].payload):
            task["previous_clarification"] = previous_plans[-1].payload
        knowledge_context = resources.get("knowledge_context")
        if isinstance(knowledge_context, dict):
            # This is the exact bounded context selected when the job opened,
            # not a fresh query whose answer could change halfway through a
            # reproducible run.
            task["knowledge_context"] = knowledge_context
        if architect.native_web:
            research = ResearchBudgetState.from_events(
                iter_events(self.ledger, job_id=job.job_id)
            )
            budget = research.turn_limit(self.research_budget_policy)
            if budget < 1 and not autonomous(resources):
                return self._block(
                    job,
                    "research_budget_exhausted",
                    "the durable job research ceiling was reached; use the collected facts "
                    "or explicitly widen the campaign research budget",
                )
            task["research_plan"] = {
                "search_budget": budget,
                "instruction": "Use existing facts to plan; zero search budget disables browsing, not development.",
                "prior": research.describe(self.research_budget_policy),
                "seen_queries": sorted(research.query_keys),
                "seen_urls": sorted(research.urls),
                "stagnant": (
                    research.stagnant_rounds
                    >= self.research_budget_policy.max_stagnant_rounds
                ),
            }
        plan_events = list(iter_events(self.ledger, job.job_id))
        retries = [e for e in plan_events if e.kind == 'plan.retry.requested'
                   and e.payload.get('iteration') == iteration]
        if retries:
            request = retries[-1]
            if any(e.kind == 'plan.retry.started' and e.payload.get('request_id') == request.event_id
                   for e in plan_events):
                return self._block(job, 'plan_format', 'Planning retry was already attempted; inspect its result before another run')
            failures = [e for e in plan_events if e.kind == 'plan.format_failed'
                        and e.payload.get('iteration') == iteration]
            failure = failures[-1].payload if failures else {}
            # Old recorders clipped long final replies. Never pretend their head
            # is a complete Plan or silently decode it into an execution plan.
            task['plan_recovery'] = {
                'mode': 'replan_from_saved_context', 'original_response_available': bool(failure.get('raw_available')),
                'rejected_response': failure.get('raw_response', ''), 'error': failure.get('error', 'legacy planning JSON syntax failure'),
                'instruction': 'Use the existing workspace, scenario and known facts. Do not repeat web research. Produce a concise valid Plan; no device execution. The original scenario authority is unchanged.',
            }
            task['research_plan'] = {'search_budget': 0}
            self.ledger.append_event(job.job_id, 'plan.retry.started',
                                     {'iteration': iteration, 'request_id': request.event_id}, agent=architect.name)
        prepared = None
        if (autonomous(resources) and previous_plans
                and previous_plans[-1].payload.get("builder_preparation")
                and (task.get("previous_verdict") or {}).get("outcome") == "satisfied"
                and not task["steering"] and not retries):
            rows = task["previous_step_results"]
            if len(rows) == 1 and rows[0].get("status") == "done" and rows[0].get("runs"):
                prepared = rows[0].get("prepared_plan")
        if isinstance(prepared, dict):
            envelope = normalize_plan({**prepared, "kind": "Plan", "agent": architect.name,
                                       "gate": job.gate, "iteration": iteration}, resources)
            self.ledger.append_event(job.job_id, "plan.prepared.selected", {
                "iteration": iteration, "source_iteration": previous_plans[-1].iteration,
                "prepared_by": "builder", "validation_pending": True,
            }, agent="platform")
        else:
            envelope = self._call(
                architect, task, job, "planning", {"gate": job.gate, "iteration": iteration},
                allow_repair=not bool(retries),
            )
        if isinstance(envelope, Advance):
            return envelope
        recovery = envelope.pop('_format_recovery', None)
        if recovery:
            self.ledger.append_event(job.job_id, 'plan.format_repaired',
                                     {'iteration': iteration, **recovery}, agent=architect.name)
        self._record_research_progress(job.job_id, architect.name, envelope)

        compiled = None
        if self.require_plan_governance and not plan_question(envelope) and envelope.get("blocked_on") not in (
            "contract_gap",
            "exhausted",
        ):
            capability_rows = self._capability_rows(resources)
            try:
                validate_development_plan(
                    envelope,
                    resources,
                    available_capabilities=capability_rows,
                )
                self._validate_simple_tools(envelope, resources)
                if any(step.get("agent") != "builder" for step in envelope.get("steps", [])):
                    raise ValidationError("every three-agent Plan step must be assigned to 'builder'")
                if self.contract_compiler is not None:
                    compiled = self._compile_or_prepare(
                        envelope,
                        job_id=job.job_id,
                        iteration=iteration,
                        scenario_sha256=str(resources.get("scenario_sha256") or ""),
                        resources={**resources, "available_capabilities": capability_rows},
                    )
                    envelope = compiled.plan
            except (ValidationError, ContractCompileError) as exc:
                self._record_rejected_plan(job.job_id, iteration, envelope, exc, correction=False)
                if not self.repair_invalid_plans or retries:
                    return self._block(
                        job, BLOCKED_INVALID, f"Lead governance rejected the Plan: {exc}"
                    )
                repair_task = {
                    **task,
                    "research_plan": {"search_budget": 0},
                    "format_correction": {
                        "error": str(exc),
                        "rejected_handoff": envelope,
                        "instruction": (
                            "Repair only the reported configuration/format problem in this Plan. "
                            "Preserve the integrated engineering design, completed work and original "
                            "scenario. Reuse saved facts; do not research again, execute devices, "
                            "invent safety data, or expand the user's hardware authority."
                        ),
                    },
                }
                envelope = self._call(
                    architect,
                    repair_task,
                    job,
                    "repairing a rejected Plan",
                    {"gate": job.gate, "iteration": iteration},
                    allow_repair=False,
                )
                if isinstance(envelope, Advance):
                    return envelope
                self._record_research_progress(job.job_id, architect.name, envelope)
                try:
                    validate_development_plan(
                        envelope,
                        resources,
                        available_capabilities=capability_rows,
                    )
                    self._validate_simple_tools(envelope, resources)
                    if any(
                        step.get("agent") != "builder"
                        for step in envelope.get("steps", [])
                    ):
                        raise ValidationError(
                            "every three-agent Plan step must be assigned to 'builder'"
                        )
                    if self.contract_compiler is not None:
                        compiled = self._compile_or_prepare(
                            envelope,
                            job_id=job.job_id,
                            iteration=iteration,
                            scenario_sha256=str(resources.get("scenario_sha256") or ""),
                            resources={
                                **resources,
                                "available_capabilities": capability_rows,
                            },
                        )
                        envelope = compiled.plan
                except (ValidationError, ContractCompileError) as repair_exc:
                    self._record_rejected_plan(job.job_id, iteration, envelope, repair_exc, correction=True)
                    return self._block(
                        job,
                        BLOCKED_INVALID,
                        "Lead could not repair the governed Plan: " + str(repair_exc),
                    )

        blocked_on = envelope.get("blocked_on")
        if blocked_on in ("contract_gap", "exhausted"):
            self.ledger.record_handoff(job.job_id, "Plan", architect.name, envelope, iteration=iteration)
            return self._finish(
                self.ledger.job(job.job_id),
                "contract_gap" if blocked_on == "contract_gap" else "exhausted",
                f"the architect stopped: {envelope.get('objective') or blocked_on}",
            )

        handoff = self.ledger.record_handoff(
            job.job_id, "Plan", architect.name, envelope, iteration=iteration
        )
        question = plan_question(envelope)
        if question:
            return self._block(self.ledger.job(job.job_id), USER_INPUT, question)
        if self.require_plan_governance:
            self._record_plan_governance(job.job_id, iteration, envelope, compiled)
            self._ingest_plan_knowledge(job.job_id, envelope, resources)
        steps = len(envelope.get("steps") or [])
        return Advance(PLAN, job.job_id, iteration, f"planned {steps} step(s)", handoff.handoff_id)

    def _compile_or_prepare(self, envelope: dict, **kwargs):
        """Keep ordinary preparation possible without allowing unprepared dispatch."""
        from .contract_compiler import CompiledPlan
        from .development import preparation_plan
        try:
            compiled = self.contract_compiler.compile(envelope, **kwargs)
        except ContractCompileError as exc:
            resources = kwargs["resources"]
            if not autonomous(resources) or "codex_workspace" not in resources.get("available_tools", []):
                raise
            prepared = preparation_plan(envelope, str(exc))
            validate_development_plan(prepared, resources, available_capabilities=[])
            compiled = CompiledPlan(prepared, "", "", "builder_preparation", False)
        if compiled.plan.get("builder_preparation"):
            self.ledger.append_event(kwargs["job_id"], "plan.preparation.required", {
                "iteration": kwargs["iteration"],
                "reason": compiled.plan["preparation_context"]["reason"],
                "hardware_access": False,
            }, agent="platform")
        return compiled

    def _record_rejected_plan(self, job_id: str, iteration: int, envelope: dict,
                              error: Exception, *, correction: bool) -> None:
        # Rejected plans are diagnostic records, NOT executable handoffs. The
        # runtime transcript may be clipped; preserve the parsed original here.
        import hashlib
        import json
        encoded = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        self.ledger.append_event(job_id, "plan.validation_failed", {
            "iteration": iteration, "error": str(error), "correction": correction,
            "rejected_plan": envelope,
            "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        }, agent=self.planner_agent)

    @staticmethod
    def _agent_plan(plan: dict[str, Any]) -> dict[str, Any]:
        """Keep design/reasoning, replace installed proposal bodies by references.

        The original handoff is untouched. Uncompiled proposals are never
        removed: the agent may still need them to repair the configuration.
        """
        context = dict(plan)
        if plan.get("hardware_contracts"):
            context.pop("hardware_contract", None)
            context.pop("hardware_boundaries", None)
        return context

    def _resources(self, brief: dict[str, Any]) -> dict[str, Any]:
        raw = brief.get("resources")
        resources = dict(raw) if isinstance(raw, dict) else {}
        if self.resource_refresher is None:
            return resources
        scenario = str(brief.get("scenario") or "")
        refreshed = self.resource_refresher(scenario, resources)
        if not isinstance(refreshed, dict):
            raise LoopError("resource_refresher must return an object")
        return refreshed

    def _record_research_progress(
        self, job_id: str, agent_name: str, envelope: dict[str, Any]
    ) -> None:
        """Move private runtime usage to the append-only ledger.

        The metadata is deliberately removed before Plan validation and
        persistence.  New fact hashes are calculated by the runtime from the
        exact accepted research findings, so later iterations can distinguish
        progress from a differently worded repeat search.
        """

        usage = envelope.pop("_research_usage", None)
        if not isinstance(usage, dict):
            return
        payload = dict(usage)
        hashes = payload.pop("finding_hashes", [])
        payload["new_fact_hashes"] = [
            str(item) for item in hashes if isinstance(item, str)
        ]
        self.ledger.append_event(job_id, RESEARCH_PROGRESS, payload, agent_name)

    def _ingest_plan_knowledge(
        self, job_id: str, plan: dict[str, Any], resources: dict[str, Any]
    ) -> None:
        findings = plan.get("research_findings") or []
        method = getattr(self.knowledge_ingestor, "ingest_simple_plan", None)
        scope_data = resources.get("knowledge_scope")
        if not findings or not callable(method) or not isinstance(scope_data, dict):
            return
        try:
            from ai_hardware_autolab.knowledge.models import KnowledgeScope

            result = method(
                plan,
                KnowledgeScope.parse(scope_data),
                source_job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 - knowledge is observable, not execution truth
            self.ledger.append_event(
                job_id,
                "knowledge.ingestion.failed",
                {"error": f"{type(exc).__name__}: {exc}"},
                "platform",
            )
            return
        self.ledger.append_event(
            job_id,
            "knowledge.ingested",
            {
                "claim_ids": [claim.claim_id for claim in result.claims],
                "count": len(result.claims),
            },
            "platform",
        )

    def _steering(self, job_id: str) -> list[dict[str, Any]]:
        """What a person told this job since the last handoff.

        Imported late: the control plane knows about the loop, and a loop that
        imported it at module scope would make the two mutually dependent for
        the sake of one list.
        """
        from ai_hardware_autolab.orchestrator.control import pending_steers

        return pending_steers(self.ledger, job_id)

    def _undelivered_steering(self, job_id: str) -> list[dict[str, Any]]:
        from ai_hardware_autolab.orchestrator.control import undelivered_steers

        return undelivered_steers(self.ledger, job_id)

    def _previous_verdict(self, job_id: str, iteration: int) -> dict[str, Any] | None:
        verdicts = [h for h in self.ledger.handoffs(job_id, "Verdict") if h.iteration < iteration]
        return verdicts[-1].payload if verdicts else None

    def _previous_step_results(self, job_id: str, iteration: int) -> list[dict[str, Any]]:
        if iteration <= 1:
            return []
        previous = iteration - 1
        # Question-only rounds did not execute anything. Keep the last actual
        # execution context instead of replacing failures with an empty list.
        while previous > 1:
            plan = self.latest_plan(job_id, previous)
            if not plan or not plan_question(plan) or self.ledger.handoffs(job_id, "StepResult", previous):
                break
            previous -= 1
        return [
            handoff.payload
            for handoff in self.ledger.handoffs(job_id, "StepResult", previous)
        ]

    def _previous_verifier_review(
        self, job_id: str, iteration: int
    ) -> dict[str, Any] | None:
        reviews = [
            event.payload
            for event in iter_events(self.ledger, job_id)
            if event.kind == "verifier.review"
            and int(event.payload.get("iteration") or 0) < iteration
        ]
        return reviews[-1] if reviews else None

    def _execute(self, job: J.Job) -> Advance:
        plan = self.latest_plan(job.job_id, job.iteration) or {}
        reported = self.step_results(job.job_id, job.iteration)
        brief_rows = self.ledger.handoffs(job.job_id, "Brief", 0)
        brief = brief_rows[-1].payload if brief_rows else {}
        resources = self._resources(brief)
        step = next(
            (s for s in plan.get("steps") or [] if str(s.get("id", "")) not in reported), None
        )
        if step is None:  # pragma: no cover - phase() already excluded this
            raise LoopError("execute was reached with every step reported")

        step_id = str(step.get("id", ""))
        agent = self.agent(str(step.get("agent", "")))

        touches_hardware = bool(
            step.get("touches_hardware")
            if "touches_hardware" in step
            else agent.touches_hardware
        )
        if touches_hardware:
            blocked = self._require_approval(job, step, step_id, agent)
            if blocked is not None:
                return blocked

        task = {
            "kind": "Plan",
            "job_id": job.job_id,
            "iteration": job.iteration,
            "step": step,
            "plan": self._agent_plan(plan) if autonomous(resources) else plan,
            "gate": job.gate,
            "mission_id": str(resources.get("mission_id") or job.gate),
            "scenario": brief.get("scenario", job.objective),
            "resources": self._builder_resources(resources, plan, step),
            "previous_step_results": list(reported.values()),
            # All iterations matter: a repaired host driver in an earlier
            # workspace turn is still not deployed by starting a new round.
            "unpublished_platform_paths": sorted({
                change["path"]
                for handoff in self.ledger.handoffs(job.job_id, "StepResult")
                for change in (handoff.payload.get("workspace_changes") or {}).get("changes", [])
                if isinstance(change, dict) and isinstance(change.get("path"), str)
            }),
            # Earlier iterations are not in `reported`. Give Builder the actual
            # failure/check references, not only Lead's paraphrase of them.
            "previous_iteration_results": [
                {key: row[key] for key in (
                    "step_id", "status", "verdict", "runs", "summary", "verification",
                    "execution_errors", "repair_target", "hardware_state_unknown", "remaining_gaps",
                ) if key in row}
                for row in self._previous_step_results(job.job_id, job.iteration)
            ],
            "previous_verifier_review": self._previous_verifier_review(job.job_id, job.iteration),
            "previous_verdict": self._previous_verdict(job.job_id, job.iteration),
            "steering": self._steering(job.job_id),
        }
        before_call = self.ledger.latest_event_id()
        if touches_hardware and autonomous(resources):
            self.ledger.append_event(job.job_id, 'step.dispatch.started',
                {'iteration': job.iteration, 'step_id': step_id, 'tool': step.get('tool')}, 'platform')
        envelope = self._call(agent, task, job, f"step {step_id}", {"step_id": step_id})
        if isinstance(envelope, Advance):
            return envelope

        if autonomous(resources) and envelope.get("status") == "needs_approval":
            authorized = any(e.kind == "approval.authorized"
                             for e in iter_events(self.ledger, job.job_id, since=before_call))
            if authorized or envelope.get("runs"):
                return self._block(job, "hardware_state_unknown",
                    "Builder requested approval after an attempted action; inspect its recorded events before resuming")
            request = self.ledger.request_approval(job.job_id, str(step.get("contract", job.gate)),
                str(envelope.get("notes") or envelope.get("summary") or f"step {step_id} needs approval"),
                artifacts=dict(step.get("artifacts") or {}), requested_by=agent.name, step_id=step_id)
            self.ledger.transition(job.job_id, J.AWAITING_APPROVAL, reason=f"step {step_id} needs approval")
            # No StepResult: a request is not evidence that the step executed.
            return Advance(EXECUTE, job.job_id, job.iteration, f"step {step_id} needs approval",
                           approval_id=request.approval_id, blocked=True)

        handoff = self.ledger.record_handoff(
            job.job_id,
            "StepResult",
            agent.name,
            envelope,
            iteration=job.iteration,
            step_id=step_id,
        )
        status = str(envelope.get("status", "done"))
        if envelope.get("repair_target") == "platform_deployment" and status == "blocked":
            return self._block(job, "platform_deployment", str(envelope.get("summary") or
                "Workspace changes are not installed in the device execution path; no device call was made"))
        if envelope.get("hardware_state_unknown") is True:
            return self._block(job, "hardware_state_unknown", "Device action was attempted without a known outcome; reconcile device state before resuming")
        if status == "needs_approval":
            self.ledger.request_approval(
                job.job_id,
                str(step.get("contract", job.gate)),
                str(envelope.get("notes") or f"step {step_id} reported it needs approval"),
                requested_by=agent.name,
                step_id=step_id,
            )
            self.ledger.transition(
                job.job_id, J.AWAITING_APPROVAL, reason=f"step {step_id} needs approval"
            )
            return Advance(
                EXECUTE, job.job_id, job.iteration, f"step {step_id} needs approval",
                handoff.handoff_id, blocked=True
            )
        return Advance(
            EXECUTE, job.job_id, job.iteration, f"step {step_id}: {status}", handoff.handoff_id
        )

    @staticmethod
    def _capability_rows(resources: Any) -> list[dict[str, Any]]:
        if not isinstance(resources, dict):
            return []
        candidates = resources.get("capability_candidates")
        if isinstance(candidates, list):
            return [row for row in candidates if isinstance(row, dict)]
        available = resources.get("available_capabilities")
        return [row for row in available if isinstance(row, dict)] if isinstance(available, list) else []

    @staticmethod
    def _validate_simple_tools(plan: dict[str, Any], resources: Any) -> None:
        if not isinstance(resources, dict) or "available_tools" not in resources:
            # Library callers predating mission intake have no tool projection.
            # The production control plane always supplies one.
            return
        values = resources.get("available_tools") if isinstance(resources, dict) else []
        available = {str(item) for item in (values or []) if isinstance(item, str)}
        for index, step in enumerate(plan.get("steps") or []):
            tool = str(step.get("tool") or "")
            if tool not in available:
                raise ValidationError(
                    f"Plan.steps[{index}].tool {tool!r} is not available for this mission"
                )
            if tool == "codex_workspace" and bool(step.get("touches_hardware")):
                raise ValidationError(
                    "codex_workspace is offline source/build/test work and cannot touch hardware"
                )

    def _builder_resources(
        self, resources: Any, plan: dict[str, Any], step: dict[str, Any]
    ) -> dict[str, Any]:
        bounded = dict(resources) if isinstance(resources, dict) else {}
        if autonomous(bounded):
            bounded["write_scope"] = list((plan.get("implementation") or {}).get("write_scope") or [])
            bounded["write_scope_advisory"] = True
            return bounded
        implementation = plan.get("implementation")
        references = implementation.get("base_capabilities") if isinstance(implementation, dict) else []
        selected_ids = {
            str(item.get("id"))
            for item in references or []
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        rows = self._capability_rows(bounded)
        selected = [row for row in rows if str(row.get("id")) in selected_ids]
        # Builder receives only what Lead selected. The complete catalog is a
        # planning input, not an invitation to silently switch implementation.
        bounded["available_capabilities"] = selected
        bounded["selected_capabilities"] = selected
        bounded.pop("capability_candidates", None)
        bounded["write_scope"] = (
            list(implementation.get("write_scope") or [])
            if isinstance(implementation, dict) and step.get("tool") == "codex_workspace"
            else []
        )
        return bounded

    def _record_plan_governance(
        self,
        job_id: str,
        iteration: int,
        plan: dict[str, Any],
        compiled: Any,
    ) -> None:
        implementation = plan.get("implementation") or {}
        selection = {
            "iteration": iteration,
            "strategy": implementation.get("strategy"),
            "base_capabilities": implementation.get("base_capabilities") or [],
            "write_scope": implementation.get("write_scope") or [],
        }
        self._append_event_once(job_id, SELECTED_EVENT, selection, "platform")
        contract = plan.get("compiled_contract")
        if isinstance(contract, dict):
            self._append_event_once(
                job_id,
                COMPILED_EVENT,
                {"iteration": iteration, **contract, "installed": bool(getattr(compiled, "installed", False))},
                "platform",
            )

    def _append_event_once(
        self, job_id: str, kind: str, payload: dict[str, Any], actor: str
    ) -> None:
        for event in iter_events(self.ledger, job_id):
            if event.kind == kind and event.payload == payload:
                return
        self.ledger.append_event(job_id, kind, payload, actor)

    def _require_approval(
        self, job: J.Job, step: dict[str, Any], step_id: str, agent: AgentDefinition
    ) -> Advance | None:
        """Stop before a hardware step unless a person has already said yes.

        The check is for a *granted* approval naming this contract and step.
        A pending one is not a yes, and a denied one is a no that the loop must
        not walk past by filing a fresh request -- that would turn a refusal
        into a retry.
        """
        contract = str(step.get("contract", job.gate))
        approver = self.auto_approver
        if self.scenario_approver is not None and self.scenario_approver.applies(job.job_id):
            # An explicit Campaign replaces submission consent (including
            # after revocation); never silently fall back to broader consent.
            explicit = any(e.kind in {"campaign.activated", "campaign.deactivated"}
                           for e in iter_events(self.ledger, job.job_id))
            if not explicit:
                approver = self.scenario_approver
        decided = [
            a
            for a in self._approvals_for(job.job_id)
            if a.contract == contract and a.step_id == step_id
        ]
        uses: dict[str, int] = {}
        for event in iter_events(self.ledger, job.job_id):
            if event.kind == "approval.authorized":
                identifier = event.payload.get("approval_id")
                uses[identifier] = uses.get(identifier, 0) + 1
        approval_now = getattr(approver, "now", J.utc_now)()
        denied = [a for a in decided if a.state in (J.APPROVAL_DENIED, J.APPROVAL_WITHDRAWN)]
        if denied:
            self._finish(
                self.ledger.job(job.job_id),
                "needs_approval",
                f"step {step_id} was refused ({denied[-1].state}); it is not re-requested",
            )
            return Advance(
                DECIDE,
                job.job_id,
                job.iteration,
                f"step {step_id} was refused; the loop stops rather than asking again",
                approval_id=denied[-1].approval_id,
                outcome="needs_approval",
                blocked=True,
            )
        if any(a.state == J.APPROVAL_PENDING for a in decided):
            pending = next(a for a in decided if a.state == J.APPROVAL_PENDING)
            return Advance(
                BLOCKED, job.job_id, job.iteration, f"step {step_id} is waiting for a person",
                approval_id=pending.approval_id, blocked=True
            )
        if any(a.state == J.APPROVAL_GRANTED and uses.get(a.approval_id, 0) < a.max_executions
               and (not a.expires_at or J._moment(a.expires_at) > J._moment(approval_now)) for a in decided):
            return None

        evaluation = None
        policy_error = ""
        if approver is not None:
            try:
                evaluation = approver.evaluate(job, step, agent)
            except Exception as exc:  # noqa: BLE001 - policy failure must fail closed
                policy_error = f"automatic approval evaluation failed: {exc}"

        auto_allowed = bool(evaluation is not None and evaluation.allowed)
        artifacts = (
            dict(evaluation.artifacts or {})
            if auto_allowed
            else dict(step.get("artifacts") or {})
        )
        approval = self.ledger.request_approval(
            job.job_id,
            contract,
            str(step.get("intent") or f"{agent.name} performing step {step_id}"),
            artifacts=artifacts,
            requested_by=agent.name,
            step_id=step_id,
            max_executions=int(evaluation.max_executions) if auto_allowed else 1,
            expires_at=evaluation.expires_at if auto_allowed else None,
        )
        if evaluation is not None:
            try:
                decided = approver.apply(approval, evaluation)
            except Exception as exc:  # noqa: BLE001 - leave the request pending
                policy_error = f"automatic approval recording failed: {exc}"
            else:
                if decided.state == J.APPROVAL_GRANTED:
                    return Advance(
                        EXECUTE,
                        job.job_id,
                        job.iteration,
                        f"step {step_id} was automatically approved by the recorded device delegation",
                        approval_id=approval.approval_id,
                    )
        self.ledger.transition(
            job.job_id, J.AWAITING_APPROVAL, reason=f"step {step_id} touches hardware"
        )
        reason = policy_error or (
            evaluation.reason
            if evaluation is not None
            else "no automatic campaign is active"
        )
        return Advance(
            BLOCKED,
            job.job_id,
            job.iteration,
            f"step {step_id} touches hardware and needs a person: {reason}",
            approval_id=approval.approval_id,
            blocked=True,
        )

    def _approvals_for(self, job_id: str) -> list[J.Approval]:
        """Every approval on this job, in any state.

        Read through the event log because the frozen ledger exposes pending
        requests and single lookups, not a per-job listing of decided ones.
        The log is authoritative anyway: an approval that produced no event
        would not exist for the UI either.
        """
        seen: dict[str, J.Approval] = {}
        for event in iter_events(self.ledger, job_id):
            approval_id = event.payload.get("approval_id")
            if event.kind.startswith("approval.") and approval_id:
                seen[approval_id] = self.ledger.approval(approval_id)
        return list(seen.values())

    def _reduce(self, job: J.Job) -> Advance:
        plan = self.latest_plan(job.job_id, job.iteration) or {}
        conditions = conditions_for(plan, self.step_results(job.job_id, job.iteration))
        from .diagnostics import diagnostic_step
        for step in plan.get("steps") or []:
            if diagnostic_step(step):
                result = self.step_results(job.job_id, job.iteration).get(str(step.get("id")), {})
                for run in result.get("runs") or []:
                    if not any(c.run == run and c.field == "measurements.diagnostic_complete" and c.expect is True
                               for c in conditions):
                        conditions.append(Condition(field="measurements.diagnostic_complete", expect=True, run=run,
                            source="diagnosis must be recorded even when connection fails"))
        briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
        if briefs and autonomous(briefs[-1].payload.get("resources")):
            for result in self.step_results(job.job_id, job.iteration).values():
                if result.get("status") == "failed":
                    for run in result.get("runs") or []:
                        conditions.append(Condition(field="verdict", expect="PASS", run=run,
                                                    source="failed execution must not become satisfied by an exit-only condition"))
        # An interrupted coding turn may have an exit-0 command in its tail.
        # Preserve that fact, but it cannot establish completion of the turn.
        for result in self.step_results(job.job_id, job.iteration).values():
            if result.get("handoff_recovery") and result.get("workspace_review"):
                for run in result.get("runs") or []:
                    conditions.append(Condition(field="measurements.runtime_completed", expect=True,
                                                source="runtime turn completion", run=run))
        reduction: Reduction = reduce_gate(
            gate=job.gate,
            conditions=conditions,
            iteration=job.iteration,
            history=self._signature_history(job.job_id, job.iteration),
            runs_dir=self.runs_dir,
            strikes=self.policy.strikes,
            corroboration=corroboration_for(plan),
        )
        if self.reasoning_verifier and not plan.get("builder_preparation"):
            self._run_reasoning_verifier(job, plan, reduction)
        # The reduction comes from deterministic code, so this can only fail if
        # the reducer and the envelope schema have drifted apart -- which they
        # had, and which is exactly the kind of divergence nobody notices until
        # a verdict is rejected months later.
        try:
            validate_handoff(
                self.stamp(
                    self.agent(self.verifier_agent),
                    reduction.describe(),
                    {"gate": job.gate, "iteration": job.iteration},
                )
            )
        except ValidationError as exc:
            return self._block(job, BLOCKED_INVALID, f"the reduction is not a valid Verdict: {exc}")

        handoff, outcome = self.ledger.record_reduction(job.job_id, reduction)
        self._ingest_reduction_knowledge(job.job_id, reduction)
        met = sum(1 for c in reduction.conditions if c.met)
        return Advance(
            REDUCE,
            job.job_id,
            job.iteration,
            f"{met}/{len(reduction.conditions)} conditions met; {outcome}",
            handoff.handoff_id,
            outcome=outcome,
        )

    def _ingest_reduction_knowledge(self, job_id: str, reduction: Reduction) -> None:
        method = getattr(self.knowledge_ingestor, "ingest_reduction", None)
        if not callable(method) or self.runs_dir is None:
            return
        briefs = self.ledger.handoffs(job_id, "Brief", 0)
        brief = briefs[-1].payload if briefs else {}
        resources = self._resources(brief)
        scope_data = resources.get("knowledge_scope")
        if not isinstance(scope_data, dict):
            return
        try:
            from ai_hardware_autolab.knowledge.models import KnowledgeScope

            result = method(
                reduction,
                KnowledgeScope.parse(scope_data),
                runs_dir=self.runs_dir,
                source_job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 - knowledge cannot change the verdict
            self.ledger.append_event(
                job_id,
                "knowledge.ingestion.failed",
                {"stage": "reduction", "error": f"{type(exc).__name__}: {exc}"},
                "platform",
            )
            return
        self.ledger.append_event(
            job_id,
            "knowledge.ingested",
            {
                "stage": "reduction",
                "claim_ids": [claim.claim_id for claim in result.claims],
                "count": len(result.claims),
            },
            "platform",
        )

    def _run_reasoning_verifier(
        self, job: J.Job, plan: dict[str, Any], reduction: Reduction
    ) -> None:
        """Ask the read-only Verifier for diagnosis without delegating truth.

        A reviewer outage or malformed commentary must not erase valid run
        evidence.  The reducer remains the sole source of condition status;
        accepted reviewer advice is a separate event consumed by the next Lead
        iteration.
        """
        verifier = self.agent(self.verifier_agent)
        expected = reduction.describe()
        briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
        brief = briefs[-1].payload if briefs else {}
        task = {
            "kind": "StepResult",
            "job_id": job.job_id,
            "gate": job.gate,
            "iteration": job.iteration,
            "plan": self._agent_plan(plan) if autonomous(brief.get("resources")) else plan,
            "step_results": list(self.step_results(job.job_id, job.iteration).values()),
            "deterministic_verdict": expected,
            "scenario": brief.get("scenario", job.objective),
            "resources": self._resources(brief),
            "steering": self._steering(job.job_id),
        }
        if autonomous(task["resources"]):
            self._review_autonomous(job, verifier, task, expected)
            return
        payload: dict[str, Any] = {
            "iteration": job.iteration,
            "status": "unavailable",
        }
        try:
            raw = self.runner(verifier, self._with_source_context(task))
            stamped, error = self._validate_agent_answer(
                verifier,
                raw,
                {"gate": job.gate, "iteration": job.iteration},
                "reviewing deterministic evidence",
            )
            if error is not None:
                payload["error"] = error
            else:
                protected = (
                    "outcome",
                    "conditions",
                    "failure_signature",
                    "hash_mismatches",
                    "strikes",
                    "corroboration",
                )
                mismatched = [key for key in protected if stamped.get(key) != expected.get(key)]
                if mismatched:
                    payload["error"] = (
                        "Verifier tried to alter deterministic field(s): "
                        + ", ".join(mismatched)
                    )
                else:
                    payload.update(
                        {
                            "status": "accepted",
                            "requirements_met": stamped.get("requirements_met"),
                            "analysis": str(stamped.get("analysis") or "").strip(),
                            "recommended_next_action": str(
                                stamped.get("recommended_next_action") or ""
                            ).strip(),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - evidence remains usable
            payload["error"] = f"{type(exc).__name__}: {exc}"
        self._append_event_once(job.job_id, "verifier.review", payload, verifier.name)

    def _review_autonomous(self, job, verifier, task, expected) -> None:
        """Review requirements without asking an LLM to transcribe evidence.

        The reducer's Verdict is still recorded separately, unchanged. One
        read-only formatting repair can never execute Builder or a device.
        """
        task = self._with_agent_knowledge(verifier, task)
        protected = ("outcome", "conditions", "failure_signature", "hash_mismatches", "strikes", "corroboration")
        payload = {"iteration": job.iteration, "status": "unavailable"}
        for attempt in range(2):
            try:
                raw = self.runner(verifier, self._with_source_context(task))
            except Exception as exc:
                payload["error"] = f"{type(exc).__name__}: {exc}"
                break  # An outage is not a reason to retry a model immediately.
            error = None
            if not isinstance(raw, dict):
                error = "Verifier review must be an object"
            elif any(raw.get(k, v) != v for k, v in {"kind": "Verdict", "agent": verifier.name,
                                                     "gate": job.gate, "iteration": job.iteration}.items()):
                error = "Verifier review identity differs from its dispatched task"
            elif any(key in raw and raw[key] != expected.get(key) for key in protected):
                changed = [key for key in protected if key in raw and raw[key] != expected.get(key)]
                error = "Verifier cannot change deterministic evidence (" + ", ".join(changed) + "); omit its copied fields"
            elif type(raw.get("requirements_met")) is not bool:
                error = "requirements_met must be a JSON boolean"
            elif not isinstance(raw.get("analysis") or raw.get("recommended_next_action"), str) or not (raw.get("analysis") or raw.get("recommended_next_action")).strip():
                error = "analysis must explain the evidence for the requirements review"
            if error is None:
                payload.update(status="accepted", requirements_met=raw["requirements_met"],
                               analysis=(raw.get("analysis") or raw.get("recommended_next_action")).strip(),
                               recommended_next_action=str(raw.get("recommended_next_action") or "").strip())
                payload.pop("error", None)
                break
            payload["error"] = error
            task = {**task, "format_correction": {"error": error, "rejected_handoff": raw,
                    "instruction": "Return only kind, agent, requirements_met, analysis and recommended_next_action. Review the supplied evidence; do not rerun work or copy the deterministic table."}}
        self._append_event_once(job.job_id, "verifier.review", payload, verifier.name)

    def _signature_history(self, job_id: str, iteration: int) -> list[str]:
        return [
            h.failure_signature
            for h in self.ledger.handoffs(job_id, "Verdict")
            if h.iteration < iteration and h.failure_signature
        ]

    def _decide(self, job: J.Job) -> Advance:
        verdict = self.ledger.handoffs(job.job_id, "Verdict", job.iteration)[-1]
        outcome = str(verdict.payload.get("outcome", "open"))

        if outcome == "satisfied":
            gap = None
            briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
            brief = briefs[-1].payload if briefs else {}
            if self.completion_guard is not None:
                gap = self.completion_guard(
                    job,
                    self.latest_plan(job.job_id, job.iteration) or {},
                    self._resources(brief),
                )
            preparation = (self.latest_plan(job.job_id, job.iteration) or {}).get("builder_preparation")
            if preparation:
                gap = "Builder preparation recorded; remaining execution still requires validation and scenario review"
            if self.reasoning_verifier and autonomous(brief.get("resources")) and not preparation:
                reviews = [e.payload for e in iter_events(self.ledger, job.job_id)
                           if e.kind == "verifier.review" and e.payload.get("iteration") == job.iteration]
                review = reviews[-1] if reviews else {}
                if review.get("status") != "accepted" or type(review.get("requirements_met")) is not bool:
                    return self._block(job, BLOCKED_INVALID, "Verifier must review original scenario coverage before completion")
                if review["requirements_met"] is False:
                    gap = str(review.get("recommended_next_action") or review.get("analysis") or "scenario requirements remain unmet")
            if gap is None:
                return self._finish(
                    job, "satisfied", "every condition met, each with a run behind it"
                )
            ceiling = self.iteration_limit(job.job_id)
            if job.iteration >= ceiling:
                return self._finish(job, "exhausted", gap)
            iteration = self.ledger.begin_iteration(job.job_id)
            return Advance(
                DECIDE,
                job.job_id,
                iteration,
                f"{gap}; re-planning as iteration {iteration}",
                outcome="open",
            )
        if outcome == "primary_run_missing":
            return self._finish(
                job, "primary_run_missing", "a run this gate depends on is not on disk"
            )
        if self.ledger.exhausted(job.job_id, self.policy.strikes):
            return self._finish(
                job,
                "exhausted",
                f"the same failure survived {self.ledger.strikes(job.job_id)} iterations",
            )
        ceiling = self.iteration_limit(job.job_id)
        if job.iteration >= ceiling:
            return self._finish(
                job,
                "exhausted",
                f"reached the {ceiling}-iteration ceiling without converging",
            )
        # Still open, still learning something. The counter is advanced here
        # rather than in _plan(), because opening the next iteration *is* the
        # decision -- and until it happens, phase() would keep routing back to
        # this method and the loop would spin without moving.
        iteration = self.ledger.begin_iteration(job.job_id)
        return Advance(
            DECIDE, job.job_id, iteration, f"{outcome}; re-planning as iteration {iteration}",
            outcome=outcome,
        )

    def _finish(self, job: J.Job, outcome: str, reason: str) -> Advance:
        """Write the report, then close the job. Never the other way round.

        A job that is already `done` cannot legally have anything else recorded
        against it, so a reporter that fails after the transition would leave a
        finished gate with nothing anyone can read.
        """
        handoff_id = None
        if self.policy.report_on_finish:
            reporter = self.agent(self.reporter_agent)
            task = {
                "kind": "Verdict",
                "job_id": job.job_id,
                "gate": job.gate,
                "outcome": outcome,
                "reason": reason,
                "iteration": job.iteration,
                "verdicts": [h.payload for h in self.ledger.handoffs(job.job_id, "Verdict")],
                # Context, not direction. A person's words are on the record and
                # should reach the write-up; letting them steer the report after
                # the evidence is in is the softening the reporter's own
                # definition forbids. The key is deliberately not "steering".
                "undelivered_steering": self._undelivered_steering(job.job_id),
            }
            envelope = self._call(
                reporter, task, job, "reporting", {"gate": job.gate, "outcome": outcome}
            )
            if isinstance(envelope, Advance):
                return envelope
            handoff_id = self.ledger.record_handoff(
                job.job_id, "Report", reporter.name, envelope, iteration=job.iteration
            ).handoff_id

        self.ledger.transition(job.job_id, J.DONE, outcome, reason=reason)
        return Advance(
            FINISHED, job.job_id, job.iteration, reason, handoff_id, outcome=outcome
        )

    # -- calling an agent ----------------------------------------------------

    def resume_step(self, job_id: str, requested_by: str, note: str) -> dict:
        from .recovery import blocked_step, unfinished_dispatch
        from .plan_format import is_plan_format_reason
        job = self.ledger.job(job_id)
        blocks = [e for e in iter_events(self.ledger, job_id) if e.kind == 'job.blocked']
        if job.state == J.BLOCKED and blocks and is_plan_format_reason(str(blocks[-1].payload.get('reason', ''))):
            row = self.ledger.resume_plan_format(job_id, requested_by, note)
            return {'job': row.describe(), 'resumed_step': 'plan', 'mode': 'replan_from_saved_context'}
        stopped = blocked_step(self.ledger, job_id)
        if job.state == J.DONE or unfinished_dispatch(self.ledger, job_id):
            raise LoopError('Cannot resume a terminal job or an unrecorded device outcome; reconcile first')
        if not stopped or stopped.payload.get('repair_target') != 'platform_deployment' or stopped.payload.get('hardware_contacted') is not False or stopped.payload.get('hardware_state_unknown') is not False:
            raise LoopError('Only a recorded pre-device deployment block can resume here; unknown device state needs reconciliation')
        if job.iteration >= self.iteration_limit(job_id):
            raise LoopError('Iteration budget exhausted; resume cannot extend the approved budget')
        check = getattr(self.runner, 'check_resume', None)
        if not callable(check):
            raise LoopError('The active runner cannot verify source deployment')
        plan = self.latest_plan(job_id, job.iteration) or {}
        step = next((s for s in plan.get('steps', []) if s.get('id') == stopped.step_id), None)
        if step is None:
            raise LoopError('Blocked step is not in the current Plan')
        check(job_id, {**stopped.payload, 'blocked_tool': step.get('tool')})
        row = self.ledger.resume_pre_device_step(job_id, stopped.handoff_id, requested_by, note)
        return {'job': row.describe(), 'resumed_step': stopped.step_id}

    def _block(self, job: J.Job, code: str, message: str) -> Advance:
        """Stop the job through the ledger's existing transition, with a code.

        No new event kind: `job.blocked` already exists and already carries a
        reason. The code is a prefix of that reason so a reader that only has
        the event log can still tell a dead runtime from an unusable answer.
        """
        reason = f"{code}: {message}"
        self.ledger.transition(job.job_id, J.BLOCKED, reason=reason)
        return Advance(
            BLOCKED, job.job_id, job.iteration, reason, blocked=True, reason_code=code
        )

    def _with_source_context(self, task: dict[str, Any]) -> dict[str, Any]:
        if self.planner_agent != "lead-agent":
            return task
        from ai_hardware_autolab.orchestrator.source_context import source_context

        raw = task.get("resources")
        resources = dict(raw) if isinstance(raw, dict) else {}
        resources["source_context"] = source_context(resources)
        result = {**task, "resources": resources}
        if autonomous(resources) and task.get("job_id"):
            feedback = [e for e in iter_events(self.ledger, str(task["job_id"])) if e.kind == "control.steer"]
            result["user_feedback_history"] = [
                {"event_id": e.event_id, "at": e.at, **e.payload} for e in feedback[-20:]
            ]
            result["user_feedback_history_truncated"] = len(feedback) > 20
        return result

    def _with_agent_knowledge(self, agent: AgentDefinition, task: dict[str, Any]) -> dict[str, Any]:
        if self.agent_knowledge is None or not autonomous(task.get("resources")):
            return task
        # Replace the old broad Brief snapshot; do not send two memory packets.
        resources = dict(task.get("resources") or {})
        resources.pop("knowledge_context", None)
        resources.pop("knowledge_refresh_error", None)
        result = {**task, "resources": resources}
        result.pop("knowledge_context", None)
        try:
            packet = self.agent_knowledge(agent.name, result)
            resources["knowledge_context"] = packet
            rows = packet.get("claims", [])
            import hashlib
            import json
            encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True)
            audit = {"iteration": task.get("iteration"), "step_id": (task.get("step") or {}).get("id"),
                     "role": agent.name, "scope": packet.get("scope"), "query": packet.get("query"),
                     "packet_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                     "serialized_chars": len(encoded),
                     "claims": [{k: row[k] for k in ("claim_id", "version_id", "status")} for row in rows],
                     "omitted_count": packet.get("omitted_count", 0)}
            kind = "knowledge.context.selected"
        except Exception as exc:
            # A broken memory provider must neither reuse an unscoped/stale
            # snapshot silently nor turn ordinary development into a blocker.
            resources["knowledge_context"] = {"kind": "AgentKnowledgeContext", "claims": [],
                "unavailable": True, "instruction": "Memory unavailable; inspect relevant original sources as needed."}
            audit = {"iteration": task.get("iteration"), "role": agent.name, "error": type(exc).__name__}
            kind = "knowledge.context.unavailable"
        if task.get("job_id"):
            self.ledger.append_event(str(task["job_id"]), kind, audit, agent=agent.name)
        return result

    def _call(
        self,
        agent: AgentDefinition,
        task: dict[str, Any],
        job: J.Job,
        what: str,
        context: dict[str, Any] | None = None,
        allow_repair: bool = True,
    ) -> dict[str, Any] | Advance:
        """Run an agent, check its answer, or block the job.

        Two failures land here and they are deliberately kept apart.

        A runtime that could not run told us nothing about the gate. Recording
        that as a failed condition would put a claim about the board into the
        evidence chain on the strength of a subprocess dying.

        A malformed envelope is different: the agent answered, and the answer
        cannot be used. It is **not recorded as a handoff** -- which means it
        never reaches the reducer, never becomes a condition, and never counts
        toward the strike rule. A rejected answer must not look like an
        iteration that was tried and failed, because the strike rule would then
        count a broken agent as evidence that the board cannot do something.
        """
        # Regenerate navigation rules rather than trusting old Brief metadata.
        task = self._with_agent_knowledge(agent, self._with_source_context(task))
        try:
            envelope = self.runner(agent, task)
        except Exception as exc:  # noqa: BLE001 - any runtime failure blocks, none concludes
            current = self.ledger.job(job.job_id)
            if current.state == J.DONE:
                return Advance(
                    FINISHED,
                    job.job_id,
                    current.iteration,
                    f"job finished while {agent.name} was running: {current.outcome}",
                    outcome=current.outcome,
                )
            from .plan_format import PlanFormatError
            if isinstance(exc, PlanFormatError) and agent.name == self.planner_agent:
                import hashlib
                raw = exc.final_text
                full = len(raw.encode('utf-8')) <= 262144
                self.ledger.append_event(job.job_id, 'plan.format_failed', {
                    'iteration': current.iteration, 'error': str(exc),
                    'raw_response': raw if full else '', 'raw_available': full,
                    'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                }, agent=agent.name)
                return self._block(job, 'plan_format', str(exc))
            step = task.get("step") or {}
            if (autonomous(task.get("resources")) and agent.name == "builder"
                    and step.get("touches_hardware") is False
                    and isinstance(exc, (FileNotFoundError, SyntaxError, ValueError))
                    and self.classify_failure(exc) != BLOCKED_RATE_LIMIT):
                from ai_hardware_autolab.hwtools.record import RunRecord
                from ai_hardware_autolab.hwtools.result import ToolResult, FAIL
                from ai_hardware_autolab.orchestrator.development import WorkspaceEvidenceContext
                record = RunRecord("development-error", WorkspaceEvidenceContext(task), self.runs_dir)
                record.add(ToolResult(tool="development", action="prepare", ok=False, verdict=FAIL,
                    error=str(exc), measurements={"execution_completed": False, "hardware_contacted": False}))
                record.finish(FAIL)
                return self.stamp(agent, {"status": "failed", "runs": [record.dir.name], "verdict": FAIL,
                    "summary": f"Repair development code/arguments/path and continue: {exc}",
                    "repair_target": "builder"}, context)
            return self._block(
                job,
                self.classify_failure(exc),
                f"{agent.name} could not run while {what}: {exc}",
            )

        current = self.ledger.job(job.job_id)
        if current.state == J.DONE:
            return Advance(
                FINISHED,
                job.job_id,
                current.iteration,
                f"job finished while {agent.name} was running: {current.outcome}",
                outcome=current.outcome,
            )

        if agent.name == self.planner_agent:
            envelope = normalize_plan(envelope, task.get("resources", {}))
        stamped, validation_error = self._validate_agent_answer(agent, envelope, context, what)
        if validation_error is None:
            return stamped

        # Simple Lead turns are read-only and frequently get the engineering
        # answer right while misspelling one machine field.  Give that same
        # agent one bounded correction turn instead of converting a formatting
        # mistake into a permanent human-operated recovery.  Builder answers
        # are never replayed automatically because their first turn may have
        # changed files or touched approved hardware.
        if (
            allow_repair
            and self.repair_invalid_plans
            and agent.name == self.planner_agent
        ):
            correction_task = {
                **task,
                "format_correction": {
                    "error": validation_error,
                    "rejected_handoff": envelope,
                    "instruction": (
                        "Return one corrected Plan using the exact required field names. "
                        "Do not change the requested engineering objective."
                    ),
                },
            }
            try:
                repaired = self.runner(agent, correction_task)
            except Exception as exc:  # noqa: BLE001
                return self._block(
                    job,
                    self.classify_failure(exc),
                    f"{agent.name} could not repair its answer while {what}: {exc}",
                )
            if agent.name == self.planner_agent:
                repaired = normalize_plan(repaired, task.get("resources", {}))
            stamped, validation_error = self._validate_agent_answer(
                agent, repaired, context, f"repairing {what}"
            )
            if validation_error is None:
                return stamped

        return self._block(job, BLOCKED_INVALID, validation_error)

    def _validate_agent_answer(
        self,
        agent: AgentDefinition,
        envelope: Any,
        context: dict[str, Any] | None,
        what: str,
    ) -> tuple[dict[str, Any], str | None]:
        if not isinstance(envelope, dict):
            return {}, (
                f"{agent.name} returned {type(envelope).__name__}, not an object, while {what}"
            )
        identity = {"kind": KIND_BY_ROLE.get(agent.role), "agent": agent.name, **(context or {})}
        for key, known in identity.items():
            declared = envelope.get(key)
            if known is not None and declared is not None and declared != known:
                return {}, (
                    f"{agent.name} while {what}: the envelope says {key}={declared!r}, but "
                    f"the loop dispatched it with {key}={known!r}"
                )
        stamped = self.stamp(agent, envelope, context)
        try:
            validate_handoff(stamped, known_agents=self.agents)
        except ValidationError as exc:
            return {}, f"{agent.name} while {what}: {exc}"
        return stamped, None

    @staticmethod
    def stamp(
        agent: AgentDefinition, envelope: dict[str, Any], context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fill in what the loop is the authority on, and leave the substance alone.

        The loop dispatched the call, so it knows which agent ran, for which
        phase, on which iteration, against which step and which gate. Making
        the agent echo those back adds a way to be wrong and no way to be
        right: an omission would become a rejected answer, and a disagreement
        is caught above rather than trusted.

        What is deliberately *not* stamped is everything only the agent can
        supply -- a plan's steps and their sources, a step's status and runs, a
        report's document. That is what the validation is actually checking, and
        stamping any of it would make the check vacuous.
        """
        stamped = dict(envelope)
        expected = KIND_BY_ROLE.get(agent.role)
        if expected:
            stamped.setdefault("kind", expected)
        stamped.setdefault("agent", agent.name)
        for key, value in (context or {}).items():
            if value is not None:
                stamped.setdefault(key, value)
        return stamped


# -- starting one ------------------------------------------------------------


def start(
    ledger: JobLedger,
    gate: str,
    objective: str,
    *,
    resources: dict[str, Any] | None = None,
    scenario: str = "",
    job_id: str | None = None,
) -> J.Job:
    """Open a job from a person's brief.

    The Brief is stored with the job so the loop's first input is in the
    evidence chain like everything after it -- including, later, the question
    of whether what was built is what was asked for.
    """
    brief = {
        "kind": "Brief",
        "agent": "human",
        "scenario": scenario or objective,
        "resources": resources or {},
    }
    # Raised, not blocked: there is no job yet to stop, and a job opened around
    # an unusable brief would be a job nobody can act on.
    validate_handoff(brief)
    return ledger.create_job(gate, objective, job_id=job_id, brief=brief)
