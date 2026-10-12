from __future__ import annotations

import sqlite3
from pathlib import Path

from QKD_PROJ.orchestrator import cloud_relay as R
from QKD_PROJ.orchestrator import wire as W


TOKEN = "device-token-" + "x" * 32


def test_knowledge_replica_resumes_from_cloud_cursor(monkeypatch):
    calls = []
    def daemon(frame, address, timeout):
        assert frame['type'] == 'knowledge.events' and frame['after'] == 12
        assert frame['limit'] == 100
        return {'ok':True,'knowledge':{'events':[{'event_id':13}], 'cursor':13,'latest':15}}
    def cloud(path, body):
        calls.append((path,body))
        return {'ok':True,'cursor':12 if 'events' not in body else 13}
    monkeypatch.setattr(R.S,'send',daemon)
    result = R.CloudRelayClient('https://rtase.pages.dev','pc',TOKEN,request=cloud).sync_knowledge()
    assert result['cursor'] == 13
    assert calls[-1][1] == {'deviceId':'pc','after':12,'events':[{'event_id':13}],'latest':15}


def test_tick_mirrors_status_and_delivers_one_steer(monkeypatch):
    daemon_calls: list[dict] = []

    def daemon(frame, address, timeout):
        daemon_calls.append(frame)
        if frame.get("command") == "remote-summary":
            return {
                "ok": True,
                "snapshot": {
                    "cursor": 7,
                    "jobs": [{
                        "job_id": "job_1",
                        "state": "pending",
                        "scheduler_status": "runnable",
                        "result": {"summary": "build ready"},
                    }],
                    "inbox": [],
                    "totalJobs": 1,
                    "truncatedJobs": 0,
                },
            }
        if frame.get("command") == "status":
            return {
                "ok": True,
                "status": {
                    "cursor": 7,
                    "jobs": {
                        "runnable": [{"job_id": "job_1", "state": "pending"}],
                        "waiting_approval": [],
                    },
                    "inbox": [],
                },
            }
        assert frame["type"] == W.JOB_STEER
        return {"type": W.COMMAND_RESULT, "commandId": "cmd_1", "ok": True}

    cloud_calls: list[tuple[str, dict]] = []

    def cloud(path, body):
        cloud_calls.append((path, body))
        if path.endswith("/poll"):
            return {
                "ok": True,
                "command": {
                    "commandId": "cmd_1",
                    "kind": "job.steer",
                    "payload": {
                        "jobId": "job_1",
                        "sentBy": "quantum1",
                        "message": "check RF1 first",
                    },
                },
            }
        return {"ok": True}

    monkeypatch.setattr(R.S, "send", daemon)
    result = R.CloudRelayClient(
        "https://rtase.pages.dev", "lab-pc", TOKEN, request=cloud
    ).tick()

    assert result["result"]["ok"] is True
    assert cloud_calls[0][0].endswith("/snapshot")
    assert cloud_calls[0][1]["cursor"] == 7
    assert cloud_calls[0][1]["snapshot"]["jobs"][0]["scheduler_status"] == "runnable"
    assert cloud_calls[0][1]["snapshot"]["jobs"][0]["result"]["summary"] == "build ready"
    assert cloud_calls[-1][0].endswith("/result")
    assert cloud_calls[-1][1]["commandId"] == "cmd_1"
    assert daemon_calls[-1]["message"] == "check RF1 first"


def test_mission_start_uses_the_preflight_digest(monkeypatch):
    sent: list[dict] = []

    def daemon(frame, address, timeout):
        sent.append(frame)
        if frame["type"] == W.SCENARIO_PREFLIGHT:
            return {
                "type": W.COMMAND_RESULT,
                "ok": True,
                "preflight": {"scenario_sha256": "a" * 64},
            }
        return {"type": W.COMMAND_RESULT, "ok": True, "job": {"job_id": "job_new"}}

    monkeypatch.setattr(R.S, "send", daemon)
    client = R.CloudRelayClient(
        "https://rtase.pages.dev", "lab-pc", TOKEN, request=lambda *_: {"ok": True}
    )
    answer = client.execute({
        "commandId": "cmd_mission",
        "kind": "mission.start",
        "payload": {
            "objective": "implement timing",
            "scenario": "Build the requested timing path without hardware.",
            "searchPolicy": "refresh",
        },
    })

    assert answer["job"]["job_id"] == "job_new"
    assert sent[1]["type"] == W.JOB_MISSION
    assert sent[1]["scenarioSha256"] == "a" * 64


def test_phone_approval_uses_the_structured_decision_wire_command(monkeypatch):
    sent: list[dict] = []

    def daemon(frame, address, timeout):
        sent.append(frame)
        return {"type": W.COMMAND_RESULT, "commandId": frame["commandId"], "ok": True}

    monkeypatch.setattr(R.S, "send", daemon)
    client = R.CloudRelayClient(
        "https://rtase.pages.dev", "lab-pc", TOKEN, request=lambda *_: {"ok": True}
    )
    answer = client.execute({
        "commandId": "cmd_approval",
        "kind": "approval.decide",
        "payload": {
            "approvalId": "approval_1",
            "decision": "accepted",
            "signedBy": "quantum1-phone",
            "note": "RF outputs are connected to scope CH1 and CH2.",
        },
    })

    assert answer["ok"] is True
    assert sent == [{
        "type": W.APPROVAL_DECIDE,
        "commandId": "cmd_approval",
        "approvalId": "approval_1",
        "decision": "accepted",
        "signedBy": "quantum1-phone",
        "note": "RF outputs are connected to scope CH1 and CH2.",
    }]


def test_phone_approval_refuses_an_unstructured_decision():
    client = R.CloudRelayClient(
        "https://rtase.pages.dev", "lab-pc", TOKEN, request=lambda *_: {"ok": True}
    )
    try:
        client.execute({
            "commandId": "cmd_approval",
            "kind": "approval.decide",
            "payload": {
                "approvalId": "approval_1",
                "decision": "yes",
                "signedBy": "quantum1-phone",
            },
        })
    except R.CloudRelayError as exc:
        assert "accepted" in str(exc)
        assert "declined" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an ambiguous approval decision reached the daemon")


def test_relay_refuses_plain_http_and_unknown_commands():
    try:
        R.CloudRelayClient("http://example.test", "lab-pc", TOKEN)
    except R.CloudRelayError as exc:
        assert "HTTPS" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("plain HTTP relay was accepted")

    client = R.CloudRelayClient(
        "https://rtase.pages.dev", "lab-pc", TOKEN, request=lambda *_: {"ok": True}
    )
    try:
        client.execute({"commandId": "x", "kind": "shell", "payload": {}})
    except R.CloudRelayError as exc:
        assert "not allowed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an arbitrary cloud command reached the daemon")


def test_d1_mailbox_schema_enforces_command_identity_and_states():
    schema = (
        Path(__file__).resolve().parents[1] / "webui" / "cloudflare" / "schema.sql"
    ).read_text(encoding="utf-8")
    database = sqlite3.connect(":memory:")
    database.executescript(schema)
    database.execute(
        "INSERT INTO commands (command_id, device_id, kind, payload, state, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("cmd", "lab-pc", "job.steer", "{}", "pending", 1, 2),
    )
    try:
        database.execute(
            "INSERT INTO commands (command_id, device_id, kind, payload, state, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("cmd", "lab-pc", "job.cancel", "{}", "pending", 1, 2),
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("D1 schema accepted a duplicate command identity")
