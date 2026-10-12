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
otherwise. Both paths match scenario terms rather than requiring the whole
sentence. `KnowledgeRetriever` takes lexical matches, then expands one graph
hop through exact entity ids. There are no embeddings or guessed edges here.
Its compatibility `KnowledgeContext` preserves each claim's scope, version and
evidence hashes, not just its prose.

For current autonomous-v1 jobs, `AgentKnowledgeContext` selects memory immediately
before a Lead, Builder or Verifier model turn. Lead prefers source material;
Builder prefers code/run references and its current step; Verifier prefers
observations/source material. Queries also consider recent feedback and errors.
All searches and exact-entity neighbor lookups use the current project/hardware/
revision scope; global claims can be shared, another board's scoped facts cannot.
UI taxonomy/co-mention lines are not used as engineering evidence.

The exact UI-generated leading execution-scope sentence is excluded only from
the retrieval query (never from the signed scenario or authority). Mixed Korean
text preserves ASCII identifiers such as BBB/PRU0/CODE0 as searchable terms.

The packet contains at most 6 claims and 9,000 serialized JSON characters (not
9,000 tokens). It preserves whole statements, units, scope, status, source job,
version and up to 3 evidence references with hashes per claim. Oversized claims
are omitted rather than cut mid-condition; omission counts make the selection
explicitly incomplete. At most 2 proposed hints and 2 one-hop neighbors are sent.
A matching refutation/failure/unmet observation is retained preferentially.
Verified status describes ingestion-time evidence checks, not current hardware
success or current source integrity. Agents must check applicability and cited
bytes before reuse. Old runs are not substitutes for newly requested measurements.

Each dispatched selection logs claim/version ids, scope, query, packet digest
and character count in `knowledge.context.selected`. A failed lookup logs
`knowledge.context.unavailable`, omits stale memory and leaves normal development
available. No additional model/search-provider call or knowledge DB mutation is
needed. Formatting-only repairs within a turn reuse the supplied packet.

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
Historical reviewed-contract jobs store a compatibility `KnowledgeContext` in
the Brief. New three-agent jobs replace that broad snapshot at model dispatch
with the small current role packet described above. Replanning sees newly
ingested applicable records without placing the entire knowledge store in a prompt.

The local control and office wire protocols expose read-only commands:

- `knowledge.claim`;
- `knowledge.search`;
- `knowledge.events` with its own cursor, never the job-ledger cursor;
- `knowledge.neighbors` by exact entity id.

`DiscoveryPipeline` can also receive a retriever and
`SourcePacketKnowledgeIngestor`.  It retrieves known facts before the system
architect turn and ingests the resulting SourcePacket.  If ingestion fails
after the SourcePacket handoff, retrying spends no new agent turn.

Historical discovery jobs retain the one-step-per-tick Mission workflow:

`MissionPlan -> SourcePacket -> ChangeAnalysis -> ArchitectureDecision ->
ContractDraft -> ContractReview`.

New missions use Lead → Builder → Verifier. The Brain Map follows its separate
knowledge cursor. Map now opens a Canvas constellation with six colored topic
categories and one point per stored record. Pan, zoom, search and selection
inspect existing knowledge without mutating it. The separate branch view shows
the selected branch and paged records. Breadcrumbs
return to ancestors; search includes collapsed branches and opens a matching
record with only its own evidence, status and scope. Original relations,
evidence graphs and run provenance remain available separately.

Board navigation uses explicit board entities or stored hardware scope, including
project and revision in its identity. Known technical keywords provide additional
navigation; unfamiliar board entities are retained even when their records also
mention a recognized keyword. Historical assignment, required-material and
contract-action records live under a separate procedure-history branch. All
records remain reachable and unchanged in storage. A record can appear under
several topics; counts are unique within a branch, not additive across branches.
Classification is not engineering evidence or a new authorization rule.

**Conversation ingestion (2026-10-08):** `ConversationKnowledgeIngestor` replays
saved role/device completion and device failure events before and after service
ticks. Its durable cursor advances after idempotent knowledge writes, including
terminal jobs. Errors defer ingestion without replaying model/device actions.
Optional `{topic, text}` notes, summary fallback, source findings and artifact
metadata remain proposed. Exact host device returns are evidence-checked as
ledger observations, not arbitrary prose or scenario acceptance. Unknown hardware
outcomes remain proposed. Scope comes from the selected board at the event.

The cloud relay incrementally copies events to authenticated D1 storage.
Production `/knowledge.html` uses the existing operator token to read this
replica and shows its last synchronization status.
