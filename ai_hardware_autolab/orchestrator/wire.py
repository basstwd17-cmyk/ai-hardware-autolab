"""The office UI's protocol: subscribe, replay, ready, then keep pushing.

The control plane answers one request with one response, which is right for a
CLI and wrong for a UI. A UI connects once, asks for everything after the
cursor it last applied, and then stays attached while the daemon pushes what
happens next. That is a different shape on the same pipe, so this module owns
it and ``ControlPlane`` stays what it was.

The contract is `agent-platform/docs/platform-integration.md`. The parts that
constrain the implementation:

* **The office is the client.** Its first frame is
  ``{"type":"subscribe","protocol":1,"after":N}``.
* **One frame per global cursor.** Every ledger row between ``after`` and the
  current tip becomes exactly one ``ledger.event``. A duplicate is harmless; a
  gap stops the projector.
* **`ready.cursor` must equal the last event sent.** The UI treats a
  disagreement as an error, because otherwise it could claim to be current
  while missing durable history. So the tip is read *after* the replay, and
  anything that arrived during it is sent before ready rather than after.
* **The stream is unfiltered and global.** Job screens filter after receipt.
  Asking the ledger for one job would skip cursors belonging to others, and the
  UI's gap detector would fire on every one of them.
* **Operator actions carry a `commandId` and get a `command.result`.** They are
  deliberately typed differently from events so a direction message can never
  be read as an approval signature.

What this module does not do: decide anything. Every command is handed to
``ControlPlane``, which is the same path the CLI takes, so an approval recorded
from the office is the same row as one recorded from a terminal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai_hardware_autolab.orchestrator.control import ControlError, ControlPlane
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.pagination import iter_events
from ai_hardware_autolab.orchestrator.results import build_result

PROTOCOL = 1

# Frames the office sends.
SUBSCRIBE = "subscribe"
SCENARIO_PREFLIGHT = "scenario.preflight"
APPROVAL_DECIDE = "approval.decide"
JOB_START = "job.start"
JOB_DISCOVER = "job.discover"
JOB_MISSION = "job.mission"
MODELS_LIST = "models.list"
PROJECTS_CATALOG = "projects.catalog"
PROJECTS_REGISTER = "projects.register"
HARDWARE_STATUS = "hardware.status"
HARDWARE_CHECK = "hardware.check"
HARDWARE_CATALOG = "hardware.catalog"
HARDWARE_DISCOVER = "hardware.discover"
HARDWARE_REGISTER = "hardware.register"
JOB_MISSION_REVISE = "job.mission.revise"
JOB_STEER = "job.steer"
JOB_CANCEL = "job.cancel"
JOB_RESUME = "job.resume"
CONTRACT_PROMOTION_PREVIEW = "contract.promotion.preview"
CONTRACT_PROMOTION_REQUEST = "contract.promotion.request"
CONTRACT_PROMOTION_APPLY = "contract.promotion.apply"
KNOWLEDGE_CLAIM = "knowledge.claim"
KNOWLEDGE_SEARCH = "knowledge.search"
KNOWLEDGE_EVENTS = "knowledge.events"
KNOWLEDGE_NEIGHBORS = "knowledge.neighbors"
CAMPAIGN_CATALOG = "campaign.catalog"
CAMPAIGN_STATUS = "campaign.status"
CAMPAIGN_PREVIEW = "campaign.preview"
CAMPAIGN_ACTIVATE = "campaign.activate"
CAMPAIGN_DEACTIVATE = "campaign.deactivate"
CAMPAIGN_BUNDLE_PREVIEW = "campaign.bundle.preview"
CAMPAIGN_BUNDLE_APPROVE = "campaign.bundle.approve"

# Frames the daemon sends.
LEDGER_EVENT = "ledger.event"
READY = "ready"
COMMAND_RESULT = "command.result"
ERROR = "error"
JOB_RESULT = "job.result"
ARTIFACT_REGISTERED = "artifact.registered"

# Handoffs after which a job's result is worth re-sending.
_RESULT_TRIGGERS = ("handoff.Verdict", "handoff.Report", "job.done")

# The office says "accepted"/"declined". It never says true/false, and this map
# is the only place that translation happens -- a decision that arrived as a
# string must not reach `ControlPlane.approve`, which takes a real boolean and
# refuses anything else.
DECISIONS = {"accepted": True, "declined": False}

# How long to wait between checks for new events when idle.
DEFAULT_POLL_S = 0.25


class WireError(ValueError):
    """A frame the daemon will not act on, with a reason the office can show."""


def _frame(kind: str, **fields: Any) -> dict[str, Any]:
    return {"type": kind, **fields}


def ledger_event_frame(event: Any) -> dict[str, Any]:
    """One ledger row, exactly as `Event.describe()` returns it."""
    return _frame(LEDGER_EVENT, event=event.describe())


@dataclass
class WireSession:
    """One connected office, from subscribe until the socket closes."""

    ledger: JobLedger
    control: ControlPlane
    send: Callable[[dict[str, Any]], None]
    poll_s: float = DEFAULT_POLL_S
    cursor: int = 0
    subscribed: bool = False
    runs_dir: Path | None = None
    project_root: Path | None = None
    runtime_profile: str | None = None
    workspace_mode: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # artifactId -> the path already announced under it. The contract calls a
    # repeat idempotent and a changed path an error, so both are decided here
    # rather than left to the office to notice.
    _artifacts: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    warnings: list[str] = field(default_factory=list, init=False, repr=False)

    # -- incoming ---------------------------------------------------------

    def handle(self, frame: Any) -> None:
        """Act on one frame from the office. Answers on the same connection."""
        if not isinstance(frame, dict):
            self.send(_frame(ERROR, error="a frame must be a JSON object"))
            return
        kind = frame.get("type")
        if not isinstance(kind, str):
            self.send(_frame(ERROR, error="a frame must have a string type"))
            return

        if kind == SUBSCRIBE:
            self._subscribe(frame)
            return

        command_id = frame.get("commandId")
        if kind not in (
            MODELS_LIST,
            PROJECTS_CATALOG,
            PROJECTS_REGISTER,
            HARDWARE_STATUS,
            HARDWARE_CHECK,
            HARDWARE_CATALOG,
            HARDWARE_DISCOVER,
            HARDWARE_REGISTER,
            SCENARIO_PREFLIGHT,
            APPROVAL_DECIDE,
            JOB_START,
            JOB_DISCOVER,
            JOB_MISSION,
            JOB_MISSION_REVISE,
            JOB_STEER,
            JOB_CANCEL,
            JOB_RESUME,
            CONTRACT_PROMOTION_PREVIEW,
            CONTRACT_PROMOTION_REQUEST,
            CONTRACT_PROMOTION_APPLY,
            KNOWLEDGE_CLAIM,
            KNOWLEDGE_SEARCH,
            KNOWLEDGE_EVENTS,
            KNOWLEDGE_NEIGHBORS,
            CAMPAIGN_CATALOG,
            CAMPAIGN_STATUS,
            CAMPAIGN_PREVIEW,
            CAMPAIGN_ACTIVATE,
            CAMPAIGN_DEACTIVATE,
            CAMPAIGN_BUNDLE_PREVIEW,
            CAMPAIGN_BUNDLE_APPROVE,
        ):
            self._result(command_id, False, error=f"unknown frame type {kind!r}")
            return
        if not isinstance(command_id, str) or not command_id.strip():
            self._result(None, False, error=f"{kind} requires a commandId")
            return

        try:
            payload = self._act(kind, frame)
        except (WireError, ControlError) as exc:
            self._result(command_id, False, error=str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a session must answer, not die
            self._result(command_id, False, error=f"{type(exc).__name__}: {exc}")
            return
        self._result(command_id, True, **payload)

    def _act(self, kind: str, frame: dict[str, Any]) -> dict[str, Any]:
        if kind in (HARDWARE_STATUS, HARDWARE_CHECK, HARDWARE_CATALOG, HARDWARE_DISCOVER, HARDWARE_REGISTER):
            return self._control_command({"command": kind.replace(".", "-"),
                                          "hardware_selection": frame.get("hardwareSelection"),
                                          **({"setup": frame.get("setup")} if kind == HARDWARE_REGISTER else {}),
                                          **({"devices": frame.get("devices")} if kind == HARDWARE_CHECK else {})})
        if kind in (PROJECTS_CATALOG, PROJECTS_REGISTER):
            return self._control_command({"command": kind.replace(".", "-"),
                                          **({"project": frame.get("project")} if kind == PROJECTS_REGISTER else {})})
        if kind == MODELS_LIST:
            return self._control_command({"command": "models-list"})
        if kind == CAMPAIGN_CATALOG:
            return self._control_command({"command": "campaign-catalog"})

        if kind == CAMPAIGN_STATUS:
            return self._control_command({"command": "campaign-status"})

        if kind == CAMPAIGN_PREVIEW:
            return self._control_command({
                "command": "campaign-preview",
                "filename": _text(frame, "filename"),
                "sha256": _text(frame, "sha256"),
            })

        if kind == CAMPAIGN_ACTIVATE:
            return self._control_command({
                "command": "campaign-activate",
                "job_id": _text(frame, "jobId"),
                "filename": _text(frame, "filename"),
                "sha256": _text(frame, "sha256"),
                "approved_by": _text(frame, "approvedBy"),
            })

        if kind == CAMPAIGN_DEACTIVATE:
            return self._control_command({
                "command": "campaign-deactivate",
                "requested_by": _text(frame, "requestedBy"),
                "reason": str(frame.get("reason", "")),
            })

        if kind == CAMPAIGN_BUNDLE_PREVIEW:
            return self._control_command({
                "command": "campaign-bundle-preview",
                "job_id": _text(frame, "jobId"),
                "max_iterations": _number(frame, "maxIterations", 8),
                "max_auto_approvals": _number(frame, "maxAutoApprovals", 24),
                "duration_hours": _number(frame, "durationHours", 12),
                "authorization_scope": frame.get("authorizationScope", "scenario"),
            })

        if kind == CAMPAIGN_BUNDLE_APPROVE:
            return self._control_command({
                "command": "campaign-bundle-approve",
                "job_id": _text(frame, "jobId"),
                "approved_by": _text(frame, "approvedBy"),
                "review_sha256": _text(frame, "reviewSha256"),
                "max_iterations": _number(frame, "maxIterations", 8),
                "max_auto_approvals": _number(frame, "maxAutoApprovals", 24),
                "duration_hours": _number(frame, "durationHours", 12),
                "authorization_scope": frame.get("authorizationScope", "scenario"),
                "comment": str(frame.get("comment", "")),
            })

        if kind == CONTRACT_PROMOTION_PREVIEW:
            answer = self.control.promotion_preview(_text(frame, "jobId"))
            return {"proposal": answer}

        if kind == CONTRACT_PROMOTION_REQUEST:
            return self.control.promotion_request(
                _text(frame, "jobId"), _text(frame, "requestedBy")
            )

        if kind == CONTRACT_PROMOTION_APPLY:
            return self.control.promotion_apply(_text(frame, "approvalId"))

        if kind == KNOWLEDGE_CLAIM:
            return self._control_command(
                {"command": "knowledge-claim", "claim_id": _text(frame, "claimId")}
            )

        if kind == KNOWLEDGE_SEARCH:
            request = {
                "command": "knowledge-search",
                "query": frame.get("query", ""),
                "limit": frame.get("limit", 20),
            }
            if "scope" in frame:
                request["scope"] = frame["scope"]
            return self._control_command(request)

        if kind == KNOWLEDGE_EVENTS:
            return self._control_command(
                {
                    "command": "knowledge-events",
                    "since": frame.get("after", 0),
                    "limit": frame.get("limit", 500),
                }
            )

        if kind == KNOWLEDGE_NEIGHBORS:
            request = {
                "command": "knowledge-neighbors",
                "entity_key": _text(frame, "entityKey"),
                "limit": frame.get("limit", 20),
            }
            if "scope" in frame:
                request["scope"] = frame["scope"]
            return self._control_command(request)

        if kind == SCENARIO_PREFLIGHT:
            options = {"hardware_selection": frame["hardwareSelection"]} if "hardwareSelection" in frame else {}
            return {"preflight": self.control.preflight(_text(frame, "scenario"), **options)}

        if kind == JOB_START:
            answer = self.control.start(
                _text(frame, "gate"),
                _text(frame, "objective"),
                _text(frame, "scenario"),
                str(frame.get("contractSha256", "")),
                str(frame.get("scenarioSha256", "")),
            )
            return {"job": answer["job"]}

        if kind == JOB_DISCOVER:
            answer = self.control.discover(
                _text(frame, "objective"),
                _text(frame, "scenario"),
                _text(frame, "scenarioSha256"),
            )
            return {"job": answer["job"], "preflight": answer["preflight"]}

        if kind == JOB_MISSION:
            options = {"model_selection": frame["modelSelection"]} if "modelSelection" in frame else {}
            if "hardwareSelection" in frame:
                options["hardware_selection"] = frame["hardwareSelection"]
            if "projectId" in frame:
                options["project_id"] = frame["projectId"]
            answer = self.control.mission(
                _text(frame, "objective"),
                _text(frame, "scenario"),
                _text(frame, "scenarioSha256"),
                _text(frame, "searchPolicy"),
                **options,
            )
            return {key: answer[key] for key in ("job", "preflight", "workflow", "execution_mode", "reused")}

        if kind == JOB_MISSION_REVISE:
            return self._control_command({
                "command": "job-mission-revise",
                "job_id": _text(frame, "jobId"),
                "message": _text(frame, "message"),
                "requested_by": _text(frame, "requestedBy"),
            })

        if kind == APPROVAL_DECIDE:
            decision = frame.get("decision")
            if decision not in DECISIONS:
                raise WireError(
                    f"decision must be one of {sorted(DECISIONS)}, got {decision!r}. "
                    "Anything else is refused rather than guessed at."
                )
            answer = self.control.approve(
                _text(frame, "approvalId"),
                DECISIONS[decision],
                _text(frame, "signedBy"),
                str(frame.get("note", "")),
            )
            return {"approval": answer["approval"]}

        if kind == JOB_STEER:
            answer = self.control.steer(
                _text(frame, "jobId"), _text(frame, "message"), _text(frame, "sentBy")
            )
            return {"pending": answer["pending"], "willBeDelivered": answer["will_be_delivered"]}

        if kind == JOB_RESUME:
            return self._control_command({'command': 'resume-step', 'job_id': _text(frame, 'jobId'),
                                          'requested_by': _text(frame, 'requestedBy'), 'note': _text(frame, 'note')})

        answer = self.control.cancel(
            _text(frame, "jobId"), _text(frame, "cancelledBy"), str(frame.get("reason", ""))
        )
        return {"job": answer["job"], "withdrawn": answer["withdrawn"]}

    def _control_command(self, request: dict[str, Any]) -> dict[str, Any]:
        answer = self.control.handle(request)
        if not answer.get("ok"):
            raise WireError(str(answer.get("error") or "control command was refused"))
        return {key: value for key, value in answer.items() if key not in ("ok", "version")}

    # -- outgoing ---------------------------------------------------------

    def _result(self, command_id: str | None, ok: bool, **fields: Any) -> None:
        frame = _frame(COMMAND_RESULT, ok=ok, **fields)
        if command_id is not None:
            frame["commandId"] = command_id
        self.send(frame)

    def _subscribe(self, frame: dict[str, Any]) -> None:
        protocol = frame.get("protocol")
        if protocol != PROTOCOL:
            self.send(
                _frame(
                    ERROR,
                    error=f"protocol {protocol!r} is not supported; this daemon speaks {PROTOCOL}",
                )
            )
            return
        after = frame.get("after", 0)
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            self.send(_frame(ERROR, error=f"after must be a non-negative integer, got {after!r}"))
            return

        with self._lock:
            self.cursor = after
            sent = self._drain_locked(emit_results=False)
            # Read the tip *after* draining, and drain again if it moved. Taking
            # it first and announcing that number would promise a cursor the
            # office had not been sent; taking it after and not re-draining
            # would promise one it had been sent past. Loop until the ledger
            # stops moving, then the two agree by construction.
            while True:
                tip = self.ledger.latest_event_id()
                if tip <= self.cursor:
                    break
                sent += self._drain_locked(emit_results=False)
            self.subscribed = True
            ready = _frame(READY, cursor=self.cursor)
            if self.runtime_profile is not None:
                ready["profile"] = self.runtime_profile
            if self.workspace_mode is not None:
                ready["workspaceMode"] = self.workspace_mode
            self.send(ready)
            # After ready, never inside the replay: the contract says every
            # frame between `after` and ready is one ledger cursor, and a
            # result is not a ledger row. Sending them here keeps the cursor
            # arithmetic exactly what it claims to be.
            for job in self.ledger.jobs():
                self._send_result(job.job_id)
        return None

    def push_new(self) -> int:
        """Send whatever has landed since the last frame. Returns how many."""
        with self._lock:
            if not self.subscribed:
                return 0
            return self._drain_locked()

    def _drain_locked(self, emit_results: bool = True) -> int:
        """Send every row after the cursor. Results follow, if the caller wants them.

        The replay must be nothing but ledger rows -- the contract counts one
        frame per global cursor, and a result is not a cursor. So `_subscribe`
        drains without results, sends ready, and only then offers them; a live
        push emits them inline because there is no ready to get in front of.
        """
        sent = 0
        settled: list[str] = []
        for event in iter_events(self.ledger, since=self.cursor):
            self.send(ledger_event_frame(event))
            self.cursor = event.event_id
            sent += 1
            if event.kind in _RESULT_TRIGGERS and event.job_id not in settled:
                settled.append(event.job_id)
        if emit_results:
            for job_id in settled:
                self._send_result(job_id)
        return sent

    # -- results and artifacts -------------------------------------------

    def _send_result(self, job_id: str) -> None:
        """The verdict this daemon actually holds, plus what may be opened.

        Nothing here is authored: the counts are arithmetic over the reducer's
        conditions, the summary quotes the outcome and the failing field names,
        and the verbatim verdict travels with its digest so the office never
        has to take the summary's word for anything.
        """
        if self.runs_dir is None or self.project_root is None:
            return
        built = build_result(
            self.ledger, job_id, runs_dir=self.runs_dir, project_root=self.project_root
        )
        if built is None:
            return
        payload, artifacts = built
        for artifact in artifacts:
            self._register(job_id, artifact)
        self.send(_frame(JOB_RESULT, result=payload))

    def _register(self, job_id: str, artifact: Any) -> None:
        known = self._artifacts.get(artifact.artifact_id)
        if known == artifact.target_path:
            return  # identical registration: idempotent, so say nothing
        if known is not None:
            # The office rejects a changed path behind an existing id, and it
            # is right to. Refusing to send it keeps the daemon from being the
            # one that breaks that rule.
            self.warnings.append(
                f"{artifact.artifact_id} already points at {known}; "
                f"refusing to re-point it at {artifact.target_path}"
            )
            return
        self._artifacts[artifact.artifact_id] = artifact.target_path
        self.send(artifact.registration(job_id))


def _text(frame: dict[str, Any], key: str) -> str:
    value = frame.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WireError(f"{key} is required and must be a non-empty string")
    return value.strip()


def _number(frame: dict[str, Any], key: str, default: int) -> int:
    """Read a JSON integer without treating booleans as 0/1."""
    value = frame.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WireError(f"{key} must be a JSON integer")
    return value


def looks_like_wire(frame: Any) -> bool:
    """Is this the office's protocol, or the internal command API?

    Both arrive as newline-delimited JSON on the same pipe. The office frames
    carry `type`; the CLI's carry `command`. Keeping one connection able to
    speak either means a terminal can still poke a daemon a UI is attached to.
    """
    return isinstance(frame, dict) and isinstance(frame.get("type"), str)
