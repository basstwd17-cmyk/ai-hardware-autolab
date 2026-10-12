"""Small, path-free job cards for the remote control surface.

The phone needs enough context to make a steering or approval decision, but it
does not need the local transcript or filesystem.  This projection is made
only from durable ledger rows and the scheduler's current answer.  Verdict
counts are calculated by the same deterministic helpers as the local result
card; model-authored handoff summaries remain clearly labelled as handoffs.
"""

from __future__ import annotations

from typing import Any, Callable

from ai_hardware_autolab.orchestrator import jobs as J
from ai_hardware_autolab.orchestrator.jobs import Handoff, Job, JobLedger
from ai_hardware_autolab.orchestrator.results import condition_counts, evidence_runs, summarise

MAX_RECENT = 5
MAX_CONDITIONS = 8
MAX_RUNS = 5


def build_remote_snapshot(
    ledger: JobLedger,
    status: dict[str, Any],
    *,
    limit: int = 24,
    phase_for: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Return recent jobs with every unfinished job retained.

    Finished history is bounded; active work is never dropped merely because a
    newer finished job exists.  The snapshot is overwritten in D1, so these
    cards do not create a second history.
    """
    waiting = _waiting_rows(status.get("jobs"))
    all_jobs = ledger.jobs()
    active = [job for job in all_jobs if job.state != J.DONE]
    finished = [job for job in all_jobs if job.state == J.DONE]
    selected = active + finished[: max(0, limit - len(active))]
    cards = [
        _job_card(
            ledger,
            job,
            waiting.get(job.job_id, {}),
            _phase(phase_for, job.job_id),
        )
        for job in selected
    ]
    inbox = status.get("inbox")
    return {
        "at": status.get("at"),
        "cursor": int(status.get("cursor", ledger.latest_event_id())),
        "jobs": cards,
        "inbox": [
            _approval_card(row) for row in inbox if isinstance(row, dict)
        ] if isinstance(inbox, list) else [],
        "totalJobs": len(all_jobs),
        "truncatedJobs": max(0, len(all_jobs) - len(selected)),
    }


def _job_card(
    ledger: JobLedger,
    job: Job,
    waiting: dict[str, Any],
    phase: str | None,
) -> dict[str, Any]:
    handoffs = ledger.handoffs(job.job_id)
    plan = _latest(handoffs, "Plan", iteration=job.iteration) or _latest(handoffs, "Plan")
    verdict = _latest(handoffs, "Verdict")
    report = _latest(handoffs, "Report")
    recent = [_activity(item) for item in handoffs[-MAX_RECENT:]]

    progress = _progress(plan, handoffs, job.iteration)
    next_action = _next_action(job, waiting, phase, plan, handoffs, verdict)
    result = _result(job, verdict, report, handoffs[-1] if handoffs else None)
    current_agent = (
        str(next_action.get("agent"))
        if isinstance(next_action, dict) and next_action.get("agent")
        else handoffs[-1].agent if handoffs else None
    )
    return {
        **job.describe(),
        "scheduler_status": waiting.get("status"),
        "detail": _clip(waiting.get("detail"), 500),
        "retry_at": waiting.get("retry_at"),
        "approval_id": waiting.get("approval_id"),
        "phase": phase or (handoffs[-1].kind if handoffs else job.state),
        "current_agent": current_agent,
        "progress": progress,
        "result": result,
        "next_action": next_action,
        "recent_activity": recent,
    }


def _result(
    job: Job,
    verdict: Handoff | None,
    report: Handoff | None,
    latest: Handoff | None,
) -> dict[str, Any] | None:
    if verdict is not None:
        payload = verdict.payload
        counts = condition_counts(payload)
        conditions = []
        for row in (payload.get("conditions") or [])[:MAX_CONDITIONS]:
            if not isinstance(row, dict):
                continue
            evidence = [item for item in row.get("evidence", []) if isinstance(item, str)]
            conditions.append({
                "field": _clip(row.get("field"), 160),
                "status": str(row.get("status") or ("met" if row.get("met") else "unmet")),
                "met": row.get("met") is True,
                "evidence": evidence[:3],
            })
        result: dict[str, Any] = {
            "provenance": "verdict",
            "outcome": job.outcome or payload.get("outcome"),
            "summary": summarise(payload, counts),
            "counts": counts,
            "conditions": conditions,
            "evidence_runs": evidence_runs(payload)[:MAX_RUNS],
            "sha256": verdict.sha256,
        }
        if report is not None:
            result["report"] = {
                "outcome": report.payload.get("outcome"),
                "label": _basename(report.payload.get("document")),
                "sha256": report.sha256,
            }
        return result

    if latest is not None:
        summary = _payload_summary(latest.payload)
        if summary:
            return {
                "provenance": "handoff",
                "source_kind": latest.kind,
                "outcome": job.outcome or latest.payload.get("outcome") or latest.payload.get("status"),
                "summary": summary,
                "sha256": latest.sha256,
            }
    if job.state == J.DONE:
        return {
            "provenance": "job",
            "outcome": job.outcome,
            "summary": f"{job.gate}: {job.outcome or 'done'}",
        }
    return None


def _progress(
    plan: Handoff | None,
    handoffs: list[Handoff],
    iteration: int,
) -> dict[str, int] | None:
    if plan is None:
        return None
    steps = [row for row in plan.payload.get("steps", []) if isinstance(row, dict)]
    if not steps:
        return None
    reported = {
        str(item.payload.get("step_id"))
        for item in handoffs
        if item.kind == "StepResult"
        and item.iteration == iteration
        and item.payload.get("step_id") is not None
    }
    planned = {str(step.get("id")) for step in steps if step.get("id") is not None}
    return {"reported": len(reported & planned), "total": len(steps)}


def _next_action(
    job: Job,
    waiting: dict[str, Any],
    phase: str | None,
    plan: Handoff | None,
    handoffs: list[Handoff],
    verdict: Handoff | None,
) -> dict[str, Any] | None:
    status = str(waiting.get("status") or "")
    if status == "waiting_approval":
        return {"kind": "approval", "label": _clip(waiting.get("detail"), 300)}
    if status == "waiting_person":
        return {"kind": "human", "label": _clip(waiting.get("detail"), 300)}
    if status == "waiting_retry":
        return {
            "kind": "retry",
            "label": _clip(waiting.get("detail"), 300),
            "retry_at": waiting.get("retry_at"),
        }
    if job.state == J.DONE:
        return None

    if plan is not None:
        steps = [row for row in plan.payload.get("steps", []) if isinstance(row, dict)]
        reported = {
            str(item.payload.get("step_id"))
            for item in handoffs
            if item.kind == "StepResult" and item.iteration == job.iteration
        }
        for step in steps:
            if str(step.get("id")) in reported:
                continue
            return {
                "kind": "step",
                "step_id": step.get("id"),
                "agent": step.get("agent"),
                "tool": step.get("tool"),
                "label": _clip(step.get("intent") or step.get("id"), 300),
                "touches_hardware": step.get("touches_hardware") is True,
            }
        current_verdict = verdict is not None and verdict.iteration == job.iteration
        if not current_verdict:
            return {"kind": "verify", "agent": "verifier", "label": "reported steps의 증거 판정"}
        if verdict is not None and verdict.payload.get("outcome") == "open":
            return {"kind": "replan", "agent": "lead-agent", "label": "다음 iteration 재계획"}

    phase_actions = {
        "mission_plan": ("lead-agent", "시나리오·기존 지식·필요 자료 계획"),
        "research_plan": ("system-architect", "조사 질문과 출처 계획"),
        "research": ("researcher", "데이터시트·공식 자료 조사"),
        "change_analysis": ("change-analyst", "기존 기준과 조사 결과의 차이 분석"),
        "architecture": ("architecture-selector", "구현 구조 선택"),
        "contract_draft": ("contract-drafter", "실행 계약 초안 작성"),
        "contract_review": ("contract-reviewer", "계약의 근거와 실행 범위 검토"),
        "decide": ("platform", "검토 결과에 따른 다음 workflow 결정"),
        "plan": ("lead-agent", "자료 확인과 실행 구조 계획"),
        "reduce": ("verifier", "run 기록으로 조건 판정"),
        "report": ("reporter", "검증 결과 보고"),
    }
    if phase in phase_actions:
        agent, label = phase_actions[phase]
        return {"kind": phase, "agent": agent, "label": label}
    return None


def _activity(handoff: Handoff) -> dict[str, Any]:
    return {
        "kind": handoff.kind,
        "agent": handoff.agent,
        "at": handoff.at,
        "summary": _payload_summary(handoff.payload),
        "sha256": handoff.sha256,
    }


def _payload_summary(payload: dict[str, Any]) -> str:
    for key in ("summary", "objective", "intent"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, 300)
    outcome = payload.get("outcome") or payload.get("status")
    if isinstance(outcome, str) and outcome.strip():
        return _clip(outcome, 300)
    contract = payload.get("contract_name")
    if isinstance(contract, str) and contract.strip():
        return _clip(contract, 300)
    return ""


def _latest(
    handoffs: list[Handoff], kind: str, iteration: int | None = None
) -> Handoff | None:
    rows = [
        item for item in handoffs
        if item.kind == kind and (iteration is None or item.iteration == iteration)
    ]
    return rows[-1] if rows else None


def _waiting_rows(value: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        rows = [row for group in value.values() if isinstance(group, list) for row in group]
    else:
        rows = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("job_id"), str):
            found[row["job_id"]] = row
    return found


def _approval_card(row: dict[str, Any]) -> dict[str, Any]:
    """Pending authority without local artifact references or paths."""
    artifacts = row.get("artifacts")
    digests = list(artifacts.values()) if isinstance(artifacts, dict) else []
    return {
        "approval_id": row.get("approval_id"),
        "job_id": row.get("job_id"),
        "contract": row.get("contract"),
        "step_id": row.get("step_id"),
        "scope": _clip(row.get("scope"), 600),
        "artifacts": [
            {"sha256": digest}
            for digest in digests
            if isinstance(digest, str) and len(digest) == 64
        ],
        "max_executions": row.get("max_executions"),
        "requested_at": row.get("requested_at"),
        "requested_by": row.get("requested_by"),
        "expires_at": row.get("expires_at"),
        "state": row.get("state"),
        "bundle_sha256": row.get("bundle_sha256"),
    }


def _phase(phase_for: Callable[[str], str] | None, job_id: str) -> str | None:
    if phase_for is None:
        return None
    try:
        value = phase_for(job_id)
    except Exception:  # a display projection must not stop the scheduler
        return None
    return value if isinstance(value, str) and value else None


def _basename(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\\", "/").rstrip("/").split("/")[-1][:160]


def _clip(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text if len(text) <= maximum else text[: maximum - 1] + "…"
