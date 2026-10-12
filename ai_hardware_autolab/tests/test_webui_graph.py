"""The mind-map projector: determinism, provenance, and what it refuses to say.

Every test here hands the projection a ``FrozenFacts``, never the real
contracts directory. The point of the resolver boundary is that the graph is a
pure function of (frames, facts); a test that read the repository would be
asserting the repository.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.orchestrator import wire as W  # noqa: E402
from ai_hardware_autolab.webui.backend import graph as G  # noqa: E402
from ai_hardware_autolab.webui.backend.facts import (  # noqa: E402
    ContractFacts,
    FileFacts,
    FrozenFacts,
    RunFacts,
)

CONTRACT = ContractFacts(
    name="gate12-pru-dac1-plan.yaml",
    sha256="c" * 64,
    gate="gate12-pru-dac1-plan",
    status="digital_control_path_verified_scope_limited",
    operations=("diamond_build", "readback_or_program_status"),
)
OTHER = ContractFacts(
    name="gate11-gpio-spi-code-registers.yaml",
    sha256="d" * 64,
    gate="gate11-gpio-spi-code-registers",
    status="pass",
    operations=("diamond_build",),  # shared with CONTRACT, on purpose
)
RUN = RunFacts(
    run_id="20260824T070705Z_job_195482831ea2",
    contract=CONTRACT.name,
    gate=CONTRACT.gate,
    verdict="pass",
    bitstreams=("a" * 64,),
)


def facts() -> FrozenFacts:
    return FrozenFacts(
        contracts={CONTRACT.name: CONTRACT, OTHER.name: OTHER}, runs={RUN.run_id: RUN}
    )


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


def history(job: str = "job_1") -> list[dict]:
    """One job's life, as frames, in the order the daemon would send them."""
    return [
        event(1, job, "job.created", {"gate": CONTRACT.name, "objective": "prove the DAC path"}),
        event(2, job, "handoff.Brief", {"iteration": 0, "agent": "human", "sha256": "b" * 64}),
        event(3, job, "handoff.Plan", {"iteration": 1, "sha256": "e" * 64}, agent="gate-architect"),
        event(4, job, "approval.pending", {"approval_id": "ap_1", "scope": "sram"}),
        {
            "type": W.ARTIFACT_REGISTERED,
            "jobId": job,
            "artifactId": "report",
            "projectRoot": "C:/KIST",
            "targetPath": f"C:/KIST/ai_hardware_autolab/runs/{RUN.run_id}/report.md",
        },
    ]


def build(frames=None, facts_=None) -> G.GraphProjection:
    projection = G.GraphProjection(facts=facts_ or facts())
    for frame in frames if frames is not None else history():
        projection.apply_frame(frame)
    return projection


def ids(snapshot, kind=None) -> list[str]:
    return [n["id"] for n in snapshot["nodes"] if kind is None or n["kind"] == kind]


def edge(snapshot, kind: str) -> dict:
    found = [e for e in snapshot["edges"] if e["kind"] == kind]
    assert found, f"no {kind} edge in {[e['kind'] for e in snapshot['edges']]}"
    return found[0]


# -- determinism --------------------------------------------------------------


def test_the_same_replay_twice_is_the_same_graph():
    once = build().snapshot()
    twice = build().snapshot()
    assert once == twice


def test_applying_the_same_frames_again_adds_nothing():
    projection = build()
    first = projection.snapshot()
    for frame in history():
        projection.apply_frame(frame)
    assert projection.snapshot() == first, "edge ids are derived, so replay is a no-op"


def test_reset_then_replay_reproduces_the_first_graph():
    projection = build()
    first = projection.snapshot()

    projection.reset()
    assert projection.snapshot()["nodes"] == []
    assert projection.cursor == 0

    for frame in history():
        projection.apply_frame(frame)
    assert projection.snapshot() == first


def test_nodes_and_edges_come_back_in_a_stable_order():
    snapshot = build().snapshot()
    assert ids(snapshot) == sorted(ids(snapshot))
    assert [e["id"] for e in snapshot["edges"]] == sorted(e["id"] for e in snapshot["edges"])


def test_an_unknown_event_changes_nothing():
    projection = build()
    before = projection.snapshot()
    assert projection.apply_ledger_event(
        {"event_id": 99, "job_id": "job_1", "kind": "runtime.item/somethingNew", "payload": {}}
    ) is False
    after = projection.snapshot()
    assert after["nodes"] == before["nodes"] and after["edges"] == before["edges"]


def test_an_unknown_frame_type_changes_nothing():
    projection = build()
    before = projection.snapshot()
    assert projection.apply_frame({"type": "webui.hello"}) is False
    assert projection.snapshot()["nodes"] == before["nodes"]


# -- identity -----------------------------------------------------------------


def test_two_jobs_may_hold_the_same_artifact_role():
    """`report` is a role. Scoping the id by job is what keeps them apart."""
    frames = []
    for job in ("job_1", "job_2"):
        frames.append(
            {
                "type": W.ARTIFACT_REGISTERED,
                "jobId": job,
                "artifactId": "report",
                "targetPath": f"C:/KIST/ai_hardware_autolab/runs/{RUN.run_id}/{job}.md",
            }
        )
    snapshot = build(frames).snapshot()
    assert ids(snapshot, "artifact") == ["artifact:job_1:report", "artifact:job_2:report"]


def test_an_agent_is_scoped_to_its_job():
    frames = [
        event(1, "job_1", "handoff.Verdict", {}, agent="verifier"),
        event(2, "job_2", "handoff.Verdict", {}, agent="verifier"),
    ]
    snapshot = build(frames).snapshot()
    assert ids(snapshot, "agent") == ["agent:job_1:verifier", "agent:job_2:verifier"]


def test_a_shared_operation_is_one_group_node_not_a_pairwise_edge():
    """Ten contracts sharing an operation is ten edges here, not forty-five."""
    frames = [
        event(1, "job_1", "job.created", {"gate": CONTRACT.name, "objective": "a"}),
        event(2, "job_2", "job.created", {"gate": OTHER.name, "objective": "b"}),
    ]
    snapshot = build(frames).snapshot()

    assert ids(snapshot, "operation") == [
        "operation:diamond_build",
        "operation:readback_or_program_status",
    ]
    shared = [
        e for e in snapshot["edges"] if e["target"] == "operation:diamond_build"
    ]
    assert {e["source"] for e in shared} == {
        f"contract:{CONTRACT.name}",
        f"contract:{OTHER.name}",
    }
    # And nothing joins the two contracts directly. That is P2-B's decision.
    assert not [
        e
        for e in snapshot["edges"]
        if e["source"].startswith("contract:") and e["target"].startswith("contract:")
    ]


# -- provenance ---------------------------------------------------------------


def test_every_edge_names_the_field_it_came_from():
    snapshot = build().snapshot()
    assert snapshot["edges"], "the fixture produces edges"
    for e in snapshot["edges"]:
        assert e["evidence"]["field"], e
        assert e["evidence"]["value"], e


def test_an_edge_from_a_ledger_row_carries_its_cursor():
    snapshot = build().snapshot()
    assert edge(snapshot, G.JOB_CONTRACT)["evidence"] == {
        "field": "gate",
        "value": CONTRACT.name,
        "source_event_id": 1,
    }
    assert edge(snapshot, G.JOB_APPROVAL)["evidence"]["source_event_id"] == 4


def test_an_edge_from_a_file_claims_no_cursor():
    """A contract's own `test_id` was never in a ledger row, so it says so."""
    snapshot = build().snapshot()
    assert edge(snapshot, G.CONTRACT_GATE)["evidence"] == {
        "field": "test_id",
        "value": CONTRACT.gate,
        "source_event_id": None,
    }
    assert edge(snapshot, G.RUN_BITSTREAM)["evidence"]["source_event_id"] is None


def test_the_brief_is_emitted_and_no_scenario_is_invented():
    """The scenario text is inside the Brief; the stream carries only a digest.

    Emitting a `scenario` node keyed on the Brief envelope's digest would be
    labelling one thing with another thing's name.
    """
    snapshot = build().snapshot()
    assert ids(snapshot, "brief") == [f"brief:{'b' * 64}"]
    assert ids(snapshot, "scenario") == []
    brief = [n for n in snapshot["nodes"] if n["kind"] == "brief"][0]
    assert brief["unresolved"] is True


# -- resolution ---------------------------------------------------------------


def test_an_unresolved_reference_is_kept_not_dropped():
    projection = build(facts_=FrozenFacts())  # resolves nothing
    snapshot = projection.snapshot()
    contract = [n for n in snapshot["nodes"] if n["kind"] == "contract"][0]

    assert contract["id"] == f"contract:{CONTRACT.name}"
    assert contract["unresolved"] is True
    assert contract["metadata"] == {}
    # The ledger named it, so the edge stands even with nothing behind it.
    assert edge(snapshot, G.JOB_CONTRACT)["target"] == contract["id"]
    # And nothing the file would have supplied is guessed at.
    assert ids(snapshot, "gate") == []
    assert ids(snapshot, "operation") == []


def test_a_reference_gains_detail_when_the_facts_arrive():
    late = FrozenFacts()
    projection = G.GraphProjection(facts=late)
    for frame in history():
        projection.apply_frame(frame)
    assert projection.snapshot()["nodes"][0]["unresolved"] in (True, False)

    late.contracts[CONTRACT.name] = CONTRACT
    projection.apply_ledger_event(
        {"event_id": 9, "job_id": "job_9", "kind": "job.created",
         "payload": {"gate": CONTRACT.name, "objective": "later"}}
    )

    snapshot = projection.snapshot()
    contract = [n for n in snapshot["nodes"] if n["kind"] == "contract"][0]
    assert contract["unresolved"] is False
    assert contract["metadata"]["sha256"] == CONTRACT.sha256
    assert ids(snapshot, "gate") == [f"gate:{CONTRACT.gate}"]


def test_a_later_reference_does_not_knock_a_resolved_node_back():
    projection = build()
    resolved = [n for n in projection.snapshot()["nodes"] if n["kind"] == "run"][0]
    assert resolved["unresolved"] is False

    projection.apply_frame(
        {
            "type": W.ARTIFACT_REGISTERED,
            "jobId": "job_2",
            "artifactId": "verdict",
            "targetPath": f"C:/KIST/ai_hardware_autolab/runs/{RUN.run_id}/verdict.json",
        }
    )
    again = [n for n in projection.snapshot()["nodes"] if n["kind"] == "run"][0]
    assert again["unresolved"] is False and again["metadata"]["verdict"] == "pass"


# -- what never reaches the browser -------------------------------------------


def test_no_path_appears_anywhere_in_the_graph():
    import json

    snapshot = build().snapshot()
    rendered = json.dumps(snapshot)
    for forbidden in ("C:/KIST", "C:\\KIST", "projectRoot", "report.md"):
        assert forbidden not in rendered, forbidden
    # The run's *name* is the only thing kept from the path.
    assert f"run:{RUN.run_id}" in rendered

    # The sharper invariant: nothing the graph *carries* holds a separator.
    # `targetPath.run` appears, and should -- it is the name of the field the
    # run was read out of, which is exactly the provenance an edge owes a
    # reader. What must never appear is a value with a path in it.
    for e in snapshot["edges"]:
        assert "/" not in e["evidence"]["value"] and "\\" not in e["evidence"]["value"], e
    for n in snapshot["nodes"]:
        for value in n["metadata"].values():
            if isinstance(value, str):
                assert "/" not in value and "\\" not in value, n


def test_the_run_is_read_off_the_path_and_nothing_else_is():
    snapshot = build().snapshot()
    assert edge(snapshot, G.ARTIFACT_RUN)["evidence"] == {
        "field": "targetPath.run",
        "value": RUN.run_id,
        "source_event_id": None,
    }


@pytest.mark.parametrize(
    "path",
    [
        "",
        "C:/KIST/ai_hardware_autolab/contracts/thing.yaml",  # not under runs/
        "C:/KIST/ai_hardware_autolab/runs",  # no component after it
        "C:/KIST/ai_hardware_autolab/runs/../secret/x",  # refuses to climb out
    ],
)
def test_a_path_that_names_no_run_produces_no_run_node(path):
    frames = [
        {"type": W.ARTIFACT_REGISTERED, "jobId": "job_1", "artifactId": "a1", "targetPath": path}
    ]
    assert ids(build(frames).snapshot(), "run") == []


# -- state --------------------------------------------------------------------


def test_job_state_comes_from_status_and_not_from_events():
    projection = build()
    job = [n for n in projection.snapshot()["nodes"] if n["kind"] == "job"][0]
    assert job["status"] is None, "job.created and handoffs say nothing about state"

    projection.apply_ledger_event(
        {"event_id": 8, "job_id": "job_1", "kind": "job.done", "payload": {"outcome": "pass"}}
    )
    job = [n for n in projection.snapshot()["nodes"] if n["kind"] == "job"][0]
    assert job["status"] is None, "a job.done event is not a licence to decide it is done"

    projection.apply_status({"jobs": {"finished": [{"job_id": "job_1", "status": "finished"}]}})
    job = [n for n in projection.snapshot()["nodes"] if n["kind"] == "job"][0]
    assert job["status"] == "finished"


def test_status_accepts_both_shapes_of_jobs():
    for jobs in (
        {"runnable": [{"job_id": "job_1", "status": "runnable"}]},
        [{"job_id": "job_1", "state": "running", "gate": CONTRACT.name}],
    ):
        projection = G.GraphProjection(facts=facts())
        assert projection.apply_status({"jobs": jobs}) is True
        job = [n for n in projection.snapshot()["nodes"] if n["kind"] == "job"][0]
        assert job["status"] in ("runnable", "running")


def test_an_inbox_approval_and_its_event_are_the_same_edge():
    projection = build()
    before = projection.snapshot()
    changed = projection.apply_status(
        {"jobs": [], "inbox": [{"approval_id": "ap_1", "job_id": "job_1"}]}
    )
    assert changed is False, "the event already said this; the ids agree by construction"
    assert projection.snapshot()["edges"] == before["edges"]


def test_the_cursor_tracks_the_events_applied_and_nothing_else():
    projection = build()
    assert projection.cursor == 4, "the artifact frame carries no cursor"
    projection.apply_status({"jobs": []})
    assert projection.cursor == 4


# -- the file-backed resolver -------------------------------------------------


def test_file_facts_read_a_contract_and_a_run(tmp_path):
    contracts = tmp_path / "contracts"
    runs = tmp_path / "runs" / "20260101T000000Z_x"
    contracts.mkdir(parents=True)
    runs.mkdir(parents=True)
    (contracts / "g.yaml").write_text(
        "test_id: gate-x\nstatus: pass\nallowed_operations:\n  - b\n  - a\n", encoding="utf-8"
    )
    (runs / "manifest.json").write_text(
        '{"run": "20260101T000000Z_x", "verdict": "pass",'
        ' "contract": {"test_spec": "g.yaml", "test_id": "gate-x"},'
        ' "artifacts": [{"kind": "bitstream", "sha256": "' + "f" * 64 + '"},'
        ' {"kind": "build_log", "sha256": "' + "0" * 64 + '"}]}',
        encoding="utf-8",
    )
    resolver = FileFacts(contracts_dir=contracts, runs_dir=tmp_path / "runs")

    contract = resolver.contract("g.yaml")
    assert contract.gate == "gate-x"
    assert contract.operations == ("a", "b"), "sorted, so the graph is order-independent"
    assert len(contract.sha256) == 64

    run = resolver.run("20260101T000000Z_x")
    assert run.contract == "g.yaml" and run.verdict == "pass"
    assert run.bitstreams == ("f" * 64,), "only the bitstream artifact, not the build log"


@pytest.mark.parametrize("name", ["", "..", "../secret.yaml", "a/b.yaml", "a\\b.yaml"])
def test_file_facts_refuse_a_name_that_leaves_its_directory(tmp_path, name):
    resolver = FileFacts(contracts_dir=tmp_path, runs_dir=tmp_path)
    assert resolver.contract(name) is None
    assert resolver.run(name) is None


def test_file_facts_return_nothing_for_what_is_not_there(tmp_path):
    resolver = FileFacts(contracts_dir=tmp_path, runs_dir=tmp_path)
    assert resolver.contract("missing.yaml") is None
    assert resolver.run("missing_run") is None
