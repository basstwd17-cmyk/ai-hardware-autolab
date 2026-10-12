"""Outbound HTTPS bridge between a local daemon and Cloudflare Pages.

Cloudflare is a mailbox, not a second orchestrator.  This process publishes a
compact control-plane snapshot, claims at most one scoped command, sends that
command through the same named-pipe protocol as the local UI, and posts the
answer.  A claimed command is never automatically reclaimed: if this process
dies after the local effect but before posting the result, repeating it could
program hardware or duplicate a mission.  The cloud UI leaves that command in
``claimed`` for a person to reconcile against the local ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ai_hardware_autolab.orchestrator import service as S
from ai_hardware_autolab.orchestrator import wire as W


class CloudRelayError(RuntimeError):
    pass


@dataclass
class CloudRelayClient:
    base_url: str
    device_id: str
    device_token: str
    daemon_address: str | None = None
    interval_s: float = 2.0
    timeout_s: float = 15.0
    request: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.base_url.startswith("https://"):
            raise CloudRelayError("relay URL must use HTTPS")
        if not self.device_id.strip() or len(self.device_id) > 100:
            raise CloudRelayError("device id must contain 1 to 100 characters")
        if len(self.device_token) < 32:
            raise CloudRelayError("device token must contain at least 32 characters")
        if self.interval_s < 0.5:
            raise CloudRelayError("poll interval must be at least 0.5 seconds")
        if self.request is None:
            self.request = self._post

    def tick(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        assert self.request is not None
        self.request("/api/relay/snapshot", {
            "deviceId": self.device_id,
            "cursor": int(snapshot.get("cursor", 0)),
            "snapshot": snapshot,
        })
        try:
            self.sync_knowledge()
        except Exception as exc:
            # Read-only replication must not block operator commands.
            print(f"knowledge sync deferred: {type(exc).__name__}", flush=True)
        polled = self.request("/api/relay/poll", {"deviceId": self.device_id})
        command = polled.get("command")
        if command is None:
            return {"snapshot": snapshot, "command": None}
        if not isinstance(command, dict):
            raise CloudRelayError("relay returned a malformed command")
        command_id = _text(command, "commandId", 100)
        try:
            result = self.execute(command)
            ok = result.get("ok") is True
            report = {
                "deviceId": self.device_id,
                "commandId": command_id,
                "ok": ok,
                "result": result,
            }
            if not ok:
                report["error"] = str(result.get("error") or "local daemon refused command")
        except Exception as exc:  # noqa: BLE001 - the claimed command needs its answer
            result = None
            report = {
                "deviceId": self.device_id,
                "commandId": command_id,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.request("/api/relay/result", report)
        return {"snapshot": snapshot, "command": command, "result": result}

    def sync_knowledge(self):
        assert self.request is not None
        state = self.request("/api/relay/knowledge", {"deviceId": self.device_id})
        after = state.get("cursor")
        if type(after) is not int or after < 0:
            raise CloudRelayError("knowledge endpoint returned no cursor")
        answer = self._wire({"type": "knowledge.events", "commandId": uuid.uuid4().hex,
                             "after": after, "limit": 100})
        knowledge = answer.get("knowledge")
        if not answer.get("ok") or not isinstance(knowledge, dict):
            raise CloudRelayError("local knowledge is unavailable")
        if knowledge["latest"] < after:
            raise CloudRelayError("local knowledge cursor moved backwards")
        return self.request("/api/relay/knowledge", {
            "deviceId": self.device_id, "after": after, "events": knowledge["events"],
            "latest": knowledge["latest"],
        })

    def run_forever(self) -> None:
        while True:
            try:
                self.tick()
            except (CloudRelayError, OSError, ValueError) as exc:
                print(f"relay waiting: {exc}", flush=True)
            time.sleep(self.interval_s)

    def execute(self, command: dict[str, Any]) -> dict[str, Any]:
        command_id = _text(command, "commandId", 100)
        kind = _text(command, "kind", 100)
        payload = command.get("payload")
        if not isinstance(payload, dict):
            raise CloudRelayError("command payload must be an object")

        if kind == "mission.start":
            scenario = _text(payload, "scenario")
            preview = self._wire({
                "type": W.SCENARIO_PREFLIGHT,
                "commandId": command_id + ":preflight",
                "scenario": scenario,
            })
            if preview.get("ok") is not True:
                return preview
            preflight = preview.get("preflight")
            if not isinstance(preflight, dict) or not isinstance(
                preflight.get("scenario_sha256"), str
            ):
                raise CloudRelayError("preflight returned no scenario digest")
            return self._wire({
                "type": W.JOB_MISSION,
                "commandId": command_id,
                "objective": _text(payload, "objective"),
                "scenario": scenario,
                "scenarioSha256": preflight["scenario_sha256"],
                "searchPolicy": str(payload.get("searchPolicy") or "refresh"),
            })

        if kind == "approval.decide":
            decision = _text(payload, "decision", 20)
            if decision not in W.DECISIONS:
                raise CloudRelayError(
                    f"decision must be one of {sorted(W.DECISIONS)}"
                )
            return self._wire({
                "type": W.APPROVAL_DECIDE,
                "commandId": command_id,
                "approvalId": _text(payload, "approvalId", 100),
                "decision": decision,
                "signedBy": _text(payload, "signedBy", 100),
                "note": str(payload.get("note") or "")[:1000],
            })

        if kind == "campaign.bundle.preview":
            return self._wire({
                "type": W.CAMPAIGN_BUNDLE_PREVIEW,
                "commandId": command_id,
                "jobId": _text(payload, "jobId", 100),
                "maxIterations": _integer(payload, "maxIterations", 8, 64),
                "maxAutoApprovals": _integer(payload, "maxAutoApprovals", 24, 256),
                "durationHours": _integer(payload, "durationHours", 12, 72),
            })

        if kind == "campaign.bundle.approve":
            return self._wire({
                "type": W.CAMPAIGN_BUNDLE_APPROVE,
                "commandId": command_id,
                "jobId": _text(payload, "jobId", 100),
                "approvedBy": _text(payload, "approvedBy", 100),
                "reviewSha256": _digest(payload, "reviewSha256"),
                "maxIterations": _integer(payload, "maxIterations", 8, 64),
                "maxAutoApprovals": _integer(payload, "maxAutoApprovals", 24, 256),
                "durationHours": _integer(payload, "durationHours", 12, 72),
                "comment": str(payload.get("comment") or "")[:4000],
            })

        if kind == "job.mission.revise":
            return self._wire({
                "type": W.JOB_MISSION_REVISE,
                "commandId": command_id,
                "jobId": _text(payload, "jobId", 100),
                "requestedBy": _text(payload, "requestedBy", 100),
                "message": _text(payload, "message"),
            })

        if kind == "job.steer":
            return self._wire({
                "type": W.JOB_STEER,
                "commandId": command_id,
                "jobId": _text(payload, "jobId", 100),
                "sentBy": _text(payload, "sentBy", 100),
                "message": _text(payload, "message"),
            })

        if kind == "job.cancel":
            return self._wire({
                "type": W.JOB_CANCEL,
                "commandId": command_id,
                "jobId": _text(payload, "jobId", 100),
                "cancelledBy": _text(payload, "cancelledBy", 100),
                "reason": str(payload.get("reason") or "stopped from Cloudflare Pages")[:1000],
            })
        raise CloudRelayError(f"cloud command kind is not allowed locally: {kind}")

    def _snapshot(self) -> dict[str, Any]:
        answer = S.send(
            {"command": "remote-summary", "limit": 24},
            self.daemon_address,
            timeout=self.timeout_s,
        )
        snapshot = answer.get("snapshot") if isinstance(answer, dict) else None
        if answer.get("ok") is True and isinstance(snapshot, dict):
            result = dict(snapshot)
            result["relayAt"] = int(time.time())
            return result

        # A relay upgraded before its local daemon should still publish the
        # old compact status rather than disappear from the phone entirely.
        fallback = S.send(
            {"command": "status"}, self.daemon_address, timeout=self.timeout_s
        )
        if fallback.get("ok") is not True:
            raise CloudRelayError(str(fallback.get("error") or "daemon status refused"))
        status = fallback.get("status")
        if not isinstance(status, dict):
            raise CloudRelayError("daemon returned no status object")
        return {
            "cursor": int(status.get("cursor", 0)),
            "jobs": _flatten_jobs(status.get("jobs")),
            "inbox": status.get("inbox") if isinstance(status.get("inbox"), list) else [],
            "relayAt": int(time.time()),
            "summaryUnavailable": True,
        }

    def _wire(self, frame: dict[str, Any]) -> dict[str, Any]:
        answer = S.send(frame, self.daemon_address, timeout=self.timeout_s)
        if not isinstance(answer, dict):
            raise CloudRelayError("daemon response was not an object")
        return answer

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "authorization": f"Bearer {self.device_token}",
                "content-type": "application/json",
                "user-agent": "rtase-cloud-relay/1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise CloudRelayError(f"relay HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise CloudRelayError(f"relay unavailable: {exc}") from exc
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise CloudRelayError(str(answer.get("error") if isinstance(answer, dict) else answer))
        return answer


def _text(value: dict[str, Any], key: str, maximum: int = 4000) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CloudRelayError(f"{key} must be a non-empty string")
    if len(item) > maximum:
        raise CloudRelayError(f"{key} exceeds {maximum} characters")
    return item.strip()


def _integer(
    value: dict[str, Any], key: str, default: int, maximum: int
) -> int:
    item = value.get(key, default)
    if not isinstance(item, int) or isinstance(item, bool) or not 1 <= item <= maximum:
        raise CloudRelayError(f"{key} must be an integer from 1 to {maximum}")
    return item


def _digest(value: dict[str, Any], key: str) -> str:
    item = _text(value, key, 64).lower()
    if len(item) != 64 or any(char not in "0123456789abcdef" for char in item):
        raise CloudRelayError(f"{key} must be a SHA-256 hex digest")
    return item


def _flatten_jobs(value: Any) -> list[dict[str, Any]]:
    """Accept both bare-ledger rows and the daemon's status buckets."""
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    flattened: list[dict[str, Any]] = []
    for status, rows in value.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["scheduler_status"] = str(status)
            flattened.append(item)
    return flattened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect the local RTASE daemon to Pages.")
    parser.add_argument(
        "--generate-token",
        action="store_true",
        help="print one token and the SHA-256 value to store as a Pages secret",
    )
    parser.add_argument("--url", default=os.environ.get("QKD_RELAY_URL"))
    parser.add_argument("--device-id", default=os.environ.get("QKD_RELAY_DEVICE_ID", "lab-pc"))
    parser.add_argument("--device-token", default=os.environ.get("QKD_RELAY_DEVICE_TOKEN"))
    parser.add_argument("--daemon", default=os.environ.get("QKD_WEBUI_DAEMON"))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.generate_token:
        token = secrets.token_urlsafe(32)
        print(f"token={token}")
        print(f"sha256={hashlib.sha256(token.encode('utf-8')).hexdigest()}")
        return 0
    if not args.url or not args.device_token:
        parser.error("--url and QKD_RELAY_DEVICE_TOKEN (or --device-token) are required")
    client = CloudRelayClient(
        args.url,
        args.device_id,
        args.device_token,
        daemon_address=args.daemon,
        interval_s=args.interval,
    )
    if args.once:
        print(json.dumps(client.tick(), ensure_ascii=False, indent=2))
    else:
        client.run_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
