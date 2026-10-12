"""Derive unresolved execution from durable records, not a process stack."""
from .pagination import iter_events


def blocked_step(ledger, job_id):
    job = ledger.job(job_id)
    rows = ledger.handoffs(job_id, 'StepResult', job.iteration)
    return next((row for row in rows if row.payload.get('status') == 'blocked'
                 or row.payload.get('hardware_state_unknown') is True), None)


def unfinished_dispatch(ledger, job_id):
    job = ledger.job(job_id)
    plans = ledger.handoffs(job_id, 'Plan', job.iteration)
    if not plans:
        return None
    reported = {row.step_id for row in ledger.handoffs(job_id, 'StepResult', job.iteration)}
    return next((event for event in iter_events(ledger, job_id, since=plans[-1].event_id)
                 if event.kind == 'step.dispatch.started'
                 and event.payload.get('iteration') == job.iteration
                 and event.payload.get('step_id') not in reported), None)
