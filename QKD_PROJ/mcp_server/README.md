# rtase-hwtools — MCP server

The layer 3 tools, reachable from any runtime that speaks MCP. The agent above
is replaceable; the tools, contracts and run records underneath are not.

```
Claude Code  ─┐
Codex        ─┼─ MCP stdio ─→ rtase-hwtools ─→ hwtools/ ─→ SafetyGate ─→ board
anything MCP ─┘                                         └─→ runs/<UTC>_<name>/
```

No API key, no network, no third-party package: the stdio transport is
newline-delimited JSON-RPC and is implemented in `protocol.py`.

## Two profiles

| launch | tools |
|---|---|
| `py -3 QKD_PROJ/mcp_server/server.py` | `list_contracts` `read_contract` `list_runs` `read_run` `build_cpld` |
| `... --allow-hardware` | the above **plus** `scan_jtag` `program_cpld_sram` `measure_gate1_gpio` `run_bbb` `deploy_pru` |

**The tool list is the first lock.** Without `--allow-hardware` the
board-touching tools are not registered, so no prompt can reach them — an agent
cannot call a tool that is not in its list. `SafetyGate` still enforces the
contract underneath, so this is a second lock rather than a replacement. The
flag is a launch argument, which is where the architecture already puts the
"this session may drive the board" decision: with a person, once.

Check a profile without serving:

```powershell
py -3 QKD_PROJ/mcp_server/server.py --list-tools
py -3 QKD_PROJ/mcp_server/server.py --list-tools --allow-hardware
```


## Role profiles

`--role <agent-name>` narrows the tool list to one row of the matrix in
`registry.py`. Role names are the agent names, because two vocabularies for the
same six actors is one too many.

| role | tools beyond the read-only four | `run_bbb` may run |
|---|---|---|
| `gate-architect` | — | `read` |
| `design-builder` | `build_cpld` | `read` |
| `board-operator` | `scan_jtag` `program_cpld_sram` `deploy_pru` `run_bbb` | `read` + `mutate` |
| `measurement` | `measure_gate1_gpio` `run_bbb` | `read` |
| `verifier` | — | `read` |
| `reporter` | — | `read` |

The two locks compose: a role that lists board tools still gets none of them
without `--allow-hardware`. Omitting `--role` keeps the historical profile. A
roled server also serves only that role's agent prompt, for the same reason it
withholds the tools.

### Why `run_bbb` needs a second column

`measurement` and `board-operator` share `run_bbb`. The tool is the same; the
scripts each may run are not. Every entry in a contract's `scripts` map carries
an `effect_class` of `read` or `mutate`, and a role that may not mutate is
refused before the tool is called.

**A script with no `effect_class` counts as `mutate`.** Failing closed is the
point: a script nobody has classified is a script nobody has thought about, and
the role that must not change the board is exactly the one that would otherwise
get the benefit of the doubt. Today no contract classifies anything, so
`--role measurement` can run no BBB script at all until a person signs the
classification — see `contracts/proposals/2026-08-21-gate9-effect-class.patch`.

Restoring a setting afterwards does not make a script a read. The restore is a
second change rather than the absence of a first one, and a process that dies
between them leaves the board altered.

This gate lives at the MCP-server layer. `SafetyGate` and the contract remain
the deeper lock, and a direct `hwtools.run_bbb()` call does not pass through
here — the same relationship the tool list already has to the flag.

## Register

**Claude Code** — `.mcp.json` at the repo root is picked up automatically. To
run the hardware profile instead, add `"--allow-hardware"` to `args`.

**Codex**

```powershell
codex mcp add rtase-hwtools -- py -3 C:/KIST/QKD_PROJ/mcp_server/server.py
codex mcp list
```

Add `--allow-hardware` after the script path for the hardware profile. Keep
them as two separate entries (`rtase-hwtools`, `rtase-hwtools-hw`) rather than
editing one back and forth, so an unattended run cannot inherit the unlocked
profile by accident.

## What a caller sees

- **Tools** take a **contract name**, never a path. `list_contracts` first; the
  contract, not the conversation, states what is permitted.
- Every call writes a `RunRecord` under `runs/` and returns its name, so a
  second agent can verify the claim from the evidence instead of the summary.
- **Prompts** are the skills in `.claude/skills/*/SKILL.md`, served verbatim
  without their frontmatter. The same procedural knowledge reaches every
  runtime; nothing in a skill body is Claude-specific.
- **Resources** are every contract (`contract://<file>`) plus `doc://STATUS.md`
  and `doc://README.md`.

## Adding a tool

1. Write the handler in `registry.py`. Take a contract name, build the
   `SafetyGate` from it, write a `RunRecord`, return `_result_payload(...)`.
2. Register it in `build_tools()`. If it can change the board's state, put it
   in the `allow_hardware` block and set `touches_hardware=True`.
3. Add a test to `tests/test_mcp_server.py` that calls it through
   `tools/call`, not through the Python function — the protocol path is what a
   client actually uses.
