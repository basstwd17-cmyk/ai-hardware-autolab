# The standalone web UI

A second client of the daemon's protocol 1, in a browser, with no VS Code and
no extension host. The plan and the reasoning are in
`../docs/2026-08-27-webui-standalone-plan.md`.

## Run it

Two processes. The daemon is unchanged and is started the way it always was:

```powershell
py -3 -m QKD_PROJ.orchestrator                    # offline profile, default pipe
py -3 -m QKD_PROJ.webui.backend.app               # prints the URL, token included
```

The second command prints something like

```
open: http://127.0.0.1:8787/?token=Xh2...
token also written to C:\KIST\QKD_PROJ\webui\state\session.json
```

Open that URL. The token is generated per run, dies with the process, and is
required on every `/api` route and on `/ws` — see below for why a loopback
server still needs one.

Over loopback TCP instead of the pipe — what the tests use, and what works on a
machine that is not Windows:

```powershell
py -3 -m QKD_PROJ.webui.backend.app --daemon tcp://127.0.0.1:9701
```

`--static <dir>` serves a built frontend at `/`. While developing against Vite,
leave it off and allow the dev server's origin:

```powershell
py -3 -m QKD_PROJ.webui.backend.app --dev-origin http://127.0.0.1:5173
```

## What it serves

Read the methods off the app, not off this table — `app.openapi()["paths"]` is
the authority, and a test asserts these agree with it.

| Route | Answers from | Notes |
| --- | --- | --- |
| `GET /api/health` | the subscription | works with no daemon running |
| `GET /api/state` | projection + last `status` | `jobs` flattened, `snapshot` verbatim |
| `GET /api/events?since=&job=&limit=` | projection | `since` is a global `event_id` |
| `GET /api/jobs/{job_id}` | control plane + projection | job state, its result, its events |
| `GET /api/graph` | the graph projection | nodes, edges, and the cursor they cover |
| `GET /api/office` | the occupancy projection | who is at a desk **now**, and doing what |
| `POST /api/jobs/{id}/artifacts/{aid}/open` | the host | reveals in the file manager; never runs |
| `GET /api/graph/layout` | `webui/state/graph-layout.json` | where the user put things |
| `PUT /api/graph/layout` | same | 409 carries the current layout back to merge onto |
| `POST /api/command` | the daemon | the general authenticated operator command door |
| `POST /api/remote-approvals` | daemon preview + memory | creates one narrow, expiring mobile-control link |
| `GET/POST /remote-approval/{token}` | memory + daemon | decides that exact campaign review once |
| `GET /remote-approval/{token}/status` | control plane | shows only the job bound to that link |
| `POST /remote-approval/{token}/message` | daemon | records a steer for that bound job |
| `POST /remote-approval/{token}/cancel` | daemon | cancels that bound job |
| `WS /ws?after=N&token=` | everything | hello → snapshot → backlog → ready → live |

## Mobile campaign control

The Campaign tab can create a 12-hour control link after a completed
hardware mission has produced an installed contract.  It is deliberately not
the normal session URL: the link is bound to one `job_id`, one review SHA-256,
the displayed iteration/approval/time limits, and one resulting job. The
approval decision can be made once. Until expiry, the same link may read that
job's compact status, send `control.steer` messages, or cancel it; it cannot
select another job, contract, board, or tool. The server stores only the
token's SHA-256 in memory, so every outstanding link is invalidated when the
web process restarts.

The decision page offers two different operations:

- **approve + direction** activates the exact campaign and attaches the text
  as a durable steer to the newly created execution job;
- **request redesign** does not approve it. It opens a new mission revision
  whose Brief names the previous mission and carries the requested change.

The server still binds only to loopback. For a phone, expose that loopback
service privately inside a Tailscale tailnet using HTTPS (do not use Funnel):

```powershell
tailscale serve --bg http://127.0.0.1:8787
py -3 -m QKD_PROJ.webui.backend.app `
  --daemon "\\.\pipe\qkd-agent-web" `
  --static QKD_PROJ\webui\frontend\dist `
  --public-origin https://<lab-pc>.<tailnet>.ts.net
```

Install and sign in to the same tailnet on the phone, then create the link in
the Campaign tab. The backend returns the configured HTTPS form, so it opens
directly on the phone. `--public-origin` accepts only an exact HTTPS origin;
the server continues listening on `127.0.0.1`, and its Host/Origin checks admit
only that configured proxy name. Stop sharing with `tailscale serve off`.

An SSH port forward remains suitable for another computer:

```powershell
ssh -L 8787:127.0.0.1:8787 <windows-user>@<lab-pc>
```

Neither route exposes the named pipe or broad web session token. If the
contract, board profile, scenario, or limits change before approval, the
daemon refuses it and asks for a fresh review.

## Why a loopback server needs a token

`127.0.0.1` is not private from a browser's point of view, and the door at the
end of this one is `approval.decide`. Three attacks, three checks:

- **Cross-origin `POST`.** CORS governs reading the *response*, not running the
  *request*: a `text/plain` fetch is a simple request, so no preflight happens
  and the handler runs. → **bearer token**, on every route.
- **A WebSocket from any page.** Not subject to CORS at all; the browser sends
  `Origin` and expects the server to check it. → **`Origin` allowlist**.
- **DNS rebinding.** The attacker's own domain resolves to `127.0.0.1`, so
  their page is genuinely same-origin and `Origin` says whatever they like. The
  header that still tells the truth is the name dialled. → **`Host` allowlist**.

Present the token as `Authorization: Bearer <token>`, `X-QKD-Token`, or
`?token=` — the last is needed for `/ws`, because a browser cannot set a header
on a WebSocket handshake. That is also why uvicorn's access log is off: a
logged URL would be a logged credential.

The static bundle is served *without* the token. It is code, not data; the page
reads its token out of its own URL and every call it then makes is guarded.

## Rules this backend keeps

- **It never decides a job.** Job and approval state is read with the control
  plane's `status`, not rebuilt from the event stream. Two implementations of
  one state machine is one too many.
- **Artifact paths do not cross the WebSocket.** `orchestrator/results.py` says
  of the registration frame: *"Never sent to a browser."* This process is the
  host that resolves `(jobId, artifactId)`; the browser gets the id and the
  label. `state.browser_frame` is where that is enforced.
- **A cursor gap resubscribes.** It never interpolates and never advances past
  a hole. Duplicates are dropped, because replay is idempotent by design.
- **A slow browser is told, and then closed.** Its queue is bounded; on
  overflow the backlog is cleared, replaced by one `webui.resync_required`
  carrying the cursor and the count, and the socket is closed with 1008. It
  reconnects with `after=<snapshot cursor>`. Continuing to deliver would be
  trusting idempotence to cover an *omission*, which it does not — dropping the
  oldest frame silently would break the one-frame-per-cursor invariant while
  the tab kept rendering.
- **Loopback bind, exact proxy names only.** A wider bind is refused. A named
  HTTPS reverse proxy may be admitted explicitly with `--public-origin`.
- **A refusal is a 409, not a 500.** The daemon decided; the reason it gave is
  the thing worth showing.

## The browser contract

```
backend/models.py  --app.openapi()-->  frontend/src/api/openapi.json
                   --npx openapi-typescript-->  frontend/src/api/schema.d.ts
```

Regenerate after changing any response model:

```powershell
py -3 QKD_PROJ/webui/tools/export_openapi.py
npx openapi-typescript QKD_PROJ/webui/frontend/src/api/openapi.json `
    -o QKD_PROJ/webui/frontend/src/api/schema.d.ts
```

`export_openapi.py --check` is a test, so a stale committed schema fails rather
than quietly describing an older server.

## Tests

```powershell
py -3 -m pytest QKD_PROJ/tests/test_webui.py -q
```

They run a real `S.Service` over loopback rather than a double, for the same
reason `test_wire.py` does: both bugs this protocol has had were transport
bugs, and an in-memory double passes those.

## The graph

`GET /api/graph` returns the whole mind-map; `{"type":"graph.changed","cursor":N}`
arrives on the WebSocket when it moves and the browser re-reads. No delta
protocol until the graph is big enough that re-reading hurts — a wrong delta is
a graph that quietly disagrees with the ledger.

Three rules it keeps, all testable:

- **Identity, never similarity.** Every edge is an identifier one record holds
  and another holds too, and carries `evidence.field` naming where it was read.
  `evidence.source_event_id` is set only for edges a ledger row carried; one
  read from a contract or a run manifest says `null` rather than borrowing a
  cursor.
- **Pure.** `graph.py` is a function of (frames, `Facts`). Same input, same
  graph, same order — which is what makes replay from cursor 0 a recovery.
  `facts.py` is the only thing that touches `contracts/` or `runs/`, and tests
  inject `FrozenFacts` instead.
- **No paths.** An artifact's `targetPath` is read once, for the name of the run
  directory, and nothing else survives into the graph.

Two things the ledger turns out not to record, so the graph does not claim them:
`contract_sha256` (verified at `job.start`, then only inside the Brief payload,
which the stream carries as a digest) and the scenario text. So `job → contract`
is keyed on the contract **name** — which `ControlPlane.start` has already
forced to equal the preflight selection — and the Brief appears as
`brief:{sha256}`, unresolved, rather than as an invented `scenario` node.

## The frontend

```powershell
cd QKD_PROJ/webui/frontend
npm install
npm run build          # tsc -b && vite build -> dist/
npm test               # vitest: the token and the socket contract
npm run types          # regenerate schema.d.ts from openapi.json
```

Serve the built bundle from the backend, which is the normal way to run it:

```powershell
py -3 -m QKD_PROJ.webui.backend.app --static QKD_PROJ/webui/frontend/dist
```

Or run Vite against a live backend. The dev server proxies `/api` and `/ws`, so
the page stays same-origin; the backend still has to be told that origin is
allowed, because the proxy forwards the WebSocket handshake with its own:

```powershell
py -3 -m QKD_PROJ.webui.backend.app --dev-origin http://127.0.0.1:5173
npm run dev
```

### The office holds the middle; everything else docks beside it

```text
+-----------------------------------------------------------------+
| RTASE agent platform          live · cursor 13   \.\pipe\...  |
+---------------------------------------+-------------------------+
|                                       | Agents  Map  Inbox  ⚙   |
|                                       |-------------------------|
|            OFFICE STAGE               | [job_c66..] runnable    |
|         (P3 renderer lands here;      |-------------------------|
|          desks and agents until then) | [human]                 |
|                                       |   opened the job        |
|                                       | [gate-architect]        |
|                                       |   said: "Reading the…"  |
|                                       |   → Plan  sha f662fd…   |
|                                       | [design-builder]        |
|                                       |   ran: …cpld build (0)  |
|                                       |   changed 2 files       |
+---------------------------------------+-------------------------+
                                        ^ drag to resize · ⇤ to swap sides
```

The office is the stage, permanently — the point of it is that it is a place
you *are* while you read about the run, not a tab you go to. The dock beside it
holds four panels:

| Panel | What it is |
| --- | --- |
| **Agents** | the run as a conversation, per agent, out of the ledger |
| **Map** | the same work as a graph, each edge showing its evidence |
| **Inbox** | approvals waiting on a person, and what this host refused |
| **Settings** | what this page is attached to and what it can claim |

The divider is draggable, `expand` gives the dock the whole width when the map
wants room, `⇤`/`⇥` moves it to the other side. Side, width and open state are
a browser preference in `localStorage` — not platform state, so not an API.

The connection line is always visible above all of it. Everything below is a
projection of a stream, so *is this current?* has to be answerable without
clicking anything: it names the daemon, the applied cursor, whether the socket
is live, and — when it has happened — that this page fell behind and re-read
rather than quietly carrying a hole.

### The conversation, and what it will not claim

A turn ends at a **handoff** — `Plan`, `StepResult`, `Verdict`, `Report` are
the platform's own turn boundaries, so the grouping is the ledger's structure
rather than a shape imposed on it. Inside a turn, the `runtime.*` rows are what
the agent actually said and ran; the item shapes (`agentMessage.text`,
`commandExecution.command`/`exitCode`, `fileChange.changes`) come from the
captured App Server traffic under `agent-platform/spikes/app-server/captures/`,
not from a guess.

Three refusals are built in:

- **A handoff shows as a digest.** The stream carries
  `{iteration, agent, step_id, sha256}` — not the Plan's text. So the bubble
  says it handed off a Plan and shows the digest, and says why there is no
  body. The Verdict is the exception, because the daemon reduces it verbatim
  into the job result.
- **A clipped payload says it was clipped.** The recorder caps a runtime
  payload at 8 KB and replaces it with `{truncated, bytes, head}`; the UI shows
  the head *and* that it is only the head.
- **`job.done` is a row, not a conclusion.** Whether a job is finished is the
  control plane's answer, shown as a pill from `status`. Nothing here reads an
  event and decides.

The transcript is read from `/api/jobs/{id}`, which goes to the **ledger**
rather than the in-memory projection: the projection is capped so a late
browser has something to draw immediately, and a transcript wants every row the
job ever wrote.

### What the page does with the token

It arrives as `?token=` because that is the only way to hand a credential to a
page that has not loaded yet. The page then takes it out of the address bar
with `history.replaceState` (a token in `location.search` ends up in the tab
title, in screenshots, and in any `Referer`) and keeps it in `sessionStorage`,
which has the same lifetime as the token itself — the server generates a new
one per run. `index.html` sets `referrer: no-referrer` for the first load,
before any of that JavaScript has run.

Deciding an approval is P4. Until then the Jobs view shows the terminal command
rather than a button that does nothing.

## The layout, and the only state the user authors

Everything else this app holds is derived and can be thrown away and rebuilt
from the ledger. A position is a judgement about what belongs next to what, so
`webui/state/graph-layout.json` is treated more carefully than its size
suggests.

- **Conflict is a refusal, not last-write-wins.** Two tabs both hold a
  revision; the second to save has a stale one. Last-write-wins would discard
  the other tab's arrangement without anyone finding out, so a stale revision
  is a **409 that carries the current layout back**. The client merges its own
  moved nodes over that copy and retries — positions are independent per node,
  so per node the later write wins and the file as a whole is never clobbered.
  The user sees neither a lost drag nor an error.
- **Only this tab's own moves are re-asserted.** The subtle half: a merge that
  wrote back every position the tab is holding — including ones it merely read
  at load — would silently undo another tab that moved one of them since. So
  the client tracks the nodes dragged *in this tab since it loaded*, and
  everything else comes from the copy the server just returned.
- **The write is atomic.** Temporary file in the same directory, then
  `os.replace`. Writing in place means a process killed mid-write leaves
  truncated JSON, and the next start finds the layout not stale but unreadable.
- **A file it cannot interpret is not overwritten.** An unknown `version`, or
  JSON that is not a layout, makes the read return an empty layout *and say so*
  in `unreadable`, which the UI shows. Treating an unreadable file as absent
  would destroy whatever a future version wrote there.
- **Bounds are refusals, not truncations.** Non-finite or out-of-range
  coordinates, non-string ids, ids over 256 characters, more than 5000
  positions: all 422 with the reason. A layout silently missing the node the
  user just moved is worse than a save that says why it did not happen.

## The office, and what it refuses to conclude

`GET /api/office` is a **separate projection from the graph**, and the
difference is the point. The graph holds every agent that ever appeared;
drawing the office from it left a finished job's agents standing at their desks
forever, which looks like work and is not.

- **Occupancy is the control plane's answer.** A desk empties when its job stops
  being open, read from `status`. Nothing reads `job.done` and concludes
  anything — `test_a_desk_is_emptied_by_status_and_never_by_an_event` is that
  rule with teeth.
- **A `runtime.*` row is an animation and nothing else.** It moves a character
  and writes to `activity`. It is not evidence, not a verdict, not an approval,
  and an approval *request* animates a character without saying it was granted.
- **A human row makes no character.** `job.created` has no agent and the Brief
  is handed off by `human`; putting an avatar there would invent a participant
  the ledger does not have.
- **Pure, and idempotent in itself.** Same frames plus same status gives the
  same room. `seenAt` is an `event_id`, never a clock, and a row at or below the
  applied cursor is not folded twice — a `turns` counter that ran again on a
  replayed row would make the room a function of how often the socket dropped.
  `test_a_full_replay_and_a_mid_cursor_reconnect_agree` pins that.

## Vendored assets

`webui/assets/` is a **copy** of the Pixel Agents art, not a link into
`agent-platform/`. A sibling checkout can be updated, moved or deleted; a
server that read its sprites from there would render differently depending on
what happened next door.

```powershell
py -3 QKD_PROJ/webui/tools/export_assets.py            # export, once
py -3 QKD_PROJ/webui/tools/export_assets.py --check    # re-hash; fails on drift
```

`assets/MANIFEST.json` records the upstream repository, version 1.4.1, the
pinned commit, the MIT licence and its copyright line, the character pack's
separate attribution (JIK-A-4, Metro City), a SHA-256 and size per file, and one
rolled digest over the tree. It also records **whether the assets drifted from
the pin** — the fork carries its own commits, so "these came from 1.4.1" is a
claim, and the export asks git rather than asserting it.

Nothing is fetched at runtime. The backend mounts `webui/assets/` at
`/office-assets`, inside the guard so the `Host` check applies and outside the
token check because sprites are media rather than data about this run. A test
walks the frontend sources looking for a fetch to any host and fails on one.
