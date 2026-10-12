from __future__ import annotations

import json

from QKD_PROJ.knowledge.ingest import SourcePacketKnowledgeIngestor
from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer
from QKD_PROJ.knowledge.store import KnowledgeStore
from QKD_PROJ.orchestrator.evidence import ConditionResult, Reduction


def test_deterministic_observation_becomes_a_run_backed_verified_claim(tmp_path) -> None:
    project = tmp_path / "QKD_PROJ"
    run_dir = project / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"measurements": {"edge_interval_avg_ns": 5.1}}),
        encoding="utf-8",
    )
    store = KnowledgeStore(tmp_path / "knowledge.db")
    ingestor = SourcePacketKnowledgeIngestor(
        KnowledgeReducer(FilesystemEvidenceResolver(project)),
        store,
    )
    reduction = Reduction(
        "mission-demo",
        1,
        "satisfied",
        [ConditionResult(
            field="measurements.edge_interval_avg_ns",
            expect="approx 5 ns",
            observed=5.1,
            status="met",
            evidence=["run-1"],
            provenance="manifest",
        )],
    )

    result = ingestor.ingest_reduction(
        reduction,
        KnowledgeScope("qkd-proj", "demo-board", "r1"),
        runs_dir=project / "runs",
        source_job_id="job_demo",
    )

    assert len(result.claims) == 1
    assert result.claims[0].status == "verified"
    assert result.claims[0].candidate.object.value == 5.1


def test_missing_observation_is_not_written_as_knowledge(tmp_path) -> None:
    project = tmp_path / "QKD_PROJ"
    project.mkdir()
    ingestor = SourcePacketKnowledgeIngestor(
        KnowledgeReducer(FilesystemEvidenceResolver(project)),
        KnowledgeStore(tmp_path / "knowledge.db"),
    )
    reduction = Reduction(
        "mission-demo",
        1,
        "open",
        [ConditionResult("x", "0", None, "evidence_missing", [])],
    )
    result = ingestor.ingest_reduction(
        reduction,
        KnowledgeScope("qkd-proj"),
        runs_dir=project / "runs",
        source_job_id="job_demo",
    )
    assert result.claims == ()
