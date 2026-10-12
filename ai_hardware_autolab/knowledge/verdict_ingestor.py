"""Turn deterministic reducer observations into run-backed knowledge candidates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import KnowledgeCandidate, KnowledgeScope


def candidates_from_reduction(
    reduction: Any,
    scope: KnowledgeScope,
    *,
    runs_dir: Path,
    source_job_id: str,
) -> tuple[KnowledgeCandidate, ...]:
    """Project only observed condition values; missing evidence is not a fact."""

    root = Path(runs_dir).resolve()
    rows: list[KnowledgeCandidate] = []
    for index, condition in enumerate(getattr(reduction, "conditions", ())):
        evidence_runs = list(getattr(condition, "evidence", ()) or ())
        observed = getattr(condition, "observed", None)
        status = str(getattr(condition, "status", ""))
        if observed is None or status not in {"met", "unmet"} or len(evidence_runs) != 1:
            continue
        run_name = str(evidence_runs[0])
        if Path(run_name).name != run_name:
            continue
        manifest = (root / run_name / "manifest.json").resolve()
        try:
            manifest.relative_to(root)
        except ValueError:
            continue
        if not manifest.is_file():
            continue
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        field = str(getattr(condition, "field", "measurement"))
        statement = f"{field} was observed as {json.dumps(observed, ensure_ascii=False)} ({status})."
        subject_id = "run-" + hashlib.sha256(run_name.encode("utf-8")).hexdigest()[:20]
        rows.append(KnowledgeCandidate.parse({
            "kind": "KnowledgeCandidate",
            "producer": "evidence-reducer",
            "scope": scope.to_dict(),
            "subject": {"kind": "run", "id": subject_id, "label": run_name},
            "relation": "observed_condition",
            "object": {"kind": "literal", "value": observed},
            "statement": statement,
            "disposition": "assert",
            "evidence": [{
                "kind": "run",
                "role": "supports",
                "locator": f"runs/{run_name}/manifest.json",
                "sha256": digest,
            }],
            "source_job_id": source_job_id,
            "tags": ["measurement", status, f"condition-{index}"],
        }))
    return tuple(rows)
