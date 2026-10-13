"""The HTTP surface. One process, loopback only, no second source of truth.

Three things arrive here and they are kept apart on purpose.

* ``GET /api/*`` answers from the projection, which is memory, which is fast
  and which is allowed to be a moment behind.
* ``WS /ws`` is the live edge: a browser gets the snapshot, then the backlog it
  asked for, then every frame as it lands. Same order, every time, so a late
  browser and a fresh one converge on the same state.
* ``POST /api/command`` is the only mutating door, and it does not decide
  anything -- it forwards an office frame to the daemon, which hands it to
  ``ControlPlane``, which is the same row a terminal approval writes.

**Loopback only.** The daemon's pipe is created with a DACL naming this
logon's user SID and ``PIPE_REJECT_REMOTE_CLIENTS``. A web server in front of
it that bound ``0.0.0.0`` would hand that guarantee away to anyone on the
network and would not be able to say who it was talking to. So the default host
is ``127.0.0.1``, and binding elsewhere takes an explicit flag and prints what
it is giving up.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ai_hardware_autolab.utils import utc_now
from ai_hardware_autolab.orchestrator import wire as W
from ai_hardware_autolab.webui.backend.channels import (
    DEFAULT_CAPACITY,
    RESYNC_CLOSE_CODE,
    RESYNC_REQUIRED,
    ClientChannel,
)
from ai_hardware_autolab.webui.backend import artifacts as A
from ai_hardware_autolab.agents.schema import load_simple_team as load_agent_definitions, team_for_agent
from ai_hardware_autolab.webui.backend import models as M
from ai_hardware_autolab.webui.backend.facts import Facts, FileFacts
from ai_hardware_autolab.webui.backend.graph import GraphProjection
from ai_hardware_autolab.webui.backend.layout import LayoutConflict, LayoutError, LayoutStore
from ai_hardware_autolab.webui.backend.office import OfficeProjection
from ai_hardware_autolab.webui.backend.remote_approval import (
    RemoteApprovalError,
    RemoteApprovalStore,
)
from ai_hardware_autolab.webui.backend.security import Guard, Policy, Session
from ai_hardware_autolab.webui.backend.state import Projection, flatten_jobs
from ai_hardware_autolab.webui.backend.wire import DaemonSubscription, DaemonUnavailable

# Frames after which the job/approval snapshot is worth re-reading. A ledger
# event can change what `status` would say; a runtime notification cannot.
_SNAPSHOT_TRIGGERS = (W.READY, W.LEDGER_EVENT, W.JOB_RESULT)


class FrontendFiles(StaticFiles):
    """Revalidate HTML; cache content-addressed Vite assets indefinitely.

    A rebuilt ``index.html`` points at a new hashed bundle.  Caching the old
    HTML therefore pins a browser to code the host no longer serves, while the
    files under ``assets/`` are safe to cache because their names change with
    their bytes.
    """

    async def get_response(self, path: str, scope: dict[str, Any]):
        response = await super().get_response(path, scope)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["cache-control"] = "no-cache"
        elif path.replace("\\", "/").startswith("assets/"):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response

# Office frames a browser is allowed to send. Anything else is refused by name
# rather than forwarded and refused by the daemon, so the refusal says which
# door it came to.
ALLOWED_COMMANDS = (
    W.HARDWARE_STATUS,
    W.HARDWARE_CHECK,
    W.HARDWARE_CATALOG,
    W.HARDWARE_DISCOVER,
    W.HARDWARE_REGISTER,
    W.MODELS_LIST,
    W.PROJECTS_CATALOG,
    W.PROJECTS_REGISTER,
    W.SCENARIO_PREFLIGHT,
    W.APPROVAL_DECIDE,
    W.JOB_START,
    # `contract_gap` is not an error. It is the deterministic answer that no
    # reviewed contract covers this scenario, and `job.discover` is the door it
    # opens -- a discovery job that goes and finds out. Refusing it here would
    # make the UI treat "nobody has specified this yet" as a failure.
    W.JOB_DISCOVER,
    # Common scenario front door. It plans and refreshes evidence even when a
    # reviewed baseline contract already exists.
    W.JOB_MISSION,
    W.JOB_MISSION_REVISE,
    W.JOB_STEER,
    W.JOB_CANCEL,
    W.JOB_RESUME,
    # Contract installation is still a three-step human workflow. Preview is
    # read-only, request writes a digest-bound approval, and apply succeeds
    # only after that exact approval was granted by a person.
    W.CONTRACT_PROMOTION_PREVIEW,
    W.CONTRACT_PROMOTION_REQUEST,
    W.CONTRACT_PROMOTION_APPLY,
    # These are read-only views over the separate knowledge ledger. They do
    # not inherit the job cursor and cannot mutate either ledger.
    W.KNOWLEDGE_CLAIM,
    W.KNOWLEDGE_SEARCH,
    W.KNOWLEDGE_EVENTS,
    W.KNOWLEDGE_NEIGHBORS,
    W.CAMPAIGN_CATALOG,
    W.CAMPAIGN_STATUS,
    W.CAMPAIGN_PREVIEW,
    W.CAMPAIGN_ACTIVATE,
    W.CAMPAIGN_DEACTIVATE,
    W.CAMPAIGN_BUNDLE_PREVIEW,
    W.CAMPAIGN_BUNDLE_APPROVE,
)

# The WebSocket notice that the graph moved. Deliberately carries only a
# cursor: the browser re-reads /api/graph rather than patching a local copy.
GRAPH_CHANGED = "graph.changed"

# Same shape for the office: a notice, and the browser re-reads. The office is
# small enough that a delta protocol would be more code than it saves.
OFFICE_CHANGED = "office.changed"

# The vendored Pixel Agents assets. Served from this directory and from
# nowhere else: not a CDN, not node_modules, not the agent-platform checkout.
# `webui/assets/MANIFEST.json` records where they came from and hashes each one.
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


class Hub:
    """Owns the subscription, the projection, and the browsers attached to it."""

    def __init__(
        self,
        address: str | None = None,
        history: int | None = None,
        facts: Facts | None = None,
    ) -> None:
        self.projection = Projection(history=history) if history else Projection()
        # The graph is fed the same frames as the projection, from the same
        # callback, so it can never be a summary of a summary. In particular it
        # is never rebuilt from `projection.events()`, which is capped.
        self.graph = GraphProjection(facts=facts or FileFacts())
        # The office is a *different* question from the graph: the graph holds
        # every agent that ever appeared, the office holds the ones working
        # now. Same frames, separate folds.
        self.office = OfficeProjection()
        self.subscription = DaemonSubscription(
            address=address,
            on_frame=self._on_frame,
            on_status=self._on_status,
            on_replay=self._on_replay,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[ClientChannel] = set()
        self._clients_lock = threading.Lock()
        self._refresh = threading.Event()
        self._stop = threading.Event()
        self._refresher: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stop.clear()
        self._refresher = threading.Thread(
            target=self._refresh_loop, name="qkd-webui-snapshot", daemon=True
        )
        self._refresher.start()
        self.subscription.start()

    def stop(self) -> None:
        self._stop.set()
        self._refresh.set()
        self.subscription.stop()
        if self._refresher is not None:
            self._refresher.join(timeout=2.0)
            self._refresher = None

    # -- from the reader thread -------------------------------------------

    def _on_frame(self, frame: dict[str, Any]) -> None:
        self.projection.apply(frame)
        # The artifact frame still has its path here. The graph reads the run
        # name out of it and keeps nothing else; `browser_frame` in the
        # projection's fan-out is what strips it before it leaves.
        if self.graph.apply_frame(frame):
            self._graph_changed()
        if self.office.apply_frame(frame):
            self._office_changed()
        if frame.get("type") in _SNAPSHOT_TRIGGERS:
            self._refresh.set()

    def _graph_changed(self) -> None:
        """Tell browsers the graph moved; they re-read `/api/graph`.

        A notice rather than a delta. A delta protocol is worth building when
        the graph is big enough that re-reading it hurts, and not before -- and
        a wrong delta is a graph that quietly disagrees with the ledger, which
        is the failure this whole design is arranged to avoid.
        """
        self._push({"type": GRAPH_CHANGED, "cursor": self.graph.cursor})

    def _office_changed(self) -> None:
        self._push({"type": OFFICE_CHANGED, "cursor": self.office.cursor})

    def _on_replay(self, after: int) -> None:
        """A subscription from cursor 0 is about to replay everything.

        Both projections are cleared together. Clearing one would leave the
        graph holding nodes from a history the event list no longer has, which
        is the disagreement this whole design exists to prevent. A reconnect
        that resumes from a cursor keeps what it has, because replay after that
        cursor is exactly what it is missing.
        """
        if after != 0:
            return
        self.projection.reset()
        self.graph.reset()
        self.office.reset()

    def _on_status(self, status: Any) -> None:
        self._push({"type": "webui.status", "status": status.describe()})

    def _refresh_loop(self) -> None:
        """Re-read job and approval state whenever the ledger moved.

        Coalesced rather than per-event: a job that emits twenty rows in a turn
        is worth one `status` call, and the flag says "something moved", not
        "how many things moved".
        """
        while not self._stop.is_set():
            if not self._refresh.wait(timeout=1.0):
                continue
            self._refresh.clear()
            if self._stop.is_set():
                return
            try:
                answer = self.subscription.control({"command": "status"})
            except DaemonUnavailable:
                continue
            if not answer.get("ok", True):
                continue
            snapshot = answer.get("status", answer)
            self.projection.set_snapshot(snapshot)
            self._push({"type": "webui.snapshot", "snapshot": snapshot})
            # Job state comes from here and only from here; the graph never
            # re-derives it from `job.*` events.
            if self.graph.apply_status(snapshot):
                self._graph_changed()
            # The only thing that empties a desk: a job that is no longer open.
            if self.office.apply_status(snapshot):
                self._office_changed()

    # -- fan-out ----------------------------------------------------------

    def attach(self, channel: ClientChannel) -> None:
        with self._clients_lock:
            self._clients.add(channel)

    def detach(self, channel: ClientChannel) -> None:
        with self._clients_lock:
            self._clients.discard(channel)

    def _push(self, frame: dict[str, Any]) -> None:
        """Hand a frame to every attached browser, from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._clients_lock:
            channels = list(self._clients)
        cursor = self.subscription.status().cursor
        for channel in channels:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(channel.offer, frame, cursor)

    def hello(self, after: int) -> list[dict[str, Any]]:
        """What a browser is sent before the live stream starts."""
        status = self.subscription.status()
        return [
            {
                "type": "webui.hello",
                "protocol": W.PROTOCOL,
                "status": status.describe(),
                "oldestEvent": self.projection.oldest_event_id(),
            },
            {"type": "webui.snapshot", "snapshot": self.projection.snapshot()},
            *(
                {"type": W.LEDGER_EVENT, "event": event}
                for event in self.projection.events(since=after, limit=0)
            ),
            *(
                {"type": W.JOB_RESULT, "result": result}
                for result in self.projection.results().values()
            ),
            {"type": "webui.ready", "cursor": status.cursor},
        ]


def create_app(
    address: str | None = None,
    static_dir: Path | None = None,
    session: Session | None = None,
    port: int = DEFAULT_PORT,
    dev_origins: tuple[str, ...] = (),
    facts: Facts | None = None,
    layout_file: Path | None = None,
    reveal: A.Reveal | None = None,
    remote_approvals: RemoteApprovalStore | None = None,
    public_origins: tuple[str, ...] = (),
) -> FastAPI:
    """The app, already wrapped in its guard.

    ``session`` is injectable so a test can hold the token it is about to
    present. Left out, one is generated per app and written to
    ``webui/state/session.json`` for a launcher to read.
    """
    hub = Hub(address=address, facts=facts)
    layout = LayoutStore(layout_file) if layout_file else LayoutStore()
    show = reveal or A.reveal
    session = session or Session.start(port=port)
    policy = Policy.build(
        session,
        port=port,
        extra_origins=dev_origins,
        public_origins=public_origins,
    )
    remote_approvals = remote_approvals or RemoteApprovalStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.start(asyncio.get_running_loop())
        try:
            yield
        finally:
            hub.stop()

    app = FastAPI(
        title="AI Hardware Autolab",
        version="0",
        lifespan=lifespan,
        responses={
            401: {"model": M.Refusal, "description": "no session token"},
            403: {"model": M.Refusal, "description": "Host or Origin refused"},
        },
    )
    app.state.hub = hub
    app.state.session = session
    app.state.layout = layout
    app.state.policy = policy
    app.state.remote_approvals = remote_approvals
    app.state.public_origins = tuple(origin.rstrip("/") for origin in public_origins)

    @app.get("/api/health", response_model=M.Health)
    def health() -> Any:
        return {"ok": True, "daemon": hub.subscription.status().describe()}

    @app.get("/api/agents", response_model=M.AgentCatalog)
    def agents() -> Any:
        """Return the exact permission metadata the runtime definitions carry.

        Prompts and source paths stay on the host.  The browser gets enough to
        explain why an agent was selected and which boundary actually applies,
        without maintaining a second, hand-written catalogue that can drift.
        """
        return {
            "agents": [
                {
                    "name": agent.name,
                    "team": team_for_agent(agent.name),
                    "role": agent.role,
                    "description": agent.description,
                    "mcpTools": list(agent.mcp_tools),
                    "localTools": list(agent.local_tools),
                    "skills": list(agent.skills),
                    "consumes": list(agent.consumes),
                    "emits": list(agent.emits),
                    "touchesHardware": agent.touches_hardware,
                    "nativeWeb": agent.native_web,
                }
                for agent in load_agent_definitions()
            ]
        }

    @app.get("/api/state", response_model=M.StateResponse)
    def state() -> Any:
        snapshot = hub.projection.snapshot()
        return {
            "daemon": hub.subscription.status().describe(),
            "cursor": snapshot.get("cursor", hub.subscription.status().cursor),
            "jobs": flatten_jobs(snapshot),
            "inbox": snapshot.get("inbox", []),
            "results": hub.projection.results(),
            "warnings": hub.projection.warnings(),
            # Verbatim, because the two `jobs` shapes carry different records
            # and a caller that needs the difference must be able to see it.
            "snapshot": snapshot,
        }

    @app.get("/api/events", response_model=M.EventsResponse)
    def events(
        since: int = Query(0, ge=0),
        job: str | None = None,
        limit: int = Query(500, ge=1, le=5000),
    ) -> Any:
        rows = hub.projection.events(since=since, job_id=job, limit=limit)
        return {
            "events": rows,
            "cursor": rows[-1].get("event_id", since) if rows else since,
            "oldest": hub.projection.oldest_event_id(),
        }

    @app.get("/api/jobs/{job_id}", response_model=M.JobDetail)
    def job(
        job_id: str,
        since: int = Query(0, ge=0),
        limit: int = Query(2000, ge=1, le=10000),
    ) -> Any:
        """One job, with its history read from the ledger rather than memory.

        The in-memory projection is capped -- it exists so a browser that
        connects late has something to draw immediately. A transcript is a
        different question: it wants everything this job ever did, including
        rows evicted from that cap. So the events come from the control plane,
        and the live WebSocket appends to them from there on.
        """
        try:
            detail = hub.subscription.control({"command": "job", "job_id": job_id})
            history = hub.subscription.control(
                {"command": "events", "job_id": job_id, "since": since, "limit": limit}
            )
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not detail.get("ok", True):
            raise HTTPException(status_code=404, detail=str(detail.get("error", "no such job")))
        events = history.get("events", []) if history.get("ok", True) else []
        return {
            "job": detail.get("job", detail),
            "result": hub.projection.result(job_id),
            "events": events,
            "cursor": history.get("cursor", since),
            "latest": history.get("latest", 0),
        }

    @app.get("/api/jobs/{job_id}/handoffs", response_model=list[M.HandoffDetail])
    def handoffs(job_id: str) -> Any:
        """Read the full, durable results of the selected job.

        This is intentionally not part of the replay stream: runtime animation
        uses small digest events, while this endpoint is opened only when the
        operator asks to read an individual job's work.
        """
        try:
            answer = hub.subscription.control({"command": "handoffs", "job_id": job_id})
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not answer.get("ok", True):
            raise HTTPException(status_code=404, detail=str(answer.get("error", "no such job")))
        return answer.get("handoffs", [])

    @app.get("/api/graph", response_model=M.GraphSnapshot)
    def graph() -> Any:
        return hub.graph.snapshot(generated_at=utc_now())

    @app.get("/api/office", response_model=M.OfficeSnapshot)
    def office() -> Any:
        return hub.office.snapshot()

    @app.post(
        "/api/jobs/{job_id}/artifacts/{artifact_id}/open",
        response_model=M.ArtifactOpened,
        responses={404: {"model": M.Refusal}, 409: {"model": M.Refusal}, 410: {"model": M.Refusal}},
    )
    def open_artifact(job_id: str, artifact_id: str) -> Any:
        """Reveal an artifact in the file manager. Never opens, never runs it.

        Every check is re-run now rather than trusted from registration time --
        see `backend/artifacts.py` for which ones and why.
        """
        try:
            target = A.resolve(hub.projection, job_id, artifact_id)
            show(target)
        except A.ArtifactError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return target.describe()

    @app.get("/api/graph/layout", response_model=M.GraphLayout)
    def read_layout() -> Any:
        return layout.read()

    @app.put(
        "/api/graph/layout",
        response_model=M.GraphLayout,
        responses={409: {"model": M.LayoutConflictBody}},
    )
    def save_layout(save: M.LayoutSave) -> Any:
        positions = {key: point.model_dump() for key, point in save.positions.items()}
        try:
            return layout.write(positions, save.revision)
        except LayoutConflict as exc:
            # The current layout travels with the refusal. The client merges
            # its own moved nodes onto it and saves again, so a second tab
            # costs a round trip rather than somebody's arrangement.
            return JSONResponse(
                {"ok": False, "detail": str(exc), "current": exc.current},
                status_code=409,
            )
        except LayoutError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/command", response_model=M.CommandResponse)
    def command(frame: dict[str, Any]) -> JSONResponse:
        kind = frame.get("type")
        if kind not in ALLOWED_COMMANDS:
            raise HTTPException(
                status_code=400,
                detail=f"{kind!r} is not an operator command; expected one of {list(ALLOWED_COMMANDS)}",
            )
        try:
            answer = hub.subscription.command(frame)
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # A refusal is an answer, not a server error: the daemon decided, and
        # the reason it gives is the thing worth showing.
        return JSONResponse(answer, status_code=200 if answer.get("ok") else 409)

    @app.post("/api/remote-approvals", response_model=M.RemoteApprovalCreated)
    def create_remote_approval(request: M.RemoteApprovalCreate) -> Any:
        frame = {
            "type": W.CAMPAIGN_BUNDLE_PREVIEW,
            "commandId": uuid.uuid4().hex,
            "jobId": request.jobId,
            "maxIterations": request.maxIterations,
            "maxAutoApprovals": request.maxAutoApprovals,
            "durationHours": request.durationHours,
        }
        try:
            answer = hub.subscription.command(frame)
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if answer.get("ok") is not True:
            raise HTTPException(status_code=409, detail=str(answer.get("error", "preview refused")))
        proposal = answer.get("proposal")
        if not isinstance(proposal, dict):
            raise HTTPException(status_code=502, detail="daemon returned no campaign proposal")
        try:
            token, row = remote_approvals.create(
                job_id=request.jobId,
                proposal=proposal,
                ttl_minutes=request.ttlMinutes,
                max_iterations=request.maxIterations,
                max_auto_approvals=request.maxAutoApprovals,
                duration_hours=request.durationHours,
            )
        except RemoteApprovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {
            "path": f"/remote-approval/{token}",
            "urls": [
                f"{origin.rstrip('/')}/remote-approval/{token}"
                for origin in public_origins
            ],
            "expiresAt": row.describe()["expiresAt"],
            "proposal": proposal,
        }

    @app.get("/remote-approval/{token}", response_class=HTMLResponse)
    def remote_approval_page(token: str) -> HTMLResponse:
        try:
            row = remote_approvals.read(token)
        except RemoteApprovalError as exc:
            return _remote_html(exc.detail, status=exc.status)
        return _remote_html("", row=row)

    @app.post("/remote-approval/{token}")
    def decide_remote_approval(token: str, decision: M.RemoteApprovalDecision) -> JSONResponse:
        try:
            row = remote_approvals.claim(token, decision.reviewSha256)
        except RemoteApprovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        try:
            if decision.decision == "revise":
                if not decision.comment.strip():
                    remote_approvals.release(token)
                    raise HTTPException(
                        status_code=422,
                        detail="a revision request must say what should change",
                    )
                answer = hub.subscription.command({
                    "type": W.JOB_MISSION_REVISE,
                    "commandId": uuid.uuid4().hex,
                    "jobId": row.job_id,
                    "requestedBy": decision.approvedBy.strip(),
                    "message": decision.comment.strip(),
                })
            else:
                answer = hub.subscription.command({
                    "type": W.CAMPAIGN_BUNDLE_APPROVE,
                    "commandId": uuid.uuid4().hex,
                    "jobId": row.job_id,
                    "approvedBy": decision.approvedBy.strip(),
                    "reviewSha256": row.review_sha256,
                    "maxIterations": row.max_iterations,
                    "maxAutoApprovals": row.max_auto_approvals,
                    "durationHours": row.duration_hours,
                    "comment": decision.comment.strip(),
                })
        except DaemonUnavailable as exc:
            remote_approvals.release(token)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if answer.get("ok") is not True:
            remote_approvals.complete(token, state="refused", result=answer)
            return JSONResponse(answer, status_code=409)
        if decision.decision == "revise":
            job = answer.get("job") if isinstance(answer.get("job"), dict) else {}
            active_job_id = job.get("job_id") if isinstance(job.get("job_id"), str) else None
            final_state = "revision_requested"
        else:
            continuation = (
                answer.get("continuation")
                if isinstance(answer.get("continuation"), dict)
                else {}
            )
            active_job_id = continuation.get("child_job_id")
            if not isinstance(active_job_id, str):
                active_job_id = None
            final_state = "approved"
        remote_approvals.complete(
            token,
            state=final_state,
            active_job_id=active_job_id,
            result=answer,
        )
        return JSONResponse(answer, status_code=200 if answer.get("ok") else 409)

    @app.get("/remote-approval/{token}/status", response_model=M.RemoteControlStatus)
    def remote_control_status(token: str) -> Any:
        try:
            row = remote_approvals.read(token)
        except RemoteApprovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        job_summary: dict[str, Any] | None = None
        if row.active_job_id:
            try:
                answer = hub.subscription.control(
                    {"command": "job", "job_id": row.active_job_id}
                )
            except DaemonUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if answer.get("ok") is True and isinstance(answer.get("job"), dict):
                resume = answer["job"]
                durable = resume.get("job") if isinstance(resume.get("job"), dict) else {}
                job_summary = {
                    key: durable.get(key)
                    for key in ("job_id", "objective", "state", "outcome", "iteration")
                }
                job_summary["waiting"] = resume.get("waiting")
                job_summary["willDeliverSteering"] = resume.get("will_deliver_steering")
                pending = resume.get("pending_steers")
                job_summary["pendingMessages"] = len(pending) if isinstance(pending, list) else 0
        recent = []
        if row.active_job_id:
            candidates = [
                event
                for event in hub.projection.events(since=0, limit=0)
                if event.get("job_id") == row.active_job_id
            ][-12:]
            recent = [
                {
                    key: event.get(key)
                    for key in ("event_id", "kind", "at", "agent")
                }
                for event in candidates
            ]
        return {"capability": row.describe(), "job": job_summary, "recentEvents": recent}

    @app.post("/remote-approval/{token}/message")
    def remote_control_message(
        token: str, request: M.RemoteControlMessage
    ) -> JSONResponse:
        try:
            row = remote_approvals.read(token)
        except RemoteApprovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if not row.active_job_id:
            raise HTTPException(status_code=409, detail="approve or request a revision first")
        try:
            answer = hub.subscription.command({
                "type": W.JOB_STEER,
                "commandId": uuid.uuid4().hex,
                "jobId": row.active_job_id,
                "sentBy": request.sentBy.strip(),
                "message": request.message.strip(),
            })
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(answer, status_code=200 if answer.get("ok") else 409)

    @app.post("/remote-approval/{token}/cancel")
    def remote_control_cancel(
        token: str, request: M.RemoteControlCancel
    ) -> JSONResponse:
        try:
            row = remote_approvals.read(token)
        except RemoteApprovalError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if not row.active_job_id:
            raise HTTPException(status_code=409, detail="there is no active job for this link")
        try:
            answer = hub.subscription.command({
                "type": W.JOB_CANCEL,
                "commandId": uuid.uuid4().hex,
                "jobId": row.active_job_id,
                "cancelledBy": request.requestedBy.strip(),
                "reason": request.reason.strip() or "stopped from remote control",
            })
        except DaemonUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(answer, status_code=200 if answer.get("ok") else 409)

    @app.websocket("/ws")
    async def stream(socket: WebSocket, after: int = 0) -> None:
        await socket.accept()
        channel = ClientChannel(capacity=DEFAULT_CAPACITY)
        hub.attach(channel)
        try:
            for frame in hub.hello(after):
                await socket.send_json(frame)
            while True:
                frame = await channel.queue.get()
                await socket.send_json(frame)
                if frame["type"] == RESYNC_REQUIRED:
                    # Sent, then closed. Leaving the socket open would let a
                    # client keep applying frames onto a view with a hole in
                    # it; idempotence resolves duplicates, not omissions.
                    await socket.close(code=RESYNC_CLOSE_CODE)
                    return
        except WebSocketDisconnect:
            return
        finally:
            hub.detach(channel)

    if ASSETS_DIR.is_dir():
        # Mounted inside the guard (so the Host check applies) but outside the
        # token check, for the same reason as the bundle: sprites are media,
        # not data about this run. Nothing here is fetched from off the disk.
        app.mount("/office-assets", StaticFiles(directory=str(ASSETS_DIR)), name="office-assets")

    if static_dir is not None and static_dir.is_dir():
        # Mounted inside the guard but outside the token check: a bundle is
        # code, and the page reads its token out of its own URL.
        app.mount("/", FrontendFiles(directory=str(static_dir), html=True), name="app")

    app.add_middleware(Guard, policy=policy)
    return app


def _remote_html(
    message: str,
    *,
    row: Any | None = None,
    status: int = 200,
) -> HTMLResponse:
    """A dependency-free approval page; it receives no operator session token."""
    if row is None:
        body = f"<h1>Approval unavailable</h1><p>{html.escape(message)}</p>"
    else:
        proposal = row.proposal
        board = proposal.get("board") if isinstance(proposal.get("board"), dict) else {}
        permissions = proposal.get("permissions") or []
        permission_html = "".join(
            "<li><b>{}</b> → {} <small>{}</small></li>".format(
                html.escape(str(item.get("role", ""))),
                html.escape(str(item.get("tool", ""))),
                html.escape(str(item.get("contract", "")) + " · " + str(item.get("phase", "hardware"))),
            )
            for item in permissions if isinstance(item, dict)
        )
        safety = (
            proposal.get("hardware_safety")
            if isinstance(proposal.get("hardware_safety"), dict)
            else {}
        )
        limit_html = "".join(
            "<li title=\"{}\">{} · {} ≤ <b>{} {}</b></li>".format(
                html.escape(str(item.get("source", "")), quote=True),
                html.escape(str(item.get("subject", ""))),
                html.escape(str(item.get("parameter", ""))),
                html.escape(str(item.get("maximum", ""))),
                html.escape(str(item.get("unit", ""))),
            )
            for item in safety.get("limits", [])
            if isinstance(item, dict)
        )
        pin_html = "".join(
            "<li title=\"{}\"><b>{}</b> · {} · {}</li>".format(
                html.escape(str(item.get("source", "")), quote=True),
                html.escape(str(item.get("signal", ""))),
                html.escape(str(item.get("pin", ""))),
                html.escape(str(item.get("direction", ""))),
            )
            for item in safety.get("pin_assignments", [])
            if isinstance(item, dict)
        )
        stop_html = "".join(
            f"<li>{html.escape(str(item))}</li>"
            for item in safety.get("stop_conditions", [])
            if isinstance(item, str)
        )
        review = html.escape(row.review_sha256)
        boundaries_html = "".join(
            "<section><h3>{}</h3><p class=\"digest\">{}</p><pre>{}</pre></section>".format(
                html.escape(str(item.get("contract", ""))),
                html.escape(str(item.get("contract_sha256", ""))),
                html.escape(json.dumps(item.get("hardware_safety", {}), ensure_ascii=False, indent=2)),
            ) for item in proposal.get("boundaries", []) if isinstance(item, dict)
        )
        safety_html = boundaries_html or (
            f"<h3>Voltage/current limits</h3><ul>{limit_html or '<li>missing</li>'}</ul>"
            f"<h3>Allowed pins</h3><ul>{pin_html or '<li>missing</li>'}</ul>"
            f"<h3>Immediate stop conditions</h3><ul>{stop_html or '<li>missing</li>'}</ul>"
        )
        initial_state = json.dumps(row.state)
        body = f"""
        <h1>RTASE mobile control</h1>
        <p class="warning">이 링크는 아래 mission과 그 결과 job 하나에만 유효합니다.</p>
        <dl>
          <dt>Job</dt><dd>{html.escape(row.job_id)}</dd>
          <dt>Board</dt><dd>{html.escape(str(board.get('title') or board.get('board_id') or '—'))}</dd>
          <dt>Contract</dt><dd>{html.escape(str(proposal.get('contract', '—')))}</dd>
          <dt>Iterations</dt><dd>{row.max_iterations}</dd>
          <dt>Auto approvals</dt><dd>{row.max_auto_approvals}</dd>
          <dt>Campaign hours</dt><dd>{row.duration_hours}</dd>
          <dt>Review digest</dt><dd class="digest">{review}</dd>
        </dl>
        <h2>Electrical safety envelope</h2>
        {safety_html}
        <h2>Hardware permissions</h2><ul>{permission_html}</ul>
        <label>이름 <input id="actor" autocomplete="name" maxlength="100"></label>
        <label>실행 지시 또는 수정 의견
          <textarea id="comment" maxlength="4000" placeholder="예: RF1 상승시간을 우선 최적화하고 CH3는 트리거 확인에만 사용"></textarea>
        </label>
        <section id="decision">
          <button id="approve">이 범위 승인 + 지시 전달</button>
          <button id="revise" class="secondary">승인하지 않고 재설계 요청</button>
        </section>
        <section id="control" hidden>
          <h2>진행 중 메시지</h2>
          <p id="status" class="status">상태를 읽는 중…</p>
          <label>다음 에이전트에게 보낼 메시지
            <textarea id="message" maxlength="4000" placeholder="지금 대화하듯 방향이나 확인할 점을 적으세요"></textarea>
          </label>
          <button id="send">메시지 보내기</button>
          <button id="cancel" class="danger">이 job 중단</button>
          <ol id="events"></ol>
        </section>
        <p id="result"></p>
        <script>
        let capabilityState = {initial_state};
        const decision = document.getElementById('decision');
        const control = document.getElementById('control');
        const result = document.getElementById('result');
        function actor() {{ return document.getElementById('actor').value.trim(); }}
        function showState() {{
          const decided = capabilityState !== 'ready' && capabilityState !== 'processing';
          decision.hidden = decided;
          control.hidden = !decided || capabilityState === 'refused';
        }}
        async function decide(kind) {{
          const actor = document.getElementById('actor').value.trim();
          const comment = document.getElementById('comment').value.trim();
          if (!actor) {{ result.textContent = '이름을 입력하세요.'; return; }}
          if (kind === 'revise' && !comment) {{ result.textContent = '바꿀 방향을 적으세요.'; return; }}
          document.getElementById('approve').disabled = true;
          document.getElementById('revise').disabled = true;
          const response = await fetch(location.pathname, {{
            method: 'POST', headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{approvedBy: actor, reviewSha256: '{review}', decision: kind, comment}})
          }});
          const payload = await response.json().catch(() => ({{}}));
          if (response.ok) {{
            capabilityState = kind === 'approve' ? 'approved' : 'revision_requested';
            result.textContent = kind === 'approve' ? '승인했습니다. 실행 job에 지시도 전달했습니다.' : '새 mission revision을 시작했습니다.';
            showState(); await refresh();
          }} else {{
            result.textContent = '거부됨: ' + (payload.detail || payload.error || response.status);
          }}
        }}
        async function refresh() {{
          if (control.hidden) return;
          const response = await fetch(location.pathname + '/status');
          const payload = await response.json().catch(() => ({{}}));
          if (!response.ok) {{ document.getElementById('status').textContent = payload.detail || '상태를 읽지 못했습니다.'; return; }}
          capabilityState = payload.capability.state;
          const job = payload.job;
          document.getElementById('status').textContent = job ?
            `${{job.state}} · iteration ${{job.iteration}}${{job.outcome ? ' · ' + job.outcome : ''}} · pending messages ${{job.pendingMessages}}` :
            '연결된 job이 아직 없습니다.';
          const events = document.getElementById('events');
          events.replaceChildren(...(payload.recentEvents || []).map((event) => {{
            const item = document.createElement('li');
            item.textContent = String(event.kind || '') + ' · ' + String(event.at || '');
            return item;
          }}));
        }}
        async function sendMessage() {{
          const message = document.getElementById('message').value.trim();
          if (!actor() || !message) {{ result.textContent = '이름과 메시지를 입력하세요.'; return; }}
          const response = await fetch(location.pathname + '/message', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{sentBy:actor(), message}})}});
          const payload = await response.json().catch(() => ({{}}));
          result.textContent = response.ok ? (payload.willBeDelivered ? '다음 에이전트에게 전달됩니다.' : '기록됐지만 이미 끝난 단계에는 전달되지 않습니다.') : '거부됨: ' + (payload.detail || payload.error || response.status);
          if (response.ok) document.getElementById('message').value = '';
          await refresh();
        }}
        async function cancelJob() {{
          if (!actor() || !confirm('이 job을 중단할까요?')) return;
          const response = await fetch(location.pathname + '/cancel', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{requestedBy:actor(), reason:'stopped from mobile control'}})}});
          const payload = await response.json().catch(() => ({{}}));
          result.textContent = response.ok ? '중단 요청을 기록했습니다.' : '거부됨: ' + (payload.detail || payload.error || response.status);
          await refresh();
        }}
        document.getElementById('approve').addEventListener('click', () => decide('approve'));
        document.getElementById('revise').addEventListener('click', () => decide('revise'));
        document.getElementById('send').addEventListener('click', sendMessage);
        document.getElementById('cancel').addEventListener('click', cancelJob);
        showState(); refresh(); setInterval(refresh, 5000);
        </script>
        """
    document = f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>RTASE campaign approval</title><style>
    body{{background:#0d1117;color:#e6edf3;font:16px system-ui;margin:40px auto;max-width:680px;padding:0 20px}}
    h1{{font-size:25px}} h2{{font-size:17px;margin-top:28px}} .warning{{color:#f2cc60}}
    dl{{display:grid;grid-template-columns:140px 1fr;gap:9px}}dt{{color:#8b949e}}dd{{margin:0}}
    .digest{{font:12px monospace;overflow-wrap:anywhere}}li{{margin:8px 0}}small{{color:#8b949e}}
    pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}}
    label{{display:grid;gap:7px;margin-top:20px}}input,textarea{{background:#161b22;border:1px solid #30363d;border-radius:6px;color:white;padding:11px}}
    textarea{{font:15px system-ui;min-height:96px;resize:vertical}}
    button{{background:#238636;border:0;border-radius:6px;color:white;font-weight:700;margin-top:12px;padding:11px 16px}}
    button.secondary{{background:#30363d}}button.danger{{background:#da3633;margin-left:8px}}
    button:disabled{{opacity:.5}}section{{margin-top:18px}}.status{{background:#161b22;border-radius:6px;padding:12px}}
    ol{{color:#8b949e;padding-left:22px}}ol b{{color:#e6edf3}}</style></head><body>{body}</body></html>"""
    return HTMLResponse(
        document,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'",
        },
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Serve the AI Hardware Autolab web UI.")
    parser.add_argument("--daemon", default=os.environ.get("QKD_WEBUI_DAEMON"))
    parser.add_argument("--host", default=os.environ.get("QKD_WEBUI_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("QKD_WEBUI_PORT", DEFAULT_PORT)))
    parser.add_argument(
        "--static",
        default=os.environ.get("QKD_WEBUI_STATIC"),
        help="built frontend to serve at /. Omit while developing against Vite.",
    )
    parser.add_argument(
        "--dev-origin",
        action="append",
        default=[],
        help="an extra allowed Origin, e.g. http://127.0.0.1:5173 for the Vite dev server",
    )
    parser.add_argument(
        "--public-origin",
        action="append",
        default=[],
        help=(
            "exact HTTPS reverse-proxy origin allowed to reach this loopback server, "
            "for example https://lab-pc.example.ts.net"
        ),
    )
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        # Refused rather than warned about. The guard rejects any request whose
        # Host is not a loopback name, so a wider bind would accept the
        # connection and then refuse every request on it -- a listening port
        # that answers nothing, which is worse than not listening.
        print(
            f"refusing to bind {args.host}: this server answers only loopback names, so "
            "binding wider would open a port that refuses everything that reaches it.",
            file=sys.stderr,
        )
        return 2

    static = Path(args.static).resolve() if args.static else None
    session = Session.start(port=args.port)
    try:
        app = create_app(
            address=args.daemon,
            static_dir=static,
            session=session,
            port=args.port,
            dev_origins=tuple(args.dev_origin),
            public_origins=tuple(args.public_origin),
        )
    except ValueError as exc:
        session.clear()
        print(f"invalid public origin: {exc}", file=sys.stderr)
        return 2
    print(f"open: {session.url(args.host, args.port)}", flush=True)
    for origin in args.public_origin:
        print(f"mobile control origin: {origin.rstrip('/')}", flush=True)
    print(f"token also written to {session.file}", flush=True)
    try:
        # access_log off: the token rides in ?token= on the WebSocket handshake,
        # because a browser cannot set a header on one, and a logged URL is a
        # logged credential.
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    finally:
        session.clear()
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
