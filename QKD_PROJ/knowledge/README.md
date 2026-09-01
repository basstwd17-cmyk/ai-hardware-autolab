# Evidence-backed knowledge layer

This package is the durable brain behind the live office.  It is deliberately
separate from `webui/`: the UI may be replaced, while a verified fact and the
evidence that earned that status must remain readable.

## Boundary

An agent may emit a closed `KnowledgeCandidate`.  It cannot choose
`verified`, `refuted` or `superseded`.

```text
KnowledgeCandidate
  -> KnowledgeReducer
       -> verify file/document bytes
       -> verify selected JSON value or cited PDF page
  -> KnowledgeClaim(proposed | verified | refuted)
  -> KnowledgeStore(append-only knowledge.* event + rebuildable projection)
```

Missing, stale or unreadable evidence leaves a candidate `proposed`.  It never
turns a process failure into a fact about hardware.  Supersession is a separate
store operation that requires both claims to exist and the replacement to have
verified evidence.

## Identity and scope

`claim_id` is the digest of scope plus the semantic subject/relation/object
triple.  Labels and prose may improve without creating a different fact.
`version_id` includes the complete candidate and evidence, so two evidence
sets remain distinct append-only events.

Every claim is scoped independently by project, hardware and revision.  A
global claim is visible everywhere; a hardware-revision claim is returned only
for that hardware and revision.  Moving from BBB to another board therefore
does not erase shared knowledge or leak BBB-only timing claims into the new
design.

## Retrieval

`KnowledgeStore` uses SQLite FTS5 when available and a bounded LIKE fallback
otherwise.  `KnowledgeRetriever` takes lexical matches, then expands one graph
hop through exact entity ids.  There are no embeddings or guessed edges in
this stage.  Its `KnowledgeContext` is the bounded object that a later daemon
integration will give to `system-architect` before it decides what still needs
web research.

## Current ingestion

`candidates_from_source_packet()` converts every page-cited `SourceClaim` into
a candidate.  The citation is checked against the immutable reference store
again during reduction.  Contract and run evidence can pin both the complete
file SHA-256 and an RFC 6901 JSON field value.

`SourcePacketKnowledgeIngestor` joins that adapter, reducer and store. Repeating
it after a daemon restart returns the same event ids rather than duplicating
knowledge. MissionPlan, ChangeAnalysis, ArchitectureDecision, ContractDraft and
ContractReview are also projected as `proposed` graph knowledge; cited source
claims remain the only rows that can become verified at this stage. The
persistent location is `QKD_PROJ/runs/knowledge.db`.

## Daemon integration

The production assembly opens `runs/knowledge.db` and pins a default scope with
`--knowledge-project`, `--knowledge-hardware` and `--knowledge-revision`.
Starting a reviewed-contract job retrieves at most 16 claims and stores that
exact `KnowledgeContext` in its durable Brief.  Every later planning iteration
therefore sees the same context rather than a moving search result.

The local control and office wire protocols expose read-only commands:

- `knowledge.claim`;
- `knowledge.search`;
- `knowledge.events` with its own cursor, never the job-ledger cursor;
- `knowledge.neighbors` by exact entity id.

`DiscoveryPipeline` can also receive a retriever and
`SourcePacketKnowledgeIngestor`.  It retrieves known facts before the system
architect turn and ingests the resulting SourcePacket.  If ingestion fails
after the SourcePacket handoff, retrying spends no new agent turn.

The normal daemon routes both a reviewed-baseline scenario and a contract gap
through the one-step-per-tick Mission workflow:

`MissionPlan -> SourcePacket -> ChangeAnalysis -> ArchitectureDecision ->
ContractDraft -> ContractReview`.

The Brain Map follows its separate knowledge cursor and shows the compact
subject/relation/object view by default; selecting an edge reveals the complete
claim, source job and evidence without discarding the provenance graph.
