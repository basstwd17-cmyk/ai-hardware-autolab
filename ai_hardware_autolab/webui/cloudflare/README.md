# Cloudflare Pages remote control

This deploys the remote project control surface to a stable `pages.dev` URL.
Pages Functions and D1 are a command mailbox. They never run an agent and do
not become the source of truth: the local Python relay sends every command to
the existing named-pipe daemon, and the local SQLite ledger remains decisive.

## What crosses the boundary

The knowledge map is `/knowledge.html`, linked from Remote control. The relay
also copies knowledge events (statements, scope, source jobs, evidence metadata)
to a device-scoped D1 replica. The existing operator token reads it; the device
token appends replication batches. Locators may be included, but referenced file
contents and credentials are not uploaded. The local DB remains authoritative.
Apply `schema.sql` on upgrade to add the two knowledge tables, deploy Pages,
then restart daemon and relay. Saved terminal conversation results are caught up
without model/device calls. Offline views show the last synchronization state.

- outbound: compact job state, path-free result/evidence cards, pending approval
  details and ledger cursor;
- inbound: one allowlisted command (`mission.start`, pending approval accept/decline, steer or cancel);
- never: arbitrary shell commands, MCP calls, login files or
  the local web session token.

A cloud command moves `pending -> claimed -> completed`. A claimed command is
not automatically retried. A relay crash after a hardware effect but before
the HTTP response is ambiguous, so silent retry would be less safe than a
visible `claimed` row.

## One-time Cloudflare setup

Build the static site:

```powershell
cd C:\KIST\ai_hardware_autolab\webui\frontend
npm run build
```

Authenticate and create the Pages project and D1 database:

```powershell
cd C:\KIST\ai_hardware_autolab\webui\cloudflare
npx wrangler login
npx wrangler pages project create rtase-agent-platform
npx wrangler d1 create rtase-relay
```

Copy `wrangler.example.jsonc` to the git-ignored `wrangler.jsonc`, then replace
`REPLACE_WITH_D1_DATABASE_ID` with the id printed by `d1 create`.

Create the remote tables:

```powershell
npx wrangler d1 execute rtase-relay --remote --file=./schema.sql
```

Generate two independent secrets from `C:\KIST`:

```powershell
cd C:\KIST
py -3 -m ai_hardware_autolab.orchestrator.cloud_relay --generate-token
py -3 -m ai_hardware_autolab.orchestrator.cloud_relay --generate-token
```

Keep each raw `token=` value. Store only the corresponding digest in Pages:

```powershell
cd C:\KIST\ai_hardware_autolab\webui\cloudflare
npx wrangler pages secret put OPERATOR_TOKEN_SHA256 --project-name rtase-agent-platform
npx wrangler pages secret put DEVICE_TOKEN_SHA256 --project-name rtase-agent-platform
```

Paste the first `sha256=` for the operator secret and the second for the
device secret. Deploy from this directory so Wrangler sees both `functions/`
and the frontend output:

```powershell
npx wrangler pages deploy ../frontend/dist --project-name rtase-agent-platform
```

Cloudflare prints the stable site address. Pages Functions require a Wrangler
or Git-integrated deployment; dashboard drag-and-drop of only `dist/` would
omit the relay API.

## Start the laboratory side

Start the normal daemon first. Then put the raw **device** token in the current
PowerShell process and start the outbound bridge:

```powershell
cd C:\KIST
$env:QKD_RELAY_DEVICE_TOKEN = "<raw device token>"
py -3 -m ai_hardware_autolab.orchestrator.cloud_relay `
  --url "https://rtase-agent-platform.pages.dev" `
  --device-id "lab-pc" `
  --daemon "\\.\pipe\qkd-agent-web"
```

Open the Pages address on a phone. Enter `lab-pc` and the raw **operator**
token. The browser keeps it only in that tab's `sessionStorage`.

The phone shows only approvals that already exist as `approval.pending` in the
local ledger. Accept or decline sends the structured `approval.decide` command
back through the named pipe; the local daemon records the signer and note. The
cloud cannot invent or broaden an approval scope.

Each job card is projected from the local Plan, StepResult, Verdict, Report and
scheduler state. It includes deterministic condition counts, bounded recent
handoff summaries, the next planned action and undelivered steering. Full
handoff payloads, local artifact references and filesystem paths never enter
the D1 snapshot.

The laboratory PC and daemon must remain on. If they are offline, the site is
still reachable but clearly reports `OFFLINE`; commands may wait for up to ten
minutes and then expire without being executed.
