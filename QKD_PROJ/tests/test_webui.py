"""The standalone web UI's backend, against a real daemon over loopback.

The projection tests run in memory because that is where the redaction and the
duplicate-artifact rules live. Everything else runs against an actual
``S.Service``, for the same reason ``test_wire.py`` does: both bugs this
protocol has had were transport bugs, and an in-memory double passes those.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import daemon as D  # noqa: E402
from QKD_PROJ.orchestrator import service as S  # noqa: E402
from QKD_PROJ.orchestrator import wire as W  # noqa: E402
from QKD_PROJ.orchestrator.jobs import JobLedger  # noqa: E402
from QKD_PROJ.orchestrator.loop import start  # noqa: E402
from QKD_PROJ.tests.test_daemon import working_runner  # noqa: E402
from QKD_PROJ.tests.test_loop import make_run  # noqa: E402
from QKD_PROJ.webui.backend import models as M  # noqa: E402
from QKD_PROJ.webui.backend.channels import (  # noqa: E402
    RESYNC_CLOSE_CODE,
    RESYNC_REQUIRED,
    ClientChannel,
)
from QKD_PROJ.webui.backend.security import Forbidden, Policy, Session, Unauthorized  # noqa: E402
from QKD_PROJ.webui.backend.state import Projection, browser_frame  # noqa: E402
from QKD_PROJ.webui.backend.wire import (  # noqa: E402
    DaemonSubscription,
    DaemonUnavailable,
    WireGapError,
    wait_for,
)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from QKD_PROJ.webui.backend.app import (  # noqa: E402
    DEFAULT_PORT,
    GRAPH_CHANGED,
    create_app,
)
from QKD_PROJ.webui.tools import export_openapi  # noqa: E402

# The guard checks Host, so the test client has to dial a loopback name rather
# than starlette's default "testserver".
BASE = f"http://127.0.0.1:{DEFAULT_PORT}"


def _app(address: str | None):
    """An app whose token this test holds, and which writes no session file."""
    session = Session(file=None)
    return create_app(address=address, session=session, port=DEFAULT_PORT), session


def _ws(path: str) -> str:
    """An absolute ws:// URL.

    ``TestClient.websocket_connect`` joins a relative path onto a hardcoded
    ``ws://testserver``, which the guard's Host check refuses -- correctly, and
    for a reason that has nothing to do with what these tests are asserting.
    """
    return f"ws://127.0.0.1:{DEFAULT_PORT}{path}"


def _client(app, session):
    return TestClient(
        app, base_url=BASE, headers={"Authorization": f"Bearer {session.token}"}
    )


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    return JobLedger(tmp_path / "jobs.db"), runs


def _service(env):
    ledger, runs = env
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    config = S.ServiceConfig(interval_s=0.05, install_signal_handlers=False, poll_s=0.02)
    return S.Service(ledger, daemon, config=config, transport=S.SocketTransport()), ledger


# -- the projection -----------------------------------------------------------


def test_events_are_kept_in_order_and_filtered_by_job():
    projection = Projection()
    for i, job in enumerate(("a", "a", "b"), start=1):
        projection.apply({"type": W.LEDGER_EVENT, "event": {"event_id": i, "job_id": job}})

    assert [e["event_id"] for e in projection.events()] == [1, 2, 3]
    assert [e["event_id"] for e in projection.events(job_id="a")] == [1, 2]
    assert [e["event_id"] for e in projection.events(since=2)] == [3]


def test_history_is_capped_and_says_where_it_starts():
    projection = Projection(history=2)
    for i in (1, 2, 3):
        projection.apply({"type": W.LEDGER_EVENT, "event": {"event_id": i, "job_id": "a"}})

    assert [e["event_id"] for e in projection.events()] == [2, 3]
    assert projection.oldest_event_id() == 2


def test_an_artifact_path_never_reaches_a_browser():
    projection = Projection()
    seen = []
    projection.listen(seen.append)
    frame = {
        "type": W.ARTIFACT_REGISTERED,
        "jobId": "j1",
        "artifactId": "a1",
        "projectRoot": "C:/KIST",
        "targetPath": "C:/KIST/runs/x/verdict.json",
    }
    projection.apply(frame)

    assert "targetPath" not in seen[0] and "projectRoot" not in seen[0]
    assert seen[0]["artifactId"] == "a1"
    # The host keeps what the browser is not given, so it can still open it.
    assert projection.artifact_path("j1", "a1") == "C:/KIST/runs/x/verdict.json"
    assert projection.artifact_path("other", "a1") is None


def test_a_repointed_artifact_is_refused_within_its_own_job():
    projection = Projection()
    first = {"type": W.ARTIFACT_REGISTERED, "jobId": "j1", "artifactId": "a1", "targetPath": "one"}
    projection.apply(first)
    projection.apply(dict(first))  # identical: idempotent, no warning
    assert projection.warnings() == []

    projection.apply({**first, "targetPath": "two"})
    assert projection.artifact_path("j1", "a1") == "one"
    assert "refusing to re-point" in projection.warnings()[0]


def test_two_jobs_may_each_register_their_own_report():
    """`artifact_id` is a role, not a global name. The key is the pair."""
    projection = Projection()
    for job, path in (("j1", "runs/one/report.md"), ("j2", "runs/two/report.md")):
        projection.apply(
            {
                "type": W.ARTIFACT_REGISTERED,
                "jobId": job,
                "artifactId": "report",
                "targetPath": path,
            }
        )

    assert projection.warnings() == [], "a second job's report is not a re-pointing"
    assert projection.artifact_path("j1", "report") == "runs/one/report.md"
    assert projection.artifact_path("j2", "report") == "runs/two/report.md"
    # And one job's id does not resolve through another's.
    assert projection.artifact_path("j3", "report") is None
    assert set(projection.artifacts()) == {("j1", "report"), ("j2", "report")}


def test_a_registration_missing_either_half_of_the_key_is_ignored():
    projection = Projection()
    projection.apply({"type": W.ARTIFACT_REGISTERED, "artifactId": "a1", "targetPath": "x"})
    projection.apply({"type": W.ARTIFACT_REGISTERED, "jobId": "j1", "targetPath": "x"})
    assert projection.artifacts() == {}


def test_only_artifact_frames_are_redacted():
    frame = {"type": W.LEDGER_EVENT, "event": {"event_id": 1, "payload": {"targetPath": "keep"}}}
    assert browser_frame(frame) is frame


# -- the subscription ---------------------------------------------------------


def test_subscription_replays_from_zero_and_reports_its_cursor(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    frames = []
    with service:
        subscription = DaemonSubscription(
            address=service.transport.address, on_frame=frames.append
        )
        subscription.start()
        try:
            assert wait_for(lambda: subscription.status().connected)
        finally:
            subscription.stop()

    events = [f for f in frames if f["type"] == W.LEDGER_EVENT]
    ready = [f for f in frames if f["type"] == W.READY]
    assert events, "a started job leaves rows to replay"
    assert ready[-1]["cursor"] == events[-1]["event"]["event_id"]
    assert subscription.status().cursor == events[-1]["event"]["event_id"]


def test_a_forward_gap_resubscribes_instead_of_showing_a_hole():
    subscription = DaemonSubscription(on_frame=lambda _: None)
    subscription._cursor = 4  # noqa: SLF001 - the gap check is the unit under test

    subscription._apply({"type": W.LEDGER_EVENT, "event": {"event_id": 5}})  # noqa: SLF001
    assert subscription.status().cursor == 5

    subscription._apply({"type": W.LEDGER_EVENT, "event": {"event_id": 5}})  # noqa: SLF001
    assert subscription.status().cursor == 5, "a replayed row is not news"

    with pytest.raises(WireGapError):
        subscription._apply({"type": W.LEDGER_EVENT, "event": {"event_id": 7}})  # noqa: SLF001
    assert subscription.status().cursor == 5, "the cursor never moved past the hole"
    assert subscription.status().gaps == 1


def test_a_ready_that_disagrees_with_the_replay_is_an_error():
    subscription = DaemonSubscription(on_frame=lambda _: None)
    subscription._apply({"type": W.LEDGER_EVENT, "event": {"event_id": 1}})  # noqa: SLF001
    with pytest.raises(WireGapError):
        subscription._apply({"type": W.READY, "cursor": 9})  # noqa: SLF001
    assert subscription.status().connected is False


def test_ready_exposes_profile_and_workspace_mode_without_guessing():
    subscription = DaemonSubscription(on_frame=lambda _: None)
    subscription._apply(  # noqa: SLF001 - applying one protocol frame is the unit
        {
            "type": W.READY,
            "cursor": 0,
            "profile": "offline",
            "workspaceMode": "isolated",
        }
    )

    assert subscription.status().profile == "offline"
    assert subscription.status().workspace_mode == "isolated"


def test_a_command_travels_on_its_own_connection(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        subscription = DaemonSubscription(address=service.transport.address, on_frame=lambda _: None)
        subscription.start()
        try:
            assert wait_for(lambda: subscription.status().connected)
            answer = subscription.command(
                {"type": W.JOB_STEER, "jobId": job.job_id, "message": "check CN1", "sentBy": "q"}
            )
        finally:
            subscription.stop()

    assert answer["ok"] is True
    assert answer["commandId"].startswith("webui-")


def test_a_refused_command_comes_back_as_a_reason(env):
    service, _ = _service(env)
    with service:
        subscription = DaemonSubscription(address=service.transport.address)
        answer = subscription.command(
            {"type": W.JOB_STEER, "jobId": "nope", "message": "x", "sentBy": "q"}
        )
    assert answer["ok"] is False
    assert answer["error"]


def test_no_daemon_is_an_unavailable_not_a_crash(tmp_path):
    subscription = DaemonSubscription(address="tcp://127.0.0.1:1", command_timeout_s=0.5)
    with pytest.raises(DaemonUnavailable):
        subscription.command({"type": W.JOB_CANCEL, "jobId": "j", "cancelledBy": "q"})


# -- the guard ----------------------------------------------------------------


def test_a_token_is_required_and_compared_whole():
    session = Session(file=None)
    policy = Policy.build(session, port=DEFAULT_PORT)

    policy.check_token({"authorization": f"Bearer {session.token}"}, None)
    policy.check_token({"x-qkd-token": session.token}, None)
    policy.check_token({}, session.token)

    for wrong in (None, "", "nope", session.token[:-1], session.token + "x"):
        with pytest.raises(Unauthorized):
            policy.check_token({}, wrong)


def test_only_loopback_names_and_known_origins_are_served():
    policy = Policy.build(Session(file=None), port=DEFAULT_PORT)

    for good in (f"127.0.0.1:{DEFAULT_PORT}", "localhost:1", "LOCALHOST", "[::1]:8787"):
        policy.check_host(good)
    # What a rebound DNS record looks like from in here.
    with pytest.raises(Forbidden):
        policy.check_host("attacker.example:8787")
    with pytest.raises(Forbidden):
        policy.check_host("192.168.1.9:8787")

    policy.check_origin(None)  # curl and the launcher; a page cannot omit it
    policy.check_origin(f"http://localhost:{DEFAULT_PORT}")
    for bad in ("http://evil.example", "null", f"https://127.0.0.1:{DEFAULT_PORT}"):
        with pytest.raises(Forbidden):
            policy.check_origin(bad)


def test_a_dev_origin_is_allowed_only_when_it_was_asked_for():
    plain = Policy.build(Session(file=None), port=DEFAULT_PORT)
    with pytest.raises(Forbidden):
        plain.check_origin("http://127.0.0.1:5173")

    dev = Policy.build(
        Session(file=None), port=DEFAULT_PORT, extra_origins=["http://127.0.0.1:5173/"]
    )
    dev.check_origin("http://127.0.0.1:5173")


def test_an_exact_https_reverse_proxy_origin_allows_its_host_and_origin():
    policy = Policy.build(
        Session(file=None),
        port=DEFAULT_PORT,
        public_origins=["https://lab-pc.example.ts.net/"],
    )
    policy.check_host("lab-pc.example.ts.net")
    policy.check_origin("https://lab-pc.example.ts.net")
    with pytest.raises(Forbidden):
        policy.check_host("another.example.ts.net")
    with pytest.raises(ValueError):
        Policy.build(
            Session(file=None),
            port=DEFAULT_PORT,
            public_origins=["http://lab-pc.example.ts.net"],
        )


def test_every_api_route_refuses_an_untokened_request():
    app, _ = _app("tcp://127.0.0.1:1")
    with TestClient(app, base_url=BASE) as http:
        assert http.get("/api/health").status_code == 401
        assert http.get("/api/state").status_code == 401
        assert http.get("/api/events").status_code == 401
        assert http.get("/api/jobs/j1").status_code == 401
        refused = http.post("/api/command", json={"type": W.JOB_CANCEL})
        assert refused.status_code == 401
        assert "session token" in refused.json()["detail"]


def test_a_cross_origin_page_is_refused_even_holding_the_token():
    """The case that motivates the origin check: the token is not enough."""
    app, session = _app("tcp://127.0.0.1:1")
    with TestClient(app, base_url=BASE) as http:
        answer = http.get(
            "/api/state",
            headers={
                "Authorization": f"Bearer {session.token}",
                "Origin": "http://evil.example",
            },
        )
    assert answer.status_code == 403
    assert "not allowed" in answer.json()["detail"]


def test_a_rebound_host_is_refused_before_anything_else():
    app, session = _app("tcp://127.0.0.1:1")
    with TestClient(app, base_url="http://attacker.example:8787") as http:
        answer = http.get("/api/health", headers={"Authorization": f"Bearer {session.token}"})
    assert answer.status_code == 403
    assert "loopback" in answer.json()["detail"]


def test_agent_catalog_is_the_safe_half_of_runtime_definitions():
    app, session = _app("tcp://127.0.0.1:1")
    with _client(app, session) as http:
        answer = http.get("/api/agents")
    assert answer.status_code == 200
    agents = {item["name"]: item for item in answer.json()["agents"]}
    assert len(agents) == 14
    assert agents["mission-architect"]["role"] == "discover"
    assert agents["board-operator"]["touchesHardware"] is True
    assert "program_cpld_sram" in agents["board-operator"]["mcpTools"]
    assert agents["firmware-builder"]["role"] == "build"
    assert agents["firmware-builder"]["touchesHardware"] is False
    assert "run_board_build" in agents["firmware-builder"]["mcpTools"]
    assert "prompt" not in agents["board-operator"]
    assert "source" not in agents["board-operator"]


def test_a_websocket_without_the_token_never_opens(env):
    service, _ = _service(env)
    with service:
        app, _ = _app(service.transport.address)
        with TestClient(app, base_url=BASE) as http:
            with pytest.raises(Exception):
                with http.websocket_connect("/ws"):
                    pass


def test_a_websocket_may_carry_the_token_in_the_query(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            with http.websocket_connect(_ws(f"/ws?token={session.token}")) as socket:
                assert socket.receive_json()["type"] == "webui.hello"


# -- backpressure --------------------------------------------------------------


def test_a_full_queue_reports_a_hole_instead_of_dropping_quietly():
    async def run():
        channel = ClientChannel(capacity=2)
        for i in (1, 2):
            channel.offer({"type": W.LEDGER_EVENT, "event": {"event_id": i}}, cursor=i)
        assert channel.queue.qsize() == 2

        channel.offer({"type": W.LEDGER_EVENT, "event": {"event_id": 3}}, cursor=3)
        assert channel.queue.qsize() == 1, "the backlog went, and said so"

        notice = channel.queue.get_nowait()
        assert notice["type"] == RESYNC_REQUIRED
        assert notice["cursor"] == 3
        assert notice["dropped"] == 3  # two queued, plus the one that did not fit
        assert channel.holed is True

    asyncio.run(run())


def test_nothing_is_delivered_after_the_hole_is_declared():
    """Idempotence resolves duplicates, not omissions.

    A client that kept applying frames after the notice would hold a view with
    a gap in the middle of it, and the frames either side would look
    consecutive. So the channel stops offering and the socket closes.
    """
    async def run():
        channel = ClientChannel(capacity=1)
        channel.offer({"n": 1}, cursor=1)
        channel.offer({"n": 2}, cursor=2)  # overflows

        channel.offer({"n": 3}, cursor=3)
        channel.offer({"n": 4}, cursor=4)
        assert channel.queue.qsize() == 1, "only the notice is left to send"
        assert channel.queue.get_nowait()["type"] == RESYNC_REQUIRED
        assert channel.dropped == 4

    asyncio.run(run())


def test_a_slow_websocket_is_told_and_then_closed(env):
    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            with http.websocket_connect(_ws(f"/ws?token={session.token}")) as socket:
                while socket.receive_json()["type"] != "webui.ready":
                    pass
                hub = app.state.hub
                channel = next(iter(hub._clients))  # noqa: SLF001
                for i in range(channel.capacity + 5):
                    channel.offer({"type": W.LEDGER_EVENT, "event": {"event_id": i}}, cursor=i)

                frame = socket.receive_json()
                assert frame["type"] == RESYNC_REQUIRED
                assert frame["dropped"] >= channel.capacity
                # The contract's second half: the stream is over, not paused.
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket.receive_json()
                assert closed.value.code == RESYNC_CLOSE_CODE


# -- the browser contract ------------------------------------------------------


def test_the_schema_names_every_field_the_browser_reads():
    """The generated TypeScript is only as good as what is modelled here."""
    app, _ = _app(None)
    schema = app.openapi()["components"]["schemas"]

    assert set(M.StateResponse.model_fields) == {
        "daemon",
        "cursor",
        "jobs",
        "inbox",
        "results",
        "warnings",
        "snapshot",
    }
    # The three free-form payloads are the daemon's to shape, and each says so.
    for model, field in (
        (M.LedgerEvent, "payload"),
        (M.StateResponse, "snapshot"),
        (M.JobDetail, "job"),
    ):
        assert model.model_fields[field].description, f"{model.__name__}.{field} needs a reason"

    # An unmodelled key on a strict model is a bug, not a passthrough.
    with pytest.raises(Exception):
        M.DaemonStatus(
            address="x", connected=True, cursor=0, attempts=0, frames=0, gaps=0, surprise=1
        )

    assert "StateResponse" in schema and "WaitingRow" in schema and "JobRow" in schema


def test_state_types_survive_both_shapes_of_jobs():
    """`status.jobs` is a dict under a daemon and a list without one."""
    waiting = {"job_id": "j1", "state": "running", "status": "runnable", "detail": "ready"}
    row = {
        "job_id": "j2",
        "gate": "g",
        "objective": "o",
        "state": "running",
        "outcome": None,
        "iteration": 1,
        "created_at": "t",
        "updated_at": "t",
    }
    parsed = M.StateResponse(
        daemon={
            "address": "a",
            "connected": True,
            "cursor": 0,
            "attempts": 1,
            "frames": 1,
            "gaps": 0,
        },
        cursor=0,
        jobs=[waiting, row],
        inbox=[],
        results={},
        warnings=[],
        snapshot={},
    )
    assert isinstance(parsed.jobs[0], M.WaitingRow)
    assert isinstance(parsed.jobs[1], M.JobRow)


def test_the_exported_schema_is_not_stale():
    """The committed OpenAPI is what TypeScript is generated from.

    If this fails the browser contract moved without the frozen copy moving
    with it, and the generated `schema.d.ts` is describing an older server.
    Regenerate with `py -3 QKD_PROJ/webui/tools/export_openapi.py`.
    """
    assert export_openapi.main(["--check"]) == 0


def test_the_route_table_is_read_off_the_app_not_off_the_prose():
    """Documented methods drift; this reads the ones the app actually has."""
    app, _ = _app(None)
    paths = app.openapi()["paths"]

    assert set(paths["/api/jobs/{job_id}"]) == {"get"}
    assert set(paths["/api/state"]) == {"get"}
    assert set(paths["/api/events"]) == {"get"}
    assert set(paths["/api/health"]) == {"get"}
    # The only mutating door.
    assert set(paths["/api/command"]) == {"post"}


# -- the HTTP surface ---------------------------------------------------------


def test_state_and_events_answer_from_the_replayed_ledger(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            hub = app.state.hub
            assert wait_for(lambda: bool(hub.projection.snapshot()), timeout=10)

            state = http.get("/api/state").json()
            assert state["daemon"]["connected"] is True
            assert [j["job_id"] for j in state["jobs"]] == [job.job_id]

            events = http.get("/api/events").json()
            assert events["events"], "the replay left history to serve"
            assert events["cursor"] == events["events"][-1]["event_id"]

            one = http.get(f"/api/jobs/{job.job_id}").json()
            assert one["job"]["job"]["job_id"] == job.job_id
            assert all(e["job_id"] == job.job_id for e in one["events"])
            assert one["cursor"] == one["events"][-1]["event_id"]
            assert one["latest"] >= one["cursor"]

            handoffs = http.get(f"/api/jobs/{job.job_id}/handoffs").json()
            assert [row["kind"] for row in handoffs] == ["Brief"]
            assert handoffs[0]["payload"]["scenario"] == "build it"
            assert handoffs[0]["sha256"]


def test_a_websocket_gets_snapshot_backlog_then_ready(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            hub = app.state.hub
            assert wait_for(lambda: bool(hub.projection.snapshot()), timeout=10)
            with http.websocket_connect(_ws(f"/ws?token={session.token}")) as socket:
                frames = []
                while True:
                    frame = socket.receive_json()
                    frames.append(frame)
                    if frame["type"] == "webui.ready":
                        break

    kinds = [f["type"] for f in frames]
    assert kinds[0] == "webui.hello"
    assert kinds[1] == "webui.snapshot"
    assert W.LEDGER_EVENT in kinds
    assert kinds[-1] == "webui.ready"
    assert frames[0]["status"]["connected"] is True


def test_the_command_endpoint_refuses_anything_that_is_not_an_operator_command(env):
    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            refused = http.post("/api/command", json={"type": "subscribe", "protocol": 1})
            assert refused.status_code == 400
            assert "operator command" in refused.json()["detail"]


def test_a_daemon_refusal_is_a_conflict_not_a_server_error(env):
    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            answer = http.post(
                "/api/command",
                json={"type": W.JOB_CANCEL, "jobId": "nope", "cancelledBy": "q"},
            )
    assert answer.status_code == 409
    assert answer.json()["ok"] is False


def test_health_answers_while_no_daemon_is_running():
    app, session = _app("tcp://127.0.0.1:1")
    with _client(app, session) as http:
        body = http.get("/api/health").json()
    assert body["ok"] is True
    assert body["daemon"]["connected"] is False


# -- the graph over HTTP -------------------------------------------------------


def test_the_graph_endpoint_answers_from_the_replayed_stream(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            hub = app.state.hub
            assert wait_for(lambda: bool(hub.projection.snapshot()), timeout=10)
            body = http.get("/api/graph").json()

    assert body["cursor"] >= 1
    assert body["generatedAt"]
    assert f"job:{job.job_id}" in [n["id"] for n in body["nodes"]]
    # `g.yaml` is the gate the ledger recorded, so the contract reference is
    # there even though no such file exists to resolve it.
    contracts = [n for n in body["nodes"] if n["kind"] == "contract"]
    assert [n["id"] for n in contracts] == ["contract:g.yaml"]
    assert contracts[0]["unresolved"] is True
    assert all(e["evidence"]["field"] for e in body["edges"])


def test_the_graph_endpoint_needs_the_token_like_everything_else():
    app, _ = _app("tcp://127.0.0.1:1")
    with TestClient(app, base_url=BASE) as http:
        assert http.get("/api/graph").status_code == 401


def test_a_browser_is_told_the_graph_moved_and_re_reads_it(env):
    service, ledger = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            hub = app.state.hub
            assert wait_for(lambda: hub.subscription.status().connected, timeout=10)
            with http.websocket_connect(_ws(f"/ws?token={session.token}")) as socket:
                while socket.receive_json()["type"] != "webui.ready":
                    pass
                start(ledger, "g.yaml", "build it")

                seen = []
                for _ in range(40):
                    frame = socket.receive_json()
                    seen.append(frame["type"])
                    if frame["type"] == GRAPH_CHANGED:
                        assert isinstance(frame["cursor"], int)
                        break
                assert GRAPH_CHANGED in seen

            body = http.get("/api/graph").json()

    assert [n for n in body["nodes"] if n["kind"] == "job"], "the notice was not empty"


def test_a_replay_from_zero_clears_both_projections_together(env):
    """Clearing one would leave the graph holding a history the events lost."""
    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session):
            hub = app.state.hub
            assert wait_for(lambda: hub.subscription.status().connected, timeout=10)

            hub.projection.apply(
                {"type": W.LEDGER_EVENT, "event": {"event_id": 900, "job_id": "ghost"}}
            )
            hub.graph.apply_ledger_event(
                {"event_id": 900, "job_id": "ghost", "kind": "job.created",
                 "payload": {"gate": "g.yaml", "objective": "left over"}}
            )
            assert hub.graph.snapshot()["nodes"]

            hub._on_replay(0)  # noqa: SLF001 - what a fresh subscribe triggers
            assert hub.projection.events() == []
            assert hub.graph.snapshot()["nodes"] == []
            assert hub.graph.cursor == 0


def test_a_resumed_subscription_keeps_what_it_has(env):
    """Only `after == 0` is a replay. A resume is not a reason to forget."""
    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session):
            hub = app.state.hub
            hub.graph.apply_ledger_event(
                {"event_id": 5, "job_id": "j", "kind": "job.created",
                 "payload": {"gate": "g.yaml", "objective": "keep me"}}
            )
            hub._on_replay(5)  # noqa: SLF001
            assert hub.graph.snapshot()["nodes"]

def test_a_transcript_comes_from_the_ledger_not_the_capped_projection(env):
    """The projection exists so a late browser draws something immediately.

    A transcript is a different question -- it wants every row this job ever
    wrote, including ones evicted from that cap -- so it is read from the
    ledger. This holds a projection of one event and checks the job route
    still returns the whole history.
    """
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    for i in range(6):
        ledger.append_event(job.job_id, "runtime.item/completed", {"n": i}, "verifier")

    with service:
        session = Session(file=None)
        app = create_app(
            address=service.transport.address, session=session, port=DEFAULT_PORT
        )
        # One row of slack: anything older has already been evicted.
        app.state.hub.projection.history = 1
        with _client(app, session) as http:
            hub = app.state.hub
            assert wait_for(lambda: hub.subscription.status().connected, timeout=10)

            transcript = http.get(f"/api/jobs/{job.job_id}").json()

    kinds = [e["kind"] for e in transcript["events"]]
    assert "job.created" in kinds, "the row the projection can no longer hold"
    assert kinds.count("runtime.item/completed") == 6
    assert all(e["job_id"] == job.job_id for e in transcript["events"])


def test_a_transcript_page_can_be_continued(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    for i in range(4):
        ledger.append_event(job.job_id, "runtime.item/completed", {"n": i}, "verifier")

    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            first = http.get(f"/api/jobs/{job.job_id}?limit=2").json()
            rest = http.get(f"/api/jobs/{job.job_id}?since={first['cursor']}").json()

    assert len(first["events"]) == 2
    assert [e["event_id"] for e in rest["events"]] == [
        e for e in range(first["cursor"] + 1, rest["cursor"] + 1)
    ]


def test_discovery_is_an_operator_command_like_the_others(env):
    """`contract_gap` is an answer, and job.discover is the door it opens.

    Refusing it at this layer would make the UI treat "nobody has specified
    this yet" as a failure with nothing to do about it.
    """
    from QKD_PROJ.webui.backend.app import ALLOWED_COMMANDS

    assert W.JOB_DISCOVER in ALLOWED_COMMANDS
    assert W.JOB_START in ALLOWED_COMMANDS

    service, _ = _service(env)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            # Forwarded rather than refused at the door: the daemon decides,
            # and a bad scenario digest comes back as its reason, not ours.
            answer = http.post(
                "/api/command",
                json={
                    "type": W.JOB_DISCOVER,
                    "objective": "find out what a contract would say",
                    "scenario": "gate13 symbol timing",
                    "scenarioSha256": "a" * 64,
                },
            )
    assert answer.status_code in (200, 409)
    body = answer.json()
    assert "not an operator command" not in str(body)
    if not body.get("ok"):
        assert body.get("error"), "a refusal must carry the daemon's reason"
