# Agent platform, phase 2: the daemon's memory

**Date:** 2026-08-21
**Status:** ledger built and verified offline. Runtime adapter and UI not started.
**Follows:** `2026-08-21-agent-platform-p1-agent-definitions.md`

## 1. What had to outlive the process

P1 closed with three gaps, all of the same shape: state that only existed
inside an agent's context. `ConsumptionLedger` already solved the equivalent
problem for approvals — reserve before acting, insert only, enforce the budget
with a database constraint rather than a counter someone remembers to check —
and this is that design applied to the loop itself.

| Table | Holds | Fills P1 gap |
|---|---|---|
| `jobs` | goal, state, iteration | #2 iteration state outside the agents |
| `events` | append-only log, monotonic id | the UI's reconnect cursor |
| `handoffs` | Brief/Plan/StepResult/Verdict/Report, verbatim + digest | #1 the Plan sink |
| `approvals` | scope, artifact digests, decision | #3 the approval queue |

Four tables, not seven. `agent_turns`, `evidence_links` and
`failure_signatures` were on the list and are all derivable from `events` and
`handoffs`. Guessing three more schemas before the first job runs is three more
chances to guess wrong, and a derived value cannot disagree with its source.

## 2. Three decisions

**The daemon owns the state, not the model.** `iteration` is advanced by
`begin_iteration()`, the failure signature rides on the Verdict handoff, and
the strike count is derived. A planner that keeps its own counter loses it at
the first rate-limit window, and a loop that forgets it already failed is a
loop that runs forever. The agent is told what iteration it is on; it is never
asked.

**The event log is the platform's clock, not the runtime's.** A UI reconnects
at `events.event_id` and replays forward. Whatever replay Codex's App Server or
`claude -p --output-format stream-json` offers is a convenience; correctness
cannot depend on it, because the log has to stay readable after that runtime is
swapped out. `event_id` is a global autoincrement; `seq` is per job. Both,
because a UI tails everything while a job view needs its own ordering.

**A state change and the event explaining it commit together.** `transition()`
writes the row and the event in one transaction. If they could be written
separately a job could sit in a state no event accounts for — and the log,
which is the thing everyone reads, would be lying by omission. A refused
transition writes nothing at all; there is a test for that.

## 3. What the schema enforces, rather than the code

```sql
UNIQUE (job_id, seq)                          -- two writers cannot claim one sequence
UNIQUE (job_id, iteration, kind, agent, step_id)  -- one Plan per iteration
CHECK  (outcome IS NULL OR state = 'done')    -- an outcome is what a finished job finished as
CHECK  ((state = 'pending') = (decided_at IS NULL))   -- no grant without a record of who granted
```

The last one is the interesting one. A granted approval with no decider is
exactly the row that would let an unattended run claim a person had signed for
it, and the check refuses it even to a caller writing raw SQL — tested by
writing raw SQL.

Job transitions are a closed state machine. `done` is terminal: a finished gate
that needs more work is a new job with a new brief, so the record of what was
decided stays readable instead of being overwritten by the next attempt.

Approvals are decided exactly once. The update is guarded on
`state = 'pending'` and the row count is checked, so two clicks race into one
decision rather than the second silently replacing the first. Withdrawing a
grant is a separate move with its own record, not a revision of the decision.

Expiry gets its own transaction. The first version marked the request expired
and raised inside one transaction — which rolled the mark back with the
refusal, leaving the request pending for the next person to grant. A test
caught it. `expire_overdue()` settles the queue on startup, because read-time
filtering hides an overdue request without recording what happened to it.

## 4. Where the reducer meets the ledger

`record_reduction()` takes a `Reduction` object, not a dict, so the verdict
written to the ledger is the one the deterministic code produced rather than a
re-typed version of it. It returns the outcome the *job* should take, which is
not always the reduction's own:

```
reduction says          job takes
  satisfied         ->  satisfied
  primary_run_missing -> primary_run_missing
  open              ->  exhausted, if this is the third identical failure
  open              ->  open, otherwise
```

The reducer decides one iteration from the evidence in front of it. Only the
ledger can see three iterations in a row failing the same way, so `exhausted`
is decided here and nowhere else.

## 5. Event kinds

The vocabulary a UI binds to. Payloads are JSON objects; every event carries
`event_id`, `job_id`, `seq`, `at`, `kind`, `agent`.

| kind | when | payload |
|---|---|---|
| `job.created` | a job opens | `gate`, `objective` |
| `job.running` / `job.blocked` / `job.awaiting_approval` / `job.done` | transition | `from`, `to`, `outcome`, `reason` |
| `iteration.started` | loop advances | `iteration` |
| `handoff.Brief` … `handoff.Report` | an envelope is stored | `iteration`, `agent`, `step_id`, `sha256` |
| `approval.pending` | a person is needed | `approval_id`, `contract`, `scope`, `artifacts`, `expires_at` |
| `approval.decided` | a person answered | `approval_id`, `state`, `decided_by`, `note` |
| `approval.expired` | deadline passed | `approval_id`, `expires_at`, `noticed_by` |
| `approval.withdrawn` | grant taken back | `approval_id`, `from`, `note` |

Runtime-level kinds — `command`, `file_change`, `mcp_call`, `agent_turn.started`
— are accepted by `append_event()` and not yet emitted by anything. What the
adapter can actually observe is the open question the App Server spike answers,
so the vocabulary is deliberately not fixed beyond what the ledger itself
writes.

## 6. Verified

```
py -3 -m pytest QKD_PROJ/tests/test_jobs.py -q     48 passed
py -3 -m pytest QKD_PROJ/tests -q                  398 passed, 1 failed
```

The single failure is the pre-existing gate9 allowlist hash guard, which the
evidence audit independently reports as `pinned_file_drift`.

Two of the tests here exist because they caught real defects while being
written: the rolled-back expiry above, and a `contracts_dir` that the audit
resolved from a module constant instead of its argument.

## 7. What is still missing

There is no daemon yet — this is its memory, not its loop. Next, in order:

1. **The App Server event spike.** Still the load-bearing unknown, and it
   decides the adapter's shape. Nothing in this ledger depends on the answer,
   which was the point of building it first.
2. **`RuntimeAdapter`,** against Codex and `claude -p` together. An
   abstraction with one implementation is usually the wrong abstraction.
3. **The loop itself:** plan → execute → reduce → record → decide, reading its
   state from here rather than holding it.

Two things this ledger does not do, on purpose. It does not repair an
interrupted job: `resume()` reports `interrupted: true` and stops, because
whether a step reached the board is unknown from here and nothing may assume it
did not. And it stores no approval *policy* — who may grant what is a question
about people, and putting it in a table would make it look decided.
