# Board profiles

The platform never treats an MCU name as a board identity. A profile binds one
exact board and revision to the sources, local board map, agents, tools and
adapter stages that may be used for it.

## Automatic development path

For a scenario naming a profiled board, the mission follows this order:

1. refresh the profile's primary datasheet, reference manual and schematic;
2. compare those sources and stored knowledge with the requested change;
3. select an architecture and compile a reviewed contract;
4. run ready offline design/build stages with the exact argv in the profile;
5. wait for one campaign covering the scenario, generated contract and board
   profile digests;
6. run ready upload and measurement stages; and
7. let the deterministic reducer judge the recorded run evidence.

The LLM supplies a stage id, never a shell command. `run_board_build` and
`run_board_stage` load the command, working directory and expected artifacts
from the digest-bound profile. A changed profile invalidates the contract.

RTM3004 stages use `measure_board_scope` instead of an executable command.
Their profile `parameters` fix one to four channels, signal labels, vertical
scale/offset, timebase, trigger channel/level/slope and timeout. The tool forces
the selected inputs to 1 Mohm (`DCLimit`), reads the settings back, and stores
CSV waveforms plus a screenshot in the run. A channel-to-signal mapping that
has not been physically checked must remain `adapter_required`.

## Adding another board

Create `QKD_PROJ/boards/<board-id>/<revision>/profile.yaml` and a board map.
Start every unverified executable stage as `adapter_required`. Change it to
`ready` only after its exact non-shell command, working directory and artifacts
have an offline test. Hardware stages additionally use `mode: hardware` and
`requires_campaign: true`.

An unknown board still cannot borrow the RTASE, BBB, NUCLEO or oscilloscope
adapter merely because a model thinks they look similar. The three-agent
mission instead creates and tests a board map, profile and adapter in its
isolated workspace. When that offline step passes, WorkspaceManager publishes
the profile and the following Lead iteration reloads the catalog automatically;
the user does not need to submit the scenario again. Physical stages remain
`adapter_required` until their exact command and bounds are verified, and then
still require the one digest-bound Campaign.

`STM32G474RE` by itself is an MCU, not a board. The catalog deliberately keeps
the NUCLEO-G474RE and B-G474E-DPOW1 identities separate.
