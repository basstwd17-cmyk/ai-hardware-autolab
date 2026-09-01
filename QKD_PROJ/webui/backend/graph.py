"""The mind-map, derived. Nothing here is authored and nothing is guessed.

The ask was Gravity Connect-shaped: build something, and watch it join a map
wherever it shares information with what is already there. The temptation in
that shape is similarity -- same words, same purpose, probably related. This
graph refuses that. **Every edge comes from an identifier one record holds and
another record holds too**, and carries the field name it was read out of, so
the graph can be read rather than trusted. That is the same rule this project
already applies to thresholds.

## What is a pure function of what

``GraphProjection`` is a projection over two inputs and nothing else:

* the frames the daemon sent, in order, and
* a ``Facts`` resolver, for details the stream references but does not carry.

Same frames plus same facts gives the same graph, node for node and edge for
edge, which is what makes "rebuild from cursor 0" a real recovery rather than a
hope. It holds no clock, reads no file, and owns no cursor: ``wire.py`` decides
what has been applied, and this only records the number it is told.

## Where each kind of truth comes from

| Question | Source | Never |
| --- | --- | --- |
| does this thing exist, and what is it attached to | ledger events | inferred from a name |
| what state is this job in *now* | the control plane's ``status`` | re-derived from `job.*` events |
| what does this contract/run contain | ``Facts`` (files) | invented when absent |

The second row is the one worth defending. Re-reading ``job.done`` to decide a
job is finished would be a second implementation of the ledger's state machine,
and the first time the two disagreed the map would be confidently wrong. Job
nodes are created by events and coloured only by ``apply_status``.

## The scenario that is not here, and why

The plan's edge table had ``job -> scenario`` keyed on ``scenarioSha256``.
Following it through the code, that digest is **verified** at
``ControlPlane.start`` and then **not recorded**: the scenario text lives in
the Brief handoff's payload, and the event stream carries a handoff only as
``{iteration, agent, step_id, sha256}`` -- the digest of the whole envelope,
not of the scenario. ``ledger.resume`` does not return the Brief either.

So there is no honest ``scenario`` node yet, and inventing one from the Brief
digest would be labelling a thing with another thing's name. What the stream
genuinely has is the Brief itself, by digest, so that is what is emitted:
``brief:{sha256}``, marked unresolved because its content has not been read.
It becomes a resolved node the day something can fetch a handoff payload.

## Identifier scoping

An id must not collide across the things it names, and two of these are roles
rather than names: ``artifact_id`` is "report", "verdict"; an agent is
"verifier", "board-operator". Both recur in every job. So both are scoped:

    artifact:{job_id}:{artifact_id}      agent:{job_id}:{agent}

``operation:`` and ``bitstream:`` are deliberately *not* scoped -- they are the
group nodes, and their whole purpose is that several jobs land on the same one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.webui.backend.facts import Facts, NoFacts

# Node kinds. Every one of these is emitted by something below; a kind nothing
# produces would be vocabulary rather than a model.
GATE = "gate"
CONTRACT = "contract"
JOB = "job"
AGENT = "agent"
APPROVAL = "approval"
ARTIFACT = "artifact"
RUN = "run"
OPERATION = "operation"
BITSTREAM = "bitstream"
BRIEF = "brief"

# Edge kinds.
JOB_CONTRACT = "job.contract"
JOB_BRIEF = "job.brief"
JOB_AGENT = "job.agent"
JOB_APPROVAL = "job.approval"
JOB_ARTIFACT = "job.artifact"
ARTIFACT_RUN = "artifact.run"
RUN_CONTRACT = "run.contract"
RUN_BITSTREAM = "run.bitstream"
CONTRACT_GATE = "contract.gate"
CONTRACT_OPERATION = "contract.operation"


@dataclass
class Node:
    id: str
    kind: str
    label: str
    status: str | None = None
    unresolved: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "unresolved": self.unresolved,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    field_name: str
    value: str
    source_event_id: int | None = None

    @property
    def id(self) -> str:
        """Derived from the inputs, so applying an event twice adds nothing."""
        return f"edge:{self.kind}:{self.source}:{self.target}:{self.field_name}"

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "evidence": {
                "field": self.field_name,
                "value": self.value,
                "source_event_id": self.source_event_id,
            },
        }


def gate_id(name: str) -> str:
    return f"{GATE}:{name}"


def contract_id(name: str) -> str:
    return f"{CONTRACT}:{name}"


def job_id_of(job: str) -> str:
    return f"{JOB}:{job}"


def agent_id(job: str, agent: str) -> str:
    return f"{AGENT}:{job}:{agent}"


def approval_id_of(approval: str) -> str:
    return f"{APPROVAL}:{approval}"


def artifact_id_of(job: str, artifact: str) -> str:
    return f"{ARTIFACT}:{job}:{artifact}"


def run_id_of(run: str) -> str:
    return f"{RUN}:{run}"


def operation_id(name: str) -> str:
    return f"{OPERATION}:{name}"


def bitstream_id(digest: str) -> str:
    return f"{BITSTREAM}:{digest}"


def brief_id(digest: str) -> str:
    return f"{BRIEF}:{digest}"


class GraphProjection:
    """Nodes and edges, rebuilt from the stream and never from a summary."""

    def __init__(self, facts: Facts | None = None) -> None:
        self.facts: Facts = facts or NoFacts()
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._cursor = 0
        self._revision = 0

    # -- ingest -----------------------------------------------------------

    def apply_frame(self, frame: dict[str, Any]) -> bool:
        """Dispatch one daemon frame. Returns whether the graph changed.

        Frames this does not recognise change nothing, deliberately: a graph
        that grew a node for every unknown frame type would be drawing the
        protocol rather than the work.
        """
        kind = frame.get("type")
        if kind == W.LEDGER_EVENT:
            return self.apply_ledger_event(frame.get("event") or {})
        if kind == W.ARTIFACT_REGISTERED:
            return self.apply_artifact(frame)
        return False

    def apply_ledger_event(self, event: dict[str, Any]) -> bool:
        """One ledger row. Identity and relationships only -- never state."""
        job = _text(event.get("job_id"))
        if not job:
            return False
        kind = _text(event.get("kind"))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_id = event.get("event_id") if isinstance(event.get("event_id"), int) else None

        with self._lock:
            before = self._revision
            self._touch(Node(job_id_of(job), JOB, job))

            if kind == "job.created":
                self._job_created(job, payload, event_id)
            elif kind == "handoff.Brief":
                self._brief(job, payload, event_id)
            elif kind.startswith("approval."):
                self._approval(job, payload, event_id)

            # `agent` is a column on every event, not a payload field of one
            # kind, so it is read from all of them -- including runtime.* rows,
            # which is how an agent appears before it has done anything else.
            agent = _text(event.get("agent"))
            if agent:
                self._touch(Node(agent_id(job, agent), AGENT, agent, metadata={"job": job}))
                self._link(
                    Edge(job_id_of(job), agent_id(job, agent), JOB_AGENT, "agent", agent, event_id)
                )

            if event_id is not None and event_id > self._cursor:
                self._cursor = event_id
            return self._revision != before

    def apply_artifact(self, frame: dict[str, Any]) -> bool:
        """An ``artifact.registered`` frame, path and all.

        The path is used here and kept nowhere: it is read once to work out
        which run directory the file sits in, and only the run's name survives
        into the graph. ``orchestrator/results.py`` says of this frame "never
        sent to a browser", and a graph served to a browser is the browser.
        """
        job = _text(frame.get("jobId") or frame.get("job_id"))
        artifact = _text(frame.get("artifactId") or frame.get("artifact_id"))
        if not job or not artifact:
            return False

        with self._lock:
            before = self._revision
            node_id = artifact_id_of(job, artifact)
            self._touch(Node(node_id, ARTIFACT, artifact, metadata={"job": job}))
            self._touch(Node(job_id_of(job), JOB, job))
            self._link(Edge(job_id_of(job), node_id, JOB_ARTIFACT, "jobId", job))

            run = _run_of(frame.get("targetPath") or frame.get("target_path"))
            if run:
                self._touch(Node(run_id_of(run), RUN, run, unresolved=True))
                self._link(Edge(node_id, run_id_of(run), ARTIFACT_RUN, "targetPath.run", run))
                self._resolve_run(run)
            return self._revision != before

    def apply_status(self, snapshot: dict[str, Any]) -> bool:
        """The control plane's answer. The only thing that colours a job.

        Pending approvals arrive here too. They also arrive as
        ``approval.pending`` events, and the two agree by construction: same
        id, same edge id, so the second application is a no-op.
        """
        with self._lock:
            before = self._revision
            for row in _rows(snapshot.get("jobs")):
                job = _text(row.get("job_id"))
                if not job:
                    continue
                node = self._touch(Node(job_id_of(job), JOB, job))
                # A `Waiting` has both; a plain ledger row has only `state`.
                self._set_status(node, _text(row.get("status")) or _text(row.get("state")) or None)
                if _text(row.get("gate")):
                    self._contract_for(job, _text(row.get("gate")), None)

            for row in snapshot.get("inbox") or []:
                if not isinstance(row, dict):
                    continue
                self._approval(
                    _text(row.get("job_id")), {"approval_id": _text(row.get("approval_id"))}, None
                )
            return self._revision != before

    def reset(self) -> None:
        """Forget everything. Paired with ``Projection.reset`` on a replay."""
        with self._lock:
            self._nodes.clear()
            self._edges.clear()
            self._cursor = 0
            self._revision += 1

    # -- event handlers ---------------------------------------------------

    def _job_created(self, job: str, payload: dict[str, Any], event_id: int | None) -> None:
        node = self._nodes[job_id_of(job)]
        objective = _text(payload.get("objective"))
        if objective:
            node.label = objective
            self._revision += 1
        gate = _text(payload.get("gate"))
        if gate:
            self._contract_for(job, gate, event_id)

    def _contract_for(self, job: str, gate: str, event_id: int | None) -> None:
        """``job.created.gate`` is the contract's file name, already checked.

        ``ControlPlane.start`` refuses a start whose gate is not the contract
        the repeated preflight selected, so this name is not a label -- it is
        the identifier the ledger was allowed to record.
        """
        self._touch(Node(contract_id(gate), CONTRACT, gate, unresolved=True))
        self._link(Edge(job_id_of(job), contract_id(gate), JOB_CONTRACT, "gate", gate, event_id))
        self._resolve_contract(gate)

    def _brief(self, job: str, payload: dict[str, Any], event_id: int | None) -> None:
        digest = _text(payload.get("sha256"))
        if not digest:
            return
        # Unresolved on purpose: this is the digest of the Brief envelope, and
        # nothing here has read what is inside it.
        self._touch(Node(brief_id(digest), BRIEF, f"brief {digest[:8]}", unresolved=True))
        self._link(Edge(job_id_of(job), brief_id(digest), JOB_BRIEF, "sha256", digest, event_id))

    def _approval(self, job: str, payload: dict[str, Any], event_id: int | None) -> None:
        approval = _text(payload.get("approval_id"))
        if not job or not approval:
            return
        self._touch(Node(approval_id_of(approval), APPROVAL, approval, metadata={"job": job}))
        self._touch(Node(job_id_of(job), JOB, job))
        self._link(
            Edge(
                job_id_of(job),
                approval_id_of(approval),
                JOB_APPROVAL,
                "approval_id",
                approval,
                event_id,
            )
        )

    # -- resolution -------------------------------------------------------

    def _resolve_contract(self, name: str) -> None:
        found = self.facts.contract(name)
        if found is None:
            return  # stays an unresolved reference; the ledger still named it
        node = self._nodes[contract_id(name)]
        self._fill(
            node,
            {"sha256": found.sha256, "gate": found.gate, "status": found.status},
        )
        if found.gate:
            self._touch(Node(gate_id(found.gate), GATE, found.gate))
            self._link(
                Edge(contract_id(name), gate_id(found.gate), CONTRACT_GATE, "test_id", found.gate)
            )
        for operation in found.operations:
            # A group node, not a pairwise edge. Ten contracts sharing an
            # operation is ten edges here and forty-five the other way.
            self._touch(Node(operation_id(operation), OPERATION, operation))
            self._link(
                Edge(
                    contract_id(name),
                    operation_id(operation),
                    CONTRACT_OPERATION,
                    "allowed_operations",
                    operation,
                )
            )

    def _resolve_run(self, run: str) -> None:
        found = self.facts.run(run)
        if found is None:
            return
        node = self._nodes[run_id_of(run)]
        self._fill(node, {"verdict": found.verdict, "gate": found.gate})
        if found.contract:
            self._touch(Node(contract_id(found.contract), CONTRACT, found.contract, unresolved=True))
            self._link(
                Edge(
                    run_id_of(run),
                    contract_id(found.contract),
                    RUN_CONTRACT,
                    "contract.test_spec",
                    found.contract,
                )
            )
            self._resolve_contract(found.contract)
        for digest in found.bitstreams:
            self._touch(Node(bitstream_id(digest), BITSTREAM, digest[:12]))
            self._link(
                Edge(
                    run_id_of(run),
                    bitstream_id(digest),
                    RUN_BITSTREAM,
                    "artifacts[kind=bitstream].sha256",
                    digest,
                )
            )

    # -- store ------------------------------------------------------------

    def _touch(self, node: Node) -> Node:
        """Insert, or return what is already there. Never overwrite details.

        A later reference to something already resolved must not knock it back
        to unresolved, which is what a plain overwrite would do.
        """
        existing = self._nodes.get(node.id)
        if existing is not None:
            return existing
        self._nodes[node.id] = node
        self._revision += 1
        return node

    def _fill(self, node: Node, metadata: dict[str, Any]) -> None:
        kept = {k: v for k, v in metadata.items() if v is not None}
        if node.metadata == {**node.metadata, **kept} and not node.unresolved:
            return
        node.metadata.update(kept)
        node.unresolved = False
        self._revision += 1

    def _set_status(self, node: Node, status: str | None) -> None:
        if node.status != status:
            node.status = status
            self._revision += 1

    def _link(self, edge: Edge) -> None:
        if edge.id in self._edges:
            return  # the id is derived, so a re-applied event is a no-op
        self._edges[edge.id] = edge
        self._revision += 1

    # -- read -------------------------------------------------------------

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    @property
    def revision(self) -> int:
        """Bumps on any change. What ``graph.changed`` is debounced against."""
        with self._lock:
            return self._revision

    def snapshot(self, generated_at: str = "") -> dict[str, Any]:
        """Sorted by id, so the same inputs render in the same order."""
        with self._lock:
            return {
                "cursor": self._cursor,
                "nodes": [self._nodes[k].describe() for k in sorted(self._nodes)],
                "edges": [self._edges[k].describe() for k in sorted(self._edges)],
                "generatedAt": generated_at,
            }


# -- helpers ------------------------------------------------------------------


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _rows(jobs: Any) -> Iterable[dict[str, Any]]:
    """``status.jobs`` in either shape. See ``state.flatten_jobs``."""
    if isinstance(jobs, dict):
        return [row for rows in jobs.values() for row in rows if isinstance(row, dict)]
    if isinstance(jobs, list):
        return [row for row in jobs if isinstance(row, dict)]
    return []


def _run_of(path: Any) -> str:
    """The run directory a registered artifact sits in, or nothing.

    Reads the component after ``runs``. Nothing else about the path is kept,
    and a path that is not under a ``runs`` directory yields no run node rather
    than a guess at one.
    """
    if not isinstance(path, str) or not path.strip():
        return ""
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    for index, part in enumerate(parts[:-1]):
        if part == "runs":
            candidate = parts[index + 1]
            return "" if candidate == ".." else candidate
    return ""
