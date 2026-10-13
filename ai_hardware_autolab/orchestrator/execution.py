"""Scheduler result values, independent of any execution engine."""
from dataclasses import dataclass
from typing import Any

BLOCKED_RUNTIME="runtime_failure"
BLOCKED_RATE_LIMIT="rate_limited"
BLOCKED="blocked"
FINISHED="finished"

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
