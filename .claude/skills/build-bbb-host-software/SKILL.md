---
name: build-bbb-host-software
description: Author and test the PC/BeagleBone host software around an RTASE or PRU protocol without contacting the board. Use for Python command encoding, SPI transport abstraction, buffering, validation, CLI behaviour, and fake-transport tests. Do not use for SSH, provisioning, reboot, pinmux, remoteproc, or live SPI/GPIO access.
---

# Build host software against a fake transport

Implement the nondeterministic host side of a contract-backed Plan step while leaving hardware unchanged. The output is source plus deterministic local tests; live deployment and diagnostics belong to separately approved roles.

## Keep the layers visible

- Protocol code owns byte layout, versioning, range checks and response decoding.
- Buffer/queue code owns capacity, ordering, timeout and backpressure.
- Transport adapters own SPI, RPMsg or file-descriptor I/O.
- CLI/application code composes those layers and presents errors.

Do not put timing guarantees in the PC or Linux scheduling layer. The host may prepare a sequence and prove what bytes it submitted; the PRU/CPLD and later measurements establish physical timing.

## Required behaviour

- Validate every user field before encoding. Reject values outside the contract instead of truncating them into a bit field.
- Define byte order, integer width, sequence identifiers and protocol version in one place shared by encoder and decoder.
- Bound queues. Report full/timeout/underrun distinctly; do not drop or overwrite silently.
- Make retries idempotent or give them a new sequence id. A transport timeout is not proof that the board did nothing.
- Keep credentials, hostnames and passwords out of source, logs and test fixtures.

## Test without hardware

Inject a fake transport and test exact request bytes, partial reads, invalid replies, timeout, duplicate acknowledgement, queue-full behaviour and protocol-version mismatch. For SPI, test framing and transaction boundaries without opening `/dev/spidev*`. For RPMsg, test stream fragmentation rather than assuming one read equals one message.

Run only deterministic local tests named by the step or contract. Do not SSH, provision packages, alter services, edit boot files, reboot or open a live device as a shortcut.

## Evidence boundary

Use a platform tool that records the approved local test command and its results when one is available. Copy its verdict and run directory into the `StepResult`.

Until such a recorder exists, source and tests may be authored but the step must return `status: "blocked"` with the missing evidence adapter. Plain terminal output is useful during development but is not a durable `RunRecord`, and a `done` StepResult may not have an empty `runs` list.

## Handoff

Report changed modules, protocol/ABI version, tested cases and the deterministic run. Leave board state absent because this skill never contacts it. If the Plan asks for deployment or a live readback, stop and route that as a new `board-operator` or `measurement` step.

