# The daemon: which job moves next, and what a hardware step may do

**Date:** 2026-08-21
**Status:** built and verified offline against a canned runner. 39 tests.
**Untouched:** `orchestrator/jobs.py` (frozen at `bee8e43`), `orchestrator/codex_runner.py`,
the runtime event vocabulary, the UI.
**Follows:** `2026-08-21-agent-platform-p2-loop.md`

## 1. One step per tick, and nothing in memory

`Loop.advance()` does one step. The daemon decides *which* job and *when*, and
its `tick()` calls `advance()` exactly once. A daemon that drained a job to
completion inside one tick would hold that job's progress in a stack frame, and
every argument for deriving the loop's phase from the ledger applies to the
schedule too.

Everything it needs is derived:

| question | derived from |
|---|---|
| which jobs can move | `jobs`, and their states |
| which wait on a person | `pending_approvals` |
| which wait on a clock, until when | the trailing run of `job.blocked` events whose reason begins `rate_limited:` |

The retry schedule in particular is **not stored**. A column saying "retry at
06:31" would need to survive the same crash the rest of this design refuses to
trust memory for, and it would be a second thing that can disagree with the
log. Counting the consecutive rate-limit blocks and multiplying gives the same
answer from evidence that already exists.

One consequence worth stating: the daemon's clock has to be the clock the
ledger stamps with. It compares its `now` against timestamps `JobLedger` wrote,
so a daemon on a skewed clock retries early or late. The tests take their `now`
from the ledger's own events for that reason.

## 2. Three ways to be stopped, and why they are not one way

| reason | who unblocks it |
|---|---|
| `needs_approval` | a person. No clock helps. |
| `rate_limited` | a clock. Nothing else helps. |
| `runtime_failure` / `invalid_handoff` | a person. Retrying just repeats it. |

Classifying wrongly is asymmetric, and that decides how the classification is
written. Calling a rate limit a runtime failure blocks a job until someone
notices — annoying, safe. Calling a runtime failure a rate limit retries
forever, quietly. So:

- an explicit `RateLimited` exception always wins,
- the message heuristic (`429`, `rate limit`, `quota exceeded`, …) is a
  documented fallback for runners that raise a plain error,
- and it is **bounded**: after `max_rate_limit_retries` consecutive rate limits
  the job needs a person, because a runtime that has been briefly throttled six
  times in a row is not briefly throttled.

The loop gained one hook for this — `classify_failure`, defaulting to "every
runtime failure is permanent". The loop knows a call failed; only the caller
knows whether this runtime's failures come back on their own.

### The bug that rule was written to catch

The first version of `rate_limit_attempts` broke its backward walk on any
`job.*` event. The scheduler writes a `job.running` resume between every two
retries, so the count reset every time: backoff never grew past the first step
and the retry ceiling could never be reached. A runtime that was simply broken
would have looked briefly throttled forever — exactly the failure the ceiling
exists to prevent. Only *progress* resets the count now: a handoff recorded, an
iteration opened, an approval answered. A test pins it.

## 3. The hardware approval gate

`CodexAgentRunner` takes a `hardware_approval(agent, task, tool, params) -> bool`
callback and calls it the moment a turn asks for a board-touching MCP tool.
Returning `True` lets that one call through; anything else makes the runner
raise, the loop blocks the job, and a person is back in the path.

`HardwareApprovalGate` answers from the ledger and the disk. Two things must
both hold:

1. **A person granted it** — for this job, this contract, this step. A pending
   request is not a yes; a denial is not a "not yet"; a withdrawal revokes.
2. **The artifacts still hash to what they signed.** A scope in words — "one
   volatile SRAM program of the pinned candidate" — cannot distinguish the
   bitstream that was reviewed from one rebuilt afterwards at the same path. A
   digest can, and re-computing it at the moment of use is the only point where
   that check is not already stale.

Where the approval names no artifacts, the contract's pinned candidate is used
instead — the same block the evidence audit checks, so what is verified here is
what a reviewer looked at. Where neither names any, the answer is **no**: an
approval bound to nothing is a signature on a description, and this callback
exists precisely because a description is not enough.

Two smaller refusals: a reference that resolves outside the project tree, and
any error inside the check itself. A check that could not complete has not
passed.

The gate never grants. It reads a decision a person already made.

## 4. What it does on start

`recover()` reports; it does not repair.

- `expire_overdue()` settles requests whose deadline passed while nothing was
  running — a queue that quietly became grantable again is what an expiry is for.
- Jobs left `running` are **reported and left alone**. Whether their step
  reached the board is unknown from here, and nothing may assume it did not.

## 5. Boundaries

No new event kind. Every event the daemon causes comes from a `JobLedger`
method, and a test asserts the kinds produced are a subset of the ledger's own
— the same guard the loop carries.

`codex_runner.py` is untouched. The daemon supplies the callback that file
already accepts and classifies the exceptions it already raises.

## 6. Verified

```
py -3 -m pytest ai_hardware_autolab/tests/test_daemon.py -q     39 passed
py -3 -m pytest ai_hardware_autolab/tests -q                    527 passed, 1 failed, 1 skipped
```

The single failure is the pre-existing gate9 allowlist hash guard, which the
evidence audit reports as `pinned_file_drift`.

## 7. What is still missing

- **Nothing calls `tick()` on a timer yet.** This is the scheduler, not the
  process that hosts it. A service wrapper, a named pipe, and the process
  lifecycle are the next layer, and the UI's replay bridge attaches at
  `events.event_id`, which the status payload already carries.
- **The gate has never refused a real runtime.** Every test drives the callback
  directly. The first live hardware step is where the two halves meet, and it
  needs a person at the bench regardless.
- **`run_bbb` effect classification is unsigned.** Until
  `contracts/proposals/2026-08-21-gate9-effect-class.patch` is reviewed, a
  `--role measurement` server can run no BBB script, which is the intended
  fail-closed state and not a bug to work around.
