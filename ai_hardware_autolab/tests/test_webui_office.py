"""Who is in the office, and everything the projection refuses to conclude.

The graph answers "who ever appeared". The office answers "who is working
now". Conflating them leaves a finished job's agents at their desks forever,
which looks like work and is not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.orchestrator import wire as W  # noqa: E402
from ai_hardware_autolab.webui.backend import office as O  # noqa: E402


def event(event_id: int, job: str, kind: str, payload=None, agent=None) -> dict:
    return {
        "type": W.LEDGER_EVENT,
        "event": {
            "event_id": event_id,
            "job_id": job,
            "seq": event_id,
            "at": "2026-08-27T00:00:00+00:00",
            "kind": kind,
            "agent": agent,
            "payload": payload or {},
        },
    }


def runtime(event_id: int, job: str, item_type: str, agent: str, method="item/completed") -> dict:
    return event(
        event_id,
        job,
        f"runtime.{method}",
        {"method": method, "item_type": item_type, "params": {"item": {"type": item_type}}},
        agent,
    )


def open_status(*job_ids: str) -> dict:
    return {"jobs": {"runnable": [{"job_id": j, "status": "runnable"} for j in job_ids]}}


def history(job: str = "job_1") -> list[dict]:
    return [
        event(1, job, "job.created", {"gate": "g.yaml", "objective": "prove it"}),
        event(2, job, "handoff.Brief", {"sha256": "b" * 64}, agent="human"),
        event(3, job, "iteration.started", {"iteration": 1}),
        runtime(4, job, "agentMessage", "gate-architect"),
        event(5, job, "handoff.Plan", {"sha256": "p" * 64}, agent="gate-architect"),
        runtime(6, job, "commandExecution", "design-builder"),
        runtime(7, job, "fileChange", "design-builder"),
    ]


def build(frames=None, status=None) -> O.OfficeProjection:
    projection = O.OfficeProjection()
    for frame in frames if frames is not None else history():
        projection.apply_frame(frame)
    projection.apply_status(status if status is not None else open_status("job_1"))
    return projection


def agents(snapshot) -> list[str]:
    return [o["agent"] for o in snapshot["occupants"]]


def test_conversation_turns_and_feedback_use_existing_office_projection():
    projection = build([
        event(1, 'job_1', 'conversation.turn.started', agent='lead'),
        event(2, 'job_1', 'conversation.turn.completed', agent='lead'),
        event(3, 'job_1', 'conversation.feedback', {'message': 'integer'}, agent='local-operator'),
    ])
    snapshot = projection.snapshot()
    assert agents(snapshot) == ['lead']
    assert snapshot['occupants'][0]['activity'] == O.WAITING
    assert snapshot['occupants'][0]['turns'] == 1


# -- who gets a desk ----------------------------------------------------------


def test_only_agents_that_acted_are_on_the_floor():
    snapshot = build().snapshot()
    assert agents(snapshot) == ["design-builder", "gate-architect"]


def test_a_human_row_makes_no_character():
    """`job.created` has no agent and the Brief is handed off by `human`.

    Neither is somebody at a desk. An avatar there would invent a participant
    the ledger does not have.
    """
    frames = [
        event(1, "job_1", "job.created", {"objective": "x"}),
        event(2, "job_1", "handoff.Brief", {"sha256": "b" * 64}, agent="human"),
    ]
    assert build(frames).snapshot()["occupants"] == []


def test_a_platform_row_makes_no_character():
    frames = [event(1, "job_1", "job.blocked", {"reason": "waiting"}, agent="platform")]
    assert build(frames).snapshot()["occupants"] == []


def test_an_agent_is_scoped_to_its_job():
    frames = [
        runtime(1, "job_1", "agentMessage", "verifier"),
        runtime(2, "job_2", "agentMessage", "verifier"),
    ]
    snapshot = build(frames, open_status("job_1", "job_2")).snapshot()
    assert [o["id"] for o in snapshot["occupants"]] == ["job_1:verifier", "job_2:verifier"]


# -- who leaves ---------------------------------------------------------------


def test_a_finished_job_empties_its_desks():
    """The whole point: a closed job's agents do not stand there forever."""
    projection = build()
    assert projection.snapshot()["occupants"], "they were there while it was open"

    projection.apply_status({"jobs": {"finished": [{"job_id": "job_1", "status": "finished"}]}})
    assert projection.snapshot()["occupants"] == []
    assert projection.snapshot()["openJobs"] == []


def test_only_the_closed_job_empties():
    frames = [
        runtime(1, "job_1", "agentMessage", "verifier"),
        runtime(2, "job_2", "agentMessage", "design-builder"),
    ]
    projection = build(frames, open_status("job_1", "job_2"))
    projection.apply_status(
        {
            "jobs": {
                "runnable": [{"job_id": "job_2", "status": "runnable"}],
                "finished": [{"job_id": "job_1", "status": "finished"}],
            }
        }
    )
    assert [o["jobId"] for o in projection.snapshot()["occupants"]] == ["job_2"]


def test_a_desk_is_emptied_by_status_and_never_by_an_event():
    """`job.done` is a row. Whether the job is done is the ledger's answer."""
    projection = build()
    projection.apply_ledger_event(
        {"event_id": 9, "job_id": "job_1", "kind": "job.done", "payload": {"outcome": "pass"},
         "agent": None}
    )
    assert agents(projection.snapshot()) == ["design-builder", "gate-architect"]


def test_both_shapes_of_status_are_understood():
    for status in (
        {"jobs": {"waiting_approval": [{"job_id": "job_1", "status": "waiting_approval"}]}},
        {"jobs": [{"job_id": "job_1", "state": "running", "gate": "g.yaml"}]},
    ):
        assert build(status=status).snapshot()["openJobs"] == ["job_1"]


# -- what a runtime row may and may not do ------------------------------------


@pytest.mark.parametrize(
    "item_type, activity",
    [
        ("agentMessage", O.TYPING),
        ("fileChange", O.TYPING),
        ("commandExecution", O.RUNNING),
        ("reasoning", O.READING),
        ("userMessage", O.READING),
        ("subAgentActivity", O.DELEGATING),
        ("collabAgentToolCall", O.DELEGATING),
    ],
)
def test_a_runtime_row_only_sets_the_animation(item_type, activity):
    projection = build([runtime(1, "job_1", item_type, "verifier")])
    occupant = projection.snapshot()["occupants"][0]
    assert occupant["activity"] == activity
    # It moved a character and nothing else: no verdict, no approval, no state.
    assert set(occupant) == {"id", "agent", "jobId", "activity", "detail", "seenAt", "turns"}


def test_an_unknown_runtime_item_still_moves_the_character():
    projection = build([runtime(1, "job_1", "somethingNew", "verifier")])
    occupant = projection.snapshot()["occupants"][0]
    assert occupant["activity"] == O.RUNNING
    assert occupant["detail"] == "somethingNew"


def test_an_approval_request_is_an_animation_not_a_decision():
    projection = build(
        [runtime(1, "job_1", "", "board-operator", method="item/commandExecution/requestApproval")]
    )
    occupant = projection.snapshot()["occupants"][0]
    assert occupant["activity"] == O.WAITING
    assert occupant["detail"] == "asked for approval"
    # Nothing here says the approval was granted; that is a ledger row this
    # projection deliberately does not read.
    assert projection.snapshot()["openJobs"] == ["job_1"]


def test_a_handoff_counts_a_turn_without_claiming_it_succeeded():
    projection = build()
    architect = [o for o in projection.snapshot()["occupants"] if o["agent"] == "gate-architect"][0]
    assert architect["turns"] == 1
    assert architect["activity"] == O.WAITING
    assert architect["detail"] == "handed off a Plan"


# -- determinism and replay ---------------------------------------------------


def test_the_same_history_gives_the_same_room():
    assert build().snapshot() == build().snapshot()


def test_reapplying_the_same_frames_changes_nothing():
    projection = build()
    first = projection.snapshot()
    for frame in history():
        projection.apply_frame(frame)
    assert projection.snapshot() == first


def test_a_full_replay_and_a_mid_cursor_reconnect_agree():
    """The invariant the whole stream design rests on.

    A browser that replays from cursor 0 and one that reconnects part-way and
    is sent the rest must end up in the same room. Anything that made them
    differ -- a clock, an arrival-order dependency -- would mean the office
    showed a different thing depending on when you opened the tab.
    """
    frames = history()
    status = open_status("job_1")

    whole = O.OfficeProjection()
    for frame in frames:
        whole.apply_frame(frame)
    whole.apply_status(status)

    # The reconnecting one: the first half applied, then a resubscribe that
    # replays what it already had before delivering the rest.
    resumed = O.OfficeProjection()
    for frame in frames[:4]:
        resumed.apply_frame(frame)
    resumed.apply_status(status)
    for frame in frames[2:]:  # overlap, as a replay after `after=` would send
        resumed.apply_frame(frame)
    resumed.apply_status(status)

    assert resumed.snapshot() == whole.snapshot()


def test_reset_then_replay_reproduces_the_room():
    projection = build()
    first = projection.snapshot()

    projection.reset()
    assert projection.snapshot()["occupants"] == []
    assert projection.cursor == 0

    for frame in history():
        projection.apply_frame(frame)
    projection.apply_status(open_status("job_1"))
    assert projection.snapshot() == first


def test_nothing_in_the_snapshot_is_a_wall_clock():
    """`seenAt` is an event_id. A clock would make two replays disagree."""
    snapshot = build().snapshot()
    for occupant in snapshot["occupants"]:
        assert isinstance(occupant["seenAt"], int)
        assert occupant["seenAt"] <= snapshot["cursor"]


def test_occupants_come_back_in_a_stable_order():
    snapshot = build().snapshot()
    assert [o["id"] for o in snapshot["occupants"]] == sorted(o["id"] for o in snapshot["occupants"])


# -- the discovery agents -----------------------------------------------------

DISCOVERY = ("architecture-selector", "contract-drafter", "contract-reviewer")


def test_the_discovery_agents_follow_the_same_rules_as_everybody():
    """No special case exists for them, and that is the point being tested.

    `architecture-selector`, `contract-drafter` and `contract-reviewer` are
    agents like any other: they get a desk because a row carries their name and
    their job is open, and they lose it when it closes.
    """
    frames = [
        event(1, "job_1", "job.created", {"gate": "-", "objective": "find out"}),
        event(2, "job_1", "handoff.Brief", {"sha256": "b" * 64}, agent="human"),
        runtime(3, "job_1", "agentMessage", "architecture-selector"),
        event(4, "job_1", "handoff.ArchitectureDecision", {"sha256": "a" * 64},
              agent="architecture-selector"),
        runtime(5, "job_1", "fileChange", "contract-drafter"),
        event(6, "job_1", "handoff.ContractDraft", {"sha256": "d" * 64}, agent="contract-drafter"),
        runtime(7, "job_1", "reasoning", "contract-reviewer"),
        event(8, "job_1", "handoff.ContractReview", {"sha256": "r" * 64},
              agent="contract-reviewer"),
    ]
    projection = build(frames)
    snapshot = projection.snapshot()

    assert agents(snapshot) == sorted(DISCOVERY)
    assert all(o["agent"] != "human" for o in snapshot["occupants"])
    by_agent = {o["agent"]: o for o in snapshot["occupants"]}
    for name in DISCOVERY:
        # Each closed a turn with its own handoff, and each is waiting after it.
        assert by_agent[name]["turns"] == 1
        assert by_agent[name]["activity"] == O.WAITING
    assert by_agent["contract-drafter"]["detail"] == "handed off a ContractDraft"
    assert by_agent["contract-reviewer"]["detail"] == "handed off a ContractReview"
    assert by_agent["architecture-selector"]["detail"] == "handed off a ArchitectureDecision"


def test_the_discovery_agents_leave_when_the_job_closes():
    frames = [
        runtime(1, "job_1", "agentMessage", "architecture-selector"),
        runtime(2, "job_1", "fileChange", "contract-drafter"),
        runtime(3, "job_1", "reasoning", "contract-reviewer"),
    ]
    projection = build(frames)
    assert len(projection.snapshot()["occupants"]) == 3

    projection.apply_status({"jobs": {"finished": [{"job_id": "job_1", "status": "finished"}]}})
    assert projection.snapshot()["occupants"] == []


def test_a_discovery_run_replays_to_the_same_room():
    frames = [
        runtime(1, "job_1", "agentMessage", "architecture-selector"),
        event(2, "job_1", "handoff.ArchitectureDecision", {"sha256": "a" * 64},
              agent="architecture-selector"),
        runtime(3, "job_1", "fileChange", "contract-drafter"),
        event(4, "job_1", "handoff.ContractDraft", {"sha256": "d" * 64}, agent="contract-drafter"),
        runtime(5, "job_1", "reasoning", "contract-reviewer"),
    ]
    status = open_status("job_1")

    whole = O.OfficeProjection()
    for frame in frames:
        whole.apply_frame(frame)
    whole.apply_status(status)

    resumed = O.OfficeProjection()
    for frame in frames[:3]:
        resumed.apply_frame(frame)
    resumed.apply_status(status)
    for frame in frames[1:]:  # the overlap a resubscribe replays
        resumed.apply_frame(frame)
    resumed.apply_status(status)

    assert resumed.snapshot() == whole.snapshot()
