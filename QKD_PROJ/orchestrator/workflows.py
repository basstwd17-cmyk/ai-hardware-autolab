"""Route a durable job to the workflow named by its recorded Brief."""

from __future__ import annotations

from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop
from QKD_PROJ.research.discovery_loop import DiscoveryLoop
from QKD_PROJ.orchestrator.continuation import MissionContinuation
from QKD_PROJ.orchestrator.simple_mission import owns_simple_mission


class WorkflowRouter:
    """A small ``advance(job_id)`` seam shared by the daemon scheduler."""

    def __init__(
        self,
        ledger: JobLedger,
        execution: Loop | None = None,
        discovery: DiscoveryLoop | None = None,
        continuation: MissionContinuation | None = None,
        simple: Loop | None = None,
        conversation=None,
    ) -> None:
        self.ledger = ledger
        self.execution = execution
        self.discovery = discovery
        self.continuation = continuation
        self.simple = simple
        self.conversation = conversation
        # The control plane reads this to answer whether late steering can be
        # delivered. Discovery has no iteration ceiling; execution does.
        self.policy = execution.policy if execution is not None else None
        # Compatibility for code that audits which campaign authority the
        # daemon is carrying. The router owns no authority of its own.
        self._auto_approver = execution.auto_approver if execution is not None else None

    @property
    def auto_approver(self):
        return self._auto_approver

    @auto_approver.setter
    def auto_approver(self, value):
        self._auto_approver = value
        if self.execution is not None:
            self.execution.auto_approver = value
        if self.simple is not None:
            self.simple.auto_approver = value

    def supports(self, job_id: str) -> bool:
        """A historical job is readable even when its executor is retired."""
        return bool(
            self.conversation is not None and self.conversation.owns(job_id)
            or self.simple is not None and owns_simple_mission(self.ledger, job_id)
            or self.discovery is not None and self.discovery.owns(job_id)
            or self.execution is not None
        )

    def advance(self, job_id: str):
        if self.conversation is not None and self.conversation.owns(job_id):
            return self.conversation.advance(job_id)
        if self.simple is not None and owns_simple_mission(self.ledger, job_id):
            return self.simple.advance(job_id)
        if self.discovery is not None and self.discovery.owns(job_id):
            advanced = self.discovery.advance(job_id)
            if advanced.done and self.continuation is not None:
                self.continuation.continue_job(job_id)
            return advanced
        if self.execution is None:
            raise ValueError("historical workflow is retired; submit a new scenario to continue development")
        return self.execution.advance(job_id)

    def recover(self) -> list[str]:
        return self.continuation.recover() if self.continuation is not None else []

    def continue_completed(self, job_id: str):
        if self.continuation is None:
            return None
        return self.continuation.continue_job(job_id)

    def phase(self, job_id: str):
        if self.conversation is not None and self.conversation.owns(job_id):
            return self.conversation.phase(job_id).action
        if self.simple is not None and owns_simple_mission(self.ledger, job_id):
            return self.simple.phase(job_id)
        if self.discovery is not None and self.discovery.owns(job_id):
            return self.discovery.phase(job_id).name
        if self.execution is None:
            return "finished" if self.ledger.job(job_id).state == "done" else "legacy_unavailable"
        return self.execution.phase(job_id)

    def resume_step(self, job_id, requested_by, note):
        if self.conversation is not None and self.conversation.owns(job_id):
            raise ValueError("대화형 작업은 질문에 답변할 수 있습니다. 중단된 실행을 기존 step으로 재시도하지 않습니다.")
        loop = self.simple if self.simple is not None and owns_simple_mission(self.ledger, job_id) else self.execution
        if loop is None:
            raise ValueError("historical workflow is retired; submit a new scenario to continue development")
        return loop.resume_step(job_id, requested_by, note)
