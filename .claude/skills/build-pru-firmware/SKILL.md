---
name: build-pru-firmware
description: Author or change AM335x PRU firmware and its host-visible protocol without deploying it. Use for PRU C/assembly, shared-memory or RPMsg ABI design, deterministic timing loops, linker layout, and offline build review. Do not use for remoteproc, service restart, pinmux, reboot, or any board mutation.
---

# Build PRU firmware without touching the board

Produce reviewable PRU source and a host-visible protocol from a contract-backed Plan step. This skill ends at a compiled or statically checked artifact; deployment belongs to `board-operator` and requires separate authority.

## Inputs to require

- The Plan step, including its contract, expected output fields and `advance_when` sources.
- The target PRU core, firmware directory and ABI version.
- Cited timing and memory facts. If instruction timing, shared-memory offsets or peripheral addresses are not backed by the contract, a collected datasheet page or an earlier run, stop and name the missing fact.

Do not infer a 5 ns guarantee merely from a nominal PRU clock. The implementation and its evidence must distinguish instruction cadence, GPIO visibility, SPI/DAC timing and host-to-PRU delivery latency.

## Architecture boundary

Keep three concerns separate:

1. The PC/BBB host validates and queues commands.
2. A bounded ABI transfers commands to PRU memory or RPMsg.
3. The PRU owns the deterministic inner timing loop.

Version every command and status structure. Give ring-buffer indices explicit width and ownership, define wrap behaviour, reject overflow rather than overwriting unread commands, and reserve fields instead of silently changing layout. Treat host acknowledgement and physical output completion as different events.

Use a buffer only where it absorbs nondeterministic host scheduling. A buffer does not make the output cadence deterministic; the PRU loop and its resource conflicts do. State the maximum queued duration, underrun behaviour and safe idle output.

## Source work

- Write only inside the firmware/source directory named by the step.
- Preserve the existing linker command file and resource table unless the contract explicitly changes them.
- Keep timing-critical loops small enough to audit. Record cycle accounting next to the loop and cite the instruction facts used.
- Do not add direct access to a pin, peripheral or physical address that the board map and contract do not name.
- Make startup, protocol rejection, underrun and shutdown converge on the contract's safe idle state.

## Verification boundary

Use a deterministic firmware build/check tool when the platform exposes one and copy its verdict and run directory. Do not substitute `deploy_pru`: it uploads, installs and restarts a real service.

If no offline PRU compiler/check tool is available, source authoring may proceed, but return `status: "blocked"` and identify the missing toolchain check. Never return `done` with an empty `runs` list or call source review a compiled artifact.

When a build exists, record at least source digests, toolchain version, firmware digest, linker/map summary, ABI version and any cycle-count check the contract requires.

## Handoff

Return one `StepResult`. A tool-backed success or failure names its run and copies the tool verdict. A missing compiler or unsupported hardware fact is `blocked`, not a guessed result. Never deploy, reboot, edit `/boot/uEnv.txt`, write remoteproc state or restart `rtase-pru.service` from this skill.

