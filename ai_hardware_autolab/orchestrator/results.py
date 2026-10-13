"""Turning a terminal ledger record into what the office shows and may open.

Two frames the UI contract asks the daemon for, and one rule that governs both:
**nothing here is written by a model, and nothing here is invented.** The
counts come from the Verdict the reducer produced; the summary quotes the
outcome and the failing fields; the artifact list comes from paths this process
resolved on disk. If a number cannot be derived it is absent rather than
guessed.

``job.result`` carries the human-readable line, deterministic condition counts,
and path-free artifact metadata. ``artifact.registered`` carries the paths, on
a separate frame, so a browser message never contains one -- the privileged
local host resolves ``(jobId, artifactId)`` and re-checks the target is still
inside its project root before revealing it.

Most results are backed by a deterministic ``Verdict``. A job may also stop
before evidence reduction -- for example, when the architect finds a contract
gap. That is still a terminal result. In that case the card quotes the job's
stored outcome, carries zero condition counts, and identifies the terminal
Plan rather than fabricating a Verdict that never existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ai_hardware_autolab.orchestrator.jobs import DONE, JobLedger

# Condition statuses the reducer produces. Counted, never re-judged.
MET = "met"
UNMET = "unmet"
EVIDENCE_MISSING = "evidence_missing"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Artifact:
    """One openable thing, named by id and resolved to a real path."""

    artifact_id: str
    kind: str
    target_path: str
    project_root: str
    label: str = ""

    def registration(self, job_id: str) -> dict[str, Any]:
        """The frame carrying the path. Never sent to a browser."""
        return {
            "type": "artifact.registered",
            "jobId": job_id,
            "artifactId": self.artifact_id,
            "projectRoot": self.project_root,
            "targetPath": self.target_path,
        }

    def metadata(self) -> dict[str, Any]:
        """The path-free half, safe to render."""
        return {
            "artifactId": self.artifact_id,
            "kind": self.kind,
            "label": self.label or Path(self.target_path).name,
        }


def condition_counts(verdict: dict[str, Any]) -> dict[str, int]:
    """How many conditions landed in each state. Arithmetic, not opinion."""
    counts = {MET: 0, UNMET: 0, EVIDENCE_MISSING: 0, AMBIGUOUS: 0}
    for condition in verdict.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        status = str(condition.get("status") or (MET if condition.get("met") else UNMET))
        counts[status] = counts.get(status, 0) + 1
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def summarise(verdict: dict[str, Any], counts: dict[str, int]) -> str:
    """One line, built from the verdict's own fields.

    Deliberately not a sentence a model wrote. The outcome and the failing
    field names are facts the reducer recorded; anything more fluent would be
    an interpretation the office would then display as though it were measured.
    """
    outcome = str(verdict.get("outcome") or "unknown")
    gate = str(verdict.get("gate") or "")
    head = f"{gate}: {outcome}" if gate else outcome
    if counts.get("total"):
        head += f" ({counts.get(MET, 0)}/{counts['total']} conditions met)"
    failing = [
        str(c.get("field"))
        for c in verdict.get("conditions") or []
        if isinstance(c, dict) and not c.get("met")
    ]
    if failing:
        shown = ", ".join(failing[:3])
        head += f"; open: {shown}" + (f" +{len(failing) - 3} more" if len(failing) > 3 else "")
    return head


def evidence_runs(verdict: dict[str, Any]) -> list[str]:
    """Every run directory the verdict cited, in first-seen order."""
    seen: list[str] = []
    for condition in verdict.get("conditions") or []:
        if not isinstance(condition, dict):
            continue
        for run in condition.get("evidence") or []:
            if isinstance(run, str) and run not in seen:
                seen.append(run)
    return seen


def collect_artifacts(
    verdict: dict[str, Any],
    report: dict[str, Any] | None,
    *,
    runs_dir: Path,
    project_root: Path,
) -> list[Artifact]:
    """Everything a person could reasonably want to open, that actually exists.

    A registration for a file that is not there would be a button that fails,
    so each candidate is resolved and checked before it is offered -- and each
    is confirmed to sit inside the project root, because the frame is an
    instruction to a privileged host to reveal a path.
    """
    project_root = Path(project_root).resolve()
    found: list[Artifact] = []

    document = (report or {}).get("document")
    if isinstance(document, str) and document.strip():
        resolved = _inside(project_root, project_root / document)
        if resolved is not None:
            found.append(
                Artifact("report", "document", str(resolved), str(project_root), "Report")
            )

    for run in evidence_runs(verdict):
        directory = _inside(project_root, Path(runs_dir) / run)
        if directory is not None:
            found.append(
                Artifact(f"run:{run}", "run", str(directory), str(project_root), run)
            )
    return found


def _inside(project_root: Path, candidate: Path) -> Path | None:
    """The resolved path, or None if it is missing or escapes the root."""
    try:
        resolved = candidate.resolve()
        resolved.relative_to(project_root)
    except (ValueError, OSError):
        return None
    return resolved if resolved.exists() else None


def _conversation_result(ledger, job_id, brief, project_root, runs_dir):
    """Quote the recorded review; never manufacture deterministic gate counts."""
    import hashlib
    from .pagination import iter_events
    job = ledger.job(job_id)
    if job.state != DONE:
        return None
    replies = [e for e in iter_events(ledger, job_id) if e.kind == "conversation.turn.completed"]
    last = replies[-1] if replies else None
    root = Path(brief["workspace"]).resolve()
    project = Path(project_root).resolve()
    artifacts = []
    # Stable identity includes the digest; a later version is not the same file.
    seen = set()
    for event in replies:
        for item in event.payload.get("artifacts", []):
            relative, digest = item["path"], item["sha256"]
            identity = hashlib.sha256((relative + "\0" + digest).encode()).hexdigest()[:24]
            if identity in seen:
                continue
            seen.add(identity)
            path = _inside(project, root / relative)
            try:
                if path is None or not path.is_relative_to(root) or not path.is_file():
                    continue
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    continue
            except OSError:
                continue
            artifacts.append(Artifact("conversation:" + identity, "document", str(path),
                                      str(project), relative))
    summary = last.payload["reply"]["summary"] if last else "에이전트 응답 전에 작업이 종료됐습니다."
    sources = {}
    for event in replies:
        for source in event.payload.get("reply", {}).get("sources", []):
            sources[(source["url"], source["finding"])] = source
    if sources:
        # The existing result view renders plain text, so preserve readable URLs
        # as well as structured notes without requiring a frontend deployment.
        summary += "\n\n조사 출처 (에이전트가 기록한 근거):\n" + "\n".join(
            f"{item['title']} — {item['url']}\n{item['finding']}"
            for item in sources.values())
    if job.outcome != "satisfied":
        summary = f"{job.outcome}: {summary}"
    return {"jobId": job_id, "gate": job.gate, "iteration": job.iteration,
            "outcome": job.outcome, "state": job.state, "title": "에이전트 검토 결과",
            "summary": summary, "sources": list(sources.values()), "reasonCode": job.outcome,
            "counts": condition_counts({}), "sourceKind": "conversation.turn.completed",
            "artifacts": [a.metadata() for a in artifacts]}, artifacts


def build_result(
    ledger: JobLedger,
    job_id: str,
    *,
    runs_dir: Path,
    project_root: Path,
) -> tuple[dict[str, Any], list[Artifact]] | None:
    """The ``job.result`` payload for a verdict or pre-reduction terminal job.

    Returns None when there is nothing to show yet, so a caller sends no frame
    rather than an empty one.
    """
    from .conversation_flow import WORKFLOW
    briefs = ledger.handoffs(job_id, "Brief", 0)
    if briefs and briefs[-1].payload.get("workflow") == WORKFLOW:
        return _conversation_result(
            ledger, job_id, briefs[-1].payload, project_root, runs_dir
        )
    verdicts = ledger.handoffs(job_id, "Verdict")
    job = ledger.job(job_id)
    reports = ledger.handoffs(job_id, "Report")
    report = reports[-1].payload if reports else None

    if not verdicts:
        if job.state != DONE:
            return None
        plans = ledger.handoffs(job_id, "Plan")
        reviews = ledger.handoffs(job_id, "ContractReview")
        drafts = ledger.handoffs(job_id, "ContractDraft")
        decisions = ledger.handoffs(job_id, "ArchitectureDecision")
        terminal = (
            reviews[-1] if reviews
            else drafts[-1] if drafts
            else decisions[-1] if decisions
            else plans[-1] if plans
            else None
        )
        counts = {MET: 0, UNMET: 0, EVIDENCE_MISSING: 0, AMBIGUOUS: 0, "total": 0}
        outcome = str(job.outcome or "unknown")
        artifacts = collect_artifacts(
            {}, report, runs_dir=runs_dir, project_root=project_root
        )
        payload: dict[str, Any] = {
            "jobId": job_id,
            "gate": job.gate,
            "iteration": job.iteration,
            "outcome": outcome,
            "state": job.state,
            "title": f"{job.gate} terminal result",
            "summary": f"{job.gate}: {outcome} (stopped before evidence reduction)",
            "reasonCode": outcome,
            "counts": counts,
            "artifacts": [a.metadata() for a in artifacts],
        }
        if reviews:
            review = reviews[-1].payload
            payload.update(
                {
                    "title": "Contract proposal",
                    "summary": (
                        f"{drafts[-1].payload.get('contract_name', job.gate)}: "
                        f"{review.get('outcome', outcome)}"
                    ),
                    "reasonCode": review.get("outcome", outcome),
                    "contractReview": review,
                    "contractReviewSha256": reviews[-1].sha256,
                }
            )
            if drafts:
                payload.update(
                    {
                        "contractDraft": drafts[-1].payload,
                        "contractDraftSha256": drafts[-1].sha256,
                    }
                )
        if terminal is not None:
            payload.update(
                {
                    "sourceKind": terminal.kind,
                    "source": terminal.payload,
                    "sourceSha256": terminal.sha256,
                }
            )
        return payload, artifacts

    verdict = verdicts[-1].payload
    from ai_hardware_autolab.orchestrator.development import autonomous
    briefs = ledger.handoffs(job_id, "Brief", 0)
    development = autonomous(briefs[-1].payload.get("resources")) if briefs else False
    provisional = development and job.state != DONE

    counts = condition_counts(verdict)
    artifacts = collect_artifacts(
        verdict, report, runs_dir=runs_dir, project_root=project_root
    )
    # Reveal the preserved isolated checkout for human review, never execute
    # or merge it as a side effect of a passing Builder command.
    for handoff in reversed(ledger.handoffs(job_id, "StepResult")):
        review = handoff.payload.get("workspace_review")
        if not isinstance(review, dict) or not isinstance(review.get("workspace"), str):
            continue
        directory = _inside(project_root.resolve(), Path(review["workspace"]))
        if directory is not None and directory.is_dir():
            artifacts.append(Artifact("workspace-review", "workspace", str(directory),
                                      str(project_root.resolve()), "작업공간 · 변경 검토 (미병합)"))
        break
    diagnostic_rows = []
    for handoff in ledger.handoffs(job_id, "StepResult", job.iteration):
        diagnostic = handoff.payload.get("diagnostic_report") or {}
        if diagnostic.get("complete") is True:
            for rows in diagnostic.get("results", {}).values():
                diagnostic_rows.extend(rows)
    summary = ("작업 진행 중 · 조건 검사: " if provisional else "") + summarise(verdict, counts)
    if diagnostic_rows:
        heading = "진단 완료" if job.state == DONE and job.outcome == "satisfied" else "진단 결과"
        observations = [f"{row['device']}: {row['device_verdict']}"
                        + (f" ({row['failure_stage']})" if row.get("failure_stage") else "")
                        for row in diagnostic_rows]
        summary = heading + " · " + "; ".join(observations) + " — 장비 성공 여부와 작업 완료는 별개입니다. " + summary
    return {
        "jobId": job_id,
        "gate": verdict.get("gate"),
        "iteration": verdict.get("iteration"),
        # The job's outcome once it is finished; until then, this iteration's.
        "outcome": job.outcome or ("open" if provisional else verdict.get("outcome")),
        "verdictOutcome": verdict.get("outcome"),
        "state": job.state,
        "summary": summary,
        "counts": counts,
        "failureSignature": verdict.get("failure_signature"),
        "hashMismatches": verdict.get("hash_mismatches") or [],
        "corroboration": verdict.get("corroboration") or [],
        # The verbatim verdict, so the office never has to take this summary's
        # word for anything. Its digest identifies which record was summarised.
        "verdict": verdict,
        "verdictSha256": verdicts[-1].sha256,
        "artifacts": [a.metadata() for a in artifacts],
    }, artifacts
