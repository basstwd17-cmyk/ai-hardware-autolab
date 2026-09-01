"""Deterministic App Server peer for codex_runner protocol tests."""

from __future__ import annotations

import json
import sys


def send(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


experimental_api = False

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        experimental_api = (
            message.get("params", {})
            .get("capabilities", {})
            .get("experimentalApi")
            is True
        )
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        if message.get("params", {}).get("dynamicTools") and not experimental_api:
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32600,
                        "message": "thread/start.dynamicTools requires experimentalApi capability",
                    },
                }
            )
            continue
        send({"method": "thread/started", "params": {"thread": {"id": "thr_fake"}}})
        send({"id": request_id, "result": {"thread": {"id": "thr_fake"}}})
    elif method == "turn/start":
        send(
            {
                "method": "mcpServer/elicitation/request",
                "id": 900,
                "params": {
                    "threadId": "thr_fake",
                    "turnId": "turn_fake",
                    "serverName": "rtase-hwtools",
                    "message": 'Allow the rtase-hwtools MCP server to run tool "list_contracts"?',
                },
            }
        )
        approval = json.loads(sys.stdin.readline())
        if approval.get("id") != 900 or approval.get("result", {}).get("action") != "accept":
            raise SystemExit("client did not accept the fake read-only MCP request")
        send(
            {
                "id": request_id,
                "result": {
                    "turn": {
                        "id": "turn_fake",
                        "status": "inProgress",
                        "items": [],
                        "error": None,
                    }
                },
            }
        )
        handoff = json.dumps({"kind": "Plan", "agent": "gate-architect", "steps": []})
        envelope = json.dumps(
            {"kind": "Plan", "agent": "gate-architect", "handoff": handoff}
        )
        item = {
            "type": "agentMessage",
            "id": "msg_fake",
            "text": envelope,
            "phase": "final_answer",
        }
        send(
            {
                "method": "item/completed",
                "params": {"threadId": "thr_fake", "turnId": "turn_fake", "item": item},
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thr_fake",
                    "turn": {
                        "id": "turn_fake",
                        "status": "completed",
                        "items": [item],
                        "error": None,
                    },
                },
            }
        )
    elif method == "thread/unsubscribe":
        send({"id": request_id, "result": {"status": "unsubscribed"}})
    else:
        send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"unsupported fake method: {method}"},
            }
        )
