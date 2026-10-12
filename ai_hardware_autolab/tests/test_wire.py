"""The office UI protocol, end to end.

The frame-level tests drive ``WireSession`` directly with a list for a socket,
because that is where the cursor arithmetic lives and it is worth reading
without a transport in the way. The transport tests then run a real service --
over loopback everywhere, and over a real ACL'd named pipe on Windows --
because the two bugs this protocol has already had were both transport bugs
that every in-memory test passed straight through.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.orchestrator import control as C  # noqa: E402
from ai_hardware_autolab.orchestrator import daemon as D  # noqa: E402
from ai_hardware_autolab.orchestrator import jobs as J  # noqa: E402
from ai_hardware_autolab.orchestrator import loop as L  # noqa: E402
from ai_hardware_autolab.orchestrator import service as S  # noqa: E402
from ai_hardware_autolab.orchestrator import wire as W  # noqa: E402
from ai_hardware_autolab.orchestrator.jobs import JobLedger  # noqa: E402
from ai_hardware_autolab.orchestrator.loop import start  # noqa: E402
from ai_hardware_autolab.research.discovery_loop import start_mission  # noqa: E402
from ai_hardware_autolab.tests.test_daemon import working_runner  # noqa: E402
from ai_hardware_autolab.tests.test_loop import build_step, make_run, plan_with  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    make_run(runs, "20260101T000000Z_build", {"drc_errors": 0})
    return JobLedger(tmp_path / "jobs.db"), runs


@pytest.fixture()
def session(env):
    ledger, runs = env
    sent: list[dict] = []
    control = C.ControlPlane(ledger, D.build(ledger, working_runner(), runs_dir=runs))
    return W.WireSession(ledger, control, sent.append), ledger, sent


@pytest.fixture()
def conversation_session(tmp_path):
    from ai_hardware_autolab.tests.test_unified_conversation import make_control
    ledger, _, _, control = make_control(tmp_path)
    sent = []
    return W.WireSession(ledger, control, sent.append), ledger, sent


def kinds(frames):
    return [f["type"] for f in frames]


def test_completed_mission_feedback_opens_a_durable_revision(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    source = start_mission(
        ledger,
        "shape RF output",
        "implement the timing path",
        resources={"search_policy": "refresh", "knowledge_context": {"claims": []}},
    )
    ledger.transition(source.job_id, J.DONE, "contract_gap", reason="review ready")

    answer = C.ControlPlane(ledger).handle({
        "command": "job-mission-revise",
        "job_id": source.job_id,
        "requested_by": "quantum1",
        "message": "prioritize RF1 and use CH3 only for triggering",
    })

    assert answer["ok"] is True
    revised = answer["job"]
    assert revised["job_id"] != source.job_id
    brief = ledger.handoffs(revised["job_id"], "Brief", 0)[0].payload
    assert brief["resources"]["revision_of"] == source.job_id
    assert brief["resources"]["review_feedback"]["message"].startswith("prioritize RF1")
    assert C.REVISION_REQUESTED in [event.kind for event in ledger.events_since(0, source.job_id)]


# -- subscribe, replay, ready -------------------------------------------------


def test_subscribe_replays_then_marks_ready(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "build it")
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})

    events = [f for f in sent if f["type"] == W.LEDGER_EVENT]
    ready = sent[-1]
    assert ready["type"] == W.READY
    assert [e["event"]["event_id"] for e in events] == [1, 2]
    assert ready["cursor"] == events[-1]["event"]["event_id"]
    assert job.job_id == events[0]["event"]["job_id"]


def test_ready_cursor_always_equals_the_last_event_sent(session):
    """The contract calls a disagreement an error, so it must not be possible."""
    wire, ledger, sent = session
    start(ledger, "g.yaml", "a")
    start(ledger, "g.yaml", "b")
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})

    last = [f for f in sent if f["type"] == W.LEDGER_EVENT][-1]["event"]["event_id"]
    assert sent[-1]["cursor"] == last == ledger.latest_event_id()


def test_an_empty_ledger_is_ready_at_the_cursor_it_was_given(session):
    wire, _, sent = session
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})
    assert sent == [{"type": W.READY, "cursor": 0}]


def test_ready_names_the_runtime_and_workspace_boundaries(env):
    ledger, runs = env
    sent: list[dict] = []
    control = C.ControlPlane(ledger, D.build(ledger, working_runner(), runs_dir=runs))
    wire = W.WireSession(
        ledger,
        control,
        sent.append,
        runtime_profile="offline",
        workspace_mode="isolated",
    )

    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})

    assert sent == [
        {
            "type": W.READY,
            "cursor": 0,
            "profile": "offline",
            "workspaceMode": "isolated",
        }
    ]


def test_replay_starts_after_the_cursor_the_office_applied(session):
    wire, ledger, sent = session
    start(ledger, "g.yaml", "a")
    start(ledger, "g.yaml", "b")
    wire.handle({"type": "subscribe", "protocol": 1, "after": 2})

    ids = [f["event"]["event_id"] for f in sent if f["type"] == W.LEDGER_EVENT]
    assert ids and ids[0] == 3


def test_one_frame_per_global_cursor_with_no_gaps(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "a")
    start(ledger, "g.yaml", "b")
    ledger.append_event(job.job_id, "runtime.item/started", {"x": 1})
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})

    ids = [f["event"]["event_id"] for f in sent if f["type"] == W.LEDGER_EVENT]
    assert ids == list(range(1, ledger.latest_event_id() + 1))


def test_the_stream_is_global_and_unfiltered(session):
    """Filtering by job here would skip cursors and trip the office's gap check."""
    wire, ledger, sent = session
    first = start(ledger, "g.yaml", "a")
    second = start(ledger, "g.yaml", "b")
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})

    job_ids = {f["event"]["job_id"] for f in sent if f["type"] == W.LEDGER_EVENT}
    assert job_ids == {first.job_id, second.job_id}


def test_a_wrong_protocol_is_refused(session):
    wire, _, sent = session
    wire.handle({"type": "subscribe", "protocol": 2, "after": 0})
    assert sent[-1]["type"] == W.ERROR and "protocol" in sent[-1]["error"]
    assert not wire.subscribed


@pytest.mark.parametrize("after", [-1, "3", 1.5, True, None])
def test_a_bad_after_is_refused(session, after):
    wire, _, sent = session
    wire.handle({"type": "subscribe", "protocol": 1, "after": after})
    assert sent[-1]["type"] == W.ERROR and "after" in sent[-1]["error"]


def test_nothing_is_pushed_before_a_subscribe(session):
    wire, ledger, sent = session
    start(ledger, "g.yaml", "a")
    assert wire.push_new() == 0
    assert sent == []


def test_push_delivers_only_what_is_new(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "a")
    wire.handle({"type": "subscribe", "protocol": 1, "after": 0})
    before = len(sent)

    ledger.append_event(job.job_id, "runtime.item/completed", {"x": 1})
    assert wire.push_new() == 1
    assert wire.push_new() == 0
    assert sent[before]["event"]["kind"] == "runtime.item/completed"


# -- commands -----------------------------------------------------------------


def test_a_decision_travels_as_a_word_and_becomes_a_boolean(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")

    wire.handle(
        {
            "type": "approval.decide",
            "commandId": "c1",
            "approvalId": approval.approval_id,
            "decision": "accepted",
            "signedBy": "quantum1",
        }
    )
    assert sent[-1] == {
        "type": W.COMMAND_RESULT,
        "ok": True,
        "approval": ledger.approval(approval.approval_id).describe(),
        "commandId": "c1",
    }
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_GRANTED


def test_declined_records_a_denial(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")
    wire.handle(
        {
            "type": "approval.decide",
            "commandId": "c1",
            "approvalId": approval.approval_id,
            "decision": "declined",
            "signedBy": "quantum1",
        }
    )
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_DENIED


@pytest.mark.parametrize("decision", ["false", "true", True, False, "yes", None, "ACCEPTED"])
def test_only_the_two_words_are_accepted(session, decision):
    """`bool("false")` is True in Python. An approval is not decided by a coercion."""
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")
    wire.handle(
        {
            "type": "approval.decide",
            "commandId": "c1",
            "approvalId": approval.approval_id,
            "decision": decision,
            "signedBy": "quantum1",
        }
    )
    assert sent[-1]["ok"] is False
    assert "accepted" in sent[-1]["error"] and "declined" in sent[-1]["error"]
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_PENDING


def test_the_control_plane_refuses_a_string_boolean_directly(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program")
    answer = C.ControlPlane(ledger).handle(
        {
            "command": "approve",
            "approval_id": approval.approval_id,
            "granted": "false",
            "decided_by": "quantum1",
        }
    )
    assert answer["ok"] is False and "JSON boolean" in answer["error"]
    assert ledger.approval(approval.approval_id).state == J.APPROVAL_PENDING


def test_steer_and_cancel_travel_with_their_command_id(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "build it")

    wire.handle(
        {
            "type": "job.steer",
            "commandId": "s1",
            "jobId": job.job_id,
            "message": "keep analog out of scope",
            "sentBy": "quantum1",
        }
    )
    assert sent[-1]["commandId"] == "s1" and sent[-1]["ok"] is True
    assert sent[-1]["willBeDelivered"] is True

    wire.handle(
        {"type": "job.cancel", "commandId": "c9", "jobId": job.job_id, "cancelledBy": "quantum1"}
    )
    assert sent[-1]["commandId"] == "c9" and sent[-1]["job"]["outcome"] == "cancelled"


def test_mission_creates_a_durable_job_and_returns_its_id(conversation_session):
    wire, ledger, sent = conversation_session
    scenario = "Gate12 PRU DAC1 digital path를 보드 없이 빌드만 해줘"
    wire.handle(
        {
            "type": "scenario.preflight",
            "commandId": "preview-start-1",
            "scenario": scenario,
        }
    )
    preview = sent[-1]["preflight"]
    wire.handle(
        {
            "type": "job.mission",
            "commandId": "start-1",
            "searchPolicy": "refresh",
            "objective": "Verify the digital path",
            "scenario": scenario,
            "scenarioSha256": preview["scenario_sha256"],
        }
    )

    answer = sent[-1]
    assert answer["type"] == W.COMMAND_RESULT
    assert answer["commandId"] == "start-1" and answer["ok"] is True
    job_id = answer["job"]["job_id"]
    assert ledger.job(job_id).objective == "Verify the digital path"
    assert ledger.handoffs(job_id, "Brief", 0)[0].payload["inputs"]["scenario"].startswith("Gate12")
    assert not ledger.handoffs(job_id, "Plan")


def test_new_board_needs_no_contract_to_start_over_the_office_protocol(conversation_session):
    wire, ledger, sent = conversation_session
    scenario = "Without hardware, build and verify an STM32G474 HRTIM firmware simulation."
    wire.handle(
        {
            "type": "scenario.preflight",
            "commandId": "preview-discovery",
            "scenario": scenario,
        }
    )
    preview = sent[-1]["preflight"]
    assert preview["status"] == "ready"

    wire.handle(
        {
            "type": "job.mission",
            "commandId": "discover-1",
            "searchPolicy": "refresh",
            "objective": "design the new protocol",
            "scenario": scenario,
            "scenarioSha256": preview["scenario_sha256"],
        }
    )

    answer = sent[-1]
    assert answer["ok"] is True and answer["commandId"] == "discover-1"
    brief = ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    assert brief["workflow"] == "conversation-prototype-v1"
    assert not ledger.approvals(answer["job"]["job_id"])


def test_ready_scenario_can_start_a_refresh_mission_over_the_office_protocol(tmp_path):
    from ai_hardware_autolab.tests.test_unified_conversation import make_control
    ledger, flow, _, control = make_control(tmp_path)
    sent = []
    wire = W.WireSession(ledger, control, sent.append)
    scenario = "Without hardware, improve and build existing Verilog"
    wire.handle({"type": "scenario.preflight", "commandId": "preview-mission", "scenario": scenario})
    preview = sent[-1]["preflight"]
    assert preview["status"] == "ready"
    wire.handle({"type": "job.mission", "commandId": "mission-1", "objective": scenario,
                 "scenario": scenario, "scenarioSha256": preview["scenario_sha256"], "searchPolicy": "refresh"})
    answer = sent[-1]
    assert answer["ok"] is True and answer["commandId"] == "mission-1"
    assert answer["workflow"] == "conversation"
    brief = ledger.handoffs(answer["job"]["job_id"], "Brief", 0)[0].payload
    assert flow.owns(answer["job"]["job_id"])
    assert brief["inputs"]["search_policy"] == "refresh"


def test_job_start_without_preflight_digests_is_refused(session):
    wire, ledger, sent = session
    wire.handle(
        {
            "type": "job.start",
            "commandId": "unbound-start",
            "gate": "gate12-pru-dac1-plan.yaml",
            "objective": "Verify the digital path",
            "scenario": "Gate12 PRU DAC1 digital path를 보드 없이 빌드만 해줘",
        }
    )

    assert sent[-1]["ok"] is False
    assert "requires contract_sha256 and scenario_sha256" in sent[-1]["error"]
    assert ledger.jobs() == []


def test_preflight_is_read_only_and_returns_the_current_roles(conversation_session):
    wire, ledger, sent = conversation_session
    before = ledger.latest_event_id()
    wire.handle(
        {
            "type": "scenario.preflight",
            "commandId": "preview-1",
            "scenario": "Gate12 PRU DAC1 Verilog을 보드 없이 빌드만 해줘",
        }
    )

    answer = sent[-1]
    assert answer["commandId"] == "preview-1" and answer["ok"] is True
    assert answer["preflight"]["status"] == "ready"
    assert answer["preflight"]["roles"] == ["lead-agent", "builder", "verifier"]
    assert "selected_contract" not in answer["preflight"]
    assert answer["preflight"]["approval_required"] is False
    assert ledger.latest_event_id() == before


def test_a_preflighted_mission_rechecks_the_scenario_digest(conversation_session):
    wire, ledger, sent = conversation_session
    scenario = "Gate12 PRU DAC1 Verilog을 보드 없이 빌드만 해줘"
    wire.handle(
        {
            "type": "scenario.preflight",
            "commandId": "preview-2",
            "scenario": scenario,
        }
    )
    preview = sent[-1]["preflight"]
    frame = {
            "type": "job.mission",
            "commandId": "start-2",
            "searchPolicy": "refresh",
            "objective": "Build Verilog without a board",
            "scenario": scenario,
            "scenarioSha256": preview["scenario_sha256"],
        }
    wire.handle({**frame, "scenarioSha256": "0" * 64})
    assert sent[-1]["ok"] is False and ledger.jobs() == []
    wire.handle(frame)
    assert sent[-1]["ok"] is True
    assert ledger.handoffs(sent[-1]["job"]["job_id"], "Brief", 0)[0].payload["inputs"]["scenario_sha256"] == preview["scenario_sha256"]


def test_a_command_without_an_id_is_refused(session):
    wire, ledger, sent = session
    job = start(ledger, "g.yaml", "build it")
    wire.handle({"type": "job.cancel", "jobId": job.job_id, "cancelledBy": "quantum1"})
    assert sent[-1]["ok"] is False and "commandId" in sent[-1]["error"]
    assert ledger.job(job.job_id).state != J.DONE


def test_a_failure_answers_with_the_same_command_id(session):
    wire, _, sent = session
    wire.handle(
        {"type": "job.cancel", "commandId": "c1", "jobId": "job_nothing", "cancelledBy": "q"}
    )
    assert sent[-1] == {
        "type": W.COMMAND_RESULT,
        "ok": False,
        "error": "no such job: job_nothing",
        "commandId": "c1",
    }


def test_an_unknown_frame_type_is_refused(session):
    wire, _, sent = session
    wire.handle({"type": "job.explode", "commandId": "c1"})
    assert sent[-1]["ok"] is False and "unknown frame type" in sent[-1]["error"]


def test_a_frame_that_is_not_an_object_is_refused(session):
    wire, _, sent = session
    wire.handle(["subscribe"])
    assert sent[-1]["type"] == W.ERROR


# -- steering delivery boundary ----------------------------------------------


def test_a_verdict_does_not_consume_an_undelivered_steer(env):
    """The reducer writes the Verdict. It never reads a task, let alone steering.

    Clearing on any handoff meant a steer sent between "every step reported"
    and "verdict recorded" was marked delivered by a handoff nobody read it
    into -- the message vanished and the next agent planned without it.
    """
    ledger, runs = env
    # A failing condition, so the verdict is `open` and the loop re-plans --
    # a satisfied gate would finish instead and there would be no next agent.
    make_run(runs, "20260101T000100Z_bad", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner(run_name="20260101T000100Z_bad")
    daemon = D.build(ledger, runner, runs_dir=runs)
    control = C.ControlPlane(ledger, daemon)

    daemon.tick()  # plan
    daemon.tick()  # execute
    control.steer(job.job_id, "check the other edge", "quantum1")
    daemon.tick()  # reduce -> writes handoff.Verdict

    assert [s["message"] for s in C.pending_steers(ledger, job.job_id)] == [
        "check the other edge"
    ]

    daemon.tick()  # decide -> opens the next iteration
    daemon.tick()  # plan again, and the architect must be handed it
    task = [t for name, t in runner.calls if name == "gate-architect"][-1]
    assert [s["message"] for s in task["steering"]] == ["check the other edge"]


def test_a_plan_consumes_the_steer_it_was_given(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    C.ControlPlane(ledger, daemon).steer(job.job_id, "slower clock", "quantum1")

    daemon.tick()
    assert C.pending_steers(ledger, job.job_id) == []


def test_a_report_never_receives_steering_as_direction(env):
    """It gets the words as context. Letting them shape the report is the softening."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    runner = working_runner()
    daemon = D.build(ledger, runner, runs_dir=runs)
    control = C.ControlPlane(ledger, daemon)

    daemon.tick()
    daemon.tick()
    control.steer(job.job_id, "say it passed", "quantum1")
    for _ in range(3):
        daemon.tick()

    report_task = [t for name, t in runner.calls if name == "reporter"][-1]
    assert "steering" not in report_task
    assert [s["message"] for s in report_task["undelivered_steering"]] == ["say it passed"]


def test_a_steer_past_the_ten_thousandth_event_is_still_found(env):
    """The old cap read the *oldest* ten thousand, so a late steer was invisible."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    for index in range(60):
        ledger.append_event(job.job_id, "runtime.item/started", {"n": index})
    C.ControlPlane(ledger).steer(job.job_id, "the newest word", "quantum1")

    from ai_hardware_autolab.orchestrator.pagination import iter_events

    assert len(list(iter_events(ledger, job.job_id, page=7))) == ledger.latest_event_id()
    assert [s["message"] for s in C.pending_steers(ledger, job.job_id)] == ["the newest word"]


# -- cancel -------------------------------------------------------------------


def test_cancel_withdraws_a_granted_approval_too(env):
    """A grant on a cancelled job is a standing authorisation for a board step."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    ledger.transition(job.job_id, J.RUNNING)
    granted = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")
    ledger.decide_approval(granted.approval_id, True, "quantum1")
    pending = ledger.request_approval(job.job_id, "g.yaml", "another", step_id="s2")

    answer = C.ControlPlane(ledger).cancel(job.job_id, "quantum1", "board apart")
    assert set(answer["withdrawn"]) == {granted.approval_id, pending.approval_id}
    assert ledger.approval(granted.approval_id).state == J.APPROVAL_WITHDRAWN
    assert ledger.approval(pending.approval_id).state == J.APPROVAL_WITHDRAWN


def test_the_gate_refuses_a_withdrawn_grant(env, tmp_path):
    """The reason the withdrawal matters: the gate reads grants, not job states."""
    from ai_hardware_autolab.agents.schema import load_all

    ledger, runs = env
    job = start(ledger, "g.yaml", "program it")
    ledger.transition(job.job_id, J.RUNNING)
    approval = ledger.request_approval(job.job_id, "g.yaml", "one program", step_id="s1")
    ledger.decide_approval(approval.approval_id, True, "quantum1")

    gate = D.HardwareApprovalGate(ledger, contracts_dir=tmp_path, project_root=tmp_path)
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

    C.ControlPlane(ledger).cancel(job.job_id, "quantum1")
    assert gate(operator, task, "program_cpld_sram", {}) is False
    assert "no granted approval" in gate.refusals[-1]


# -- over a transport ---------------------------------------------------------


def _service(env, **kwargs):
    ledger, runs = env
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    config = S.ServiceConfig(
        interval_s=0.05, install_signal_handlers=False, poll_s=0.02, **kwargs
    )
    transport = None if kwargs.get("pipe_name") else S.SocketTransport()
    return S.Service(ledger, daemon, config=config, transport=transport), ledger


def _subscribe(client, after=0):
    client.send({"type": "subscribe", "protocol": 1, "after": after})
    return client.until("ready")


def test_subscribe_replay_and_ready_over_a_transport(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    with service:
        with S.Client(service.transport.address) as client:
            frames = _subscribe(client)
    events = [f for f in frames if f["type"] == W.LEDGER_EVENT]
    assert frames[-1]["cursor"] == events[-1]["event"]["event_id"]


def test_new_events_are_pushed_on_the_same_connection(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        with S.Client(service.transport.address) as client:
            _subscribe(client)
            threading.Thread(
                target=service.run_forever, kwargs={"max_ticks": 3}, daemon=True
            ).start()

            pushed = []
            deadline = time.time() + 10
            while time.time() < deadline and len(pushed) < 3:
                pushed.append(client.receive())
    assert all(f["type"] == W.LEDGER_EVENT for f in pushed)
    assert "handoff.Plan" in [f["event"]["kind"] for f in pushed]


def test_a_command_and_the_event_it_causes_both_arrive(env):
    service, ledger = _service(env)
    job = start(ledger, "g.yaml", "build it")
    with service:
        with S.Client(service.transport.address) as client:
            _subscribe(client)
            client.send(
                {
                    "type": "job.steer",
                    "commandId": "c1",
                    "jobId": job.job_id,
                    "message": "hold the analog",
                    "sentBy": "quantum1",
                }
            )
            seen = []
            deadline = time.time() + 10
            while time.time() < deadline and len(seen) < 2:
                seen.append(client.receive())

    result = [f for f in seen if f["type"] == W.COMMAND_RESULT]
    events = [f for f in seen if f["type"] == W.LEDGER_EVENT]
    assert result and result[0]["commandId"] == "c1" and result[0]["ok"] is True
    assert events and events[0]["event"]["kind"] == C.STEER


def test_reconnecting_after_a_cursor_resumes_exactly(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    with service:
        with S.Client(service.transport.address) as first:
            applied = _subscribe(first)[-1]["cursor"]
        ledger.append_event(ledger.jobs()[0].job_id, "runtime.item/completed", {"x": 1})
        with S.Client(service.transport.address) as second:
            frames = _subscribe(second, after=applied)

    ids = [f["event"]["event_id"] for f in frames if f["type"] == W.LEDGER_EVENT]
    assert ids == [applied + 1]
    assert frames[-1]["cursor"] == applied + 1


def test_the_one_shot_command_api_still_works_on_the_same_transport(env):
    service, _ = _service(env)
    with service:
        assert S.send({"command": "ping"}, service.transport.address)["pong"] is True


def test_a_clean_shutdown_with_a_subscriber_attached(env):
    service, ledger = _service(env)
    start(ledger, "g.yaml", "build it")
    service.start()
    try:
        with S.Client(service.transport.address) as client:
            _subscribe(client)
            service.close()
            assert service.stopping.is_set()
    finally:
        service.stop()
    assert not any(t.is_alive() for t in service._connections)


@pytest.mark.skipif(not S.IS_WINDOWS, reason="named pipes are a Windows transport")
def test_the_whole_protocol_over_a_real_named_pipe(env):
    """Both bugs this protocol has had were transport bugs. This is where they show."""
    name = r"\\.\pipe\qkd-wire-test-" + str(time.time_ns())
    service, ledger = _service(env, pipe_name=name)
    job = start(ledger, "g.yaml", "build it")
    with service:
        assert service.transport.sid.startswith("S-1-")
        with S.Client(name) as client:
            frames = _subscribe(client)
            applied = frames[-1]["cursor"]

            threading.Thread(
                target=service.run_forever, kwargs={"max_ticks": 3}, daemon=True
            ).start()
            pushed = []
            deadline = time.time() + 15
            while time.time() < deadline and len(pushed) < 2:
                pushed.append(client.receive())
            assert all(f["type"] == W.LEDGER_EVENT for f in pushed)

            client.send(
                {
                    "type": "job.cancel",
                    "commandId": "x1",
                    "jobId": job.job_id,
                    "cancelledBy": "quantum1",
                }
            )
            result = None
            while result is None and time.time() < deadline:
                frame = client.receive()
                if frame["type"] == W.COMMAND_RESULT:
                    result = frame
            assert result["ok"] is True and result["commandId"] == "x1"

        with S.Client(name) as again:
            resumed = _subscribe(again, after=applied)
        ids = [f["event"]["event_id"] for f in resumed if f["type"] == W.LEDGER_EVENT]
        assert ids and ids[0] == applied + 1


# -- willBeDelivered after a terminal verdict --------------------------------


def test_a_steer_after_a_terminal_verdict_is_not_promised_delivery(env):
    """The loop has decided; only the reporter runs, and it takes no direction."""
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    control = C.ControlPlane(ledger, daemon)

    for _ in range(3):  # plan, execute, reduce -> a satisfied Verdict
        daemon.tick()
    assert ledger.handoffs(job.job_id, "Verdict")[-1].payload["outcome"] == "satisfied"

    answer = control.steer(job.job_id, "one more thing", "quantum1")
    assert answer["will_be_delivered"] is False
    assert C.will_deliver_steering(ledger, job.job_id) is False


def test_a_steer_on_an_open_verdict_is_still_promised(env):
    ledger, runs = env
    make_run(runs, "20260101T000100Z_bad", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(run_name="20260101T000100Z_bad"), runs_dir=runs)
    control = C.ControlPlane(ledger, daemon)

    for _ in range(3):
        daemon.tick()
    assert ledger.handoffs(job.job_id, "Verdict")[-1].payload["outcome"] == "open"
    assert control.steer(job.job_id, "try the other edge", "quantum1")["will_be_delivered"] is True


def test_the_iteration_ceiling_also_ends_delivery(env):
    """An `open` verdict on the last allowed iteration still finishes the job."""
    ledger, runs = env
    make_run(runs, "20260101T000100Z_bad", {"drc_errors": 4})
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(
        ledger,
        working_runner(run_name="20260101T000100Z_bad"),
        runs_dir=runs,
        loop_policy=L.LoopPolicy(max_iterations=1),
    )
    for _ in range(3):
        daemon.tick()

    assert C.will_deliver_steering(ledger, job.job_id) is True
    assert C.will_deliver_steering(ledger, job.job_id, max_iterations=1) is False
    assert C.ControlPlane(ledger, daemon).steer(job.job_id, "x", "q")["will_be_delivered"] is False


def test_before_any_verdict_a_steer_is_promised(env):
    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    assert C.will_deliver_steering(ledger, job.job_id) is True


# -- results and artifacts ---------------------------------------------------


def test_the_result_counts_come_from_the_verdict_not_from_prose(env):
    from ai_hardware_autolab.orchestrator import results as R

    verdict = {
        "gate": "gate12.yaml",
        "outcome": "open",
        "conditions": [
            {"field": "a", "met": True, "status": "met", "evidence": ["r1"]},
            {"field": "b", "met": False, "status": "unmet", "evidence": ["r1"]},
            {"field": "c", "met": False, "status": "evidence_missing", "evidence": ["r2"]},
        ],
    }
    counts = R.condition_counts(verdict)
    assert counts == {"met": 1, "unmet": 1, "evidence_missing": 1, "ambiguous": 0, "total": 3}

    summary = R.summarise(verdict, counts)
    assert "gate12.yaml: open" in summary and "1/3 conditions met" in summary
    assert "open: b, c" in summary
    assert R.evidence_runs(verdict) == ["r1", "r2"]


def test_an_artifact_outside_the_project_root_is_not_offered(tmp_path):
    from ai_hardware_autolab.orchestrator import results as R

    root = tmp_path / "project"
    (root / "runs").mkdir(parents=True)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")

    found = R.collect_artifacts(
        {"conditions": []},
        {"document": "../elsewhere.md"},
        runs_dir=root / "runs",
        project_root=root,
    )
    assert found == []


def test_a_missing_artifact_is_not_offered(tmp_path):
    from ai_hardware_autolab.orchestrator import results as R

    root = tmp_path / "project"
    (root / "runs").mkdir(parents=True)
    found = R.collect_artifacts(
        {"conditions": [{"field": "a", "met": True, "evidence": ["never_written"]}]},
        {"document": "docs/absent.md"},
        runs_dir=root / "runs",
        project_root=root,
    )
    assert found == [], "a registration for a missing file is a button that fails"


def test_the_result_carries_the_verdict_verbatim_with_its_digest(env, tmp_path):
    from ai_hardware_autolab.orchestrator import results as R

    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    daemon.run()

    built = R.build_result(ledger, job.job_id, runs_dir=runs, project_root=runs.parent)
    assert built is not None
    payload, artifacts = built
    stored = ledger.handoffs(job.job_id, "Verdict")[-1]
    assert payload["verdict"] == stored.payload
    assert payload["verdictSha256"] == stored.sha256
    assert payload["outcome"] == "satisfied"
    assert any(a.kind == "run" for a in artifacts)


def test_no_result_before_a_verdict(env):
    from ai_hardware_autolab.orchestrator import results as R

    ledger, runs = env
    job = start(ledger, "g.yaml", "build it")
    assert R.build_result(ledger, job.job_id, runs_dir=runs, project_root=runs.parent) is None


def test_a_contract_gap_before_reduction_still_has_a_result_and_report_button(env):
    from ai_hardware_autolab.orchestrator import results as R

    ledger, runs = env
    project_root = runs.parent
    report_path = project_root / "docs" / "contract-gap.md"
    report_path.parent.mkdir()
    report_path.write_text("contract gap", encoding="utf-8")

    job = start(ledger, "g.yaml", "review only")
    ledger.transition(job.job_id, "running", reason="first plan")
    iteration = ledger.begin_iteration(job.job_id)
    plan = ledger.record_handoff(
        job.job_id,
        "Plan",
        "gate-architect",
        {
            "kind": "Plan",
            "agent": "gate-architect",
            "gate": "g.yaml",
            "iteration": iteration,
            "objective": "review only",
            "blocked_on": "contract_gap",
            "steps": [],
        },
        iteration=iteration,
    )
    ledger.record_handoff(
        job.job_id,
        "Report",
        "reporter",
        {
            "kind": "Report",
            "agent": "reporter",
            "gate": "g.yaml",
            "outcome": "contract_gap",
            "document": "docs/contract-gap.md",
        },
        iteration=iteration,
    )
    ledger.transition(job.job_id, "done", "contract_gap", reason="no reviewed operation")

    built = R.build_result(ledger, job.job_id, runs_dir=runs, project_root=project_root)
    assert built is not None
    payload, artifacts = built
    assert payload["outcome"] == "contract_gap"
    assert payload["counts"] == {
        "met": 0,
        "unmet": 0,
        "evidence_missing": 0,
        "ambiguous": 0,
        "total": 0,
    }
    assert payload["sourceKind"] == "Plan"
    assert payload["source"] == plan.payload
    assert payload["sourceSha256"] == plan.sha256
    assert "stopped before evidence reduction" in payload["summary"]
    assert [artifact.artifact_id for artifact in artifacts] == ["report"]


def test_a_reviewed_discovery_proposal_is_the_terminal_result_source(env):
    from ai_hardware_autolab.orchestrator import results as R
    from ai_hardware_autolab.research.discovery_loop import start_discovery

    ledger, runs = env
    job = start_discovery(ledger, "new protocol", "Offline design a new protocol.")
    ledger.transition(job.job_id, "running", reason="discovery started")
    draft = ledger.record_handoff(
        job.job_id,
        "ContractDraft",
        "contract-drafter",
        {
            "kind": "ContractDraft",
            "agent": "contract-drafter",
            "contract_name": "new-protocol",
        },
        iteration=0,
    )
    review = ledger.record_handoff(
        job.job_id,
        "ContractReview",
        "contract-reviewer",
        {
            "kind": "ContractReview",
            "agent": "contract-reviewer",
            "outcome": "ready_for_human_review",
        },
        iteration=0,
    )
    ledger.transition(job.job_id, "done", "contract_gap", reason="proposal ready")

    built = R.build_result(ledger, job.job_id, runs_dir=runs, project_root=runs.parent)
    assert built is not None
    payload, artifacts = built
    assert payload["title"] == "Contract proposal"
    assert payload["summary"] == "new-protocol: ready_for_human_review"
    assert payload["reasonCode"] == "ready_for_human_review"
    assert payload["contractDraft"] == draft.payload
    assert payload["contractDraftSha256"] == draft.sha256
    assert payload["sourceKind"] == "ContractReview"
    assert payload["sourceSha256"] == review.sha256
    assert artifacts == []


def test_job_done_triggers_a_pre_reduction_terminal_result(env):
    ledger, runs = env
    sent: list[dict] = []
    job = start(ledger, "g.yaml", "review only")
    ledger.transition(job.job_id, "running", reason="first plan")
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(
        job.job_id,
        "Plan",
        "gate-architect",
        {
            "kind": "Plan",
            "agent": "gate-architect",
            "gate": "g.yaml",
            "iteration": 1,
            "objective": "review only",
            "blocked_on": "contract_gap",
            "steps": [],
        },
        iteration=1,
    )
    ledger.transition(job.job_id, "done", "contract_gap", reason="no reviewed operation")

    session = W.WireSession(
        ledger,
        C.ControlPlane(ledger),
        sent.append,
        runs_dir=runs,
        project_root=runs.parent,
    )
    session.handle({"type": "subscribe", "protocol": 1, "after": 0})

    result = next(frame for frame in sent if frame["type"] == W.JOB_RESULT)
    assert result["result"]["jobId"] == job.job_id
    assert result["result"]["outcome"] == "contract_gap"


def test_an_identical_registration_is_sent_once(env, tmp_path):
    ledger, runs = env
    sent: list[dict] = []
    job = start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    daemon.run()

    session = W.WireSession(
        ledger,
        C.ControlPlane(ledger, daemon),
        sent.append,
        runs_dir=runs,
        project_root=runs.parent,
    )
    session.handle({"type": "subscribe", "protocol": 1, "after": 0})
    first = [f for f in sent if f["type"] == W.ARTIFACT_REGISTERED]
    assert first

    sent.clear()
    session._send_result(job.job_id)
    assert [f for f in sent if f["type"] == W.ARTIFACT_REGISTERED] == []
    assert session.warnings == []


def test_repointing_an_artifact_id_is_refused(env):
    from ai_hardware_autolab.orchestrator.results import Artifact

    ledger, runs = env
    sent: list[dict] = []
    session = W.WireSession(ledger, C.ControlPlane(ledger), sent.append)
    session._register("job_1", Artifact("report", "document", "a.md", "root"))
    session._register("job_1", Artifact("report", "document", "b.md", "root"))

    assert len([f for f in sent if f["type"] == W.ARTIFACT_REGISTERED]) == 1
    assert session.warnings and "refusing to re-point" in session.warnings[0]


def test_results_are_sent_after_ready_not_inside_the_replay(env):
    """A result is not a ledger row; the cursor arithmetic must stay exact."""
    ledger, runs = env
    sent: list[dict] = []
    start(ledger, "g.yaml", "build it")
    daemon = D.build(ledger, working_runner(), runs_dir=runs)
    daemon.run()

    session = W.WireSession(
        ledger,
        C.ControlPlane(ledger, daemon),
        sent.append,
        runs_dir=runs,
        project_root=runs.parent,
    )
    session.handle({"type": "subscribe", "protocol": 1, "after": 0})

    types = [f["type"] for f in sent]
    ready_at = types.index(W.READY)
    assert set(types[:ready_at]) == {W.LEDGER_EVENT}
    assert W.JOB_RESULT in types[ready_at:]

    ids = [f["event"]["event_id"] for f in sent if f["type"] == W.LEDGER_EVENT]
    assert ids == list(range(1, ledger.latest_event_id() + 1))
    assert sent[ready_at]["cursor"] == ids[-1]
