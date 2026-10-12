from __future__ import annotations

from fastapi.testclient import TestClient

from ai_hardware_autolab.orchestrator import wire as W
from ai_hardware_autolab.webui.backend.app import DEFAULT_PORT, create_app
from ai_hardware_autolab.webui.backend.remote_approval import (
    RemoteApprovalError,
    RemoteApprovalStore,
)
from ai_hardware_autolab.webui.backend.security import Session


REVIEW = "a" * 64
PROPOSAL = {
    "job_id": "job_remote",
    "contract": "generated.yaml",
    "contract_sha256": "b" * 64,
    "scenario_sha256": "c" * 64,
    "board": {"board_id": "example", "revision": "v1", "title": "Example"},
    "permissions": [{
        "id": "measure", "role": "measurement", "tool": "measure_board_scope",
        "phase": "measure", "contract": "generated.yaml", "artifact_mode": "pinned",
        "max_approvals": 4, "max_executions": 1,
    }],
    "blockers": [],
    "ready": True,
    "max_iterations": 4,
    "max_auto_approvals": 8,
    "duration_hours": 6,
    "review_sha256": REVIEW,
}


def test_remote_capability_decides_once_but_controls_the_bound_job_until_expiry():
    current = [1000.0]
    store = RemoteApprovalStore(clock=lambda: current[0])
    token, _ = store.create(
        job_id="job_remote", proposal=PROPOSAL, ttl_minutes=1,
        max_iterations=4, max_auto_approvals=8, duration_hours=6,
    )

    row = store.claim(token, REVIEW)
    assert row.job_id == "job_remote"
    store.complete(token, state="approved", active_job_id="job_child")
    assert store.read(token).active_job_id == "job_child"
    try:
        store.claim(token, REVIEW)
    except RemoteApprovalError as exc:
        assert exc.status == 409
    else:  # pragma: no cover - the approval decision itself is single-use
        raise AssertionError("a second approval decision was accepted")

    expiring, _ = store.create(
        job_id="job_remote", proposal=PROPOSAL, ttl_minutes=1,
        max_iterations=4, max_auto_approvals=8, duration_hours=6,
    )
    current[0] += 61
    try:
        store.read(expiring)
    except RemoteApprovalError as exc:
        assert exc.status == 410
    else:  # pragma: no cover
        raise AssertionError("expired token was accepted")


def test_remote_page_needs_only_its_narrow_token_and_approves_exact_digest():
    session = Session(file=None)
    app = create_app(address=None, session=session, port=DEFAULT_PORT)
    calls: list[dict] = []

    def command(frame):
        calls.append(frame)
        if frame["type"] == W.CAMPAIGN_BUNDLE_PREVIEW:
            return {"ok": True, "proposal": PROPOSAL}
        if frame["type"] == W.CAMPAIGN_BUNDLE_APPROVE:
            return {
                "ok": True,
                "campaign": {"active": True},
                "continuation": {"child_job_id": "job_child"},
            }
        if frame["type"] == W.JOB_STEER:
            return {"ok": True, "willBeDelivered": True, "pending": []}
        if frame["type"] == W.JOB_CANCEL:
            return {"ok": True, "job": {"job_id": "job_child", "state": "done"}}
        raise AssertionError(frame)

    def control(frame):
        assert frame == {"command": "job", "job_id": "job_child"}
        return {
            "ok": True,
            "job": {
                "job": {
                    "job_id": "job_child", "objective": "run", "state": "pending",
                    "outcome": None, "iteration": 0,
                },
                "waiting": None,
                "will_deliver_steering": True,
                "pending_steers": [],
            },
        }

    app.state.hub.subscription.command = command
    app.state.hub.subscription.control = control
    with TestClient(app, base_url=f"http://127.0.0.1:{DEFAULT_PORT}") as client:
        created = client.post(
            "/api/remote-approvals",
            headers={"Authorization": f"Bearer {session.token}"},
            json={
                "jobId": "job_remote", "ttlMinutes": 15, "maxIterations": 4,
                "maxAutoApprovals": 8, "durationHours": 6,
            },
        )
        assert created.status_code == 200
        path = created.json()["path"]

        # No broad session token: possession of the short-lived path is the
        # only capability this minimal page receives.
        page = client.get(path)
        assert page.status_code == 200
        assert "generated.yaml" in page.text and REVIEW in page.text
        assert session.token not in page.text

        decided = client.post(path, json={"approvedBy": "quantum1", "reviewSha256": REVIEW})
        assert decided.status_code == 200
        assert calls[-1]["type"] == W.CAMPAIGN_BUNDLE_APPROVE
        assert calls[-1]["reviewSha256"] == REVIEW

        replay = client.post(path, json={"approvedBy": "quantum1", "reviewSha256": REVIEW})
        assert replay.status_code == 409

        status = client.get(path + "/status")
        assert status.status_code == 200
        assert status.json()["job"]["job_id"] == "job_child"

        message = client.post(
            path + "/message",
            json={"sentBy": "quantum1", "message": "RF1 first"},
        )
        assert message.status_code == 200
        assert calls[-1]["type"] == W.JOB_STEER

        stopped = client.post(
            path + "/cancel",
            json={"requestedBy": "quantum1", "reason": "bench closed"},
        )
        assert stopped.status_code == 200
        assert calls[-1]["jobId"] == "job_child"


def test_remote_revision_starts_a_new_mission_instead_of_approving_campaign():
    session = Session(file=None)
    app = create_app(address=None, session=session, port=DEFAULT_PORT)
    calls: list[dict] = []

    def command(frame):
        calls.append(frame)
        if frame["type"] == W.CAMPAIGN_BUNDLE_PREVIEW:
            return {"ok": True, "proposal": PROPOSAL}
        if frame["type"] == W.JOB_MISSION_REVISE:
            return {"ok": True, "job": {"job_id": "job_revision", "state": "pending"}}
        raise AssertionError(frame)

    app.state.hub.subscription.command = command
    with TestClient(app, base_url=f"http://127.0.0.1:{DEFAULT_PORT}") as client:
        created = client.post(
            "/api/remote-approvals",
            headers={"Authorization": f"Bearer {session.token}"},
            json={"jobId": "job_remote", "ttlMinutes": 720},
        )
        path = created.json()["path"]
        answer = client.post(path, json={
            "approvedBy": "quantum1",
            "reviewSha256": REVIEW,
            "decision": "revise",
            "comment": "change the timing architecture",
        })
        assert answer.status_code == 200
        assert calls[-1]["type"] == W.JOB_MISSION_REVISE
        assert calls[-1]["message"] == "change the timing architecture"
