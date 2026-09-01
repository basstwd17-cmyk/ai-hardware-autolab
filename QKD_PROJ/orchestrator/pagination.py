"""Reading a whole event history out of a frozen ledger.

``JobLedger.events_since(cursor, job_id, limit)`` returns the *oldest* `limit`
events after a cursor. Every caller in this package that wanted "the recent
history" was passing ``limit=10_000`` and reading the first ten thousand
events, which is the opposite of what it needed: on a job with more than that,
the newest steer, the newest block and the newest approval were all invisible.

It went unnoticed because nothing produced ten thousand events per job. The
runtime recorder does -- one real Codex turn emits several hundred
notifications, most of them token deltas -- so the bound was about to be
crossed by an ordinary run.

The ledger is frozen, so the fix is here: page forward until the ledger stops
returning rows. Each helper below is written so it holds only what it needs,
because "read the whole history into a list" is the other way to fall over on
a long job.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from QKD_PROJ.orchestrator.jobs import Event, JobLedger

DEFAULT_PAGE = 1_000


def iter_events(
    ledger: JobLedger,
    job_id: str | None = None,
    since: int = 0,
    page: int = DEFAULT_PAGE,
) -> Iterator[Event]:
    """Every event after `since`, oldest first, without holding them all."""
    cursor = int(since)
    while True:
        batch = ledger.events_since(cursor, job_id, page)
        if not batch:
            return
        for event in batch:
            yield event
        if len(batch) < page:
            return
        cursor = batch[-1].event_id


def last_event(
    ledger: JobLedger,
    job_id: str,
    predicate: Callable[[Event], bool],
    page: int = DEFAULT_PAGE,
) -> Event | None:
    """The most recent event matching `predicate`, or None."""
    found: Event | None = None
    for event in iter_events(ledger, job_id, page=page):
        if predicate(event):
            found = event
    return found


def tail_since(
    ledger: JobLedger,
    job_id: str,
    boundary: Callable[[Event], bool],
    page: int = DEFAULT_PAGE,
) -> list[Event]:
    """Events after the last one matching `boundary`.

    Used wherever a question is "what has happened since X" -- steers since the
    last handoff, blocks since the last progress. Resetting the accumulator on
    each boundary keeps the memory bounded by the tail rather than the history.
    """
    tail: list[Event] = []
    for event in iter_events(ledger, job_id, page=page):
        if boundary(event):
            tail = []
        else:
            tail.append(event)
    return tail


def reduce_events(
    ledger: JobLedger,
    job_id: str,
    step: Callable[[Any, Event], Any],
    initial: Any,
    page: int = DEFAULT_PAGE,
) -> Any:
    """Fold the whole history without materialising it."""
    state = initial
    for event in iter_events(ledger, job_id, page=page):
        state = step(state, event)
    return state
