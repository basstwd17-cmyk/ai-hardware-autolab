# Current scenario agents

New missions run through `orchestrator/conversation_flow.py` and its
`INSTRUCTIONS`. `conversation_codex.py` passes those instructions and the
ledger-derived context to Codex. `conversation_service.py` connects the runtime
to the isolated job workspace and the host hardware adapter.

| Role | Responsibility | Execution |
|---|---|---|
| Lead | Understand the original scenario, research missing facts, choose an approach and observable completion criteria | Workspace reading, web research and selective connection checks |
| Builder | Implement, build, test, correct failures and request device operations | Workspace edits/commands; hardware through the host adapter |
| Verifier | Compare current artifacts and device results with the original scenario and feedback | Read, assess and optionally check connections; return concrete corrections |

## Handoff

Agents exchange a short reply containing status, summary, artifacts, sources
and relevant run references. Every role can request a `check_connections`
device call with an explicit `devices` list. Builder also requests implementation
and measurement device calls. After a tool result, the requesting role continues.
The ledger carries the original scenario, user feedback, previous replies,
research findings, artifact hashes and device results into the next turn.

No role writes an execution contract, compiled Plan, Gate or Campaign.
There is no contract-approval step between agents. Lead's design and acceptance
criteria guide the engineering work; Verifier assesses the returned evidence.

## Hardware execution

There is no startup connection sweep. Agents choose only devices needed for the
scenario, reuse timestamped `inputs.hardware_connections` from the job ledger,
and request a fresh observation when needed. `inputs.hardware_devices` describes
the installed setup, not a live observation. Connection checks do not detect wiring
or prove output functionality. Hardware mode permits tools; source-only work can
finish without contacting devices. Verifier judges required evidence against the
original goal, and cited run names must come from this job's actual tool results.

Builder chooses a tool and supplies its ordinary arguments. The host binds the
installed equipment, validates workspace paths and device identity/electrical
limits, executes the operation and records the result. Newly written workspace
scripts and SRAM images need no per-scenario registration.

Hardware is the default operator mode; Offline hides physical-device tools.
Supported operations within a submitted Hardware scenario run automatically.
Unknown outcomes after device contact are not replayed automatically. Missing
physical facts can produce a question; ordinary implementation errors return
to the agent that can fix them.

The Start view selects a registered board/setup per job. `inputs.selected_board`
contains that board's source paths, datasheets, notes and device descriptions.
Use those facts rather than assuming other workspace board folders describe the
selected hardware. Knowledge retrieval follows the selected board and revision.
New USB/serial configurations use stable device IDs in `check_connections`.
OS presence is not board identity. Builder can use `serial_exchange` for the
documented serial protocol; USB enumeration alone does not enable programming.
The current generic registration path does not include firmware flash adapters.
Existing RTM3004 settings can be reused with user-declared wiring for the new board.

## Source of truth and history

- Current prompts: `orchestrator/conversation_flow.py::INSTRUCTIONS`.
- Runtime and response schema: `orchestrator/conversation_codex.py`.
- Equipment calls: `orchestrator/conversation_hardware.py`.
- Installed RTASE setup: `boards/rtase-v1/v1.0/hardware.yaml`.
- `agents/simple/`, other role Markdown files, `agents/codex/`, and the
  export/schema utilities describe historical Plan workflows. The production
  daemon no longer initializes those workflows. Keep their records readable;
  do not use their Campaign/contract instructions for new missions.

See [the current flow](../docs/unified-scenario-flow.md) and
[board-independent development design](../docs/board-independent-development.md).
