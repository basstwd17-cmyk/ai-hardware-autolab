# The service: a process, a pipe, a timer, and what a person can say to it

**Date:** 2026-08-21
**Status:** built and verified offline, including against a real out-of-process
Node client over a Windows named pipe.
98 tests (`test_service.py` 36, `test_wire.py` 54, `test_ui_bridge.py` 8).
**Untouched:** `orchestrator/jobs.py` (frozen at `bee8e43`), `orchestrator/codex_runner.py`.
**Follows:** `2026-08-21-agent-platform-p2-daemon.md`

## 1. One instance, held by the kernel

Two daemons on one ledger would both pick the same runnable job and both call
`advance()` on it. SQLite keeps the *writes* consistent — the handoff
uniqueness constraint refuses the second — but both would already have spent a
turn on the runtime to find that out.

So the lock is an **OS file lock held for the process lifetime**, not a pid
file. A pid file survives the crash that made it stale, and the next start
either refuses for no reason or has to guess whether a pid belongs to the
daemon or to whatever reused the number. A lock is released by the kernel when
the holder dies, which is the property actually wanted. `msvcrt.locking` on
Windows, `fcntl.flock` elsewhere.

## 2. A timer that sleeps for the right length of time

The scheduler already knows when every waiting job becomes due, so
`sleep_for()` derives the delay from the survey rather than using a fixed
interval. A job retryable in 90 seconds does not need seventeen five-second
wake-ups to notice.

There is still a ceiling (30 s). A job's state can change underneath this
process — an approval granted through the pipe by a person — and that should be
noticed without waiting on somebody else's retry clock.

`tick_once()` calls `daemon.tick()`, which calls `advance()` exactly once. The
chain holds all the way down: one call, one step, everything else in the ledger.

## 3. A pipe, with an ACL on it

`\\.\pipe\qkd-agent-platform`, created with `CreateNamedPipeW` through ctypes — no
new dependency. A fixed localhost port is the specific mistake in Connect AI
worth not repeating: any page in any browser can post to localhost.

**The first version overstated its own security, and this section repeated the
overstatement.** It created the pipe with a null `lpSecurityAttributes` while
the text claimed "reachable by the user who owns it" — which that code did not
establish. A default pipe DACL grants the creator *plus* LocalSystem and
administrators, and says nothing at all about clients arriving over a share.

What is true now:

| control | effect |
|---|---|
| explicit DACL `D:P(A;;GA;;;<logon SID>)` | only this logon's account may open it; `P` protects it so nothing is inherited in |
| `PIPE_REJECT_REMOTE_CLIENTS` | a client reaching the name across a share is refused |
| `FILE_FLAG_FIRST_PIPE_INSTANCE` | if another process already owns the name we fail at start, rather than becoming a second server for its clients |

An administrator can still take ownership. That is a property of the OS rather
than something this can prevent, and it is worth saying instead of implying
otherwise.

The first instance is created inside `open()`, so a bad name, a denied ACL or
an already-owned name fails the **start**. The earlier version discovered that
on the accept thread and the service went on to announce itself as running with
no transport behind it. Measured, not assumed: a name someone else owns comes
back as `ERROR_ACCESS_DENIED` (5), not `ERROR_PIPE_BUSY`; a name with a
shell-eaten backslash comes back as `ERROR_INVALID_NAME` (123).

A loopback-TCP transport exists as a fallback for other platforms and for the
tests that need to run anywhere. It is the fallback, not the default, and the
named pipe has its own test on Windows because that is the transport that
ships.

Two ctypes details cost a debugging round and are worth recording. `HANDLE` is
pointer-sized, so without `restype = wintypes.HANDLE` ctypes truncates it to a
32-bit int on 64-bit Windows and a perfectly good handle looks invalid. And
`GetLastError` is meaningless without `WinDLL(..., use_last_error=True)` — a
failed `CreateNamedPipe` came back as "error 0" until that was set.

`ConnectNamedPipe` blocks until a client arrives, so stopping the service
connects to its own pipe once to wake the accept loop. That is the standard way
out; killing the thread leaves a pipe instance behind.

**Reads are gated by `PeekNamedPipe`, and that is load-bearing.** A pipe handle
created without `FILE_FLAG_OVERLAPPED` is synchronous, and Windows serialises
I/O on one: while a `ReadFile` is pending, a `WriteFile` from another thread
queues behind it. The office subscribed, got its replay, and then never
received another frame — while the identical code over TCP worked, because
sockets have no such rule. Peeking first means the reader never holds a pending
operation, so the pusher's writes go out immediately. The cost is a 20 ms poll
on the command path, which is nothing for a control channel.

## 4. What a person can say

One request object in, one response object out, newline-delimited JSON. The
command surface is transport-free, so the pipe, a test and a future UI are the
same code path.

| command | effect |
|---|---|
| `ping` `status` `job` `events` `inbox` | read; `events` takes an `event_id` cursor |
| `approve` | records a decision |
| `cancel` | withdraws outstanding requests, then closes the job as `cancelled` |
| `steer` | leaves a message for whichever agent runs next |
| `shutdown` | stops the service |

Each of the three that change something has a rule that is not obvious.

**`approve` authorises nothing by itself.** It records what a person said. The
gate in `daemon.py` reads that decision back and re-hashes the artifacts before
a board-touching call goes through, so a grant is a decision, not a key. A test
grants an approval through the control plane and then watches the gate refuse
it anyway, because it was bound to no digest.

**`cancel` withdraws before it closes.** A granted approval left behind on a
cancelled job is a signature with nothing attached to it, and the next thing to
quote it would be quoting a decision nobody made about that.

**`steer` is delivered, not stored as a setting.** The message goes into the
log, and the loop hands the agent every steer since its last handoff. Delivery
is therefore derived: the handoff that follows is the proof it was seen. A
delivered-flag column would be a second record of the same fact, and the two
would part company the first time a process died between them.

## 5. Recording what the runtime did

`CodexAgentRunner` takes an `event_sink`. `RuntimeEventRecorder` fills it and
writes each notification into the job's log.

**The names stay the runtime's.** A notification arrives as `method` and is
recorded as `runtime.<method>`. The first version of this section named
`runtime.commandExecution` and `runtime.fileChange`, which do not exist: the
capture under `agent-platform/spikes/app-server/captures/` shows the methods are
`item/started` and `item/completed`, and `commandExecution`/`fileChange` are
*item types* inside their payload. So the real kinds are
`runtime.item/started`, `runtime.item/completed`, `runtime.mcpToolCall`,
`runtime.subAgentActivity`, `runtime.turn/diff/updated` and the rest, and the
item type is lifted to `payload.item_type` so a projector does not have to dig
for the one field that separates a command from an edit.
Renaming Codex's vocabulary into one of our own would put a translation table
between what happened and what is written, and the table would be wrong the
first time a vendor added a method. The namespace exists only so a runtime
event can never collide with `job.*`, `handoff.*` or `approval.*` — a test
feeds the recorder a notification literally named `job.done` and checks it
lands as `runtime.job.done`.

**They are observability, not evidence.** Nothing decides a gate from these;
the evidence chain is `runs/` and the handoffs.

Be precise about what the buffer buys, because the first version of this
section was not. `JobLedger.append_event` is one transaction per event with
`synchronous = FULL`, and the ledger is frozen — so **buffering does not reduce
the number of fsyncs.** It only moves them out of the runtime's callback. What
actually reduces write volume is the exclusion list: a real captured Codex turn
emitted 258 `item/agentMessage/delta` notifications out of ~600, and those
per-token streams are excluded by default because the content arrives whole in
the `item/completed` that follows.

**The sink has no idea which job it is for.** It is set once on a runner reused
across calls, so the context comes from wrapping the runner. A notification
that arrives with no job in context is **counted, not filed** — attributing it
to whichever job ran last would be worse than losing it.

Payloads over 8 KB are clipped, and a `methods` allowlist exists for a caller
that wants the log smaller. Both are volume controls, and neither is on by
default, because the first question a UI asks is what the runtime actually
sent.

## 6. The office UI protocol

`wire.py` implements the contract in
`agent-platform/docs/platform-integration.md`. The office connects once and
stays connected:

```
->  {"type":"subscribe","protocol":1,"after":42}
<-  {"type":"ledger.event","event":{ ...Event.describe()... }}   x N
<-  {"type":"ready","cursor":44}
<-  ...every later ledger row, pushed on the same connection
```

Four rules from the contract shape the implementation.

**`ready.cursor` must equal the last event sent.** The office treats a
disagreement as an error, because otherwise it could claim to be current while
missing durable history. So the tip is read *after* the replay and drained
again if it moved, looping until the ledger stops moving — then the two agree
by construction rather than by luck.

**One frame per global cursor, unfiltered.** Job screens filter after receipt.
Asking the ledger for one job would skip cursors belonging to others and trip
the office's gap detector on every one.

**Operator actions carry a `commandId` and get a `command.result`.** They are
typed differently from events so a direction message can never be read as an
approval signature.

**The decision is a word, not a boolean.** The office sends
`"accepted"`/`"declined"`, and this is the only place that becomes `True`/
`False`. `ControlPlane.approve` now takes a real JSON boolean and refuses
anything else — because `bool("false")` is `True` in Python, and a request
carrying `"granted": "false"` would otherwise have recorded a grant against a
decline. An approval decided by a type coercion is not a decision.

The one-shot command API still works on the same pipe; frames are told apart by
whether they carry a `type` (office) or a `command` (CLI), so a terminal can
poke a daemon a UI is attached to.

## 7. Steering delivery, corrected

The first version cleared pending steers on any `handoff.*`. A `Verdict` is
written by `EvidenceReducer` — deterministic code that never reads a task, let
alone a steering list — so a message sent while the loop sat between "every
step reported" and "verdict recorded" was marked delivered by a handoff nobody
read it into. The words vanished and the next agent planned as though they had
never been said.

Only `handoff.Plan` and `handoff.StepResult` count as delivery now: those are
produced by agents that were handed `task["steering"]`.

The reporter is deliberately **not** given steering. A report describes what
happened, and letting a message shape it after the evidence is in is the
softening the reporter's own definition forbids. Undelivered messages reach it
as `undelivered_steering` — context to write down under its own heading, not
direction. `steer` also answers with `willBeDelivered`, so a person is told
when their message will reach the record rather than the next agent.

## 8. Reading a history that outgrew its cap

Every "recent history" question in this package was asking
`events_since(0, job_id, limit=10_000)`, which returns the **oldest** ten
thousand — the opposite of what it needed. It went unnoticed because nothing
produced that many events per job, and the runtime recorder was about to: one
captured Codex turn is several hundred notifications.

`pagination.py` pages forward until the ledger stops returning rows, and each
helper holds only the tail it needs rather than the whole history. The ledger
is frozen, so the fix belongs on this side of it.


## 9. Verified

```
py -3 -m pytest ai_hardware_autolab/tests/test_service.py -q     36 passed
py -3 -m pytest ai_hardware_autolab/tests/test_wire.py -q        54 passed
py -3 -m pytest ai_hardware_autolab/tests/test_ui_bridge.py -q    8 passed (real node)
py -3 -m pytest ai_hardware_autolab/tests/test_wire.py -q        54 passed
py -3 -m pytest ai_hardware_autolab/tests/test_ui_bridge.py -q    8 passed (real node)
py -3 -m pytest ai_hardware_autolab/tests -q                     625 passed, 1 failed, 1 skipped
```

The named-pipe test runs for real on Windows and is skipped elsewhere. The
single failure is the pre-existing gate9 allowlist hash guard.

## 11. What an external bridge found, and what it did not

A real Node bridge killed the accept loop on its first connection —
`ServiceError: ... error 5` creating the second pipe instance, then EPIPE and a
repeating ENOENT at the client, which is what a dead accept loop looks like from
outside: the name has no instance left in the listening state.

**It has not been reproduced here.** Eight sequential Node reconnects, four
concurrent Node clients, and a CLI connecting while a Node client held the pipe
all pass, before and after the change. That is stated rather than smoothed over,
because "fixed" and "not reproducible" are different claims and only one of them
is true.

What changed is the structure that made the report plausible:

- **`first` is a parameter, not remembered state.** The flag that fails with
  ERROR_ACCESS_DENIED when the name exists is exactly the flag that must apply
  once. It used to be governed by a mutable `self._first`; now the only caller
  that means it passes it, and there is no value left to get wrong.
- **The accept loop no longer dies on a creation failure.** It records the
  fault, backs off, and retries, giving up only after twenty consecutive
  refusals. A loop that exits takes the pipe name with it, which turns a
  transient fault into ENOENT forever — the exact symptom reported.
- **There is no longer a window with nothing listening.** The next instance is
  created *before* the current one is handed to a connection thread. Between
  handing off and creating, the name had no listening instance, and a bridge
  reconnecting in that window gets ENOENT. This one is a real defect regardless
  of the report.
- **`transport.warnings` records what was survived.** Empty is the healthy
  state; the regression tests assert it.

`tests/test_ui_bridge.py` runs a real `node` process against the pipe, because
every in-process test passed while the bridge failed: a client in this
interpreter shares its GIL and its scheduling, and that is precisely the
difference the earlier `PeekNamedPipe` bug lived in too.

## 12. Steering delivery, and what a person is promised

`will_be_delivered` used to mean "the job is not finished", which promised
delivery for a job whose loop had already decided. It now reads the current
iteration's verdict: `open` means an architect will be handed the message,
anything terminal means only the reporter is left — and the reporter takes no
direction. The iteration ceiling ends delivery too, which is knowable only with
the loop's policy, so it is a parameter rather than a guess.

## 13. Results and artifacts

`job.result` is the daemon saying what it holds, sent after `ready` and after
any later Verdict or Report — never inside the replay, because the contract
counts one frame per global cursor and a result is not a cursor.

Nothing in it is authored. The counts are arithmetic over the reducer's
conditions; the summary quotes the outcome and the failing field names; the
verbatim verdict travels with its digest so the office never has to take the
summary's word for anything.

`artifact.registered` carries paths on its own frame, so a browser message
never contains one. Each candidate is resolved, confirmed to exist, and
confirmed to sit inside the project root before it is offered — a registration
for a missing file is a button that fails. A repeat of the same id and path is
silently idempotent; re-pointing an id at a different path is refused and
recorded, because the contract calls that an error and the daemon should not be
the one committing it.

## 14. What is still missing

- **Nothing installs the service.** There is no Windows service registration,
  no autostart, no log rotation. `py -3 -m ai_hardware_autolab.orchestrator` runs it and
  `--dry-run` wires everything without starting an agent; making that survive a
  logout is a packaging question.
- **No authentication beyond the pipe's ACL.** Anyone who can open the pipe can
  approve a hardware step. The DACL now limits that to this logon's account by
  construction rather than by default, but if the daemon ever runs as a
  different account than the person approving, that needs a real token before
  it does. The UI contract already anticipates this ("only a token-authenticated
  local UI may send these commands").
- **The recorder has still never run against a live App Server.** Its method
  names and payload shapes now come from the recorded capture rather than from
  a guess, which is a real improvement, but a replay of a file is not a turn.
- **The reported accept-loop failure is still unexplained.** The structure that
  would produce it is gone and the regression tests cover the shape of it, but
  nobody has seen it happen here. If the bridge hits it again, `transport.warnings`
  and `transport_error` are where the evidence will be.
- **One client at a time is no longer true, and untested at scale.** Each
  connection gets its own thread and its own pipe instance; two UIs and a CLI
  work in principle and only two have been exercised.
