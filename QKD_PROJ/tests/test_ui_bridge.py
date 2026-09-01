"""The daemon against a real client in another process.

Every in-process test passed while an external Node bridge killed the accept
loop on its first connection. A client that shares this interpreter shares its
GIL and its scheduling, and the pipe bug lived in exactly that difference: the
reader and the pusher were on threads of the same process, so the handle's
serialised I/O never showed. These tests spawn `node` and let the operating
system do the interleaving.

They are skipped where node is missing, which makes them worth stating plainly
rather than trusting: this file is the only place the transport is exercised
the way it actually ships.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

CLIENT = Path(__file__).parent / "fixtures" / "ui_bridge_client.mjs"
NODE = shutil.which("node")

pytestmark = [
    pytest.mark.skipif(NODE is None, reason="node is needed to drive a real client"),
    pytest.mark.skipif(not S.IS_WINDOWS, reason="the named pipe transport is Windows-only"),
]


@pytest.fixture()
def service(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    ledger = JobLedger(tmp_path / "jobs.db")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    name = r"\\.\pipe\qkd-uibridge-" + str(time.time_ns())
    started = S.Service(
        ledger,
        daemon,
        config=S.ServiceConfig(
            interval_s=0.1,
            pipe_name=name,
            install_signal_handlers=False,
            poll_s=0.05,
            runs_dir=runs,
            project_root=tmp_path,
        ),
    )
    started.start()
    try:
        yield started, ledger, name
    finally:
        started.close()


def run_client(
    pipe: str,
    after: int = 0,
    wanted: int = 3,
    hold_ms: int = 0,
    timeout: float = 30,
    command: dict | None = None,
):
    """One Node client, run to completion. Returns what it saw."""
    proc = subprocess.run(
        [
            NODE,
            str(CLIENT),
            pipe,
            str(after),
            str(wanted),
            str(hold_ms),
            json.dumps(command) if command else "",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.stdout.strip(), f"node printed nothing; stderr={proc.stderr[:400]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def spawn_client(pipe: str, after: int = 0, wanted: int = 3, hold_ms: int = 3000):
    return subprocess.Popen(
        [NODE, str(CLIENT), pipe, str(after), str(wanted), str(hold_ms)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# -- the failure the reviewer saw --------------------------------------------


def test_a_node_client_subscribes_and_the_accept_loop_survives(service):
    """The reported failure: accept died creating the second instance, error 5."""
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    seen = run_client(pipe, wanted=3)
    assert seen["ready"] is not None, seen
    assert seen["errors"] == []
    assert started.transport_error is None
    assert started._server.is_alive()
    assert started.transport.warnings == []


def test_a_cli_gets_through_while_a_node_ui_holds_the_pipe(service):
    """The regression: one client must not queue the other out."""
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    proc = spawn_client(pipe, wanted=3, hold_ms=4000)
    try:
        time.sleep(1.2)
        assert started.transport_error is None
        answer = S.send({"command": "ping"}, pipe, timeout=8)
        assert answer["pong"] is True

        status = S.send({"command": "status"}, pipe, timeout=8)
        assert status["ok"] is True
    finally:
        out, err = proc.communicate(timeout=25)

    seen = json.loads(out.strip().splitlines()[-1])
    assert seen["ready"] is not None
    assert seen["errors"] == []
    assert started.transport_error is None


def test_several_node_clients_at_once(service):
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    procs = [spawn_client(pipe, wanted=3, hold_ms=2500) for _ in range(3)]
    time.sleep(1.0)
    assert started.transport_error is None
    assert S.send({"command": "ping"}, pipe, timeout=8)["pong"] is True

    ready = 0
    for proc in procs:
        out, err = proc.communicate(timeout=25)
        answer = json.loads(out.strip().splitlines()[-1])
        if answer["ready"] is not None:
            ready += 1
    assert ready == 3
    assert started.transport_error is None
    assert started.transport.warnings == []


def test_repeated_reconnects_do_not_exhaust_the_pipe(service):
    """A bridge retrying after a dropped connection must keep finding the name."""
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    for attempt in range(6):
        seen = run_client(pipe, wanted=3)
        assert seen["ready"] is not None, f"attempt {attempt}: {seen}"
        assert "ENOENT" not in seen["errors"], f"attempt {attempt}: the pipe name vanished"
        assert started.transport_error is None, f"attempt {attempt}: {started.transport_error}"


def test_a_node_client_resumes_after_its_cursor(service):
    started, ledger, pipe = service
    job = start(ledger, "g.yaml", "build it")

    first = run_client(pipe, wanted=3)
    applied = first["ready"]
    ledger.append_event(job.job_id, "runtime.item/completed", {"x": 1})

    second = run_client(pipe, after=applied, wanted=2)
    replayed = [f for f in second["frames"] if f["type"] == W.LEDGER_EVENT]
    assert [f["cursor"] for f in replayed] == [applied + 1]
    assert second["ready"] == applied + 1


def test_a_node_client_sees_live_pushes(service):
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    proc = spawn_client(pipe, wanted=8, hold_ms=500)
    time.sleep(0.8)
    threading.Thread(target=started.run_forever, kwargs={"max_ticks": 4}, daemon=True).start()
    out, err = proc.communicate(timeout=30)

    seen = json.loads(out.strip().splitlines()[-1])
    kinds = [f["kind"] for f in seen["frames"] if f["type"] == W.LEDGER_EVENT]
    assert "handoff.Plan" in kinds, kinds
    assert started.transport_error is None


def test_a_node_client_can_start_a_durable_job(service):
    started, ledger, pipe = service
    scenario = "Gate12 PRU DAC1 digital path를 보드 없이 빌드만 해줘"
    preview = started.control.preflight(scenario)
    selected = preview["selected_contract"]
    seen = run_client(
        pipe,
        wanted=4,
        command={
            "type": "job.start",
            "commandId": "start-real-node",
            "gate": selected["name"],
            "objective": "Verify the digital path",
            "scenario": scenario,
            "contractSha256": selected["sha256"],
            "scenarioSha256": preview["scenario_sha256"],
        },
    )

    answer = next(f for f in seen["frames"] if f["commandId"] == "start-real-node")
    assert answer["ok"] is True
    assert ledger.job(answer["jobId"]).objective == "Verify the digital path"
    kinds = [f["kind"] for f in seen["frames"]]
    assert "job.created" in kinds and "handoff.Brief" in kinds
    assert started.transport_error is None


def test_a_node_client_can_preflight_without_creating_a_job(service):
    started, ledger, pipe = service
    before = ledger.latest_event_id()
    seen = run_client(
        pipe,
        wanted=2,
        command={
            "type": "scenario.preflight",
            "commandId": "preview-real-node",
            "scenario": "Gate12 PRU DAC1 Verilog을 보드 없이 빌드만 해줘",
        },
    )

    answer = next(f for f in seen["frames"] if f["commandId"] == "preview-real-node")
    assert answer["ok"] is True
    assert answer["preflightStatus"] == "ready"
    assert answer["selectedContract"] == "gate12-pru-dac1-plan.yaml"
    assert answer["executionMode"] == "offline"
    assert ledger.latest_event_id() == before
    assert ledger.jobs() == []
    assert started.transport_error is None


def test_the_office_is_offered_a_result_and_its_artifacts(service):
    """`job.result` is the daemon's own verdict, not the projector's version."""
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")
    started.run_forever(max_ticks=6)

    seen = run_client(pipe, wanted=40)
    frames = seen["frames"]
    results = [f for f in frames if f["type"] == W.JOB_RESULT]
    artifacts = [f for f in frames if f["type"] == W.ARTIFACT_REGISTERED]
    assert results, [f["type"] for f in frames]
    assert artifacts, "the run behind the verdict should be openable"
    assert seen["ready"] is not None


def test_a_clean_shutdown_while_a_node_client_is_attached(service):
    started, ledger, pipe = service
    start(ledger, "g.yaml", "build it")

    proc = spawn_client(pipe, wanted=3, hold_ms=6000)
    time.sleep(1.2)
    started.close()
    assert started.stopping.is_set()

    out, _ = proc.communicate(timeout=25)
    seen = json.loads(out.strip().splitlines()[-1])
    assert seen["ready"] is not None, "the client had a working session before shutdown"
    assert not any(t.is_alive() for t in started._connections)
