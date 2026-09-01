# Discovery reference boundary

This package is the first layer of the scenario-to-architecture discovery
loop. A Codex `researcher` may use the CLI-owned native web-search tool to
decide what to query and which result to inspect. Deterministic code then makes
selected PDF bytes stable enough to cite. A search result or snippet alone is
never evidence.

## What exists now

`ReferenceStore.ingest_pdf()`:

1. copies a local PDF while enforcing a byte limit;
2. computes the document id from the copied bytes, never from a caller claim;
3. extracts page text in a child process with a hard timeout;
4. stores the original, `manifest.json`, and page-indexed `text.json` together
   under `references/<sha256>/`;
5. returns the existing record when identical bytes are ingested again.

The extraction is explicitly marked `layout_faithful: false`. It is a search
aid. A timing number still cites the original PDF page, and a later page
renderer or OCR tool must be used when tables, diagrams or visual layout carry
the meaning.

`ReferenceRecord.public_manifest()` removes the acquisition locator. The full
manifest may contain an absolute private path or a URL with a private query and
must remain on the privileged backend side.

## Store shape

```text
references/
└── <document-sha256>/
    ├── original.pdf
    ├── manifest.json
    └── text.json
```

`manifest.json` records content identity, size, media type, source provenance,
page count and extraction status. A timeout or parser error is visible and the
original is still retained; no caller may treat a non-`ok` extraction as a
complete searchable document.

## Native web search and agent-facing tools

The installed Codex CLI owns login and native search. The App Server spike on
2026-08-27 observed completed `webSearch` items carrying the query, action and
result URLs. Only the `researcher` role is launched with
`web_search="live"`; every other role is explicitly launched with
`web_search="disabled"`.

Fetching selected bytes still belongs behind a provider interface rather than
in the store:

```python
class ResearchProvider(Protocol):
    def search(self, query: str) -> list[SearchResult]: ...
    def fetch(self, result: SearchResult) -> FetchedDocument: ...
```

The agent owns the research decisions:

```text
identify an information gap
→ choose a query
→ inspect search results
→ select a source
→ read indexed pages
→ issue a follow-up query or emit SourcePacket
```

The provider and store own deterministic safety:

- allowed URL schemes and redirects;
- response byte and time limits;
- media-type and PDF signature checks;
- content SHA-256 and immutable storage;
- acquisition and citation records;
- no execution of downloaded content.

`ResearchTools` exposes the provider-independent surface. During a researcher
turn, `NativeWebResearchSession` also exposes the collection and reading calls
as App Server dynamic tools:

- `search_sources(query, reason, limit)`;
- `collect_source(result_id)`;
- `search_reference(document_id, query)`;
- `read_reference_page(document_id, page)`.

Collection accepts only a result id returned by a search in the same tool
session. It requires credential-free HTTPS, optionally enforces an allowed-host
list, rechecks the result identity and downloaded byte count, and refuses a
provider path outside the assigned temporary destination. Search snippets are
marked as untrusted; only a document that entered `ReferenceStore` can be read
or cited.

`HttpsPdfProvider` downloads only credential-free public HTTPS PDFs, with
redirect, size, media-type, signature and public-address checks. The exact URL
must have appeared in a completed native-search result in the same turn. HTML
pages may be searched and opened by Codex, but are not yet admitted into the
durable evidence store; a claim that exists only in HTML therefore remains an
open question for now.

## Durable objects

The package now defines and validates:

- `MissionPlan`: scenario classification, existing baseline, bounded research
  questions, required materials, execution agents and proposed contract action;
- `ResearchPlan`: questions, source classes, completion criteria and search budget;
- `SourcePacket`: search trace, collected documents, page-cited claims and
  explicitly open questions;
- `ArchitectureDecision`: a selected architecture or an explicit
  `needs_research`/`not_feasible` stop, linked to SourcePacket claim ids;
- `ContractDraft`: a bounded proposal with declared hardware effects and
  deterministic advance conditions;
- `ContractReview`: findings tied to the exact canonical draft SHA-256.
- `ChangeAnalysis`: sourced differences between the durable baseline and the
  newly collected SourcePacket, including impacted agents and contract action.

`DiscoveryPipeline` runs one `system-architect` turn to create a
`ResearchPlan`, then one `researcher` turn to create a `SourcePacket`. Both are
stored in the existing handoff ledger. If the process dies after the plan, a
new pipeline instance reuses that plan instead of asking the model to recreate
it.

`DiscoveryLoop` is the daemon form. A preflight `contract_gap` can be opened
with `qkd-daemon discover`; each daemon tick performs at most one agent turn:

```text
ResearchPlan → SourcePacket → ArchitectureDecision → ContractDraft → ContractReview
```

The common `job.mission` front door accepts both a reviewed baseline and a
contract gap. It never skips the first planning pass:

```text
MissionPlan → SourcePacket → ChangeAnalysis → ArchitectureDecision → ContractDraft → ContractReview
```

Every phase is derived from the missing ledger handoff, so recreating the loop
continues at the next missing envelope. The final draft remains in the ledger
as a proposal. It is copied under `contracts/` only by the separate
digest-bound human promotion workflow and never grants hardware authority.

Discovery envelopes use the existing handoff/event ledger rather than add a
second document database. SQLite schema version 4 admits mission and change
handoffs as well as discovery; opening a version 1, 2 or 3 ledger migrates the handoff constraint and
preserves its rows in one transaction.

## Verification performed

- 86 targeted tests pass (one platform-specific test skipped) across agent
  definitions, App Server command construction, native-search bridging,
  references and discovery restart behavior.
- A live subscription-backed App Server turn autonomously refined its queries,
  emitted three structured web-search/open actions, and opened the official
  Codex CLI reference. No API key or credential file was read by this code.
- The current 537,503-byte, four-page RTASE schematic ingests successfully as
  document `c030d25ddc211d25add9879f7ee936c17015a50e557d55c0e46d9e601418da04`.
- Its public manifest contains no acquisition locator.
- A parser worker has a hard timeout and cannot block the long-lived daemon.

## Conversation entry point

The web application's single text box should not send every sentence to
the mutation loop. It should produce one closed routing decision:

```text
inquiry  -> read-only researcher/adviser turn, no job and no hardware
job      -> scenario preflight, durable Brief and discovery/development loop
steer    -> message attached to one existing job
```

The UI may look like one conversation, but the chosen route, target job and
resulting command must be recorded. A question can never acquire hardware
authority merely because a model classified its wording as implementation.
