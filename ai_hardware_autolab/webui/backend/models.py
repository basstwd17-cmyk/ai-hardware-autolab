"""The browser contract, written down once so both sides compile against it.

Everything the HTTP surface returns is a model here, and the frontend's types
are generated from the OpenAPI schema these produce
(``webui/tools/export_openapi.py`` then ``npx openapi-typescript``). The point
is not documentation. It is that a field renamed on one side stops the other
side from building, instead of arriving as ``undefined`` at runtime.

**Where ``dict[str, Any]`` is still honest.** Three payloads genuinely have no
fixed shape and pretending otherwise would be a worse lie than admitting it:

* ``LedgerEvent.payload`` -- the ledger stores whatever a runtime notification
  carried, including vendor fields this project has never seen.
* ``StateResponse.snapshot`` -- passed through verbatim so a caller that needs
  the control plane's exact answer can have it.
* ``JobDetail.job`` -- ``ControlPlane.job`` is a resume state whose keys depend
  on how far the job got.

Each of those is annotated where it is declared. Everything else is named.

**Where a union is used instead of a merge.** ``status.jobs`` is a list of
ledger rows when no daemon is attached and a list of scheduler ``Waiting``
records when one is. Those carry different information -- a ``Waiting`` knows
*why* a job is not moving -- so they stay two models and the field is a union.
Flattening them into one optional-everything model would let a caller read
``detail`` off a record that never had one.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

JsonDict = dict[str, Any]


class Strict(BaseModel):
    """Reject unknown keys. A field this server forgot to model is a bug."""

    model_config = ConfigDict(extra="forbid")


class Passthrough(BaseModel):
    """Keep unknown keys. Used where the daemon owns the shape, not us."""

    model_config = ConfigDict(extra="allow")


# -- the connection -----------------------------------------------------------


class DaemonStatus(Strict):
    address: str = Field(description="pipe name or tcp:// address of the daemon")
    connected: bool
    cursor: int = Field(description="the last global event_id actually applied")
    attempts: int
    frames: int
    gaps: int
    last_error: str | None = None
    profile: str | None = Field(None, description="daemon tool profile: offline or hardware")
    workspace_mode: str | None = Field(
        None, description="shared checkout or isolated per-job workspace"
    )


class Health(Strict):
    ok: bool
    daemon: DaemonStatus


# -- the ledger ---------------------------------------------------------------


class LedgerEvent(Strict):
    event_id: int = Field(description="global cursor; unique and gapless")
    job_id: str
    seq: int = Field(0, description="orders one job only, never the stream")
    at: str = ""
    kind: str = ""
    agent: str | None = None
    payload: JsonDict = Field(
        default_factory=dict,
        description="free-form by design: whatever the runtime notification carried",
    )


class WaitingRow(Strict):
    """A scheduler record: why this job is not moving."""

    job_id: str
    state: str
    status: str
    detail: str
    retry_at: str | None = None
    approval_id: str | None = None
    attempt: int = 0


class JobRow(Strict):
    """A ledger row, as returned when no daemon is attached."""

    job_id: str
    gate: str
    objective: str
    state: str
    outcome: str | None = None
    iteration: int
    created_at: str
    updated_at: str


JobEntry = Union[WaitingRow, JobRow]


class ApprovalRow(Passthrough):
    """A pending approval. Extra keys are kept: the ledger owns this shape."""

    approval_id: str
    job_id: str
    contract: str
    step_id: str
    scope: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    max_executions: int = 1
    requested_at: str
    requested_by: str = ""
    expires_at: str | None = None
    state: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    bundle_sha256: str | None = None


# -- results and artifacts ----------------------------------------------------


class ArtifactMetadata(Strict):
    """The path-free half of a registration. The only half a browser gets."""

    artifactId: str
    kind: str = ""
    label: str = ""


class JobResult(Passthrough):
    """What the daemon reduced a job to.

    Passthrough because the verdict-backed, stopped-early and contract-proposal
    shapes differ in which optional fields they carry, and all three are the
    daemon's to define.

    The contract-proposal fields below are declared so the browser gets types
    for them rather than reaching into an untyped bag. **`reasonCode` is not an
    approval.** `results.py` copies it from the review's own `outcome`, and the
    reviewer's definition of `ready_for_human_review` is "no review error or
    required change is known" -- explicitly not approval, and explicitly not
    executable. The UI renders it as those words and no others.
    """

    jobId: str
    gate: str | None = None
    iteration: int | None = None
    outcome: str | None = None
    state: str | None = None
    summary: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    artifacts: list[ArtifactMetadata] = Field(default_factory=list)
    verdictSha256: str | None = None
    reasonCode: str | None = Field(
        None, description="the review's own outcome; never an approval state"
    )
    contractDraft: JsonDict | None = Field(
        None, description="the ContractDraft envelope verbatim; not a file artifact"
    )
    contractDraftSha256: str | None = None
    contractReview: JsonDict | None = Field(
        None, description="the ContractReview envelope verbatim, findings and all"
    )
    contractReviewSha256: str | None = None


# -- responses ----------------------------------------------------------------


class StateResponse(Strict):
    daemon: DaemonStatus
    cursor: int
    jobs: list[JobEntry]
    inbox: list[ApprovalRow]
    results: dict[str, JobResult]
    warnings: list[str]
    snapshot: JsonDict = Field(
        default_factory=dict,
        description="the control plane's answer verbatim; jobs may be a dict here",
    )


class EventsResponse(Strict):
    events: list[LedgerEvent]
    cursor: int
    oldest: int = Field(description="lowest event_id still in memory; below it, replay")


class JobDetail(Strict):
    job: JsonDict = Field(description="ControlPlane.job resume state; keys depend on progress")
    result: JobResult | None = None
    events: list[LedgerEvent] = Field(
        description="from the ledger, not the capped projection: a transcript wants all of it"
    )
    cursor: int = Field(0, description="the last event_id in this page")
    latest: int = Field(0, description="the ledger tip, so a caller can tell it is behind")


class HandoffDetail(Strict):
    """One durable agent-to-agent envelope, read only for its own job."""

    handoff_id: str
    job_id: str
    iteration: int
    kind: str
    agent: str
    step_id: str = ""
    at: str
    payload: JsonDict = Field(description="the envelope body the next agent received")
    sha256: str
    failure_signature: str | None = None
    event_id: int | None = Field(
        None, description="the ledger event that announced this envelope, when it has one"
    )


class CommandResponse(Passthrough):
    """A daemon ``command.result``. ``ok`` false is an answer, not a failure."""

    ok: bool
    commandId: str | None = None
    error: str | None = None


class Occupant(Strict):
    """One character on the office floor."""

    id: str
    agent: str
    jobId: str
    activity: str = Field(
        description="animation only - never a job state, a verdict or an approval"
    )
    detail: str
    seenAt: int = Field(description="an event_id, not a clock: replays must agree")
    turns: int


class OfficeSnapshot(Strict):
    cursor: int
    openJobs: list[str] = Field(description="from the control plane, never re-derived")
    occupants: list[Occupant]


class ArtifactOpened(Strict):
    """The acknowledgement a click gets. No path: the host keeps that."""

    ok: bool
    jobId: str
    artifactId: str
    kind: str = Field(description="file or directory - what was revealed, not where")


class AgentProfile(Strict):
    """The reviewable, non-prompt half of one runtime agent definition."""

    name: str
    team: str = Field(description="one of the seven user-facing agent teams")
    role: str
    description: str
    mcpTools: list[str]
    localTools: list[str]
    skills: list[str]
    consumes: list[str]
    emits: list[str]
    touchesHardware: bool
    nativeWeb: bool


class AgentCatalog(Strict):
    agents: list[AgentProfile]


class RemoteApprovalCreate(Strict):
    jobId: str = Field(min_length=1)
    ttlMinutes: int = Field(720, ge=1, le=1440)
    maxIterations: int = Field(8, ge=1, le=64)
    maxAutoApprovals: int = Field(24, ge=1, le=256)
    durationHours: int = Field(12, ge=1, le=72)


class RemoteApprovalCreated(Strict):
    path: str
    urls: list[str]
    expiresAt: str
    proposal: JsonDict


class RemoteApprovalDecision(Strict):
    approvedBy: str = Field(min_length=1, max_length=100)
    reviewSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "revise"] = "approve"
    comment: str = Field("", max_length=4000)


class RemoteControlMessage(Strict):
    sentBy: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


class RemoteControlCancel(Strict):
    requestedBy: str = Field(min_length=1, max_length=100)
    reason: str = Field("", max_length=1000)


class RemoteControlStatus(Strict):
    capability: JsonDict
    job: JsonDict | None
    recentEvents: list[JsonDict] = Field(default_factory=list)


class Refusal(Strict):
    ok: bool = False
    detail: str


# -- the graph ----------------------------------------------------------------


class EdgeEvidence(Strict):
    """Why this edge exists, in terms a reader can go and check.

    ``field`` names the field the relationship was read out of, ``value`` is
    what it held, and ``source_event_id`` is the ledger cursor that carried it
    -- or ``None`` when the relationship came from a file (a contract's own
    ``test_id``, a run manifest's ``test_spec``) rather than from a row. An
    edge cannot claim a cursor a ledger row never said.
    """

    field: str
    value: str
    # Required, not defaulted: `Edge.describe` always emits it, and a schema
    # that called it optional would make the browser's type `| undefined` for
    # a key that is never absent -- guarding for a case that cannot happen is
    # how a contract stops being read.
    source_event_id: int | None = Field(...)


class GraphNode(Strict):
    id: str = Field(description="kind-prefixed and scoped, e.g. artifact:{jobId}:{artifactId}")
    kind: str
    label: str
    # All three are required for the same reason as `EdgeEvidence`: the
    # projection emits every key on every node, so the browser should not have
    # to prove they are there.
    status: str | None = Field(
        ..., description="job state, and only from the control plane's status"
    )
    unresolved: bool = Field(
        ..., description="the ledger named it; nothing has filled in its details yet"
    )
    metadata: JsonDict = Field(
        ...,
        description="browser-safe facts only: never a path, a token or a pipe address",
    )


class GraphEdge(Strict):
    id: str = Field(description="edge:{kind}:{source}:{target}:{field} - derived, so stable")
    source: str
    target: str
    kind: str
    evidence: EdgeEvidence


class GraphSnapshot(Strict):
    cursor: int = Field(description="the ledger cursor this graph has applied up to")
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    generatedAt: str


class Position(Strict):
    x: float
    y: float


class GraphLayout(Strict):
    """Where the user put things. The only state in this app they author."""

    version: int
    revision: int = Field(
        description="bumped on every write; a save must name the one it is replacing"
    )
    positions: dict[str, Position]
    unreadable: str | None = Field(
        ...,
        description="set when the file on disk could not be interpreted; it was left alone",
    )


class LayoutSave(Strict):
    revision: int = Field(ge=0, description="the revision this save is replacing")
    positions: dict[str, Position]


class LayoutConflictBody(Strict):
    """A 409 that carries the layout back, so the client can merge and retry."""

    ok: bool = False
    detail: str
    current: GraphLayout
