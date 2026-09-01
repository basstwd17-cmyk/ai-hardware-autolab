"""Audit every contract against the runs it cites.

A contract carries two kinds of statement. Some are permissions -- what may be
done to the board -- and a person signs those. The rest are *claims*: this
build passed, that bitstream has this digest, this readback had no bit errors.
Claims are written after the fact, by hand or by a tool, into a file that is
also the thing everyone reads to decide what happened.

Nothing has been checking them. ``runs/`` is gitignored, so contracts and
candidates are the surviving evidence chain, and a claim that drifted from its
run is indistinguishable from one that did not -- until someone plans a gate on
top of it.

This walks the whole corpus and answers four questions per claim:

    Does the run it names exist?
    Does that run's manifest agree it belongs to this contract?
    Do the artifacts still hash to what the run recorded?
    Where the contract states a number the run also recorded, do they match?

The fourth is the one with teeth, and the one that has to be careful: plenty of
legitimate numbers live in side files rather than in the manifest, so a value
the manifest never recorded is reported as *unevidenced*, not as wrong. Saying
"this is a lie" when the truth is "this cannot be checked here" would be the
same mistake the audit exists to find.

    py -3 -m QKD_PROJ.orchestrator.audit               human-readable report
    py -3 -m QKD_PROJ.orchestrator.audit --json        machine-readable
    py -3 -m QKD_PROJ.orchestrator.audit --markdown FILE
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from QKD_PROJ.orchestrator.evidence import (
    RUN_ABSENT,
    classify_run,
    load_manifest,
    verify_artifacts,
)

QKD_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = QKD_ROOT / "contracts"
RUNS_DIR = QKD_ROOT / "runs"

RUN_NAME = re.compile(r"^\d{8}T\d{6}Z_[A-Za-z0-9._-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")

ERROR = "error"
WARNING = "warning"
INFO = "info"

# Keys a contract states that a manifest also records under the same name.
# Restricted on purpose: a wider net turns prose fields into false positives.
CHECKED_SCALARS = {
    "bit_errors",
    "drc_errors",
    "timing_errors",
    "device_count",
    "bitstream_bytes",
    "user_io_count",
    "expected_device_count",
    "idcode",
    "expected_idcode",
    "max_frequency_mhz",
}


@dataclass
class Finding:
    severity: str
    kind: str
    detail: str
    contract: str | None = None
    run: str | None = None
    field: str | None = None
    claimed: Any = None
    recorded: Any = None

    def describe(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class AuditReport:
    contracts: int = 0
    runs: int = 0
    claims: int = 0
    findings: list[Finding] = dc_field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    def describe(self) -> dict[str, Any]:
        return {
            "contracts": self.contracts,
            "runs": self.runs,
            "claims": self.claims,
            "counts": {s: len(self.by_severity(s)) for s in (ERROR, WARNING, INFO)},
            "findings": [f.describe() for f in self.findings],
        }


# -- walking a contract ------------------------------------------------------


def iter_nodes(node: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Every (dotted path, value) in a parsed YAML document."""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from iter_nodes(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_nodes(value, f"{path}[{index}]")


def cited_runs(contract: dict[str, Any]) -> dict[str, str]:
    """Run name -> the contract path that cites it."""
    found: dict[str, str] = {}
    for path, value in iter_nodes(contract):
        if isinstance(value, str) and RUN_NAME.match(value) and path.split(".")[-1].startswith("run"):
            found.setdefault(value, path)
        if isinstance(value, str) and value.startswith("runs/"):
            candidate = value.split("/")[1]
            if RUN_NAME.match(candidate):
                found.setdefault(candidate, path)
    return found


@dataclass(frozen=True)
class Scope:
    """A block of a contract, and the one run its claims are about.

    Scoping matters more than it looks. A contract that records four builds --
    two rejected, one superseded, one kept -- states four sets of numbers, and
    comparing every number against every run manufactures contradictions that
    are really just claims about different runs. A claim belongs to the block
    it was written in, and that block names its own run.
    """

    path: str
    run: str
    node: dict[str, Any]


def claim_scopes(contract: dict[str, Any]) -> list[Scope]:
    scopes: list[Scope] = []
    for path, value in iter_nodes(contract):
        if not isinstance(value, dict):
            continue
        run = value.get("run")
        if isinstance(run, str) and RUN_NAME.match(run):
            scopes.append(Scope(path, run, value))
            continue
        # `evidence: {file: runs/<run>/x.json, sha256: ...}`
        reference = value.get("file")
        if isinstance(reference, str) and reference.startswith("runs/"):
            candidate = reference.split("/")[1]
            if RUN_NAME.match(candidate):
                scopes.append(Scope(path, candidate, value))
    return scopes


def manifest_index(manifest: dict[str, Any]) -> dict[str, list[Any]]:
    """Every scalar the manifest recorded, keyed by its leaf name."""
    index: dict[str, list[Any]] = {}
    for path, value in iter_nodes(manifest):
        if isinstance(value, (dict, list)):
            continue
        leaf = path.split(".")[-1].split("[")[0]
        index.setdefault(leaf, []).append(value)
    return index


def _normalise(value: Any) -> Any:
    """0x012BC043 and 19644483 are the same idcode written two ways."""
    if isinstance(value, str) and value.lower().startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return value
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _same(a: Any, b: Any) -> bool:
    a, b = _normalise(a), _normalise(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return a == b


# -- the audit ---------------------------------------------------------------


def audit_contract(path: Path, report: AuditReport, runs_dir: Path) -> None:
    try:
        contract = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        report.add(Finding(ERROR, "unparsable_contract", str(exc), contract=path.name))
        return
    if not isinstance(contract, dict):
        report.add(Finding(ERROR, "unparsable_contract", "not a mapping", contract=path.name))
        return

    report.contracts += 1
    scopes = claim_scopes(contract)
    _audit_candidates(contract, path.name, report)
    _audit_unscoped_claims(contract, path.name, scopes, report)

    if not scopes:
        status = str(contract.get("status", ""))
        # A contract that claims a result without naming a run has nothing
        # behind it. A plan-only contract naming no run is fine.
        if any(k.endswith("_result") for k in contract):
            report.add(
                Finding(
                    WARNING,
                    "result_without_run",
                    f"contract records a result block but cites no run (status: {status})",
                    contract=path.name,
                )
            )
        return

    checked_runs: set[str] = set()
    for scope in scopes:
        report.claims += 1
        manifest = load_manifest(scope.run, runs_dir)
        if manifest is None:
            presence = classify_run(scope.run, runs_dir)
            if presence == RUN_ABSENT:
                report.add(
                    Finding(
                        ERROR,
                        "primary_run_missing",
                        f"cited at {scope.path}, but runs/{scope.run} is not on disk. The claims "
                        "in this block have no evidence behind them; only an approved re-run "
                        "can restore it.",
                        contract=path.name,
                        run=scope.run,
                    )
                )
            else:
                files = sorted(
                    p.name for p in (Path(runs_dir) / scope.run).iterdir() if p.is_file()
                )
                report.add(
                    Finding(
                        ERROR,
                        "run_record_missing",
                        f"cited at {scope.path}: runs/{scope.run} exists and holds "
                        f"{', '.join(files) or 'nothing'}, but wrote no RunRecord. The evidence is "
                        "there and no tool pinned it.",
                        contract=path.name,
                        run=scope.run,
                    )
                )
            continue

        declared = str((manifest.get("contract") or {}).get("test_spec") or "")
        if declared and declared != path.name:
            report.add(
                Finding(
                    ERROR,
                    "contract_mismatch",
                    f"cited at {scope.path}, but the run says it belongs to {declared}",
                    contract=path.name,
                    run=scope.run,
                )
            )

        if scope.run not in checked_runs:
            checked_runs.add(scope.run)
            for mismatch in verify_artifacts(scope.run, runs_dir):
                report.add(
                    Finding(
                        ERROR,
                        "artifact_drift",
                        f"{mismatch.get('artifact')}: {mismatch.get('problem')}",
                        contract=path.name,
                        run=scope.run,
                        claimed=mismatch.get("claimed"),
                        recorded=mismatch.get("actual"),
                    )
                )

        _audit_evidence_file(scope, path.name, report, runs_dir)

        prefix = scope.path + "."
        inner = [s.path[len(prefix) :] for s in scopes if s.path.startswith(prefix)]
        _compare_claims(scope, inner, path.name, manifest, report)


def _audit_evidence_file(
    scope: Scope, contract_name: str, report: AuditReport, runs_dir: Path
) -> None:
    """`evidence: {file: runs/<run>/x.json, sha256: ...}` is checked against the file.

    Comparing this digest against the manifest would be the wrong test twice
    over: the manifest lists other artifacts' hashes under the same key name,
    and a side file the tool never registered as an artifact has no manifest
    entry to compare with. Both mistakes produce a confident, wrong answer, so
    the file itself is the only honest source here -- and whether the tool
    registered it at all is worth saying out loud.
    """
    reference, claimed = scope.node.get("file"), scope.node.get("sha256")
    if not isinstance(reference, str) or not isinstance(claimed, str):
        return
    target = Path(runs_dir).parent / reference
    if not target.is_file():
        report.add(
            Finding(
                ERROR,
                "evidence_file_missing",
                f"{scope.path}.file does not exist: {reference}",
                contract=contract_name,
                run=scope.run,
            )
        )
        return

    from QKD_PROJ.hwtools.result import sha256_file

    actual = sha256_file(target)
    if actual != claimed:
        report.add(
            Finding(
                ERROR,
                "evidence_file_drift",
                f"{scope.path}.file no longer hashes to the digest the contract pinned",
                contract=contract_name,
                run=scope.run,
                field=f"{scope.path}.sha256",
                claimed=claimed,
                recorded=actual,
            )
        )
        return

    manifest = load_manifest(scope.run, runs_dir) or {}
    registered = {
        str(a.get("path", "")).replace("\\", "/").split("/")[-1]
        for a in _manifest_artifacts(manifest)
    }
    if target.name not in registered:
        report.add(
            Finding(
                WARNING,
                "evidence_not_registered",
                f"{reference} matches the contract, but the run never recorded it as an "
                "artifact -- the digest is pinned by hand, not by the tool",
                contract=contract_name,
                run=scope.run,
            )
        )


def _manifest_artifacts(manifest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for artifact in manifest.get("artifacts") or []:
        if isinstance(artifact, dict):
            yield artifact
    for step in manifest.get("steps") or []:
        for artifact in step.get("artifacts") or []:
            if isinstance(artifact, dict):
                yield artifact


def _audit_unscoped_claims(
    contract: dict[str, Any],
    contract_name: str,
    scopes: Sequence[Scope],
    report: AuditReport,
) -> None:
    """A result block that names no run is checked by nothing at all.

    This is the gap that scoping opened. Once claims are compared only against
    the run their block names, a block with no ``run:`` key stops being
    compared -- and the strongest-sounding numbers in this corpus are in
    exactly such a block. ``gate11.gpio_protocol_result`` states
    ``bit_errors: 0`` for the GPIO round trip that Gates 11 and 12 rest on, and
    names no run.

    An audit that goes quiet on the claims nobody attached to evidence is worse
    than no audit, because the silence reads as a pass.
    """
    scope_paths = {s.path for s in scopes}
    for path, node in iter_nodes(contract):
        if not isinstance(node, dict):
            continue
        if not (path.endswith("_result") or path.endswith("_test")):
            continue
        if path in scope_paths or any(path.startswith(s + ".") for s in scope_paths):
            continue
        if any(s.path.startswith(path + ".") for s in scopes):
            continue  # a child block carries the run for this one

        claims = [
            (f"{path}.{suffix}", value)
            for suffix, value in iter_nodes(node)
            if not isinstance(value, (dict, list))
            and suffix.split(".")[-1].split("[")[0] in CHECKED_SCALARS
        ]
        if not claims:
            continue
        listed = ", ".join(f"{p.split('.')[-1]}={v!r}" for p, v in claims)
        report.add(
            Finding(
                ERROR,
                "unscoped_claim",
                f"{path} states {listed} but names no run, so nothing verifies it",
                contract=contract_name,
                field=path,
            )
        )


def _audit_candidates(contract: dict[str, Any], contract_name: str, report: AuditReport) -> None:
    """A candidate is marked immutable. Check that it still is.

    ``runs/`` is gitignored, so a pinned candidate directory is often the only
    surviving copy of what was actually programmed. If its bitstream no longer
    hashes to the digest the contract pinned, every gate that closed on it
    closed on a file nobody has today.
    """
    for path, node in iter_nodes(contract):
        if not isinstance(node, dict) or "directory" not in node:
            continue
        directory = QKD_ROOT.parent / str(node["directory"])
        if not directory.is_dir():
            report.add(
                Finding(
                    ERROR,
                    "candidate_missing",
                    f"{path}.directory does not exist: {node['directory']}",
                    contract=contract_name,
                )
            )
            continue
        for name_key, hash_key in (("bitstream", "bitstream_sha256"),):
            filename, claimed = node.get(name_key), node.get(hash_key)
            if not filename or not isinstance(claimed, str):
                continue
            target = directory / str(filename)
            if not target.is_file():
                report.add(
                    Finding(
                        ERROR,
                        "candidate_missing",
                        f"{path}: {filename} is not in {node['directory']}",
                        contract=contract_name,
                    )
                )
                continue
            from QKD_PROJ.hwtools.result import sha256_file

            actual = sha256_file(target)
            if actual != claimed:
                report.add(
                    Finding(
                        ERROR,
                        "candidate_drift",
                        f"{path}: {filename} no longer hashes to the pinned digest",
                        contract=contract_name,
                        field=f"{path}.{hash_key}",
                        claimed=claimed,
                        recorded=actual,
                    )
                )


def _compare_claims(
    scope: Scope,
    inner_scopes: Sequence[str],
    contract_name: str,
    manifest: dict[str, Any],
    report: AuditReport,
) -> None:
    """Compare only the claims written inside this scope against its run."""
    index = manifest_index(manifest)

    for suffix, value in iter_nodes(scope.node):
        if isinstance(value, (dict, list)):
            continue
        if any(suffix == inner or suffix.startswith(inner + ".") for inner in inner_scopes):
            continue  # belongs to a nested block that names its own run
        if suffix == "sha256" and "file" in scope.node:
            continue  # checked against the file itself by _audit_evidence_file
        path = f"{scope.path}.{suffix}" if suffix else scope.path
        leaf = suffix.split(".")[-1].split("[")[0] if suffix else ""

        if leaf.endswith("_sha256") or (isinstance(value, str) and SHA256.match(str(value))):
            recorded = index.get(leaf)
            if recorded is None:
                continue
            if not any(_same(value, r) for r in recorded):
                report.add(
                    Finding(
                        ERROR,
                        "hash_claim_mismatch",
                        f"{path}: the run recorded a different digest under the same name",
                        contract=contract_name,
                        run=scope.run,
                        field=path,
                        claimed=value,
                        recorded=recorded[0] if len(recorded) == 1 else recorded,
                    )
                )
            continue

        if leaf in CHECKED_SCALARS:
            recorded = index.get(leaf)
            if recorded is None:
                report.add(
                    Finding(
                        INFO,
                        "unevidenced_claim",
                        f"{path} = {value!r} is stated in the contract but not recorded in this run",
                        contract=contract_name,
                        run=scope.run,
                        field=path,
                        claimed=value,
                    )
                )
                continue
            if not any(_same(value, r) for r in recorded):
                report.add(
                    Finding(
                        ERROR,
                        "value_claim_mismatch",
                        f"{path}: contract says {value!r}, run recorded {recorded!r}",
                        contract=contract_name,
                        run=scope.run,
                        field=path,
                        claimed=value,
                        recorded=recorded[0] if len(recorded) == 1 else recorded,
                    )
                )


def audit_runs(report: AuditReport, runs_dir: Path, contracts_dir: Path, cited: set[str]) -> None:
    """Runs nobody cites, and runs citing a contract that no longer exists."""
    for directory in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        report.runs += 1
        manifest = load_manifest(directory.name, runs_dir)
        if manifest is None:
            report.add(
                Finding(WARNING, "unreadable_run", "no readable manifest", run=directory.name)
            )
            continue
        declared = str((manifest.get("contract") or {}).get("test_spec") or "")
        if declared and not (contracts_dir / declared).is_file():
            report.add(
                Finding(
                    ERROR,
                    "unknown_contract",
                    f"the run names {declared}, which is not in contracts/",
                    run=directory.name,
                )
            )
        if directory.name not in cited:
            report.add(
                Finding(
                    INFO,
                    "uncited_run",
                    f"verdict {manifest.get('verdict')}; no contract points at this evidence",
                    contract=declared or None,
                    run=directory.name,
                )
            )


def run_audit(contracts_dir: Path | None = None, runs_dir: Path | None = None) -> AuditReport:
    contracts_dir = Path(contracts_dir or CONTRACTS_DIR)
    runs_dir = Path(runs_dir or RUNS_DIR)
    report = AuditReport()

    cited: set[str] = set()
    for path in sorted(contracts_dir.glob("*.yaml")):
        audit_contract(path, report, runs_dir)
        try:
            contract = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(contract, dict):
                cited |= set(cited_runs(contract))
        except yaml.YAMLError:
            pass

    _audit_cross_contract_hashes(contracts_dir, report)

    if runs_dir.is_dir():
        audit_runs(report, runs_dir, contracts_dir, cited)
    return report


def _audit_cross_contract_hashes(contracts_dir: Path, report: AuditReport) -> None:
    """Every contract block that names a repo path and pins its digest.

    An earlier version of this compared digests between contracts by filename.
    It found nothing true and a dozen things false, because two contracts name
    the same script differently while one contract pins two unrelated digests --
    the bitstream file and the design inside it -- next to each other.

    Hashing the file on disk has neither problem. It is also the check that
    answers the standing question about the gate9 allowlist directly: either
    the file changed since the contract was signed, or it did not.
    """
    repo_root = contracts_dir.parent.parent
    for contract_path in sorted(contracts_dir.glob("*.yaml")):
        try:
            contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(contract, dict):
            continue
        for where, node in iter_nodes(contract):
            if not isinstance(node, dict):
                continue
            claimed = node.get("sha256")
            reference = node.get("path") or node.get("script")
            if not isinstance(claimed, str) or not isinstance(reference, str):
                continue
            if not SHA256.match(claimed) or reference.startswith("runs/"):
                continue  # run artifacts are covered by verify_artifacts
            target = repo_root / reference
            if not target.is_file():
                report.add(
                    Finding(
                        WARNING,
                        "pinned_file_missing",
                        f"{where}.path points at {reference}, which is not on disk",
                        contract=contract_path.name,
                        field=where,
                    )
                )
                continue

            from QKD_PROJ.hwtools.result import sha256_file

            actual = sha256_file(target)
            if actual != claimed:
                report.add(
                    Finding(
                        ERROR,
                        "pinned_file_drift",
                        f"{where}: {reference} on disk hashes to {actual[:12]}…, the contract "
                        f"pins {claimed[:12]}…. Either the file changed after signing, or the "
                        "contract records a version that no longer exists.",
                        contract=contract_path.name,
                        field=where,
                        claimed=claimed,
                        recorded=actual,
                    )
                )


# -- reporting ---------------------------------------------------------------


def render_markdown(report: AuditReport) -> str:
    counts = {s: len(report.by_severity(s)) for s in (ERROR, WARNING, INFO)}
    lines = [
        "# Evidence audit",
        "",
        f"Contracts: **{report.contracts}** · runs: **{report.runs}** · "
        f"cited runs checked: **{report.claims}**",
        "",
        f"Errors: **{counts[ERROR]}** · warnings: **{counts[WARNING]}** · notes: **{counts[INFO]}**",
        "",
        "An *error* is a contradiction between a contract and the run it cites. A *warning* is a",
        "claim that cannot be traced. A *note* is evidence that exists but nothing points at, or a",
        "number the contract states that this run never recorded -- often legitimate, since some",
        "results live in side files rather than in the manifest.",
        "",
    ]
    for severity, title in ((ERROR, "Errors"), (WARNING, "Warnings"), (INFO, "Notes")):
        findings = [f for f in report.by_severity(severity) if f.kind != "uncited_run"]
        if not findings:
            lines += [f"## {title}", "", "None.", ""]
        else:
            lines += [
                f"## {title} ({len(findings)})",
                "",
                "| kind | contract | run | detail |",
                "|---|---|---|---|",
            ]
            for finding in findings:
                detail = finding.detail.replace("|", "\\|")
                lines.append(
                    f"| `{finding.kind}` | {finding.contract or '—'} | "
                    f"`{finding.run or '—'}` | {detail} |"
                )
            lines.append("")

    uncited = [f for f in report.findings if f.kind == "uncited_run"]
    if uncited:
        by_contract: dict[str, int] = {}
        for finding in uncited:
            name = finding.contract or "unknown"
            by_contract[name] = by_contract.get(name, 0) + 1
        lines += [
            f"## Uncited runs ({len(uncited)})",
            "",
            "Runs that exist and record a verdict, which no contract points at. Most are",
            "superseded builds and are harmless. They are listed because `runs/` is gitignored:",
            "evidence nothing cites is evidence nobody will find, and it is the pool a claim gets",
            "silently attached to later.",
            "",
            "| contract the run names | uncited runs |",
            "|---|---|",
        ]
        for name, count in sorted(by_contract.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {count} |")
        lines += ["", "Full list: `py -3 -m QKD_PROJ.orchestrator.audit --json`.", ""]
    return "\n".join(lines)


def render_text(report: AuditReport) -> str:
    counts = {s: len(report.by_severity(s)) for s in (ERROR, WARNING, INFO)}
    lines = [
        f"contracts={report.contracts} runs={report.runs} cited={report.claims}",
        f"errors={counts[ERROR]} warnings={counts[WARNING]} notes={counts[INFO]}",
        "",
    ]
    for finding in report.findings:
        lines.append(
            f"[{finding.severity:7}] {finding.kind:24} "
            f"{finding.contract or '-':45} {finding.run or '-':45} {finding.detail}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit contracts against their runs.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", metavar="FILE")
    parser.add_argument("--contracts-dir")
    parser.add_argument("--runs-dir")
    args = parser.parse_args(argv)

    report = run_audit(args.contracts_dir, args.runs_dir)

    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(render_markdown(report))
        print(f"wrote {path}")
    if args.json:
        print(json.dumps(report.describe(), ensure_ascii=False, indent=2))
    elif not args.markdown:
        print(render_text(report))

    # Exit code says "did the audit run", not "is the corpus clean". A tool that
    # fails when it finds something cannot be put in front of a loop that is
    # supposed to react to what it found.
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
