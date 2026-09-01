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

**It never edits the ledger's schema or writes an event kind of its own.**
Every event it causes comes from a ``JobLedger`` method. What a runtime
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

from QKD_PROJ.agents.schema import (
    AgentDefinition,
    ValidationError,
    load_all,
    validate_handoff,
)
from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.evidence import Condition, Reduction, reduce_gate
from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.orchestrator.pagination import iter_events

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
        if job.state in (J.AWAITING_APPROVAL, J.BLOCKED):
            return BLOCKED
        if job.iteration == 0:
            return PLAN
        plan = self.latest_plan(job_id, job.iteration)
        if plan is None:
            return PLAN
        reported = self.step_results(job_id, job.iteration)
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
        if job.iteration >= self.policy.max_iterations:
            return self._finish(
                job,
                "exhausted",
                f"reached the {self.policy.max_iterations}-iteration ceiling without converging",
            )

        # phase() only routes here when the current iteration has no plan, so
        # the counter is advanced exactly once: at the start, and by _decide()
        # when it sends the loop round again.
        iteration = job.iteration or self.ledger.begin_iteration(job.job_id)
        architect = self.agent("gate-architect")
        briefs = self.ledger.handoffs(job.job_id, "Brief", 0)
        brief = briefs[-1].payload if briefs else {}
        task = {
            "kind": "Brief" if iteration == 1 else "Verdict",
            "job_id": job.job_id,
            "gate": job.gate,
            "objective": job.objective,
            # The objective is a short title. The durable Brief is the person's
            # actual direction and must reach the architect intact; otherwise
            # a UI-derived title silently becomes the whole assignment.
            "scenario": brief.get("scenario", job.objective),
            "resources": brief.get("resources", {}),
            "iteration": iteration,
            "strikes": self.ledger.strikes(job.job_id),
            "previous_verdict": self._previous_verdict(job.job_id, iteration),
            # The reducer says which conditions lack evidence; the StepResult
            # says why the tool produced none. Omitting the latter made the
            # architect repeat an identical build after a pre-run tool error.
            "previous_step_results": self._previous_step_results(
                job.job_id, iteration
            ),
            "steering": self._steering(job.job_id),
        }
        knowledge_context = brief.get("resources", {}).get("knowledge_context")
        if isinstance(knowledge_context, dict):
            # This is the exact bounded context selected when the job opened,
            # not a fresh query whose answer could change halfway through a
            # reproducible run.
            task["knowledge_context"] = knowledge_context
        envelope = self._call(
            architect, task, job, "planning", {"gate": job.gate, "iteration": iteration}
        )
        if isinstance(envelope, Advance):
            return envelope

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
        steps = len(envelope.get("steps") or [])
        return Advance(PLAN, job.job_id, iteration, f"planned {steps} step(s)", handoff.handoff_id)

    def _steering(self, job_id: str) -> list[dict[str, Any]]:
        """What a person told this job since the last handoff.

        Imported late: the control plane knows about the loop, and a loop that
        imported it at module scope would make the two mutually dependent for
        the sake of one list.
        """
        from QKD_PROJ.orchestrator.control import pending_steers

        return pending_steers(self.ledger, job_id)

    def _undelivered_steering(self, job_id: str) -> list[dict[str, Any]]:
        from QKD_PROJ.orchestrator.control import undelivered_steers

        return undelivered_steers(self.ledger, job_id)

    def _previous_verdict(self, job_id: str, iteration: int) -> dict[str, Any] | None:
        verdicts = [h for h in self.ledger.handoffs(job_id, "Verdict") if h.iteration < iteration]
        return verdicts[-1].payload if verdicts else None

    def _previous_step_results(self, job_id: str, iteration: int) -> list[dict[str, Any]]:
        if iteration <= 1:
            return []
        return [
            handoff.payload
            for handoff in self.ledger.handoffs(job_id, "StepResult", iteration - 1)
        ]

    def _execute(self, job: J.Job) -> Advance:
        plan = self.latest_plan(job.job_id, job.iteration) or {}
        reported = self.step_results(job.job_id, job.iteration)
        step = next(
            (s for s in plan.get("steps") or [] if str(s.get("id", "")) not in reported), None
        )
        if step is None:  # pragma: no cover - phase() already excluded this
            raise LoopError("execute was reached with every step reported")

        step_id = str(step.get("id", ""))
        agent = self.agent(str(step.get("agent", "")))

        if agent.touches_hardware:
            blocked = self._require_approval(job, step, step_id, agent)
            if blocked is not None:
                return blocked

        task = {
            "kind": "Plan",
            "job_id": job.job_id,
            "iteration": job.iteration,
            "step": step,
            "gate": job.gate,
            "steering": self._steering(job.job_id),
        }
        envelope = self._call(agent, task, job, f"step {step_id}", {"step_id": step_id})
        if isinstance(envelope, Advance):
            return envelope

        handoff = self.ledger.record_handoff(
            job.job_id,
            "StepResult",
            agent.name,
            envelope,
            iteration=job.iteration,
            step_id=step_id,
        )
        status = str(envelope.get("status", "done"))
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
        decided = [
            a
            for a in self._approvals_for(job.job_id)
            if a.contract == contract and a.step_id == step_id
        ]
        if any(a.state == J.APPROVAL_GRANTED for a in decided):
            return None
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

        evaluation = None
        policy_error = ""
        if self.auto_approver is not None:
            try:
                evaluation = self.auto_approver.evaluate(job, step, agent)
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
                decided = self.auto_approver.apply(approval, evaluation)
            except Exception as exc:  # noqa: BLE001 - leave the request pending
                policy_error = f"automatic approval recording failed: {exc}"
            else:
                if decided.state == J.APPROVAL_GRANTED:
                    return Advance(
                        EXECUTE,
                        job.job_id,
                        job.iteration,
                        f"step {step_id} was automatically approved by a signed campaign",
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
        reduction: Reduction = reduce_gate(
            gate=job.gate,
            conditions=conditions,
            iteration=job.iteration,
            history=self._signature_history(job.job_id, job.iteration),
            runs_dir=self.runs_dir,
            strikes=self.policy.strikes,
            corroboration=corroboration_for(plan),
        )
        # The reduction comes from deterministic code, so this can only fail if
        # the reducer and the envelope schema have drifted apart -- which they
        # had, and which is exactly the kind of divergence nobody notices until
        # a verdict is rejected months later.
        try:
            validate_handoff(
                self.stamp(
                    self.agent("verifier"),
                    reduction.describe(),
                    {"gate": job.gate, "iteration": job.iteration},
                )
            )
        except ValidationError as exc:
            return self._block(job, BLOCKED_INVALID, f"the reduction is not a valid Verdict: {exc}")

        handoff, outcome = self.ledger.record_reduction(job.job_id, reduction)
        met = sum(1 for c in reduction.conditions if c.met)
        return Advance(
            REDUCE,
            job.job_id,
            job.iteration,
            f"{met}/{len(reduction.conditions)} conditions met; {outcome}",
            handoff.handoff_id,
            outcome=outcome,
        )

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
            return self._finish(job, "satisfied", "every condition met, each with a run behind it")
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
        if job.iteration >= self.policy.max_iterations:
            return self._finish(
                job,
                "exhausted",
                f"reached the {self.policy.max_iterations}-iteration ceiling without converging",
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
            reporter = self.agent("reporter")
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

    def _call(
        self,
        agent: AgentDefinition,
        task: dict[str, Any],
        job: J.Job,
        what: str,
        context: dict[str, Any] | None = None,
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
        try:
            envelope = self.runner(agent, task)
        except Exception as exc:  # noqa: BLE001 - any runtime failure blocks, none concludes
            return self._block(
                job,
                self.classify_failure(exc),
                f"{agent.name} could not run while {what}: {exc}",
            )

        if not isinstance(envelope, dict):
            return self._block(
                job,
                BLOCKED_INVALID,
                f"{agent.name} returned {type(envelope).__name__}, not an object, while {what}",
            )

        identity = {"kind": KIND_BY_ROLE.get(agent.role), "agent": agent.name, **(context or {})}
        for key, known in identity.items():
            declared = envelope.get(key)
            if known is not None and declared is not None and declared != known:
                return self._block(
                    job,
                    BLOCKED_INVALID,
                    f"{agent.name} while {what}: the envelope says {key}={declared!r}, but the "
                    f"loop dispatched it with {key}={known!r}",
                )

        stamped = self.stamp(agent, envelope, context)
        try:
            validate_handoff(stamped, known_agents=self.agents)
        except ValidationError as exc:
            return self._block(job, BLOCKED_INVALID, f"{agent.name} while {what}: {exc}")
        return stamped

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
