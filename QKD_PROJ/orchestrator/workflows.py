"""Route a durable job to the workflow named by its recorded Brief."""

from __future__ import annotations

from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop
from QKD_PROJ.research.discovery_loop import DiscoveryLoop
from QKD_PROJ.orchestrator.continuation import MissionContinuation


class WorkflowRouter:
    """A small ``advance(job_id)`` seam shared by the daemon scheduler."""

    def __init__(
        self,
        ledger: JobLedger,
        execution: Loop,
        discovery: DiscoveryLoop,
        continuation: MissionContinuation | None = None,
    ) -> None:
        self.ledger = ledger
        self.execution = execution
        self.discovery = discovery
        self.continuation = continuation
        # The control plane reads this to answer whether late steering can be
        # delivered. Discovery has no iteration ceiling; execution does.
        self.policy = execution.policy
        # Compatibility for code that audits which campaign authority the
        # daemon is carrying. The router owns no authority of its own.
        self.auto_approver = execution.auto_approver

    def advance(self, job_id: str):
        if self.discovery.owns(job_id):
            advanced = self.discovery.advance(job_id)
            if advanced.done and self.continuation is not None:
                self.continuation.continue_job(job_id)
            return advanced
        return self.execution.advance(job_id)

    def recover(self) -> list[str]:
        return self.continuation.recover() if self.continuation is not None else []

    def continue_completed(self, job_id: str):
        if self.continuation is None:
            return None
        return self.continuation.continue_job(job_id)

    def phase(self, job_id: str):
        if self.discovery.owns(job_id):
            return self.discovery.phase(job_id).name
        return self.execution.phase(job_id)
