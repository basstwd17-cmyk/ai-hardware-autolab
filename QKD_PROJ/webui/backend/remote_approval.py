"""Short-lived, job-scoped capability links for campaign control.

The regular browser session token grants the whole operator UI.  It must not
be copied into a message just so somebody can approve one campaign.  A remote
approval link therefore carries a different, narrower capability: one mission,
one exact review digest, its one resulting job and one expiry.  Approval is
single-use; status, steering and cancellation remain available until expiry.
The token is kept only as a SHA-256 digest and disappears with the web process.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


class RemoteApprovalError(ValueError):
    def __init__(self, detail: str, status: int = 404) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass
class RemoteApproval:
    token_sha256: str
    job_id: str
    review_sha256: str
    proposal: dict[str, Any]
    max_iterations: int
    max_auto_approvals: int
    duration_hours: int
    expires_at: float
    state: str = "ready"
    active_job_id: str | None = None
    decision_result: dict[str, Any] | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "reviewSha256": self.review_sha256,
            "proposal": self.proposal,
            "maxIterations": self.max_iterations,
            "maxAutoApprovals": self.max_auto_approvals,
            "durationHours": self.duration_hours,
            "state": self.state,
            "activeJobId": self.active_job_id,
            "expiresAt": datetime.fromtimestamp(
                self.expires_at, timezone.utc
            ).isoformat(),
        }


class RemoteApprovalStore:
    """In-memory capabilities with one atomic approval decision.

    The token remains readable after that decision because it is also the
    narrow control channel for the resulting job.  It never gains authority
    over a second job.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._rows: dict[str, RemoteApproval] = {}
        self._lock = threading.Lock()

    def create(
        self,
        *,
        job_id: str,
        proposal: dict[str, Any],
        ttl_minutes: int,
        max_iterations: int,
        max_auto_approvals: int,
        duration_hours: int,
    ) -> tuple[str, RemoteApproval]:
        review = proposal.get("review_sha256")
        if not isinstance(review, str) or len(review) != 64:
            raise RemoteApprovalError("proposal has no full review digest", 409)
        if proposal.get("ready") is not True:
            blockers = proposal.get("blockers") or []
            raise RemoteApprovalError(
                "campaign is not ready: " + "; ".join(str(item) for item in blockers), 409
            )
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        row = RemoteApproval(
            digest,
            job_id,
            review,
            dict(proposal),
            max_iterations,
            max_auto_approvals,
            duration_hours,
            self.clock() + ttl_minutes * 60,
        )
        with self._lock:
            self._rows[digest] = row
        return token, row

    def read(self, token: str) -> RemoteApproval:
        digest = self._digest(token)
        with self._lock:
            row = self._rows.get(digest)
            self._check_alive(row)
            assert row is not None
            return row

    def claim(self, token: str, review_sha256: str) -> RemoteApproval:
        digest = self._digest(token)
        with self._lock:
            row = self._rows.get(digest)
            self._check_alive(row)
            assert row is not None
            if row.state == "processing":
                raise RemoteApprovalError("this approval is already being processed", 409)
            if row.state != "ready":
                raise RemoteApprovalError("this approval decision was already made", 409)
            if not secrets.compare_digest(row.review_sha256, review_sha256):
                raise RemoteApprovalError("the displayed review digest does not match", 409)
            row.state = "processing"
            return row

    def complete(
        self,
        token: str,
        *,
        state: str,
        active_job_id: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        if state not in ("approved", "revision_requested", "refused"):
            raise ValueError(f"invalid remote decision state: {state}")
        digest = self._digest(token)
        with self._lock:
            row = self._rows.get(digest)
            if row is not None and row.state == "processing":
                row.state = state
                row.active_job_id = active_job_id
                row.decision_result = dict(result or {})

    def release(self, token: str) -> None:
        """Make a transiently failed decision attempt available again."""
        digest = self._digest(token)
        with self._lock:
            row = self._rows.get(digest)
            if row is not None and row.state == "processing":
                row.state = "ready"

    def _check_alive(self, row: RemoteApproval | None) -> None:
        if row is None:
            raise RemoteApprovalError("this approval link is unknown")
        if self.clock() >= row.expires_at:
            row.state = "expired"
            raise RemoteApprovalError("this approval link expired", 410)

    @staticmethod
    def _digest(token: str) -> str:
        if not isinstance(token, str) or not token:
            raise RemoteApprovalError("this approval link is unknown")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
