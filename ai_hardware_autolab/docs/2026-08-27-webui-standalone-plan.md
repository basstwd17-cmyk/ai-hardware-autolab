# A standalone web UI, off the VS Code extension

**Date:** 2026-08-27
**Status:** P2 complete — bridge, guard, generated contract, backpressure,
graph projector, React shell and layout persistence, all built and tested
offline. The office is a placeholder until P3.
**Replaces:** nothing. `agent-platform/` keeps working exactly as it does today.
**Upstream:** Pixel Agents v1.4.1, MIT, `pixel-agents-hq/pixel-agents`, pinned at
`3537e140c2094761beae748592aeb92ece8edfdd`.

## 1. Why this is a small job and not a rewrite

`agent-platform/docs/platform-integration.md` already says the thing that makes
this cheap:

> This fork keeps Pixel Agents as a replaceable office UI. The durable agent
> platform owns jobs, approvals, handoffs, evidence, and the event cursor.

The daemon's contract with any UI is `orchestrator/wire.py` — protocol 1,
newline-delimited JSON over a named pipe (or loopback TCP), with a
subscribe/replay/ready handshake and five operator commands. The VS Code
extension is *a* client of that protocol. Nothing in the protocol knows it is
talking to VS Code, and nothing in the daemon has to change for a second client
to attach.

So the standalone web app is one more wire client. It is not a fork of the
daemon, and it is not a reimplementation of the ledger.

## 2. What is kept, what is replaced

| Layer | Today | Standalone web |
| --- | --- | --- |
| Truth | `orchestrator/` (Python), SQLite ledger | **unchanged, same process** |
| UI transport | Windows named pipe → Node server → WebSocket | **FastAPI**, same pipe → WebSocket/SSE |
| Office renderer | `agent-platform/webview-ui` (React, canvas) | **vendored**, MIT, unchanged rendering |
| Office data producer | `server/src/` (~8.6k LOC of hooks, transcripts, file watching) | **not ported.** See §4 |
| Graph view | does not exist | **new** |

The Node server is 8.6k lines, and almost all of it exists to discover Claude
Code sessions by watching hook output, parsing transcripts and tailing files.
This project does not need any of that: the daemon already knows what every
agent is doing, because every agent is a job it started. Porting `fileWatcher.ts`
(1650 lines) would be porting a discovery mechanism to a place where discovery
is not a problem.

## 3. Shape

```text
                  ai_hardware_autolab/orchestrator  (unchanged)
                     ledger + daemon + loop
                            |
                            |  protocol 1 NDJSON
                            |  \\.\pipe\qkd-agent-platform  |  tcp://127.0.0.1:PORT
                            v
                  ai_hardware_autolab/webui/backend  (FastAPI + Uvicorn)
                    wire.py      one persistent subscription, replay from 0
                    state.py     ledger.event -> jobs / inbox / results / artifacts
                    security.py  token + Origin + Host, as ASGI middleware
                    models.py    the browser contract, in Pydantic
                    channels.py  one bounded queue per browser; overflow closes it
                    graph.py     the mind-map: derived, never authored
                    facts.py     contracts/ and runs/, behind an injected resolver
                    office.py    ledger.event -> office ServerMessage frames   (P3)
                    app.py       GET /api/*  ·  POST /api/command  ·  WS /ws
                            |
                            v
                  ai_hardware_autolab/webui/frontend  (Vite + React + TS)
                    api/        generated from openapi.json
                    app/        the stage, the dock, and the conversation
                    office-vendor/  Pixel Agents renderer, MIT, kept unmodified (P3)
                    graph/      @xyflow/react + elkjs
```

One process serves the API, the WebSocket and the built frontend, on
`127.0.0.1` only — and it refuses to bind anything else (§5). The daemon's pipe
already carries a DACL naming this logon's user SID and rejects remote clients;
binding the web server wider would hand that ACL's guarantee away.

## 4. The office, without porting the Node server

The renderer needs two kinds of message, and they have very different costs.

**Assets** — `characterSpritesLoaded`, `furnitureAssetsLoaded`,
`floorTilesLoaded`, `wallTilesLoaded`, `carpetTilesLoaded`. These are pixel
grids (`string[][][]`), produced by `server/src/assetLoader.ts` (847 lines)
scanning PNGs under `webview-ui/public/assets/` at startup. The scan result is a
pure function of files that do not change at runtime. So it is **exported once**
by a Node script that imports the upstream loader, written to
`webui/backend/static/office-assets.json`, and served as a static file. 847
lines of runtime scanning becomes a build step and a `FileResponse`.

**State** — `agentCreated`, `agentStatus`, `agentToolStart`, `agentToolDone`,
`platformState`, `platformJobResult`. These are exactly what
`server/src/platform/platformOfficeProjector.ts` (207 lines) and
`jobLedgerEventAdapter.ts` (372 lines) produce from ledger events. That is the
piece worth porting to `webui/backend/office.py`, and it is the only piece.

Everything else the renderer sends — layout edits, seat positions, settings —
is persisted as JSON by `layoutPersistence.ts` / `configPersistence.ts`. In the
web app that is one file each under `webui/state/`.

## 5. The browser is not a trusted client

An earlier version of this plan said a loopback-only server owned by the same
logon as the pipe does not need authentication. **That was wrong**, and wrong
in the direction that matters, because the door it leaves open ends at
`approval.decide`.

`127.0.0.1` is not private from a browser's point of view. Any page the user
has open can reach it, by three routes that need three different answers:

| Attack | Why the obvious defence fails | Check |
| --- | --- | --- |
| cross-origin `POST` | CORS governs reading the *response*, not running the *request*. A `text/plain` fetch is a simple request: no preflight, the handler runs. The attacker never sees a byte and has still started a job. | bearer token |
| `new WebSocket('ws://127.0.0.1:8787/ws')` | WebSockets are not subject to CORS at all. The browser sends `Origin` and expects the server to check it. | `Origin` allowlist |
| DNS rebinding | The attacker's domain resolves to `127.0.0.1`, so their page *is* same-origin. `Origin` now says what they want it to. | `Host` allowlist — the name dialled, which a rebound request cannot fake |

So: a `secrets.token_urlsafe(32)` generated per run, required on **every**
route rather than only the mutating ones. Splitting read from write would mean
adjudicating each new endpoint, and `GET /api/jobs/{id}` already returns a
job's entire history. One rule is easier to keep than a taxonomy.

The token is presented as `Authorization: Bearer`, or as `?token=` on the
WebSocket handshake, which a browser cannot attach a header to. It dies with
the process; it is written to `webui/state/session.json` (git-ignored) for a
launcher to read, and printed at startup.

Three consequences worth stating:

* **The static bundle is served without the token.** It is code, not data. The
  page reads its token out of its own URL, and every call it then makes is
  guarded. The `Host` check still applies to it.
* **An absent `Origin` is allowed.** curl, the launcher and the tests do not
  send one, and a browser page cannot suppress it, so absence is not something
  an attacker can arrange.
* **A wider bind is refused, not warned about.** The guard rejects any request
  whose `Host` is not a loopback name, so binding `0.0.0.0` would open a port
  that accepts connections and then refuses everything on them. Worse than not
  listening.

The same guard covers everything added later — layout persistence, artifact
opening, the graph — because it is stated as *what is protected* (`/api`,
`/ws`) rather than as a list of exemptions to remember.

## 6. The browser contract is generated, not agreed

```text
webui/backend/models.py   Pydantic
        |  app.openapi()
        v
frontend/src/api/openapi.json      committed, so the diff shows contract changes
        |  npx openapi-typescript
        v
frontend/src/api/schema.d.ts       the frontend compiles against this
```

Two hand-written type declarations agree until they do not, and the failure
arrives as `undefined` in a browser nobody is watching. Generated from one
source, a renamed field stops the frontend building.

`dict[str, Any]` survives in exactly three places, each annotated with why:
`LedgerEvent.payload` (the ledger stores whatever a runtime notification
carried), `StateResponse.snapshot` (passed through verbatim), and
`JobDetail.job` (a resume state whose keys depend on how far the job got).
Everything else is named, and the strict models reject unknown keys — a field
this server forgot to model is a bug, not a passthrough.

`status.jobs` stays a **union**, not a merge: ledger rows when no daemon is
attached, scheduler `Waiting` records when one is. A `Waiting` knows *why* a
job is not moving. Flattening them into one optional-everything model would let
a caller read `detail` off a record that never had one.

A committed schema goes stale silently, so `export_openapi.py --check` is a
test.

## 7. A slow browser is told, not quietly holed

A backgrounded or wedged tab stops draining its socket while the daemon keeps
pushing. Three options, and only one of them is honest:

* **Grow the queue** — memory becomes a function of how long a tab stays
  wedged. Not a bound, a hope.
* **Drop the oldest frame** — cheap, and it silently breaks the invariant the
  whole protocol rests on: one frame per global cursor, no gaps. The tab keeps
  rendering, now missing durable history, and nothing says so.
* **Say so, and stop** — clear the queue, put one `webui.resync_required` in
  its place carrying the cursor actually applied and how many frames went with
  it, and close the socket.

The third, and the *and stop* is a correction. The first version sent the
notice and kept delivering, reasoning that the client could rebase from
`/api/state` while newer frames arrived, since the protocol's idempotence rule
would drop the overlap. That reasoning fails in one direction: **idempotence
resolves duplicates, not omissions.** The frames lost in the overflow are gone
either way, and a client that goes on applying what arrives after the notice
holds a view with a hole in the middle of it and no way to know.

So the contract is:

```text
overflow → send webui.resync_required (cursor applied, frames dropped)
         → close the WebSocket (1008, not 1000 — this is not routine)
         → client re-reads /api/state (or /api/graph)
         → client reconnects with after=<that snapshot's cursor>
```

The server closing is what makes it a contract rather than a request. A client
that ignores the notice and keeps its socket open cannot exist, because the
socket is not open. That rules out the wrong client implementation instead of
documenting it.

## 8. The graph view

The ask is Gravity Connect-style: implement something, and see it join a
mind-map wherever it shares information with what is already there. The
temptation in that shape is similarity — same words, same purpose, probably
related. This graph refuses it. **Every edge comes from an identifier one
record holds and another record holds too**, and carries the field name it was
read out of, so the graph can be read rather than trusted. Same rule this
project already applies to thresholds.

### What the ledger actually records

The first draft of this table was written from the plan, not from the code, and
two rows of it were wrong. Following them through:

- **`contractSha256` is verified and then not recorded.** `ControlPlane.start`
  refuses a start whose digest does not match the repeated preflight — and what
  reaches the ledger is `job.created` with `{gate, objective}`. The digest lives
  in the Brief handoff's payload; the event stream carries a handoff only as
  `{iteration, agent, step_id, sha256}`, and `ledger.resume` does not return the
  Brief either. So the edge is keyed on the **contract name**, which is not a
  label — `start` has already forced it to equal the selected contract's file
  name — and the digest arrives from the file (§8.3).
- **There is no scenario node.** Same reason, one step worse: the scenario text
  is inside the Brief payload, so the stream has neither the text nor its
  digest. Emitting a `scenario` keyed on the Brief envelope's digest would be
  labelling one thing with another thing's name. What is emitted instead is the
  thing that *is* there: `brief:{sha256}`, marked unresolved because nothing has
  read what is inside it. It becomes a resolved node the day something can fetch
  a handoff payload.

### 8.1 Edges, P2-A

| Edge | Field it is read from | Cursor |
| --- | --- | --- |
| `job → contract` | `job.created.payload.gate` | yes |
| `job → brief` | `handoff.Brief.payload.sha256` | yes |
| `job → agent` | the `agent` column, on every event | yes |
| `job → approval` | `approval.*.payload.approval_id` | yes |
| `job → artifact` | `artifact.registered.jobId` | no |
| `artifact → run` | the component after `runs` in `targetPath` | no |
| `run → contract` | `manifest.contract.test_spec` | no |
| `run → bitstream` | `manifest.artifacts[kind=bitstream].sha256` | no |
| `contract → gate` | the contract's own `test_id` | no |
| `contract → operation` | the contract's `allowed_operations` | no |

"Cursor" is whether `evidence.source_event_id` is set. It is set only for the
rows a ledger event carried. An edge read from a file says `null` rather than
borrowing the cursor of whatever event happened to trigger the lookup — the
graph cannot claim a cursor a ledger row never said.

### 8.2 What is deliberately absent until P2-B

No `contract ↔ contract`, no `run ↔ run`, no shared-gate or shared-operation
pair. Ten things sharing one value is 45 edges drawn pairwise and 10 drawn
through a group node, so the shared value **is** a node:

```text
contract A ─┐
contract B ─┼─→ operation:program_cpld_sram
contract C ─┘
```

Secondary edges, if they are ever wanted, go behind a "show related" toggle.

### 8.3 Purity, and where the files come in

`graph.py` is a projection over exactly two inputs: the frames, in order, and a
`Facts` resolver. Same frames plus same facts gives the same graph, node for
node, which is what makes "rebuild from cursor 0" a recovery rather than a
hope. It holds no clock, reads no file and owns no cursor — `wire.py` decides
what has been applied and the graph records the number it is told.

`facts.py` is the resolver: `contracts/*.yaml` for a name's digest, `test_id`
and `allowed_operations`; `runs/*/manifest.json` for a run's contract, verdict
and bitstreams. A test injects `FrozenFacts` and touches no repository. A name
that resolves to nothing stays in the graph as an **unresolved reference** — the
ledger did name it — and gains its details if the file appears later. A later
reference never knocks a resolved node back to unresolved.

### 8.4 Identity

Two of these are roles rather than names — `artifact_id` is "report",
"verdict"; an agent is "verifier", "board-operator" — and both recur in every
job. Both are scoped:

```text
artifact:{job_id}:{artifact_id}      agent:{job_id}:{agent}
```

`operation:` and `bitstream:` are deliberately **not** scoped. They are the
group nodes, and several jobs landing on the same one is the entire point.

Edge ids are derived — `edge:{kind}:{source}:{target}:{field}` — so applying an
event twice adds nothing.

### 8.5 State

Existence and relationships come from events. **Current job state comes from
the control plane's `status` and from nowhere else.** Re-reading `job.done` to
decide a job is finished would be a second implementation of the ledger's state
machine, and the first time the two disagreed the map would be confidently
wrong.

### 8.6 Serving it

`GET /api/graph` returns the whole snapshot, guarded like every other `/api`
route. A change is announced as `{"type": "graph.changed", "cursor": N}` and
the browser re-reads. No delta protocol: it is worth building when the graph is
big enough that re-reading hurts, and a *wrong* delta is a graph that quietly
disagrees with the ledger — the exact failure this design is arranged to avoid.

Layout is ELK's layered algorithm, because this is a provenance graph and
reads best as a flow — a job on the left, what it was run against to the right,
what it produced further right again. Force-directed would make the same data
look like a cloud with no direction in it. A node the user drags becomes a
fixed point fed back to ELK, so the rest lays out *around* it rather than
reflowing out from under it, and the graph is only re-laid out when its
*shape* changes — an identical snapshot must not make the map jump under the
cursor. Persisting those positions to `webui/state/graph-layout.json` behind
`GET`/`PUT /api/graph/layout`, written to a temporary file and `os.replace`d,
is §8.7.

### 8.7 Persisting what the user arranged

The layout is the one piece of state in this app the *user* authors, so its
failure modes are the ones worth spending care on.

**Conflict is a refusal that carries the answer.** Two tabs both hold a
revision and the second to save has a stale one. The choice was
last-write-wins or a 409, and last-write-wins loses the other tab's
arrangement without anyone finding out — the exact failure shape this whole
design avoids. So a stale revision returns **409 with the current layout in the
body**, and the client merges and retries. Positions are per node, so the merge
is well defined: per node the later write wins, and the file as a whole is
never clobbered. Neither a lost drag nor a visible error.

**Only this tab's own moves are re-asserted.** A merge that wrote back
everything the tab holds — including positions it merely *read* at load —
would silently undo another tab that has since moved one of them. So the client
tracks the nodes dragged in this tab since it loaded; the rest comes from the
copy the server just returned.

**The write is atomic and the unreadable is untouched.** Temporary file then
`os.replace`, because a kill mid-write otherwise leaves truncated JSON. An
unknown `version` reads as empty *and says so* rather than being overwritten:
a file a future version wrote is still somebody's work.

**Bounds refuse rather than truncate.** Non-finite coordinates, ±10⁶ range,
256-character ids, 5000 positions — each a 422 with its reason. A layout
silently missing the node just moved is worse than a save that explains itself.

### 8.8 What the view is for

The one rule the UI adds: **the evidence is on screen, not in a tooltip nobody
opens.** Each edge is labelled with its `evidence.field`, and selecting a node
lists what it points at and what points at it, each with the field, the value,
and either the ledger cursor or the words *from a file*. A graph whose
relationships have to be taken on trust is the thing this project spends its
effort not building.

An unresolved node is drawn with a dashed border and says so in the inspector,
because "the ledger named this and nothing has filled in its details" is a
different state from "this does not exist", and the difference is the one a
person debugging a gate actually needs.

## 8b. The shape of the screen

The first cut made the office one full-screen tab among four. That was wrong
about what the office is *for*. Four equal tabs make it a place you **go**; the
reason to have an animated office at all is that it is a place you **are** —
the state of the run on screen while you read about it. A tab you have to leave
to read the transcript is a tab you stop opening.

So the office is the stage, permanently, and the panels that explain it dock
beside it:

```text
+---------------------------------------+-------------------------+
|                                       | Agents  Map  Inbox  ⚙   |
|            OFFICE STAGE               |-------------------------|
|                                       |  the run, per agent,    |
|                                       |  as a conversation      |
+---------------------------------------+-------------------------+
                                        ^ draggable · swappable side
```

**Agents** is the panel that did not exist before, and it is the one that
answers "what is this agent doing and how far has it got". Its rules are in
`frontend/src/app/conversation.ts`, and all three are refusals:

* A turn ends at a **handoff**, because those are the platform's own turn
  boundaries. The grouping is the ledger's structure, not a shape imposed on it.
* A handoff **shows as a digest**. The stream carries
  `{iteration, agent, step_id, sha256}`, not the Plan's text, so the bubble
  says so rather than rendering an empty body. The Verdict is the exception —
  the daemon reduces it verbatim into the job result.
* A clipped payload **says it was clipped**. The recorder caps a runtime
  payload at 8 KB; the UI shows the head and that it is only the head.

The item shapes it reads (`agentMessage.text`, `commandExecution.command` and
`exitCode`, `fileChange.changes`, `subAgentActivity.agentPath`) are taken from
the captured App Server traffic in `agent-platform/spikes/app-server/captures/`.
A renderer written against guessed field names passes its own tests and draws
blanks against the real runtime.

One backend change came with it: `/api/jobs/{id}` now reads its events from the
**ledger** rather than the in-memory projection. The projection is capped so a
late browser draws something immediately; a transcript is a different question
and wants every row the job ever wrote.

Dock side, width and open state are a browser preference in `localStorage`.
They are not platform state, so putting them behind an API would make a
preference into a synchronised fact for no gain.

## 9. Phases

| Phase | Deliverable | State |
| --- | --- | --- |
| P1 | `wire.py` + `state.py` + `app.py`: one subscription, replayed, served over HTTP/WS | **built** |
| P1.5 | `security.py` guard, generated browser contract, backpressure that closes | **built** |
| P2-A | `graph.py` + `facts.py` + `/api/graph` + `graph.changed`: the pure projector | **built** |
| P2-C | React shell — Mind Map / Jobs / Office (placeholder) / Settings | **built** |
| P2-D | `GET`/`PUT /api/graph/layout`, atomic replace, reject-then-merge | **built** |
| P3 | asset export + manifest, vendored renderer, `office.py` with golden parity tests | after P2 |
| P4 | operator commands from the web, and artifact opening re-checked at click time | after P3 |
| P5 | `py -m ai_hardware_autolab.webui`: one command, token, browser, logs, licence screen | last |

P2 is ahead of P3 deliberately. The graph is the part that does not exist
anywhere yet; the office already exists and works in VS Code, so it is the part
whose absence costs the least while the rest is being built.

## 10. What this does not do

- It does not touch the ledger, the daemon, the loop or the contracts.
- It does not add a second approval path. Every command goes through
  `ControlPlane`, which is the same row a terminal approval writes.
- It does not bind off loopback, and it refuses to try. Authentication is
  **not** skipped — see §5 for why the earlier version of this line was wrong.
- It does not vendor Pixel Agents until P3, and when it does, `LICENSE` and the
  upstream commit travel with the copy.
