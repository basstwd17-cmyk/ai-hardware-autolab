"""User questions and replies derived from existing Plan/steer records.

No approval or outcome is inferred from an answer. Only a new planning turn
becomes eligible; device authority is still checked at actual execution.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from QKD_PROJ.orchestrator import jobs as J
from QKD_PROJ.orchestrator.jobs import Handoff, JobLedger
from QKD_PROJ.orchestrator.pagination import iter_events

USER_INPUT = "user_input"
LEGACY_EMPTY_PLAN_ERROR = (
    "scheduler_failure: LoopError: the plan carries no advance conditions, "
    "so nothing about this gate can be decided"
)


def plan_question(plan: dict[str, Any]) -> str | None:
    """A nonterminal, explicitly blocked Plan with no executable steps."""
    if plan.get("steps") != []:
        return None
    reason = plan.get("blocked_on")
    if not isinstance(reason, str) or not reason.strip():
        return None
    if reason.strip() in ("contract_gap", "exhausted"):
        return None
    question = plan.get("clarification_question")
    return question.strip() if isinstance(question, str) and question.strip() else reason.strip()


@dataclass(frozen=True)
class Clarification:
    plan: Handoff
    question: str
    answers: list[dict[str, Any]]


def pending_clarification(ledger: JobLedger, job_id: str) -> Clarification | None:
    job = ledger.job(job_id)
    if job.state not in (J.RUNNING, J.BLOCKED):
        return None
    plans = ledger.handoffs(job_id, "Plan", job.iteration)
    if not plans:
        return None
    plan = plans[-1]
    question = plan_question(plan.payload)
    if not question or ledger.handoffs(job_id, "StepResult", job.iteration) or ledger.handoffs(job_id, "Verdict", job.iteration):
        return None
    answers = []
    block_reason = ""
    for event in iter_events(ledger, job_id, since=plan.event_id or 0):
        if event.kind == "job.blocked":
            block_reason = str(event.payload.get("reason", ""))
        if event.kind == "control.steer":
            message = event.payload.get("message")
            if isinstance(message, str) and message.strip():
                answers.append({"event_id": event.event_id, "message": message, "by": event.payload.get("by")})
    # An unrelated runtime/unknown-hardware failure must not be unlocked by
    # ordinary feedback. Recognize the exact old empty-Plan defect only.
    if job.state == J.BLOCKED and not (
        block_reason.startswith(USER_INPUT + ":") or block_reason == LEGACY_EMPTY_PLAN_ERROR
    ):
        return None
    return Clarification(plan, question, answers)
