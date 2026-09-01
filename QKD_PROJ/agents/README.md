# Agent definitions

Thirteen agents, split across mission planning, discovery and execution. A person writes a
**Brief** — the scenario and the resources it may use. The mission architect loads the
existing contract and knowledge, chooses research questions, required materials, registered
execution agents and registered skills, and records those choices in a **MissionPlan**.
Discovery then refreshes cited source material and compares it with the baseline before the
development loop runs until it can say, from evidence, that the gate is closed or why it is not.

```
Brief (a person)
   │
   ├─▶ mission-architect ──MissionPlan──▶ researcher ──SourcePacket
   │                                      │
   │                                      ▼
   │                               change-analyst ──ChangeAnalysis
   │                                      │
   │                                      ▼
   │                               architecture-selector
   │
   ├─▶ system-architect ──ResearchPlan──▶ researcher   legacy contract-gap path
   │                                                   │ read-only web research
   │                                                   ▼
   │      contract-reviewer ◀─ContractDraft─ contract-drafter
   │               ▲                              ▲
   │         ContractReview              ArchitectureDecision
   │               │                              │
   │               └──────── architecture-selector
   │
   ▼
gate-architect ──Plan──┐
   ▲                   │
   │                   ├─▶ design-builder ─┐   offline, unattended
   │                   ├─▶ board-operator ─┤   changes board state, one approved step
   │                   └─▶ measurement   ──┤   observes only
   │                                       │
   │                              StepResult
   │                                       ▼
   └──── Verdict (not satisfied) ──── verifier      reads runs/ only
                                          │
                                    satisfied / needs_approval
                                    / contract_gap / exhausted
                                          ▼
                                      reporter ──▶ docs/…md
```

| Agent | Role | Touches board | Can write |
|---|---|---|---|
| `mission-architect` | discover | no | nothing |
| `change-analyst` | discover | no | nothing |
| `system-architect` | discover | no | nothing |
| `researcher` | research | no | nothing |
| `architecture-selector` | discover | no | nothing |
| `contract-drafter` | plan | no | nothing |
| `contract-reviewer` | verify | no | nothing |
| `gate-architect` | plan | no | nothing |
| `design-builder` | build | no | its design directory |
| `board-operator` | operate | **yes** | nothing |
| `measurement` | measure | **yes** (read-only) | nothing |
| `verifier` | verify | no | nothing |
| `reporter` | report | no | `docs/` |

## Four things the shape is for

**The planner cannot write, and the verifier cannot write.** Both ends of the
loop are read-only. A plan is reviewed before anything happens; a verdict is
worth reading precisely because the agent producing it has no way to change
what it judges.

**The agent that changed the board is not the agent that reports on it.**
`board-operator` programs, deploys and reboots. `measurement` only observes.
The split is enforced by the tool lists, not by instruction.

**Verdicts are copied, never authored.** `hwtools` judges PASS/FAIL against the
contract deterministically. An agent may carry that value forward. If a tool did
not judge, the field stays empty rather than being filled in by a model.

**Every advance condition names a field and a source.** `field` is a path into
the `ToolResult` the step produces, so the verifier reads it out of
`run_status.json` instead of asking anyone how it went. `source` is the
datasheet page, contract field or prior run the number came from — the
validator rejects an empty one, because an untraceable threshold reads as
though somebody measured it.

## How the loop ends

`satisfied` is the only success. The other three are real outcomes and get
written up the same way:

| Outcome | Meaning |
|---|---|
| `satisfied` | every condition met, each with a run behind it |
| `needs_approval` | a person has to sign before the next step may run |
| `contract_gap` | the plan needs authority no contract grants |
| `exhausted` | the same failure for three iterations, no new evidence |
| `primary_run_missing` | a run the gate depends on is not on disk |

One more, `open`, is a **Verdict** outcome only: this iteration ended and the
loop did not. A Report may not carry it, because a report describes a loop that
stopped.

`exhausted` is what stops a loop from spinning. `gate-architect` must name a
concrete change on every re-plan — a different measurement point, a slower
clock, a split step — and when it cannot, the honest move is to stop and record
what would distinguish the remaining explanations.

## Using them

```powershell
py -3 -m QKD_PROJ.agents.export --check-only                                 # validate
py -3 -m QKD_PROJ.agents.export --runtime claude --profile hardware --write  # .claude/agents/*.md
py -3 -m QKD_PROJ.agents.export --runtime codex  --profile hardware --write  # agents/codex/*.md
py -3 -m QKD_PROJ.agents.export --runtime claude --profile offline           # --agents JSON
```

Two profiles, matching the MCP server's two profiles:

- `offline` — all read-only discovery agents plus `gate-architect`,
  `design-builder`, `verifier`, `reporter`. This
  is the set for an unattended `claude -p --agents <json>` run: the
  board-touching agents are not in it at all.
- `hardware` — all eleven. This is the set written to disk, because an
  interactive session has a person at the bench. Their tools still do not
  exist unless the server was started `--allow-hardware`, which `.mcp.json`
  does not do.

The checked-in exports must match the definitions; `test_agents.py` fails if
they drift, because a stale prompt is read by the runtime while the reviewer
reads the source.

## Adding an agent

1. Write `<name>.md` here. Frontmatter: `name` (must match the file), `role`
   from `schema.ROLES`, `description`, `emits`, and the tools it needs.
2. `--check-only` will refuse an unknown tool, a board tool on an agent that
   declares `touches_hardware: false`, a `verify` agent that can write, and a
   `consumes` kind nothing emits.
3. Add its case to `tests/test_agents.py` if it carries a new boundary.
4. Re-export. The MCP server serves definitions as prompts named
   `agent-<name>`, so Codex picks up the change without an export at all.

## What is not enforced here

Claude Code applies a per-agent tool allowlist, so `tools` really does bound
that agent. Codex has no equivalent, so on that runtime the boundary is the
server's launch profile — without `--allow-hardware` the board tools are not
registered for anyone. Two mechanisms, one rule, and the rule is chosen at
launch by a person rather than by a definition.

Nothing here tests that a model obeys its prompt. What the tests assert is that
the permission a definition grants matches the permission its prompt describes,
and that an envelope carrying an unevidenced claim is rejected before the next
agent reads it.

## Running the loop through Codex App Server

`QKD_PROJ.orchestrator.codex_runner.CodexAgentRunner` implements the loop's
`AgentRunner` seam without reading an API key or a Codex credential file. The
installed Codex CLI owns login and the runner speaks its stdio JSONL protocol.

Each call starts a fresh ephemeral thread. The ledger-derived task is complete,
so a prior conversation is never a hidden second source of loop state. App
Server processes are cached by agent name because each one launches
`rtase-hwtools --role <agent>`; roles therefore do not share a broader MCP tool
set merely because they run in the same outer loop.

```python
from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig
from QKD_PROJ.orchestrator.loop import Loop

with CodexAgentRunner(CodexRunnerConfig(profile="offline")) as runner:
    step = Loop(ledger, runner).advance(job_id)
```

The `hardware` profile only makes the selected role's hardware tools visible.
It does not approve them. Supply `hardware_approval` as a callback that verifies
the current JobLedger approval against the agent, step, contract and tool. With
no callback, every hardware MCP request is refused and the loop becomes
`blocked`; a runtime can never turn its own request into a signature.

Pass `event_sink` to receive raw App Server notifications. The daemon may
normalize those into its event ledger for UI observability; they are not handoff
evidence and do not decide loop state.

The `researcher` is the only definition with `native_web: true`. Its runner
enables native live search and supplies dynamic tools that collect a selected
PDF into `ReferenceStore`, search its extracted text and read cited pages.
Other roles launch with native web search disabled, so research authority does
not silently spread to builders or board operators.
