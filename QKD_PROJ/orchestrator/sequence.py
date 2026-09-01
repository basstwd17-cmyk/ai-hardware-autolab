"""Layer 2 -- the Gate 1 state machine.

Plain Python. No AI, no thresholds, no interpretation. This layer decides only
*what runs next and whether it is allowed to*; every PASS/FAIL judgement stays
in layer 3 where the contract thresholds live, and every explanation of a
failure stays in layer 1.

The sequence is scan -> program -> measure, and it is not negotiable:

  * A step may only start from the state its predecessor leaves behind. There is
    no path to `program` that does not go through a passing `scan`.
  * A step runs only if the bundle authorises it and the ledger still has budget.
  * A failing step ends the sequence. Layer 2 never retries. A retry is a new
    decision, and decisions are layer 1's or a human's.

Two terminal failure states, kept apart because they call for different actions:

    ABORTED   a step ran and failed, or a stop condition was hit.
              The board has been touched. Read the run record.
    BLOCKED   the sequence could not legitimately start a step -- the bundle
              does not cover it, the budget is gone, or the tool does not
              exist yet. Nothing was touched.

A run that stopped *between* steps -- killed, closed, or blocked partway -- can
be resumed from the ground it gained, so a one-execution program approval is not
spent twice to reach a measurement. An ABORTED one cannot be resumed, because
doing so would step over a failure nobody looked at.

Layer 2 cannot power the board down; the bench supply has no remote control.
So `safe_off()` does not pretend to. It stops issuing commands and records the
manual actions a human still has to take.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from QKD_PROJ.hwtools.record import RunRecord
from QKD_PROJ.hwtools.result import FAIL, PASS, Artifact, ToolResult, utc_now
from QKD_PROJ.hwtools.safety import SafetyGate

from .bundle import ApprovalBundle
from .ledger import ConsumptionLedger, LedgerError
from .preflight import PreflightReport, _artifact_path, preflight


class Gate1State(str, Enum):
    INIT = "INIT"
    READY = "READY"
    SCANNED = "SCANNED"
    PROGRAMMED = "PROGRAMMED"
    MEASURED = "MEASURED"
    COMPLETE = "COMPLETE"
    ABORTED = "ABORTED"
    BLOCKED = "BLOCKED"


TERMINAL = {Gate1State.COMPLETE, Gate1State.ABORTED, Gate1State.BLOCKED}

# States that represent ground actually gained. `state` can jump to a terminal
# value from any of these; `progress` remembers how far the sequence got, which
# is what a later resume needs.
PROGRESS_STATES = (
    Gate1State.READY,
    Gate1State.SCANNED,
    Gate1State.PROGRAMMED,
    Gate1State.MEASURED,
)

# Which state a step may start from, and which it leaves behind.
STEP_STATES: dict[str, tuple[Gate1State, Gate1State]] = {
    "scan": (Gate1State.READY, Gate1State.SCANNED),
    "program": (Gate1State.SCANNED, Gate1State.PROGRAMMED),
    "measure": (Gate1State.PROGRAMMED, Gate1State.MEASURED),
}

# What a human must do by hand when the sequence stops. Layer 2 has no way to
# do any of it, so it says so rather than reporting a safe state it did not
# bring about.
SAFE_OFF_ACTIONS: tuple[str, ...] = (
    "Turn the bench supply off. The board has no remote power control.",
    "Leave CN1 disconnected. It is the only control over the analog output.",
    "Leave the JTAG cable in place until the run record has been read; "
    "unplugging it discards nothing but makes a re-check need a re-scan.",
)


class SequenceError(RuntimeError):
    """A caller drove the state machine wrong. Not a hardware failure."""


@dataclass
class StepTools:
    """The layer 3 functions this sequence calls.

    Injected rather than imported at call sites so the state machine can be
    tested without Diamond, a cable, or an oscilloscope -- and so a tool that
    does not exist yet is a None here rather than an ImportError at module load.
    """

    scan: Callable[..., ToolResult] | None = None
    program: Callable[..., ToolResult] | None = None
    measure: Callable[..., ToolResult] | None = None

    @classmethod
    def resolve(cls) -> "StepTools":
        from QKD_PROJ.hwtools.jtag import scan_jtag
        from QKD_PROJ.hwtools.sram import program_cpld_sram

        # measure_gate1_gpio is still being written. Leaving it None makes the
        # measure step BLOCKED with a clear reason instead of crashing the
        # import of everything above it.
        measure: Callable[..., ToolResult] | None = None
        try:  # pragma: no cover - depends on work in progress elsewhere
            from QKD_PROJ.hwtools.gpio import measure_gate1_gpio  # type: ignore

            measure = measure_gate1_gpio
        except ImportError:
            measure = None

        return cls(scan=scan_jtag, program=program_cpld_sram, measure=measure)


@dataclass
class Transition:
    at: str
    step: str | None
    frm: Gate1State
    to: Gate1State
    reason: str
    attempt: int | None = None
    verdict: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "step": self.step,
            "from": self.frm.value,
            "to": self.to.value,
            "reason": self.reason,
            "attempt": self.attempt,
            "verdict": self.verdict,
        }


def _result_from_dict(data: dict[str, Any]) -> ToolResult:
    """Rebuild a ToolResult that a previous process wrote to a manifest.

    Only what a downstream tool actually reads is restored. program_cpld_sram()
    re-checks the scan it is handed -- action, ok, verdict, device count and
    IDCODE -- so those have to survive the round trip exactly; the rest is
    carried for the record.
    """
    result = ToolResult(
        tool=data.get("tool", ""),
        action=data.get("action", ""),
        ok=bool(data.get("ok", False)),
        started_at=data.get("started_at", utc_now()),
        duration_s=float(data.get("duration_s", 0.0)),
        commands=list(data.get("commands") or []),
        measurements=dict(data.get("measurements") or {}),
        verdict=data.get("verdict"),
        warnings=list(data.get("warnings") or []),
        error=data.get("error"),
        versions=dict(data.get("versions") or {}),
    )
    result.artifacts = [Artifact(**a) for a in (data.get("artifacts") or [])]
    return result


@dataclass
class Gate1Sequence:
    gate: SafetyGate
    bundle: ApprovalBundle
    ledger: ConsumptionLedger
    record: RunRecord
    tools: StepTools = field(default_factory=StepTools.resolve)

    state: Gate1State = Gate1State.INIT
    progress: Gate1State = Gate1State.INIT
    transitions: list[Transition] = field(default_factory=list)
    results: dict[str, ToolResult] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    safe_off_required: bool = False
    resumed_from: str | None = None
    # Only meaningful on a resumed sequence: whether the caller states the board
    # stayed powered since the bitstream was loaded into volatile SRAM.
    power_maintained: bool | None = None
    preflight_report: PreflightReport | None = None
    # Filename of the bundle as copied into the run directory.
    bundle_copy: str | None = None

    # -- state bookkeeping ---------------------------------------------------

    def _move(
        self,
        to: Gate1State,
        reason: str,
        *,
        step: str | None = None,
        attempt: int | None = None,
        verdict: str | None = None,
    ) -> Gate1State:
        self.transitions.append(
            Transition(
                at=utc_now(),
                step=step,
                frm=self.state,
                to=to,
                reason=reason,
                attempt=attempt,
                verdict=verdict,
            )
        )
        self.state = to
        if to in PROGRESS_STATES:
            self.progress = to
        self._write()
        return to

    def _block(self, reason: str, *, step: str | None = None) -> Gate1State:
        self.blockers.append(reason)
        return self._move(Gate1State.BLOCKED, reason, step=step)

    def _abort(
        self, reason: str, *, step: str | None = None, verdict: str | None = None
    ) -> Gate1State:
        # A step ran, so the board has been touched. Everything after this needs
        # a human before it happens.
        self.safe_off_required = True
        return self._move(Gate1State.ABORTED, reason, step=step, verdict=verdict)

    # -- phase 1: preflight and verify --------------------------------------

    def _preserve_bundle(self) -> None:
        """Copy the bundle into the run directory, once.

        The digest alone proves a bundle changed; it does not show what it said.
        Since the bundle is the authority a run acted under, the run has to
        carry the text of it -- otherwise an approval can be edited or deleted
        afterwards and the record can only report that something no longer
        matches, which is the least useful moment to find out.
        """
        if self.bundle_copy is not None:
            return
        copied = self.record.adopt(self.bundle.path, "approval_bundle")
        self.bundle_copy = copied.name

    def preflight(self, **kwargs: Any) -> PreflightReport:
        """Run every check that can be made from files alone. Changes nothing."""
        report = preflight(self.gate, self.bundle, self.ledger, self.tools, **kwargs)
        self.preflight_report = report
        self._preserve_bundle()
        self._write()
        return report

    def verify(self, **kwargs: Any) -> Gate1State:
        """Preflight, then open the sequence for its first step.

        Nothing here touches hardware. Everything that can be refused is refused
        at this point, while refusing is free.
        """
        if self.state not in (Gate1State.INIT, *PROGRESS_STATES):
            raise SequenceError(
                f"verify() may not run from {self.state.value}."
            )
        if self.state is not Gate1State.INIT and self.resumed_from is None:
            raise SequenceError("verify() may only run once per sequence.")

        report = self.preflight(**kwargs)
        if not report.ok:
            return self._block("Preflight refused the run: " + report.reason())

        for advisory in report.advisories:
            self.blockers.append(f"advisory: {advisory.detail}")

        # A resumed sequence keeps the ground it already gained.
        if self.resumed_from is not None and self.progress in PROGRESS_STATES:
            return self._move(
                self.progress,
                f"Resumed from {self.resumed_from} at {self.progress.value}; "
                f"preflight passed.",
            )
        return self._move(
            Gate1State.READY,
            f"Bundle {self.bundle.bundle_id} ({self.bundle.sha256[:12]}) verified "
            f"against {self.gate.contracts.test_spec_path.name}.",
        )

    def _artifact_path(self, step_name: str, artifact_key: str) -> Path | None:
        root = self.gate.contracts.test_spec_path.parents[1].parent
        return _artifact_path(self.bundle, step_name, artifact_key, root)

    # -- phase 2: the steps --------------------------------------------------

    def _start_step(self, name: str) -> tuple[Any, Any] | None:
        """Shared preconditions. Returns (step, reservation) or None if refused."""
        expected_from, _ = STEP_STATES[name]
        if self.state in TERMINAL:
            self._blocked_note(
                f"Sequence is already {self.state.value}; '{name}' will not run."
            )
            return None
        if self.state is not expected_from:
            raise SequenceError(
                f"Step '{name}' may only start from {expected_from.value}, "
                f"not {self.state.value}."
            )

        step = self.bundle.step(name)
        if step is None:
            # Not a failure. A bundle that stops after scan is a legitimate
            # bundle; the sequence simply ends where the signature ends.
            self._move(
                Gate1State.COMPLETE,
                f"Bundle {self.bundle.bundle_id} does not authorise '{name}'. The "
                f"sequence ends where the approval ends.",
                step=name,
            )
            return None

        tool = getattr(self.tools, name)
        if tool is None:
            self._block(
                f"Step '{name}' is authorised but its layer 3 tool is not "
                f"implemented yet. Nothing was touched.",
                step=name,
            )
            return None

        try:
            reservation = self.ledger.reserve(
                self.bundle.sha256, name, step.max_executions
            )
        except LedgerError as exc:
            self._block(str(exc), step=name)
            return None
        return step, reservation

    def _finish_step(
        self, name: str, result: ToolResult, reservation: Any
    ) -> Gate1State:
        self.results[name] = result
        # No record.add() here. Every hwtools tool records itself in its own
        # _finish(), so adding it again would put the same ToolResult in
        # manifest.steps twice and make one run look like two.
        reservation.complete(verdict=result.verdict, run=self.record.dir.name)

        _, next_state = STEP_STATES[name]
        if not result.ok or result.verdict != PASS:
            return self._abort(
                f"Step '{name}' returned {result.verdict or 'no verdict'}: "
                f"{result.error or 'see the run record'}",
                step=name,
                verdict=result.verdict,
            )
        return self._move(
            next_state,
            f"Step '{name}' passed.",
            step=name,
            attempt=reservation.attempt,
            verdict=result.verdict,
        )

    def _blocked_note(self, message: str) -> None:
        self.blockers.append(message)
        self._write()

    def scan(self, **kwargs: Any) -> Gate1State:
        started = self._start_step("scan")
        if started is None:
            return self.state
        _step, reservation = started
        try:
            result = self.tools.scan(self.gate, self.record, **kwargs)  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - a raising tool still spent an attempt
            reservation.complete(verdict=FAIL, run=self.record.dir.name)
            return self._abort(
                f"Step 'scan' raised {type(exc).__name__}: {exc}", step="scan"
            )
        return self._finish_step("scan", result, reservation)

    def program(self, **kwargs: Any) -> Gate1State:
        started = self._start_step("program")
        if started is None:
            return self.state
        _step, reservation = started

        program_xcf = self._artifact_path("program", "program_xcf_sha256")
        if program_xcf is None:
            reservation.complete(verdict=None, run=self.record.dir.name)
            return self._block(
                "The bundle's program step does not say where program_xcf lives. "
                "Add it under params.files.program_xcf.",
                step="program",
            )
        try:
            # program_cpld_sram consumes the scan result directly, which is what
            # makes "program without a passing scan" impossible at layer 3 too.
            result = self.tools.program(  # type: ignore[misc]
                self.gate,
                self.results["scan"],
                program_xcf=program_xcf,
                record=self.record,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            reservation.complete(verdict=FAIL, run=self.record.dir.name)
            return self._abort(
                f"Step 'program' raised {type(exc).__name__}: {exc}", step="program"
            )
        return self._finish_step("program", result, reservation)

    def measure(self, **kwargs: Any) -> Gate1State:
        started = self._start_step("measure")
        if started is None:
            return self.state
        step, reservation = started
        params = {k: v for k, v in step.params.items() if k != "files"}
        try:
            result = self.tools.measure(  # type: ignore[misc]
                self.gate, self.record, params=params, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            reservation.complete(verdict=FAIL, run=self.record.dir.name)
            return self._abort(
                f"Step 'measure' raised {type(exc).__name__}: {exc}", step="measure"
            )
        return self._finish_step("measure", result, reservation)

    # -- driving -------------------------------------------------------------

    def run(self, **verify_kwargs: Any) -> Gate1State:
        """Verify, then run each authorised step in order until one stops it."""
        opened = self.verify(**verify_kwargs)
        if opened not in PROGRESS_STATES:
            return self.state
        for name in ("scan", "program", "measure"):
            # A resumed sequence skips what a previous process already did.
            if name in self.results:
                continue
            runner = getattr(self, name)
            if runner() in TERMINAL:
                return self.state
        return self._move(
            Gate1State.COMPLETE, "Every step the bundle authorises has passed."
        )

    def safe_off(self) -> list[str]:
        """Record what a human still has to do. Layer 2 cannot do any of it."""
        self.safe_off_required = True
        self._write()
        return list(SAFE_OFF_ACTIONS)

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "sequence": "gate1",
            "state": self.state.value,
            "progress": self.progress.value,
            "resumed_from": self.resumed_from,
            "bundle_copy": self.bundle_copy,
            "power_maintained": self.power_maintained,
            "updated_at": utc_now(),
            "bundle": self.bundle.describe(),
            "contract": self.gate.describe(),
            "ledger": self.ledger.summary(self.bundle.sha256),
            "preflight": self.preflight_report.as_dict() if self.preflight_report else None,
            "transitions": [t.as_dict() for t in self.transitions],
            "steps": {
                name: {
                    "tool": r.tool,
                    "action": r.action,
                    "ok": r.ok,
                    "verdict": r.verdict,
                    "error": r.error,
                }
                for name, r in self.results.items()
            },
            "blockers": list(self.blockers),
            "safe_off_required": self.safe_off_required,
            "safe_off_actions": list(SAFE_OFF_ACTIONS) if self.safe_off_required else [],
        }

    def _write(self) -> None:
        # Flushed on every transition so a crash still leaves the state visible.
        (self.record.dir / "sequence.json").write_text(
            json.dumps(self.summary(), indent=2, ensure_ascii=False), encoding="utf-8"
        )


# -- resuming ---------------------------------------------------------------


def resume_sequence(
    *,
    gate: SafetyGate,
    bundle: ApprovalBundle,
    ledger: ConsumptionLedger,
    record: RunRecord,
    resume_from: Path,
    tools: StepTools | None = None,
    power_maintained: bool | None = None,
) -> Gate1Sequence:
    """Continue a sequence a previous process left unfinished.

    The case this exists for: `program` succeeded and then the process ended --
    killed, closed, or blocked partway -- before `measure` ran. Re-running the
    whole sequence would spend the program approval a second time, and that
    approval is granted for one execution.

    What may be resumed is a run that stopped *between* steps, with the ground
    it gained intact:

      COMPLETE  refused -- there is nothing to continue.
      ABORTED   refused -- a step ran and failed. Resuming steps over a failure
                nobody has looked at, so it is not made convenient.
      dangling  refused -- an attempt was reserved and never resolved, so a
                tool was called and its outcome is unknown. Whether the board
                was touched cannot be established from here.

    `power_maintained` is required when resuming across the program/measure
    boundary. The MachXO2 holds this bitstream in SRAM, which is volatile: if
    the bench supply was cycled between the two processes the device is no
    longer programmed, and a measurement taken then describes an unconfigured
    chip while looking exactly like a real result. Layer 2 has no way to know
    whether power was held, so it asks rather than assumes.

    The resumed run gets its own RunRecord. The old directory stays exactly as
    it was, and the new one names it, so neither is rewritten to tell the story
    of the other.
    """
    resume_from = Path(resume_from)
    state_file = resume_from / "sequence.json"
    if not state_file.is_file():
        raise SequenceError(f"No sequence.json to resume from in {resume_from}.")
    prior = json.loads(state_file.read_text(encoding="utf-8"))

    prior_sha = (prior.get("bundle") or {}).get("bundle_sha256")
    if prior_sha != bundle.sha256:
        raise SequenceError(
            f"{resume_from.name} ran under bundle {prior_sha}, not {bundle.sha256}. "
            f"A different bundle is a different approval, so this would be a new "
            f"run rather than a resume."
        )

    prior_state = Gate1State(prior.get("state", Gate1State.INIT.value))
    if prior_state is Gate1State.COMPLETE:
        raise SequenceError(
            f"{resume_from.name} is COMPLETE; there is nothing to continue."
        )
    if prior_state is Gate1State.ABORTED:
        raise SequenceError(
            f"{resume_from.name} is ABORTED: a step ran and failed. Read the run "
            f"record and decide what to do, rather than resuming past it."
        )

    unresolved = ledger.dangling(bundle.sha256)
    if unresolved:
        raise SequenceError(
            f"{resume_from.name} has {len(unresolved)} unresolved attempt(s) in the "
            f"ledger ({', '.join(e['entry_id'] for e in unresolved)}). A tool was "
            f"called and its outcome was never written, so whether the board was "
            f"touched is unknown. A human has to establish what happened before "
            f"this bundle is used again."
        )

    progress = Gate1State(prior.get("progress", Gate1State.INIT.value))
    if progress not in PROGRESS_STATES:
        raise SequenceError(
            f"{resume_from.name} never got past {progress.value}, so there is no "
            f"ground to resume from. Start a new sequence."
        )

    # Rebuild the ToolResults the next step will be handed. They come from the
    # manifest rather than sequence.json, because sequence.json holds a summary
    # and program_cpld_sram() re-checks fields the summary does not carry.
    manifest_file = resume_from / "manifest.json"
    if not manifest_file.is_file():
        raise SequenceError(f"No manifest.json to restore results from in {resume_from}.")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    by_action = {s.get("action"): s for s in manifest.get("steps") or []}

    results: dict[str, ToolResult] = {}
    for name, action in (("scan", "jtag_scan"), ("program", "program_sram")):
        done = name in (prior.get("steps") or {})
        if not done:
            continue
        data = by_action.get(action) or by_action.get(
            (prior["steps"][name] or {}).get("action", "")
        )
        if data is None:
            raise SequenceError(
                f"{resume_from.name} records a completed '{name}' step, but its "
                f"ToolResult is not in manifest.json. The evidence for a step that "
                f"already ran is missing, so it will not be assumed."
            )
        results[name] = _result_from_dict(data)

    # Crossing the program/measure boundary in a new process is the one resume
    # that can produce a confidently wrong number rather than an error.
    if "program" in results and bundle.authorises("measure") and "measure" not in results:
        if power_maintained is not True:
            raise SequenceError(
                f"{resume_from.name} programmed the CPLD, and this resume would "
                f"measure it from a new process. The MachXO2 holds that bitstream "
                f"in SRAM, which is volatile: if the bench supply was cycled in "
                f"between, the device is no longer configured and a measurement "
                f"would describe an unconfigured chip while looking like a real "
                f"result. Pass power_maintained=True only if the board stayed "
                f"powered, or re-run the program step under a new approval."
            )

    sequence = Gate1Sequence(
        gate=gate,
        bundle=bundle,
        ledger=ledger,
        record=record,
        tools=tools if tools is not None else StepTools.resolve(),
        state=progress,
        progress=progress,
        results=results,
        resumed_from=resume_from.name,
        power_maintained=power_maintained,
    )
    sequence.transitions.append(
        Transition(
            at=utc_now(),
            step=None,
            frm=Gate1State.INIT,
            to=progress,
            reason=(
                f"Resumed from {resume_from.name}, which stopped at "
                f"{progress.value} in state {prior_state.value}"
                + (
                    ": " + "; ".join(prior["blockers"])
                    if prior.get("blockers")
                    else ""
                )
                + (
                    ". Caller states the board stayed powered."
                    if power_maintained
                    else ""
                )
            ),
        )
    )
    sequence._write()
    return sequence
