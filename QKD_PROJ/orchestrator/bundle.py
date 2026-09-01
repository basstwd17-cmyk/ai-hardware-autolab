"""The pre-approved bundle: one human signature covering one session.

A SafetyGate answers "is this operation allowed on this board at all?" from the
contract. That is not the same question as "did a person authorise this run,
right now, against these exact files, this many times?" -- and the second
question is the one that matters once a sequence runs unattended. The contract
is long-lived and describes the board; a bundle is short-lived and describes a
session.

Keeping them apart is deliberate. If the orchestrator could grant itself
permission by editing the contract, the contract would stop being a document a
human reviews. So a bundle is a separate file, signed separately, and layer 2
may only *read* it.

A bundle names artifacts by SHA-256, never by path alone. A path can be made to
point at a different file between the signature and the run; a digest cannot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import yaml

from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.hwtools.safety import SafetyGate

SHA256 = re.compile(r"[0-9a-f]{64}")


class SignatureVerifier(Protocol):
    """Checks that a bundle's payload really was signed by an approver.

    `approved_by: "someone"` is a name typed into a file. It records intent and
    it is enough while a person is at the bench watching the run, because the
    person is the control. It is not enough once a CLI runs a sequence
    unattended: at that point the only thing standing between a bundle and the
    board is the file itself, and anyone who can write the file can grant
    themselves an approval.

    No scheme is built in on purpose -- choosing one is a decision about key
    custody, not about code. Implement this and pass it to load_bundle().
    """

    def verify(self, payload: bytes, signature: dict[str, Any]) -> bool: ...


def canonical_payload(data: dict[str, Any]) -> bytes:
    """The exact bytes a signature covers.

    The `signature` block is excluded (it cannot cover itself) and the rest is
    re-serialised as sorted-key JSON rather than signed as raw YAML. Two YAML
    files can mean the same thing and differ byte for byte -- different quoting,
    key order, line wrapping -- and a signature over raw text would break on a
    reformat while still describing the same approval.
    """
    payload = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")

# The only order these steps may ever run in. A bundle may authorise a prefix
# of this sequence -- scan alone, or scan then program -- but never a later
# step without the earlier ones, and never a different order.
STEP_ORDER: tuple[str, ...] = ("scan", "program", "measure")

# What each step is called in the test spec's allowed_operations. The bundle
# must agree with the contract about this, so a bundle cannot quietly authorise
# an operation the contract forbids.
STEP_OPERATIONS: dict[str, str] = {
    "scan": "jtag_scan",
    "program": "sram_fast_program",
    "measure": "oscilloscope_capture",
}

# Digests a step must carry. Naming a step without pinning what it acts on
# would make the signature meaningless.
REQUIRED_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "scan": ("scan_xcf_sha256",),
    "program": ("program_xcf_sha256", "bitstream_sha256"),
    "measure": (),
}


class BundleError(RuntimeError):
    """Raised instead of running anything. Never caught to retry."""


@dataclass(frozen=True)
class BundleStep:
    step: str
    operation: str
    max_executions: int
    artifacts: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalBundle:
    bundle_id: str
    test_spec: str
    approved_by: str
    issued_on: date
    expires_on: date
    steps: tuple[BundleStep, ...]
    path: Path
    sha256: str
    signature: dict[str, Any] | None = None
    signature_verified: bool = False

    def step(self, name: str) -> BundleStep | None:
        for candidate in self.steps:
            if candidate.step == name:
                return candidate
        return None

    def authorises(self, name: str) -> bool:
        return self.step(name) is not None

    def describe(self) -> dict[str, Any]:
        return {
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.sha256,
            "bundle_path": str(self.path).replace("\\", "/"),
            "test_spec": self.test_spec,
            "approved_by": self.approved_by,
            "issued_on": self.issued_on.isoformat(),
            "expires_on": self.expires_on.isoformat(),
            "signature_algorithm": (self.signature or {}).get("algorithm"),
            "signature_key_id": (self.signature or {}).get("key_id"),
            "signature_verified": self.signature_verified,
            "steps": [
                {
                    "step": s.step,
                    "operation": s.operation,
                    "max_executions": s.max_executions,
                    "artifacts": dict(s.artifacts),
                }
                for s in self.steps
            ],
        }


def _as_date(value: Any, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise BundleError(f"{field_name} is not an ISO date: {value!r}") from exc


def load_bundle(
    path: Path,
    *,
    today: date | None = None,
    verifier: SignatureVerifier | None = None,
    require_signature: bool = False,
) -> ApprovalBundle:
    """Parse and check a bundle. Every failure here happens before hardware.

    ``today`` is injected rather than read from the clock so the expiry rule can
    be tested. Production callers leave it alone.
    """
    path = Path(path)
    if not path.is_file():
        raise BundleError(f"Bundle not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, ValueError) as exc:
        # A bundle that will not parse is a refused bundle, not a crash. YAML
        # raises ValueError of its own accord on things like an impossible date.
        raise BundleError(f"Bundle {path} could not be parsed: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleError(f"Bundle is not a mapping: {path}")

    if data.get("schema_version") != 1:
        raise BundleError(
            f"Unsupported bundle schema_version {data.get('schema_version')!r}. "
            f"This loader understands version 1 only."
        )

    for required in ("bundle_id", "test_spec", "approved_by", "issued_on", "expires_on"):
        if not str(data.get(required) or "").strip():
            raise BundleError(f"Bundle is missing '{required}'.")

    issued = _as_date(data["issued_on"], "issued_on")
    expires = _as_date(data["expires_on"], "expires_on")
    if expires < issued:
        raise BundleError(f"Bundle expires ({expires}) before it was issued ({issued}).")

    # An approval that never lapses becomes a standing permission nobody
    # remembers granting. Expiry is checked before anything else runs.
    now = today or date.today()
    if now > expires:
        raise BundleError(
            f"Bundle {data['bundle_id']} expired on {expires} (today is {now}). "
            f"Have it re-issued rather than editing the date."
        )
    if now < issued:
        raise BundleError(
            f"Bundle {data['bundle_id']} is not valid until {issued} (today is {now})."
        )

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise BundleError("Bundle authorises no steps.")

    steps: list[BundleStep] = []
    seen: list[str] = []
    for entry in raw_steps:
        if not isinstance(entry, dict):
            raise BundleError(f"Bundle step is not a mapping: {entry!r}")
        name = str(entry.get("step") or "").strip()
        if name not in STEP_ORDER:
            raise BundleError(
                f"Unknown bundle step {name!r}. Known steps: {', '.join(STEP_ORDER)}."
            )
        if name in seen:
            raise BundleError(f"Bundle lists step {name!r} more than once.")
        seen.append(name)

        operation = str(entry.get("operation") or "").strip()
        if operation != STEP_OPERATIONS[name]:
            raise BundleError(
                f"Step {name!r} declares operation {operation!r}, but that step is "
                f"always {STEP_OPERATIONS[name]!r}. A bundle may not rename an "
                f"operation to something the contract allows."
            )

        try:
            max_executions = int(entry.get("max_executions"))
        except (TypeError, ValueError) as exc:
            raise BundleError(f"Step {name!r} has a non-integer max_executions.") from exc
        if max_executions < 1:
            raise BundleError(
                f"Step {name!r} has max_executions={max_executions}. A bundle that "
                f"authorises zero runs should omit the step instead."
            )

        artifacts = entry.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            raise BundleError(f"Step {name!r} has a non-mapping artifacts block.")
        artifacts = {str(k): str(v).lower() for k, v in artifacts.items()}
        for required in REQUIRED_ARTIFACTS[name]:
            digest = artifacts.get(required)
            if not digest:
                raise BundleError(
                    f"Step {name!r} must pin '{required}'. A step that does not name "
                    f"what it acts on cannot be meaningfully approved."
                )
            if not SHA256.fullmatch(digest):
                raise BundleError(
                    f"Step {name!r} field '{required}' is not a 64-hex SHA-256: {digest!r}"
                )

        params = entry.get("params") or {}
        if not isinstance(params, dict):
            raise BundleError(f"Step {name!r} has a non-mapping params block.")

        steps.append(
            BundleStep(
                step=name,
                operation=operation,
                max_executions=max_executions,
                artifacts=artifacts,
                params=dict(params),
            )
        )

    # A prefix of STEP_ORDER, in order. Programming without a scan in the same
    # bundle is not a shortcut a signature should be able to grant.
    if seen != list(STEP_ORDER[: len(seen)]):
        raise BundleError(
            f"Bundle steps {seen} are out of order or skip an earlier step. A "
            f"bundle may authorise {' -> '.join(STEP_ORDER)} or any prefix of it, "
            f"nothing else."
        )

    signature = data.get("signature")
    if signature is not None and not isinstance(signature, dict):
        raise BundleError("Bundle 'signature' must be a mapping.")
    verified = False
    if verifier is not None and signature is not None:
        try:
            verified = bool(verifier.verify(canonical_payload(data), signature))
        except Exception as exc:  # noqa: BLE001 - a verifier that raises has failed
            raise BundleError(f"Signature verification raised {type(exc).__name__}: {exc}") from exc
        if not verified:
            raise BundleError(
                f"Bundle {data['bundle_id']} carries a signature that does not "
                f"verify. Do not run it and do not re-sign it without finding out "
                f"why it changed."
            )
    if require_signature and not verified:
        raise BundleError(
            f"Bundle {data['bundle_id']} is not cryptographically signed"
            + (" and no verifier was supplied" if signature is not None else "")
            + ". 'approved_by' is a name typed into a file, which is enough while "
            "a person is watching the run and not enough for unattended "
            "operation."
        )

    return ApprovalBundle(
        bundle_id=str(data["bundle_id"]).strip(),
        test_spec=str(data["test_spec"]).strip(),
        approved_by=str(data["approved_by"]).strip(),
        issued_on=issued,
        expires_on=expires,
        steps=tuple(steps),
        path=path,
        sha256=sha256_file(path),
        signature=signature,
        signature_verified=verified,
    )


def check_against_contract(bundle: ApprovalBundle, gate: SafetyGate) -> list[str]:
    """Cross-check a bundle against the contract it claims to act under.

    Returns the problems found; an empty list means the two documents agree.
    This is where a bundle naming a different bitstream than the pinned
    candidate is caught -- before a cable is touched, not after.
    """
    problems: list[str] = []
    spec_name = gate.contracts.test_spec_path.name
    if bundle.test_spec != spec_name:
        problems.append(
            f"Bundle is written against '{bundle.test_spec}' but the gate loaded "
            f"'{spec_name}'."
        )

    allowed = set(gate.contracts.test_spec.get("allowed_operations") or [])
    forbidden = set(gate.contracts.test_spec.get("forbidden_operations") or [])
    for step in bundle.steps:
        if step.operation in forbidden:
            problems.append(
                f"Step '{step.step}' needs '{step.operation}', which the contract "
                f"lists as forbidden."
            )
        elif allowed and step.operation not in allowed:
            problems.append(
                f"Step '{step.step}' needs '{step.operation}', which is not in the "
                f"contract's allowed_operations."
            )

    program = bundle.step("program")
    if program is not None:
        candidate = gate.contracts.test_spec.get("sram_program_candidate") or {}

        # program_cpld_sram() checks this too, and its check is the one that
        # counts -- a direct tool call must not get past it. Repeating it here
        # only moves the refusal earlier: without it the sequence would run a
        # scan, spend it out of the budget, and only then discover that the
        # step after it was never approved.
        # Three different situations, and telling a user the wrong one wastes
        # their time. "Not signed yet" and "already spent" both leave
        # approved=false, but only one of them is fixed by signing.
        used = int(candidate.get("approval_executions_used") or 0)
        allowed = int(candidate.get("approval_max_executions") or 0)
        if candidate.get("status") == "executed" or (allowed and used >= allowed):
            problems.append(
                f"The bundle authorises 'program', but Approval 2 is spent: "
                f"{used} of {allowed} permitted executions used"
                + (f" (run {candidate['execution_run']})" if candidate.get("execution_run") else "")
                + ". Programming again needs a new approval, not a new bundle."
            )
        elif candidate.get("approved") is not True:
            problems.append(
                "The bundle authorises 'program', but the contract's "
                "sram_program_candidate is not approved "
                f"(approved={candidate.get('approved')!r}, "
                f"status={candidate.get('status')!r}). Sign Approval 2 first, or "
                "issue a scan-only bundle."
            )

        pinned = str(candidate.get("bitstream_sha256") or "").lower()
        named = program.artifacts.get("bitstream_sha256", "")
        if not pinned:
            problems.append(
                "The contract has no sram_program_candidate.bitstream_sha256, so the "
                "bundle's program step cannot be checked against a pinned artifact."
            )
        elif pinned != named:
            problems.append(
                f"Bundle's program step names bitstream {named}, but the contract "
                f"pins {pinned}. One of the two is stale; do not guess which."
            )
    return problems


def verify_artifact(path: Path, expected_sha256: str, what: str) -> str:
    """Confirm a file on disk is the one the bundle signed for."""
    path = Path(path)
    if not path.is_file():
        raise BundleError(f"{what} named by the bundle does not exist: {path}")
    actual = sha256_file(path)
    if actual != expected_sha256.lower():
        raise BundleError(
            f"{what} at {path} has SHA-256 {actual}, but the bundle authorises "
            f"{expected_sha256.lower()}. This is a different file."
        )
    return actual
