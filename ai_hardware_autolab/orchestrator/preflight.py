"""Everything that can be refused before the first command goes out.

A hardware session is expensive in a way a unit test is not: the board has to
be powered, the cable has to be on, and somebody is standing there. Finding out
at step three that step three was never going to run wastes all of that, and
worse, it wastes it *after* steps one and two have already spent executions out
of a budget that does not refill.

So every check that can be made from files alone is made here, at once, and the
result is a list rather than the first failure. Someone reading a preflight
report should be able to fix everything in one pass.

Two severities, because they call for different decisions:

    blocking   the sequence cannot start at all
    advisory   the sequence can start, but a later authorised step will not
               run -- so it will stop partway by design

An advisory is not automatically a reason to abandon the session. Running a
scan while the measurement tool is still being written is a perfectly good use
of a bundle. It just has to be a decision somebody makes knowingly, which is
why it is reported rather than silently tolerated.

Nothing in this module touches hardware, spends an execution, or changes state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ai_hardware_autolab.hwtools.safety import SafetyGate

from .bundle import ApprovalBundle, BundleError, check_against_contract, verify_artifact
from .ledger import ConsumptionLedger

BLOCKING = "blocking"
ADVISORY = "advisory"


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    severity: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass
class PreflightReport:
    checks: list[PreflightCheck] = field(default_factory=list)

    def add(self, name: str, ok: bool, severity: str, detail: str) -> None:
        self.checks.append(
            PreflightCheck(name=name, ok=ok, severity=severity, detail=detail)
        )

    @property
    def blocking(self) -> list[PreflightCheck]:
        return [c for c in self.checks if not c.ok and c.severity == BLOCKING]

    @property
    def advisories(self) -> list[PreflightCheck]:
        return [c for c in self.checks if not c.ok and c.severity == ADVISORY]

    @property
    def ok(self) -> bool:
        """True when nothing blocks. Advisories do not make this False."""
        return not self.blocking

    def reason(self) -> str:
        return "; ".join(c.detail for c in self.blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocking": [c.as_dict() for c in self.blocking],
            "advisories": [c.as_dict() for c in self.advisories],
            "checks": [c.as_dict() for c in self.checks],
        }

    def __str__(self) -> str:
        lines = []
        for check in self.checks:
            mark = "ok  " if check.ok else ("FAIL" if check.severity == BLOCKING else "warn")
            lines.append(f"[{mark}] {check.name}: {check.detail}")
        return "\n".join(lines)


def _artifact_path(bundle: ApprovalBundle, step_name: str, artifact_key: str, root: Path) -> Path | None:
    step = bundle.step(step_name)
    if step is None:
        return None
    files = step.params.get("files") or {}
    location = files.get(artifact_key.removesuffix("_sha256"))
    if not location:
        return None
    path = Path(location)
    return path if path.is_absolute() else root / path


def preflight(
    gate: SafetyGate,
    bundle: ApprovalBundle,
    ledger: ConsumptionLedger,
    tools: Any,
    *,
    repo_root: Path | None = None,
    diamond_root: Path | None = None,
    require_signature: bool = False,
) -> PreflightReport:
    """Check everything checkable. Returns a report; raises nothing."""
    report = PreflightReport()
    root = repo_root or gate.contracts.test_spec_path.parents[1].parent
    authorised = [s.step for s in bundle.steps]

    # -- how much the approval is actually worth -----------------------------
    # Attended operation leans on the person at the bench, so an unsigned
    # bundle is reported rather than refused. Unattended operation has no such
    # person, and then the file is the only control: pass require_signature.
    report.add(
        "bundle_signature",
        bundle.signature_verified,
        BLOCKING if require_signature else ADVISORY,
        f"Signed and verified ({(bundle.signature or {}).get('algorithm')}, "
        f"key {(bundle.signature or {}).get('key_id')})."
        if bundle.signature_verified
        else f"Not cryptographically signed. 'approved_by: {bundle.approved_by}' is "
        f"a name typed into a file, so this bundle's authority rests on whoever "
        f"can write it. Acceptable while a person is watching the run; not for "
        f"unattended operation.",
    )

    # -- the bundle agrees with the contract it claims to act under ----------
    problems = check_against_contract(bundle, gate)
    report.add(
        "bundle_matches_contract",
        not problems,
        BLOCKING,
        "; ".join(problems) if problems else
        f"Bundle {bundle.bundle_id} agrees with {gate.contracts.test_spec_path.name}.",
    )

    # -- a human opened the gate at the entry point --------------------------
    report.add(
        "hardware_enabled",
        gate.allow_hardware,
        BLOCKING,
        "allow_hardware=True."
        if gate.allow_hardware
        else "The gate is in documents-only mode (allow_hardware=False). This is "
        "set by a human at the entry point and layer 2 may not change it.",
    )

    # -- the files on disk are the files that were signed for ----------------
    mismatches: list[str] = []
    for step in bundle.steps:
        for key, digest in step.artifacts.items():
            path = _artifact_path(bundle, step.step, key, root)
            if path is None:
                continue  # pinned by digest only; the tool resolves the file
            try:
                verify_artifact(path, digest, f"{step.step} {key}")
            except BundleError as exc:
                mismatches.append(str(exc))
    report.add(
        "artifacts_match_digests",
        not mismatches,
        BLOCKING,
        "; ".join(mismatches) if mismatches else
        "Every artifact the bundle names is present and matches its digest.",
    )

    # -- no unresolved attempt from a previous process -----------------------
    dangling = ledger.dangling(bundle.sha256)
    report.add(
        "no_dangling_attempts",
        not dangling,
        BLOCKING,
        "No unresolved attempts." if not dangling else
        f"{len(dangling)} attempt(s) were reserved and never resolved: "
        + ", ".join(e["entry_id"] for e in dangling)
        + ". A tool was called and its outcome was never written, so whether the "
        "board was touched is unknown from here. A human has to establish what "
        "happened before this bundle is used again.",
    )

    # -- budget, and tools that exist ----------------------------------------
    # Every authorised step is checked, and a missing tool blocks wherever it
    # sits -- not just on the first step. The reason is what the earlier steps
    # cost: `program` carries a one-execution approval, so starting a run whose
    # measurement cannot happen spends that approval and leaves the board
    # programmed with nothing measured. The approval does not come back.
    #
    # Budget works the same way. A sequence that will certainly stop partway
    # should not consume the steps in front of the stop.
    for name in authorised:
        step = bundle.step(name)
        assert step is not None
        left = ledger.remaining(bundle.sha256, name, step.max_executions)
        report.add(
            f"budget_{name}",
            left > 0,
            BLOCKING,
            f"{left} of {step.max_executions} execution(s) left."
            if left > 0
            else f"Step '{name}' has used all {step.max_executions} authorised "
            f"execution(s). A new bundle is needed, not a new run.",
        )
        tool = getattr(tools, name, None)
        report.add(
            f"tool_{name}",
            tool is not None,
            BLOCKING,
            f"{getattr(tool, '__name__', 'callable')} is available."
            if tool is not None
            else f"The layer 3 tool for '{name}' is not implemented yet. Nothing "
            f"runs: the steps before it would spend their approvals on a "
            f"sequence that cannot finish. Issue a bundle that stops before "
            f"'{name}' if the earlier steps are wanted on their own.",
        )

    # -- the programmer binary the JTAG steps need ---------------------------
    if {"scan", "program"} & set(authorised):
        try:
            from ai_hardware_autolab.hwtools.jtag import DEFAULT_DIAMOND_ROOT

            root_dir = Path(diamond_root or DEFAULT_DIAMOND_ROOT)
            pgrcmd = root_dir / "bin" / "nt64" / "pgrcmd.exe"
            report.add(
                "pgrcmd_present",
                pgrcmd.is_file(),
                BLOCKING,
                f"{pgrcmd} found."
                if pgrcmd.is_file()
                else f"{pgrcmd} not found. Set LSC_DIAMOND or pass diamond_root.",
            )
        except ImportError as exc:  # pragma: no cover - jtag.py is present
            report.add("pgrcmd_present", False, BLOCKING, f"jtag tool unavailable: {exc}")

    # -- surface the contract's stop conditions ------------------------------
    # Not a pass/fail check. These are the conditions a human watches for while
    # the sequence runs, and they belong in front of them before it starts.
    stops = gate.stop_conditions()
    report.add(
        "stop_conditions",
        True,
        ADVISORY,
        f"{len(stops)} stop condition(s) from the contract: " + " | ".join(stops)
        if stops
        else "The contract lists no stop conditions.",
    )

    return report
