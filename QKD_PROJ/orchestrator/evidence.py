"""Deciding whether a condition is met, without asking a model.

Layer 3 judges one tool call against one contract and writes a RunRecord.
Nothing until now judged a *gate*: whether the conditions a plan set out are
all satisfied, by which runs, and whether the loop should stop. P1 left that
to the verifier agent -- which meant the last step of a chain built on "tools
judge, models interpret" was performed by a model reading JSON.

The schema in ``agents/schema.py`` catches a condition marked met with no
evidence. It cannot catch a condition marked met against evidence that says
otherwise, because checking that means reading the run.

So this module reads the run. It resolves the field the condition names,
compares it with the operator the condition carries, recomputes the artifact
hashes, and returns a verdict that no prompt participates in. The verifier
agent still has a job -- explaining *why* something failed and proposing the
next experiment -- but it can no longer decide *whether*.

Three rules follow from that:

* **A field that is absent is not a failure, and not a pass.** It is
  ``evidence_missing``. The difference matters: a failed measurement tells you
  about the board, a missing one tells you about the process.
* **An ambiguous field is not resolved by picking one.** If two steps in a run
  both recorded ``bit_errors`` with different values, the condition did not say
  which one it meant. Guessing would produce a defensible-looking number.
* **Hashes are recomputed, not trusted.** A manifest listing an artifact's
  digest proves what the tool saw at the time. Re-hashing the file proves the
  evidence still says that today.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

from QKD_PROJ.hwtools.result import sha256_file

QKD_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = QKD_ROOT / "runs"

# How many iterations of the identical failure before a loop is declared dead.
DEFAULT_STRIKES = 3

MET = "met"
UNMET = "unmet"
EVIDENCE_MISSING = "evidence_missing"
AMBIGUOUS = "ambiguous"

# Why a run could not answer. "The run is gone" and "the run exists but wrote no
# record" are different failures with different fixes, and collapsing them into
# one message sent someone looking in the wrong place once already.
RUN_PRESENT = "present"
RUN_NO_RECORD = "no_record"
RUN_ABSENT = "absent"

# Where a value came from. A number the tool recorded in the manifest is pinned
# by the tool. A number read out of a side file in the run directory is real
# evidence, but its digest was never registered by anything -- so a condition
# met that way is met, and says so.
FROM_MANIFEST = "manifest"
FROM_SIDE_FILE = "side_file"

SATISFIED = "satisfied"
OPEN = "open"
EXHAUSTED = "exhausted"

# The gate cannot be decided by reading harder: a run it depends on is not on
# disk. Distinct from `open`, which means the evidence exists and disagrees, and
# from `exhausted`, which means the experiment stopped producing new evidence.
# Only an approved re-run changes this one.
PRIMARY_RUN_MISSING = "primary_run_missing"

_OPERATOR_TEXT = re.compile(r"^\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$")
_OPERATORS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "in": lambda a, b: a in b,
    "contains": lambda a, b: b in a,
    "empty": lambda a, _: len(a) == 0,
}


class EvidenceError(ValueError):
    """The condition itself is malformed. Distinct from a condition being unmet."""


# -- conditions --------------------------------------------------------------


@dataclass(frozen=True)
class Condition:
    """One advance condition out of a Plan step.

    ``expect`` accepts three forms, in increasing explicitness:

        0                       equality
        "<= 10"                 comparison, parsed from the string
        {"op": "in", "value": [...]}

    ``run`` names the run directory that is supposed to contain the answer.
    A condition with no run cannot be checked -- that is a planning defect, and
    it surfaces as ``evidence_missing`` rather than being quietly skipped.
    """

    field: str
    expect: Any
    source: str = ""
    run: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Condition":
        if "field" not in data or "expect" not in data:
            raise EvidenceError(f"a condition needs 'field' and 'expect': {data!r}")
        return cls(
            field=str(data["field"]),
            expect=data["expect"],
            source=str(data.get("source", "")),
            run=data.get("run"),
        )

    def operator(self) -> tuple[str, Any]:
        """Return (op, value) for this expectation."""
        expect = self.expect
        if isinstance(expect, dict):
            op = str(expect.get("op", "=="))
            if op not in _OPERATORS:
                raise EvidenceError(f"unknown operator {op!r} in {self.field}")
            if op != "empty" and "value" not in expect:
                raise EvidenceError(f"operator {op!r} needs a 'value' in {self.field}")
            return op, expect.get("value")
        if isinstance(expect, str):
            match = _OPERATOR_TEXT.match(expect)
            if match:
                op, raw = match.group(1), match.group(2)
                try:
                    return op, json.loads(raw)
                except json.JSONDecodeError:
                    return op, raw
        return "==", expect

    def describe(self) -> str:
        op, value = self.operator()
        return f"{value!r}" if op == "==" else f"{op} {value!r}"


@dataclass(frozen=True)
class ConditionResult:
    field: str
    expect: str
    observed: Any
    status: str
    evidence: list[str]
    detail: str = ""
    tool: str | None = None
    provenance: str | None = None
    run_presence: str | None = None

    @property
    def met(self) -> bool:
        return self.status == MET

    def describe(self) -> dict[str, Any]:
        """The shape a Verdict envelope and the evidence drawer both render."""
        return {
            "field": self.field,
            "expect": self.expect,
            "observed": self.observed,
            "met": self.met,
            "status": self.status,
            "evidence": list(self.evidence),
            "detail": self.detail,
            "provenance": self.provenance,
            "run_presence": self.run_presence,
        }


@dataclass(frozen=True)
class Reduction:
    """Everything decided about a gate, with nothing decided by a model."""

    gate: str
    iteration: int
    outcome: str
    conditions: list[ConditionResult]
    failure_signature: str | None = None
    hash_mismatches: list[dict[str, str]] = dc_field(default_factory=list)
    strikes: int = 0
    corroboration: list[dict[str, Any]] = dc_field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return self.outcome == SATISFIED

    def describe(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "iteration": self.iteration,
            "outcome": self.outcome,
            "conditions": [c.describe() for c in self.conditions],
            "failure_signature": self.failure_signature,
            "hash_mismatches": self.hash_mismatches,
            "strikes": self.strikes,
            "corroboration": self.corroboration,
        }


# -- reading a run -----------------------------------------------------------


def classify_run(run: str, runs_dir: Path | None = None) -> str:
    """Absent, present-but-recordless, or present.

    ``gate10`` cites two runs that both report "missing". One directory is not
    on disk at all; the other holds a capture and a summary but no manifest,
    because the tool that wrote it never opened a RunRecord. The first needs a
    re-run, the second needs the capture path fixed. One message for both sends
    the next person to the wrong problem.
    """
    directory = Path(runs_dir or RUNS_DIR) / run
    if not directory.is_dir():
        return RUN_ABSENT
    if not (directory / "manifest.json").is_file():
        return RUN_NO_RECORD
    return RUN_PRESENT


def read_side_file(run: str, filename: str, runs_dir: Path | None = None) -> Any:
    """A JSON file inside a run directory that no manifest registers."""
    path = Path(runs_dir or RUNS_DIR) / run / filename
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def registered_artifacts(manifest: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for artifact in manifest.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("path"):
            names.add(str(artifact["path"]).replace("\\", "/").split("/")[-1])
    for step in manifest.get("steps") or []:
        for artifact in step.get("artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("path"):
                names.add(str(artifact["path"]).replace("\\", "/").split("/")[-1])
    return names


def load_manifest(run: str, runs_dir: Path | None = None) -> dict[str, Any] | None:
    """The manifest of one run, or None when there is nothing to read."""
    directory = Path(runs_dir or RUNS_DIR) / run
    path = directory / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def resolve_field(manifest: dict[str, Any], path: str) -> tuple[str, Any, str | None]:
    """Find ``path`` in a manifest. Returns (status, value, tool).

    Three shapes are understood, because three are what plans actually write:

        verdict                            a top-level key
        measurements.bit_errors            any step that recorded it
        steps.program_sram.measurements.x  one named step

    The middle form is the one that can go wrong. Searching every step is what
    makes a condition writable before you know which step will answer it, but
    if two steps disagree the condition is under-specified and says so.
    """
    steps = manifest.get("steps") or []

    if path.startswith("steps."):
        parts = path.split(".", 2)
        action = parts[1] if len(parts) > 1 else ""
        remainder = parts[2] if len(parts) > 2 else ""
        matching = [s for s in steps if s.get("action") == action]
        if not matching:
            return EVIDENCE_MISSING, None, None
        if len(matching) > 1:
            return AMBIGUOUS, None, f"{action} ran {len(matching)} times in this run"
        found, value = _walk(matching[0], remainder)
        return (MET if found else EVIDENCE_MISSING), value, matching[0].get("tool")

    if path.startswith("measurements."):
        key = path[len("measurements.") :]
        hits = []
        for step in steps:
            found, value = _walk(step.get("measurements") or {}, key)
            if found:
                hits.append((value, step.get("tool"), step.get("action")))
        if not hits:
            return EVIDENCE_MISSING, None, None
        distinct = {json.dumps(v, sort_keys=True, default=str) for v, _, _ in hits}
        if len(distinct) > 1:
            actions = ", ".join(str(a) for _, _, a in hits)
            return AMBIGUOUS, [v for v, _, _ in hits], f"recorded by: {actions}"
        return MET, hits[0][0], hits[0][1]

    found, value = _walk(manifest, path)
    return (MET if found else EVIDENCE_MISSING), value, None


def _walk(data: Any, path: str) -> tuple[bool, Any]:
    if not path:
        return True, data
    node = data
    for segment in path.split("."):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
        elif isinstance(node, list) and segment.isdigit() and int(segment) < len(node):
            node = node[int(segment)]
        else:
            return False, None
    return True, node


def verify_artifacts(run: str, runs_dir: Path | None = None) -> list[dict[str, str]]:
    """Re-hash every artifact a run claims. Returns the mismatches.

    A manifest digest records what the tool saw. Recomputing it records what
    the evidence says now. Those are the same number right up until they are
    not, and a gate that closed on a file that has since changed is the case
    this exists to catch.
    """
    directory = Path(runs_dir or RUNS_DIR) / run
    manifest = load_manifest(run, runs_dir)
    if manifest is None:
        return [{"run": run, "problem": "no manifest"}]

    mismatches: list[dict[str, str]] = []
    for artifact in _all_artifacts(manifest):
        claimed = artifact.get("sha256")
        relative = artifact.get("path")
        if not claimed or not relative:
            continue
        path = directory / Path(str(relative)).name
        if not path.is_file():
            mismatches.append({"run": run, "artifact": str(relative), "problem": "file missing"})
            continue
        actual = sha256_file(path)
        if actual != claimed:
            mismatches.append(
                {
                    "run": run,
                    "artifact": str(relative),
                    "problem": "hash changed",
                    "claimed": claimed,
                    "actual": actual,
                }
            )
    return mismatches


def _all_artifacts(manifest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for artifact in manifest.get("artifacts") or []:
        if isinstance(artifact, dict):
            yield artifact
    for step in manifest.get("steps") or []:
        for artifact in step.get("artifacts") or []:
            if isinstance(artifact, dict):
                yield artifact


# -- the reducer -------------------------------------------------------------


def compare(observed: Any, condition: Condition) -> bool:
    op, expected = condition.operator()
    try:
        return bool(_OPERATORS[op](observed, expected))
    except TypeError:
        # Comparing a string to a number, say. Not an exception the loop should
        # crash on -- it is a condition that does not hold, and the detail says why.
        return False


def evaluate(condition: Condition, runs_dir: Path | None = None) -> ConditionResult:
    """One condition against the run it names. No model involved."""
    expect_text = condition.describe()

    if not condition.run:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[],
            detail="the condition names no run, so nothing can be read",
        )

    presence = classify_run(condition.run, runs_dir)
    if presence == RUN_ABSENT:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[condition.run],
            detail=f"runs/{condition.run} is not on disk; the evidence this condition names "
            "does not exist",
            run_presence=presence,
        )

    manifest = load_manifest(condition.run, runs_dir)

    # A side file is readable whether or not the run wrote a record. Refusing to
    # look because the manifest is absent would report a measurement that is
    # sitting right there as missing.
    if condition.field.startswith("file:"):
        return _evaluate_side_file(condition, manifest or {}, expect_text, presence, runs_dir)

    if manifest is None:
        detail = (
            f"runs/{condition.run} is not on disk; the evidence this condition names does not exist"
            if presence == RUN_ABSENT
            else f"runs/{condition.run} exists but wrote no RunRecord, so nothing can be read from it"
        )
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[condition.run],
            detail=detail,
            run_presence=presence,
        )

    status, observed, tool_or_reason = resolve_field(manifest, condition.field)
    tool = tool_or_reason
    if status == EVIDENCE_MISSING:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[condition.run],
            detail=f"{condition.field} is not recorded in this run",
        )
    if status == AMBIGUOUS:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=observed,
            status=AMBIGUOUS,
            evidence=[condition.run],
            detail=(
                f"{condition.field} is recorded more than once with different values "
                f"({tool_or_reason}); the condition does not say which step it means"
            ),
            tool=None,
        )

    met = compare(observed, condition)
    return ConditionResult(
        field=condition.field,
        expect=expect_text,
        observed=observed,
        status=MET if met else UNMET,
        evidence=[condition.run],
        detail="" if met else f"observed {observed!r}, expected {expect_text}",
        tool=tool,
        provenance=FROM_MANIFEST,
        run_presence=presence,
    )


def _evaluate_side_file(
    condition: Condition,
    manifest: dict[str, Any],
    expect_text: str,
    presence: str,
    runs_dir: Path | None,
) -> ConditionResult:
    """Read `file:<name>:<path>` out of a file sitting in the run directory.

    This exists because real evidence sometimes lands beside the record instead
    of inside it -- ``digital_verification.json`` holds the only machine-written
    GPIO round-trip result in the corpus, and no manifest lists it. Refusing to
    read it would call a measured result missing. Reading it silently would let
    an unpinned file look tool-verified.

    So it is read, and the provenance says where it came from. A condition met
    from an unregistered file is met and flagged; that flag is what tells a
    reviewer the digest behind this number was never taken by a tool.
    """
    _, _, remainder = condition.field.partition("file:")
    filename, _, path = remainder.partition(":")
    assert condition.run is not None
    data = read_side_file(condition.run, filename, runs_dir)
    if data is None:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[condition.run],
            detail=f"{filename} is not readable in runs/{condition.run}",
            run_presence=presence,
        )

    found, observed = _walk(data, path)
    if not found:
        return ConditionResult(
            field=condition.field,
            expect=expect_text,
            observed=None,
            status=EVIDENCE_MISSING,
            evidence=[condition.run],
            detail=f"{path} is not in {filename}",
            provenance=FROM_SIDE_FILE,
            run_presence=presence,
        )

    registered = filename in registered_artifacts(manifest)
    note = "" if registered else f"{filename} is not a registered artifact of this run"
    met = compare(observed, condition)
    return ConditionResult(
        field=condition.field,
        expect=expect_text,
        observed=observed,
        status=MET if met else UNMET,
        evidence=[condition.run],
        detail=note if met else f"observed {observed!r}, expected {expect_text}. {note}".strip(),
        provenance=FROM_MANIFEST if registered else FROM_SIDE_FILE,
        run_presence=presence,
    )


def failure_signature(gate: str, results: Sequence[ConditionResult]) -> str | None:
    """A stable name for *this* failure, so a repeat is recognisable.

    Deliberately includes the observed value. A condition that fails at 3 and
    then at 7 is new information and resets the count; failing at 3 three times
    running is not, and that is what the strike rule is for.
    """
    failing = [r for r in results if r.status != MET]
    if not failing:
        return None
    parts = [f"{r.field}|{r.tool or '-'}|{json.dumps(r.observed, sort_keys=True, default=str)}" for r in sorted(failing, key=lambda r: r.field)]
    return f"{gate}|" + ";".join(parts)


def check_corroboration(
    entries: Sequence[dict[str, Any]], runs_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Report what a supporting run actually contains, and never let it close a gate.

    Corroboration is the honest name for "a different run points the same way".
    It is worth recording and it is not a substitute: replacing a missing
    citation with a supporting one does not fill the gap, it moves it somewhere
    less visible. So each entry is resolved and reported -- present or not, and
    whether it records the field the primary evidence was supposed to -- with
    ``closes_gate`` pinned to false so no later reader has to infer it.
    """
    checked: list[dict[str, Any]] = []
    for entry in entries:
        run = str(entry.get("run", ""))
        presence = classify_run(run, runs_dir) if run else RUN_ABSENT
        manifest = load_manifest(run, runs_dir) if presence == RUN_PRESENT else None
        record = {
            "run": run,
            "supports": entry.get("supports", ""),
            "presence": presence,
            "closes_gate": False,
            "note": str(entry.get("note", "")),
        }
        field = entry.get("field")
        if field and manifest is not None:
            status, observed, _ = resolve_field(manifest, str(field))
            record["field"] = field
            record["recorded"] = status == MET
            record["observed"] = observed if status == MET else None
        elif field:
            record["field"] = field
            record["recorded"] = False
            record["observed"] = None
        checked.append(record)
    return checked


def reduce_gate(
    gate: str,
    conditions: Sequence[Condition | dict[str, Any]],
    iteration: int = 1,
    history: Sequence[str] = (),
    runs_dir: Path | None = None,
    strikes: int = DEFAULT_STRIKES,
    corroboration: Sequence[dict[str, Any]] = (),
) -> Reduction:
    """Decide a gate from its conditions and the runs behind them.

    ``history`` is the failure signature of each previous iteration, oldest
    first. It comes from the ledger, not from a model's memory of the session
    -- an agent that holds its own strike count loses it at the first
    rate-limit window, and a loop that forgets it failed is a loop that runs
    forever.
    """
    parsed = [c if isinstance(c, Condition) else Condition.from_dict(c) for c in conditions]
    if not parsed:
        raise EvidenceError("a gate with no conditions cannot be decided")

    results = [evaluate(c, runs_dir) for c in parsed]
    supporting = check_corroboration(list(corroboration), runs_dir)

    mismatches: list[dict[str, str]] = []
    for run in dict.fromkeys(c.run for c in parsed if c.run):
        if classify_run(str(run), runs_dir) == RUN_PRESENT:
            mismatches.extend(verify_artifacts(str(run), runs_dir))

    signature = failure_signature(gate, results)
    if signature is None and not mismatches:
        return Reduction(gate, iteration, SATISFIED, results, None, [], 0, supporting)

    if signature is None:
        # Every condition holds but an artifact no longer hashes to what the
        # run claimed. That is not a satisfied gate; it is a broken record.
        return Reduction(
            gate, iteration, OPEN, results, f"{gate}|artifact-hash-mismatch", mismatches, 0, supporting
        )

    repeats = 0
    for previous in reversed(list(history)):
        if previous != signature:
            break
        repeats += 1
    consecutive = repeats + 1

    # A gone run outranks the strike count. Re-running the loop cannot produce
    # evidence that is not on disk, so calling this `exhausted` after three
    # tries would describe the wrong problem and suggest the wrong fix.
    if any(r.run_presence == RUN_ABSENT for r in results):
        outcome = PRIMARY_RUN_MISSING
    elif consecutive >= strikes:
        outcome = EXHAUSTED
    else:
        outcome = OPEN
    return Reduction(gate, iteration, outcome, results, signature, mismatches, consecutive, supporting)


# -- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Reduce one gate from a JSON file of conditions.

    The file is the machine-readable half of a Plan: a gate name, its advance
    conditions, and any corroboration the contract declares. Keeping it a file
    rather than a script means the reduction can be re-run later and compared,
    which is the whole point of deciding this deterministically.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Decide a gate from run evidence.")
    parser.add_argument("conditions", help="JSON file: {gate, iteration?, conditions[], corroboration[]}")
    parser.add_argument("--runs-dir")
    parser.add_argument("--out", help="write the reduction JSON here instead of stdout")
    args = parser.parse_args(argv)

    spec = json.loads(Path(args.conditions).read_text(encoding="utf-8"))
    reduction = reduce_gate(
        gate=str(spec["gate"]),
        conditions=spec["conditions"],
        iteration=int(spec.get("iteration", 1)),
        history=spec.get("history", []),
        runs_dir=Path(args.runs_dir) if args.runs_dir else None,
        corroboration=spec.get("corroboration", []),
    )
    payload = json.dumps(reduction.describe(), ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
        print(f"wrote {out}  outcome={reduction.outcome}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
