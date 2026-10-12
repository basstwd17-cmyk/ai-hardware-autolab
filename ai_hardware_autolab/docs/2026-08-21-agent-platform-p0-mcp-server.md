# Agent platform, phase 0: the tools became runtime-independent

**Date:** 2026-08-21
**Status:** P0 built and verified offline. P1–P4 not started.

## 1. Goal, and the constraint that shapes it

Run agents that plan, call the real tools, read the results, verify their own
work and keep going — with a UI of our own rather than someone's chat client,
and with **Codex** as the long-running runtime.

The constraint: **there is no API key.** Capacity comes from CLI subscriptions.
So the platform must never call a model SDK. Instead it spawns the CLIs that
already hold the authentication:

| | non-interactive | MCP | agent definition |
|---|---|---|---|
| Claude Code | `claude -p` | `--mcp-config` | `--agents <json>` |
| Codex | `codex exec` | `codex mcp add`, `codex mcp-server` | config |

Both are installed and signed in on this machine. The platform is therefore a
**process orchestrator**, not an API client — which is also what makes the
runtime swappable. It is the same principle as "the Tool API is the asset, not
the AI", applied one layer up.

The real budget is not money but **rate-limit windows**. Jobs must checkpoint
and resume rather than restart, which is already how `RunRecord` behaves.

## 2. Phases

| Phase | Content | State |
|---|---|---|
| **P0** | MCP server: `hwtools` tools + skills reachable from any runtime | **done** |
| P1 | Agent definitions (planner / hw-executor / verifier) reusing the skills | not started |
| P2 | Job runner + approval queue: continue across sessions and rate limits | not started |
| P3 | UI: run list, live log, artifacts, approve button | not started |
| P4 | Multi-agent pipelines, scheduled runs | not started |

Three lines hold from the start: hardware approval is pressed by a person even
when the button is in a UI; agents cannot write contract files; unattended
execution covers offline work only, and board contact queues for approval.

## 3. What P0 delivers

```
Claude Code  ─┐
Codex        ─┼─ MCP stdio ─→ rtase-hwtools ─→ hwtools/ ─→ SafetyGate ─→ board
anything MCP ─┘                                        └─→ runs/<UTC>_<name>/
```

`ai_hardware_autolab/mcp_server/` — no third-party dependency. MCP over stdio is
newline-delimited JSON-RPC 2.0, implemented directly in `protocol.py`, so the
server installs anywhere the rest of `hwtools` runs. No key, no network.

| File | Role |
|---|---|
| `protocol.py` | transport and dispatch: initialize, tools, prompts, resources |
| `registry.py` | which tools exist, and which a person has unlocked |
| `server.py` | entry point, `--allow-hardware`, `--list-tools` |
| `README.md` | registration, and how to add a tool |
| `../tests/test_mcp_server.py` | 25 tests, protocol-level |

**Two profiles, and the tool list is the first lock:**

```
default            list_contracts  read_contract  list_runs  read_run  build_cpld
--allow-hardware   + scan_jtag  program_cpld_sram  measure_gate1_gpio  run_bbb  deploy_pru
```

Without the flag the board-touching tools are **not registered**, so no prompt
can reach them — an agent cannot call a tool that is not in its list.
`SafetyGate` still enforces the contract underneath; this is a second lock, not
a replacement. The flag is a launch argument, which is where the architecture
already puts the "this session may drive the board" decision.

**Tools take a contract name, never a path.** Every call names a file in
`contracts/`, and the tool derives sources, device and permitted pin-out from
it. Path arguments are rejected, including traversal attempts (tested).

**Skills became portable.** `.claude/skills/*/SKILL.md` is served as MCP
prompts with the frontmatter stripped. Nothing in a skill body was ever
Claude-specific — only the loading mechanism was — so Codex now reads the same
instrument traps that took a session each to learn.

## 4. Why the verification loop is different here

Three loops, two of which already work:

1. **Inner loop — works as soon as a runtime connects.** `claude -p` / `codex
   exec` call a tool, read the `ToolResult` JSON, and decide the next call.
   Nothing to build; it works because layer 3 returns machine-readable results
   instead of console text.
2. **Verification loop — the part general agent platforms lack.** Every call
   writes a `RunRecord` with artifact hashes, so a *second* agent verifies from
   evidence (`list_runs` → `read_run` → compare hashes) rather than by asking
   the first agent whether it succeeded. The judgement it reads was made
   deterministically by a tool against contract thresholds, not by a model.
3. **Outer loop — P2.** Continuing across sessions and rate limits, retries,
   agent handoff, and the human approval queue.

## 5. Verified

- 25 protocol-level tests: handshake and version fallback, notifications
  silent, unknown method → `-32601`, hardware tools absent by default and a
  locked server refusing them by name, contract path traversal rejected, prompt
  frontmatter stripped, resources readable, stdio framing including a malformed
  line, a crashing handler reported as `isError` without killing the connection.
- End-to-end launch through the exact `.mcp.json` command, as a client does it:
  handshake, `tools/list`, `tools/call` returning real contract data, prompts
  listed. The startup banner goes to stderr; stdout carries protocol only.

```
launched: py -3 C:/KIST/ai_hardware_autolab/mcp_server/server.py
initialize -> {'name': 'rtase-hwtools', 'version': '0.1.0'} 2025-06-18
tools      -> ['list_contracts', 'read_contract', 'list_runs', 'read_run', 'build_cpld']
contracts  -> 15 files, isError= False
prompts    -> ['build-cpld', 'scope-measure']
```

## 6. Register

Claude Code picks up `.mcp.json` at the repo root. For Codex:

```powershell
codex mcp add rtase-hwtools -- py -3 C:/KIST/ai_hardware_autolab/mcp_server/server.py
codex mcp list
```

Keep the unlocked profile as a **separate entry** rather than editing one back
and forth, so an unattended run cannot inherit `--allow-hardware` by accident.

## 7. Next decision

P1 (agent definitions) before P2 (job runner) is the recommendation: once a
verifier agent exists, the job runner's real requirements show up as concrete
gaps instead of guesses.
