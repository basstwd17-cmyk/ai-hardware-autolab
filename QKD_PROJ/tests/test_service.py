"""The control plane, the runtime event recorder, and the process that hosts them.

Most of these drive ``ControlPlane.handle()`` directly, because that is the same
code path the pipe carries and it is the part where a mistake is durable. The
transport gets its own small set: one over loopback TCP, which runs anywhere,
and one over a real Windows named pipe, which is the transport that actually
ships.

The service tests all start and stop a real ``Service``. That is deliberate --
the lifecycle is the thing being checked, and a mocked one would pass while the
lock, the accept loop or the shutdown path was broken.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.orchestrator import __main__ as CLI  # noqa: E402
from QKD_PROJ.orchestrator import control as C  # noqa: E402
from QKD_PROJ.orchestrator import daemon as D  # noqa: E402
from QKD_PROJ.orchestrator import jobs as J  # noqa: E402
from QKD_PROJ.orchestrator import runtime_events as RE  # noqa: E402
from QKD_PROJ.orchestrator import service as S  # noqa: E402
from QKD_PROJ.orchestrator.jobs import JobLedger  # noqa: E402
from QKD_PROJ.orchestrator.loop import start  # noqa: E402
from QKD_PROJ.tests.test_daemon import working_runner  # noqa: E402
from QKD_PROJ.tests.test_loop import build_step, make_run, plan_with  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    ledger = JobLedger(tmp_path / "jobs.db")
    return ledger, runs


@pytest.fixture()
def plane(env):
    ledger, runs = env
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    return C.ControlPlane(ledger, daemon), ledger, daemon


# -- dispatch ----------------------------------------------------------------


def test_a_request_that_is_not_an_object_is_refused(plane):
    control, _, _ = plane
    assert control.handle("status")["error"] == "a request must be a JSON object"


def test_an_unknown_command_lists_the_known_ones(plane):
    control, _, _ = plane
    answer = control.handle({"command": "explode"})
    assert answer["ok"] is False
    for expected in ("approve", "cancel", "steer", "status", "events"):
        assert expected in answer["error"]


def test_a_handler_that_raises_still_answers(plane):
    """A control plane that dies takes the daemon's only interface with it."""
    control, _, _ = plane
    assert control.handle({"command": "job", "job_id": "job_nothing"})["ok"] is False


def test_ping_carries_the_cursor(plane):
    control, ledger, _ = plane
    answer = control.handle({"command": "ping"})
    assert answer["pong"] is True and answer["cursor"] == ledger.latest_event_id()


def test_start_opens_a_job_with_the_human_brief_in_the_ledger(plane):
    control, ledger, _ = plane
    scenario = "Gate12 PRU DAC1 digital path를 보드 없이 빌드만 해줘"
    preview = control.preflight(scenario)
    selected = preview["selected_contract"]
    answer = control.handle(
        {
            "command": "start",
            "gate": selected["name"],
            "objective": "Verify the safe digital path",
            "scenario": scenario,
            "contract_sha256": selected["sha256"],
            "scenario_sha256": preview["scenario_sha256"],
        }
    )

    assert answer["ok"] is True
    job_id = answer["job"]["job_id"]
    assert ledger.job(job_id).state == J.PENDING
    brief = ledger.handoffs(job_id, "Brief", 0)[0]
    assert brief.payload["scenario"].startswith("Gate12")
    assert brief.payload["resources"]["selected_contract"]["name"] == selected["name"]
    assert brief.agent == "human"


def test_start_job_cli_preflights_and_forwards_the_bound_digests(monkeypatch):
    calls = []

    def send(request, address):
        calls.append((request, address))
        if request["command"] == "preflight":
            return {
                "ok": True,
                "preflight": {
                    "status": "ready",
                    "scenario_sha256": "b" * 64,
                    "selected_contract": {
                        "name": "g.yaml",
                        "sha256": "a" * 64,
                    },
                },
            }
        return {"ok": True, "job": {"job_id": "job-1"}}

    monkeypatch.setattr(CLI.S, "send", send)

    assert CLI.main(["--pipe", "test-pipe", "start-job", "g.yaml", "Do it"]) == 0
    assert [request["command"] for request, _ in calls] == ["preflight", "start"]
    assert calls[1][0]["contract_sha256"] == "a" * 64
    assert calls[1][0]["scenario_sha256"] == "b" * 64


def test_start_job_cli_refuses_a_gate_other_than_the_preflight_selection(monkeypatch):
    calls = []

    def send(request, address):
        calls.append((request, address))
        return {
            "ok": True,
            "preflight": {
                "status": "ready",
                "scenario_sha256": "b" * 64,
                "selected_contract": {
                    "name": "selected.yaml",
                    "sha256": "a" * 64,
                },
            },
        }

    monkeypatch.setattr(CLI.S, "send", send)

    assert CLI.main(["start-job", "other.yaml", "Do it"]) == 1
    assert len(calls) == 1


def test_start_refuses_an_empty_scenario_boundary(plane):
    control, ledger, _ = plane
    before = ledger.latest_event_id()
    answer = control.handle(
        {"command": "start", "gate": "g.yaml", "objective": "Do it", "scenario": ""}
    )

    assert answer["ok"] is False
    assert "scenario" in answer["error"]
    assert ledger.latest_event_id() == before


def test_events_replays_from_a_cursor(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    ledger.append_event(job.job_id, "runtime.commandExecution", {"command": "x"})

    everything = control.handle({"command": "events", "since": 0})
    midpoint = everything["events"][0]["event_id"]
    rest = control.handle({"command": "events", "since": midpoint})
    assert [e["event_id"] for e in rest["events"]] == [
        e["event_id"] for e in everything["events"][1:]
    ]
    assert rest["latest"] == ledger.latest_event_id()


# -- approve -----------------------------------------------------------------


def test_approve_records_a_decision(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")

    answer = control.handle(
        {
            "command": "approve",
            "approval_id": approval.approval_id,
            "granted": True,
            "decided_by": "quantum1",
            "note": "at the bench",
        }
    )
    assert answer["approval"]["state"] == J.APPROVAL_GRANTED
    assert ledger.pending_approvals(job.job_id) == []


def test_approve_does_not_authorise_by_itself(env):
    """The gate still re-hashes. A grant is a decision, not a key."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    control = C.ControlPlane(ledger)
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")
    control.handle(
        {
            "command": "approve",
            "approval_id": approval.approval_id,
            "granted": True,
            "decided_by": "quantum1",
        }
    )

    from QKD_PROJ.agents.schema import load_all

    gate = D.HardwareApprovalGate(ledger, contracts_dir=runs.parent, project_root=runs.parent)
    operator = next(a for a in load_all() if a.name == "board-operator")
    task = {
        "job_id": job.job_id,
        "gate": "g.yaml",
        "step": {
            "id": "s1",
            "contract": "g.yaml",
            "tool": "program_cpld_sram",
        },
    }
    assert gate(operator, task, "program_cpld_sram", {}) is False
    assert "names no artifact digests" in gate.refusals[-1]


def test_a_decision_needs_a_decider(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    answer = control.handle(
        {
            "command": "approve",
            "approval_id": approval.approval_id,
            "granted": True,
            "decided_by": "  ",
        }
    )
    assert "non-empty" in answer["error"]


def test_deciding_twice_is_refused(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    request = {
        "command": "approve",
        "approval_id": approval.approval_id,
        "granted": True,
        "decided_by": "quantum1",
    }
    assert control.handle(request)["ok"] is True
    assert "already granted" in control.handle(request)["error"]


# -- cancel ------------------------------------------------------------------


def test_cancel_closes_the_job_and_withdraws_its_requests(plane):
    """A grant left on a cancelled job is a signature with nothing attached."""
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "program it")
    ledger.transition(job.job_id, J.RUNNING)
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")

    answer = control.handle(
        {"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1", "reason": "board apart"}
    )
    assert answer["job"]["outcome"] == "cancelled"
    assert answer["withdrawn"] == [approval.approval_id]
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_WITHDRAWN
    assert ledger.job(job.job_id).state == J.DONE


def test_cancel_is_recorded_before_the_job_closes(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    control.handle({"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"})

    kinds = [e.kind for e in ledger.events_since(0, job.job_id)]
    assert kinds.index(C.CANCEL) < kinds.index("job.done")
    reason = ledger.events_since(0, job.job_id)[-1].payload["reason"]
    assert "cancelled by quantum1" in reason


def test_a_finished_job_is_not_cancelled_again(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    control.handle({"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"})
    answer = control.handle({"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"})
    assert "already finished as cancelled" in answer["error"]


def test_a_cancelled_job_is_not_scheduled(plane):
    control, ledger, daemon = plane
    job = start(ledger, "g.yaml", "build it")
    control.handle({"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"})
    assert daemon.tick().advanced is None
    assert daemon.waiting_for(job.job_id).status == D.FINISHED


# -- steer -------------------------------------------------------------------


def test_a_steer_is_pending_until_a_handoff_follows(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    ledger.transition(job.job_id, J.RUNNING)

    answer = control.handle(
        {"command": "steer", "job_id": job.job_id, "message": "slow the clock", "sent_by": "quantum1"}
    )
    assert [(s["message"], s["by"]) for s in answer["pending"]] == [("slow the clock", "quantum1")]
    assert answer["will_be_delivered"] is True

    ledger.record_handoff(job.job_id, "Plan", "gate-architect", {"kind": "Plan"})
    assert C.pending_steers(ledger, job.job_id) == []


def test_the_loop_hands_a_steer_to_the_next_agent(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    daemon = D.build(ledger, runner, runs_dir=runs)
    C.ControlPlane(ledger, daemon).handle(
        {"command": "steer", "job_id": job.job_id, "message": "try P9_21", "sent_by": "quantum1"}
    )
    daemon.tick()

    task = next(task for name, task in runner.calls if name == "gate-architect")
    assert [(s["message"], s["by"]) for s in task["steering"]] == [("try P9_21", "quantum1")]


def test_an_empty_steer_is_refused(plane):
    """Refused twice over: by the dispatcher's required-field check, and by steer() itself.

    The second matters because the control plane is also a Python API, and a
    caller that skips handle() must not be able to log a message with nothing
    in it and have the loop hand it to an agent.
    """
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    answer = control.handle(
        {"command": "steer", "job_id": job.job_id, "message": "   ", "sent_by": "quantum1"}
    )
    assert answer["ok"] is False and "non-empty" in answer["error"]

    with pytest.raises(C.ControlError, match="steers nothing"):
        control.steer(job.job_id, "   ", "quantum1")
    assert C.pending_steers(ledger, job.job_id) == []


def test_a_finished_job_cannot_be_steered(plane):
    control, ledger, _ = plane
    job = start(ledger, "g.yaml", "build it")
    control.handle({"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"})
    answer = control.handle(
        {"command": "steer", "job_id": job.job_id, "message": "hello", "sent_by": "quantum1"}
    )
    assert "nobody to steer" in answer["error"]


# -- runtime events ----------------------------------------------------------


def test_runtime_notifications_keep_the_runtime_name(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger, buffer_size=10)

    with recorder.recording(job.job_id, "design-builder"):
        recorder.sink({"method": "commandExecution", "params": {"command": "diamond"}})
        recorder.sink({"method": "fileChange", "params": {"path": "top.v"}})

    kinds = [e.kind for e in ledger.events_since(0, job.job_id)]
    assert "runtime.commandExecution" in kinds and "runtime.fileChange" in kinds


def test_a_runtime_kind_can_never_collide_with_the_ledgers_own(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger, buffer_size=1)
    with recorder.recording(job.job_id):
        recorder.sink({"method": "job.done"})
        recorder.sink({"method": "../../handoff.Plan"})

    for event in ledger.events_since(0, job.job_id):
        if event.kind != "job.created" and event.kind != "handoff.Brief":
            assert event.kind.startswith(RE.NAMESPACE)


def test_a_notification_with_no_job_is_counted_not_misfiled(env):
    """Filing it against whichever job ran last would be worse than losing it."""
    ledger, _ = env
    recorder = RE.RuntimeEventRecorder(ledger)
    recorder.sink({"method": "commandExecution"})
    assert recorder.dropped == 1
    assert recorder.flush() == 0


def test_a_malformed_notification_never_reaches_the_runtime(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger)
    with recorder.recording(job.job_id):
        recorder.sink("not a dict")
        recorder.sink({"no": "method"})
    assert recorder.dropped == 2


def test_a_huge_payload_is_clipped(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger, buffer_size=1)
    with recorder.recording(job.job_id):
        recorder.sink({"method": "fileChange", "params": {"diff": "x" * 40_000}})

    event = ledger.events_since(0, job.job_id)[-1]
    assert event.payload["params"]["truncated"] is True
    assert event.payload["params"]["bytes"] > RE.MAX_PAYLOAD_BYTES


def test_a_method_filter_keeps_the_log_small(env):
    ledger, _ = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger, buffer_size=1, methods=["mcpToolCall"])
    with recorder.recording(job.job_id):
        recorder.sink({"method": "commandExecution"})
        recorder.sink({"method": "mcpToolCall"})

    assert [e.kind for e in ledger.events_since(0, job.job_id)][-1] == "runtime.mcpToolCall"
    assert len(RE.runtime_events(ledger, job.job_id)) == 1


def test_wrapping_a_runner_attributes_events_to_its_job(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    recorder = RE.RuntimeEventRecorder(ledger, buffer_size=10)
    inner = working_runner()

    def narrating(agent, task):
        recorder.sink({"method": "mcpToolCall", "params": {"tool": "build_cpld"}})
        return inner(agent, task)

    daemon = D.build(ledger, recorder.wrap(narrating), runs_dir=runs)
    daemon.tick()

    recorded = RE.runtime_events(ledger, job.job_id)
    assert recorded and recorded[-1]["agent"] == "gate-architect"


# -- the service -------------------------------------------------------------


def test_only_one_instance_may_hold_a_ledger(tmp_path):
    first = S.InstanceLock(tmp_path / "jobs.lock")
    first.acquire()
    try:
        with pytest.raises(S.ServiceError, match="another daemon already holds"):
            S.InstanceLock(tmp_path / "jobs.lock").acquire()
    finally:
        first.release()
    S.InstanceLock(tmp_path / "jobs.lock").acquire()


def test_the_lock_is_released_when_the_holder_lets_go(tmp_path):
    lock = S.InstanceLock(tmp_path / "jobs.lock")
    with lock:
        pass
    second = S.InstanceLock(tmp_path / "jobs.lock")
    second.acquire()
    second.release()


def _service(env, **kwargs):
    ledger, runs = env
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    config = S.ServiceConfig(interval_s=0.05, install_signal_handlers=False, **kwargs)
    return S.Service(ledger, daemon, config=config, transport=S.SocketTransport()), ledger


def test_the_service_starts_recovers_and_stops(env):
    service, ledger = _service(env)
    info = service.start()
    try:
        assert info["address"].startswith("tcp://")
        assert info["cursor"] == ledger.latest_event_id()
        assert isinstance(info["recovered"], list)
    finally:
        service.close()


def test_a_tick_moves_one_step_and_the_service_counts_it(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        assert service.tick_once().advanced.action == "plan"
        assert service.ticks == 1
        assert ledger.job(job.job_id).state == J.RUNNING


def test_run_forever_stops_when_asked(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    with service:
        thread = threading.Thread(target=service.run_forever, daemon=True)
        thread.start()
        time.sleep(0.4)
        service.stop()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_the_sleep_is_derived_from_when_something_is_due(env):
    service, ledger = _service(env)
    with service:
        idle = D.Tick(None, waiting=[])
        assert service.sleep_for(idle) == pytest.approx(0.05)

        due = D.Tick(
            None,
            waiting=[
                D.Waiting("j", J.BLOCKED, D.WAITING_RETRY, "x", retry_at="2026-01-01T00:02:00+00:00")
            ],
        )
        assert service.sleep_for(due, now="2026-01-01T00:00:00+00:00") == pytest.approx(30.0)
        assert service.sleep_for(due, now="2026-01-01T00:01:50+00:00") == pytest.approx(10.0)
        assert service.sleep_for(due, now="2026-01-01T00:03:00+00:00") == 0.0

        moved = D.Tick(D.Advance if False else None, waiting=[])
        assert service.sleep_for(moved) >= 0


def test_a_tick_that_advanced_does_not_sleep(env):
    from QKD_PROJ.orchestrator.loop import Advance

    service, _ = _service(env)
    with service:
        assert service.sleep_for(D.Tick(Advance("plan", "j", 1, "did a thing"))) == 0.0


# -- transports --------------------------------------------------------------


def test_a_request_round_trips_over_the_socket_transport(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        address = service.transport.address
        assert S.send({"command": "ping"}, address)["pong"] is True

        answer = S.send(
            {"command": "steer", "job_id": job.job_id, "message": "hi", "sent_by": "quantum1"},
            address,
        )
        assert answer["pending"][0]["message"] == "hi"
        assert S.send({"command": "not-a-command"}, address)["ok"] is False


def test_malformed_input_gets_an_answer_not_a_dead_connection(env):
    service, _ = _service(env)
    with service:
        import socket as sock

        host, _, port = service.transport.address[len("tcp://") :].rpartition(":")
        with sock.create_connection((host, int(port)), timeout=5) as connection:
            connection.sendall(b"{not json}\n")
            with connection.makefile("rb") as reader:
                answer = json.loads(reader.readline())
        assert answer["ok"] is False and "not JSON" in answer["error"]


@pytest.mark.skipif(not S.IS_WINDOWS, reason="named pipes are a Windows transport")
def test_a_request_round_trips_over_a_real_named_pipe(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    name = r"\\.\pipe\rtase-test-" + str(time.time_ns())
    service = S.Service(
        ledger,
        daemon,
        config=S.ServiceConfig(interval_s=0.05, pipe_name=name, install_signal_handlers=False),
    )
    with service:
        assert service.transport.address == name
        assert S.send({"command": "ping"}, name)["pong"] is True
        answer = S.send(
            {"command": "cancel", "job_id": job.job_id, "cancelled_by": "quantum1"}, name
        )
        assert answer["job"]["outcome"] == "cancelled"


def test_shutdown_over_the_wire_stops_the_service(env):
    service, _ = _service(env)
    with service:
        address = service.transport.address
        assert S.send({"command": "shutdown"}, address)["stopping"] is True
        assert service.stopping.is_set()


def test_a_control_plane_with_no_service_refuses_shutdown(env):
    ledger, _ = env
    assert C.ControlPlane(ledger).handle({"command": "shutdown"})["ok"] is False
