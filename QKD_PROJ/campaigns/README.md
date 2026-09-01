# Unattended campaign policies

A campaign is a person's bounded, time-limited authorisation for a specific
scenario. It is not expanded by an agent. The daemon accepts one only when the
person reviews the exact SHA-256, either from the web **Campaign** tab or at
launch:

```powershell
py -3 -m QKD_PROJ.orchestrator --profile hardware `
  --campaign night-2026-08-24.yaml `
  --campaign-sha256 <64-hex-digest>
```

Schema v1 pins one exact artifact. Schema v2 keeps the contract exact while
allowing repeated builds whose bytes change. Every v2 artifact must come from
a PASS `runs/<run>/manifest.json`, from an approved producer and artifact kind;
that iteration's bytes are then pinned into their own approval row before a
hardware call:

```yaml
schema_version: 2
campaign_id: night-2026-08-24
approved_by: quantum1
expires_at: 2026-08-25T00:00:00+09:00
scenario_sha256: <sha256 of the exact scenario text>
max_auto_approvals: 8
max_iterations: 6
permissions:
  - id: repeated-reviewed-sram-loads
    contract: gate12-pru-dac1-plan.yaml
    contract_sha256: <sha256 of the exact contract>
    role: board-operator
    tool: program_cpld_sram
    artifacts: {}
    artifact_mode: run_manifest
    artifact_kinds: [bitstream]
    producer_tools: [cpld.build]
    max_approvals: 8
    max_executions: 1
```

All fields are enforced fail-closed. Unknown fields fail loading. Contract and
scenario drift refuse automatic approval. A changing v2 artifact is refused if
it is outside `runs/`, missing from its manifest, from an unapproved producer,
of an unapproved kind, not PASS, or no longer hashes to the manifest. Each
hardware tool attempt consumes one execution before it starts, so a crash
cannot silently make an uncertain board operation retryable. A web activation
is an append-only `campaign.activated` event and is restored only after the
same filename, campaign digest, contracts, expiry and unfinished job revalidate.

Electrical limits are not accepted as free-form text here. Pin direction,
voltage/current limits, scope termination and attenuation, RF1/RF2 limits,
watchdog behaviour, and emergency shutdown must be implemented by signed
contracts and deterministic device interlocks before unattended bench use.
