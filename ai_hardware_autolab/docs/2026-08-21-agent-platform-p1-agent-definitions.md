# Agent platform, phase 1: the loop has named parts

**Date:** 2026-08-21
**Status:** P1 built and verified offline. P2–P4 not started.
**Follows:** `2026-08-21-agent-platform-p0-mcp-server.md`

## 1. What P1 is for

P0 made the tools reachable from any runtime. That is enough for a single agent
to call a tool and read the result, and it was already enough to be useful. It
is not enough to run a gate unattended, because one agent that plans, executes,
measures and then judges its own work has no way to be wrong out loud.

P1 splits that into six agents and one loop:

```
Brief (a person)
   │
   ▼
gate-architect ──Plan──┐
   ▲                   ├─▶ design-builder ─┐   offline, unattended
   │                   ├─▶ board-operator ─┤   changes board state, one approved step
   │                   └─▶ measurement   ──┤   observes only
   │                                       ▼
   │                                  StepResult
   │                                       ▼
   └──── Verdict (not satisfied) ──── verifier      reads runs/ only
                                          │
                                    satisfied / needs_approval
                                    / contract_gap / exhausted
                                          ▼
                                      reporter ──▶ docs/…md
```

A person supplies the scenario and the resources. The architect reads the
datasheets, the schematic, the contracts and the prior runs, and returns a plan
in which every step names one tool, one contract, and the condition that lets
the loop advance past it. Execution splits three ways. The verifier decides,
from run records alone, whether those conditions were met. If they were not,
the architect re-plans with a change it has to name. When the loop stops — for
any of four reasons — the reporter writes the document a person reads instead
of the runs.

## 2. The four boundaries that make it more than a prompt chain

**Both ends of the loop are read-only.** `gate-architect` has no Write or Edit
tool, so a plan is reviewed before anything happens and no agent can grant
itself authority by editing a contract. `verifier` has none either, and that is
the whole reason its verdict is worth reading: it cannot change what it judges,
cannot re-run a failing step until it passes, and cannot touch the runs it
reads from. A test asserts both, and `export.check()` refuses to render a
`verify`-role agent that carries a writing tool.

**The agent that changed the board is not the agent that reports on it.**
`board-operator` holds `program_cpld_sram`, `deploy_pru`, `scan_jtag`.
`measurement` holds `measure_gate1_gpio` and read-only diagnostics. Enforced by
the tool lists, not by instruction: a test asserts that no agent other than the
operator can name a state-changing tool.

**A verdict is copied, never authored.** `hwtools` judges PASS/FAIL against the
contract's thresholds deterministically, before any model sees the number. An
agent may carry that value forward; the envelope validator rejects anything
that is not one of `PASS`/`FAIL`/`WARNING`, so a model cannot slip its own
opinion into the field that reads like a measurement. If a tool did not judge,
the field stays empty.

**A claim is not evidence; the run behind it is.** For each advance condition
the verifier reads the named run with `read_run`, finds the field in
`run_status.json`, and records the run directory alongside the value. A field
absent from the run is `evidence_missing` — not a judgement call, and never
"the StepResult said it passed". The schema enforces the shape of this:
`met: true` with an empty `evidence` list is rejected, and `outcome:
"satisfied"` cannot be returned while any condition is unmet.

## 3. How a loop ends

Only `satisfied` is success. The other three outcomes are results, and get
written up the same way:

| Outcome | Meaning | Who decides |
|---|---|---|
| `satisfied` | every condition met, each with a run behind it | verifier |
| `needs_approval` | a person must sign before the next step runs | operator or verifier |
| `contract_gap` | the plan needs authority no contract grants | architect or verifier |
| `exhausted` | same failure signature for three iterations, no new evidence | verifier |

`exhausted` is what stops the loop from spinning. Two rules produce it. The
architect must name a concrete change on every re-plan — a different
measurement point, a slower clock, a step split in two — and when it cannot,
it plans the one experiment that would distinguish the remaining explanations.
The verifier tracks a `failure_signature` (contract, field, tool, observed
value); when the same one survives three iterations, the loop stops and the
reporter writes what would have distinguished them.

Without this, a self-verifying loop does not fail. It runs.

## 4. What is enforceable, and what is not

Claude Code accepts a per-agent tool allowlist, so `tools` really does bound
what that agent can call. Codex has no equivalent, so on that runtime the bound
is the server's launch profile: without `--allow-hardware` the board tools are
not registered for anyone. Two mechanisms, one rule — and the rule is chosen at
launch by a person, which is where the architecture already puts that decision.

Two profiles, matching the server's:

```
offline    gate-architect  design-builder  verifier  reporter
hardware   + board-operator  measurement
```

The offline set is what an unattended `claude -p --agents <json>` run gets. The
board-touching agents are not in it at all — not present-but-restrained, absent.
An agent whose purpose is to change hardware has no business in a process
nobody is watching, even a well-behaved one that would have stopped at the
approval check.

Nothing here tests that a model obeys its prompt, and the documentation should
not imply otherwise. What is tested is that the permission a definition grants
matches the permission its prompt describes, and that an envelope carrying an
unevidenced claim is rejected before the next agent reads it.

## 5. What P1 delivers

| File | Role |
|---|---|
| `agents/*.md` | six definitions: frontmatter is the permission, body is the prompt |
| `agents/schema.py` | definition loading, and the five handoff envelopes |
| `agents/export.py` | render for Claude Code or Codex; `check()` refuses a lying definition |
| `agents/README.md` | the loop, the table, how to add an agent |
| `agents/codex/*.md` | generated Codex prompts |
| `.claude/agents/*.md` | generated Claude Code agents |
| `tests/test_agents.py` | 41 tests |

The definitions are markdown plus YAML frontmatter, the same shape as
`.claude/skills/*/SKILL.md`, so they belong to no vendor. `mcp_server` now
serves them as prompts named `agent-<name>`, withholding the board-touching
ones on a locked server exactly as it withholds their tools — a runtime that
cannot call `program_cpld_sram` should not be handed a prompt that tells it to.

The exported copies are checked in and a test fails if they drift from the
source, because the runtime reads the export while the reviewer reads the
definition.

## 6. Verified

```
py -3 -m ai_hardware_autolab.agents.export --check-only     ok: 6 definitions, no problems
py -3 -m pytest ai_hardware_autolab/tests/test_agents.py    41 passed
py -3 -m pytest ai_hardware_autolab/tests/test_mcp_server.py  25 passed
```

Prompt lists, showing the profile lock reaching the definitions:

```
allow_hardware=False -> build-cpld, scope-measure, agent-gate-architect,
                        agent-design-builder, agent-verifier, agent-reporter
allow_hardware=True  -> + agent-board-operator, agent-measurement
```

## 7. What P1 does not do, and what that tells P2

There is no runner. Today a person invokes each agent and carries the envelope
to the next one. That is deliberate — the point of building the definitions
first was to find P2's requirements as gaps rather than guesses. Three showed
up immediately:

1. **Something must persist the Plan.** The architect deliberately has no Write
   tool, so the plan exists only as its return value. P2 needs a sink that
   writes it to a fixed location — a tool with one directory, not a general
   Write, or the read-only boundary is gone.
2. **Iteration state has to live outside the agents.** `iteration`,
   `failure_signature` and the three-strike count are what stop the loop, and
   an agent that holds its own counter across a rate-limit window will lose it.
   `ConsumptionLedger` already solves the equivalent problem for approvals;
   the loop counter belongs beside it, not in a prompt.
3. **The approval queue needs a shape.** `needs_approval` is currently a string
   in an envelope. For an unattended run it has to become a durable item a
   person can act on later — which is the same object the P3 UI's approve
   button will press.

Recommended next: P2, and specifically the ledger-backed loop state, because
the three-strike rule is the only thing standing between an unattended run and
an infinite one.
