# The loop, on top of the frozen ledger

**Date:** 2026-08-21
**Status:** built and verified offline against a canned runner. No runtime attached.
**Ledger:** frozen at `bee8e43`; `orchestrator/jobs.py` is unchanged by this work.
**Follows:** `2026-08-21-agent-platform-p2-job-ledger.md`

## 1. One call, one step

`Loop.advance(job_id)` reads the ledger, works out what has not happened yet,
does that, and returns. It never loops. Continuing across a rate-limit window,
a closed editor or a reboot is calling it again — there is no in-memory
position to lose, because there is no in-memory position.

`run(job_id, max_steps=…)` is a convenience that calls `advance` until the job
finishes or blocks. Its budget bounds *that call*, not the job: stopping leaves
everything exactly where it was.

## 2. The phase is derived, never stored

```
iteration == 0, or no Plan for this iteration   -> plan
Plan has a step with no StepResult              -> execute the first of them
every step reported, no Verdict                 -> reduce and record
Verdict recorded                                -> decide
```

A stored phase column would be a second thing that can disagree with the
handoffs. Deriving it means a `Loop` object thrown away mid-job and rebuilt on
the same ledger picks up exactly where the last one stopped — which is what the
resumability tests do, at every phase.

This also settles where the iteration counter is advanced. `_decide()` does it,
not `_plan()`: opening the next iteration **is** the decision, and until it
happens `phase()` keeps routing back to `decide`. The first version advanced it
in `_plan()` and the loop span in place without moving, which is the failure the
whole derived-phase design is supposed to make impossible. A test now pins it.

## 3. Three boundaries this module does not cross

**It writes no event kind of its own.** Every event it causes comes from a
`JobLedger` method — `job.*`, `iteration.started`, `handoff.*`, `approval.*`.
What a runtime observes (command execution, file changes, MCP calls, sub-agent
activity) is the adapter's vocabulary and the adapter writes it. A test asserts
the produced kinds are a subset of the ledger's own, so this stays true by
accident-detection rather than by discipline.

A "step dispatched" event would have been convenient here, and inventing one
was the wrong call: the resumable signal is the **absence** of a StepResult,
which is derived and cannot drift. If the UI wants a dispatch marker, that kind
belongs in the event-vocabulary work, not in this module.

**It never decides whether a condition holds.** `EvidenceReducer` does, from
run records. The loop's only contribution to a verdict is binding each
condition to the run its step actually produced — the one fact a plan written
beforehand could not contain.

**It never grants an approval.** A step whose agent touches hardware stops the
loop and files a request. Whether the agent touches hardware is read from its
definition (`agents/*.md`, `touches_hardware`), so that permission has one
source rather than a second list here that can fall out of step.

## 4. Binding conditions to runs

`conditions_for(plan, step_results)` is a pure function, and two of its cases
are deliberately not resolved by choosing:

| case | what happens | why not the alternative |
|---|---|---|
| step never reported | condition gets no run → `evidence_missing` | dropping it lets a gate close on the steps that happened to finish |
| step produced several runs, condition names none | `LoopError` | picking the first produces a defensible-looking number out of an under-specified plan |

A condition may name its own run, which is how a plan resolves the second case
in advance.

## 5. Where the loop stops, and why each is different

| stop | meaning | resumes on |
|---|---|---|
| `blocked` (approval) | a hardware step needs a person | the decision |
| `blocked` (runtime) | the runner could not run | a person deciding what it meant |
| `needs_approval` (final) | a request was **refused** | nothing — it is not re-requested |
| `satisfied` | every condition met, each with a run | — |
| `exhausted` | same failure three times, or the iteration ceiling | — |
| `primary_run_missing` | a run this gate depends on is not on disk | an approved re-run |
| `contract_gap` | the architect found no authority for a step | a person signing a contract |

Two of these are worth spelling out.

**A refusal is not a retry.** After a denial, the loop stops with
`needs_approval` rather than filing a fresh request. Re-requesting would make a
refusal mean "ask again", which is the same as meaning nothing.

**A runtime failure blocks; it never concludes.** A dead subprocess says
nothing about the board. Recording it as a failed condition would put a claim
about hardware into the evidence chain on the strength of a process dying, so
it becomes a blocked job and a person decides what it meant.

## 6. Two stopping rules, because one is blind

`exhausted` fires on three iterations with the identical failure signature.
That rule cannot see a loop that fails differently every time — every signature
differs, so the count never builds. `LoopPolicy.max_iterations` (default 8) is
the ceiling for that case. The test for it makes the failure move on every
round and asserts the strike count stayed at 1 while the ceiling stopped the
job anyway.

## 7. The seam to a runtime

```python
class AgentRunner(Protocol):
    def __call__(self, agent: AgentDefinition, task: dict) -> dict: ...
```

That is the whole interface. An implementation spawns `codex exec` or
`claude -p`, hands over the agent definition and the task, and returns the JSON
envelope. It may log whatever runtime detail it observes; none of that
vocabulary appears here. Raising is a legitimate answer and becomes a blocked
job.

The task dict carries what the agent cannot be trusted to remember —
`iteration`, `strikes`, `previous_verdict` — because the daemon owns that state
and tells the agent what it is.

## 8. Verified

```
py -3 -m pytest ai_hardware_autolab/tests/test_loop.py -q     32 passed
py -3 -m pytest ai_hardware_autolab/tests -q                  430 passed, 1 failed
git status ai_hardware_autolab/orchestrator/jobs.py           (empty — ledger unchanged)
```

The single failure is the pre-existing gate9 allowlist hash guard.

## 9. What is next, and what is not ours

Not ours, by agreement: the `event_id`/`seq` contract and the UI replay bridge,
and the runtime event vocabulary the `CodexAppServerAdapter` emits.

Ours, next:

1. **An `AgentRunner` over the adapter** — the first real one. Until a runner
   exists, every test here proves the loop's behaviour and none of them prove a
   model can produce these envelopes.
2. **Envelope validation at the seam.** `agents/schema.py` already validates
   Plan/StepResult/Verdict shapes; the loop currently trusts the runner to
   return something well-formed and only checks that it is a dict. Wiring
   `validate_handoff()` into `_call()` is a small change with an ordering
   question behind it — a malformed envelope should probably block rather than
   fail a condition, for the same reason a dead subprocess does.
3. **A `run_bbb` effect split** (`read` vs `mutate` in the contract's script
   allowlist). Today `measurement` and `board-operator` share that tool, so
   "measurement only observes" is enforced by the definitions and not yet by
   the tool layer.
