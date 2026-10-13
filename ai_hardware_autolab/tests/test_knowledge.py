"""The long-term brain promotes evidence, not model confidence."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.knowledge.models import (  # noqa: E402
    KnowledgeCandidate,
    KnowledgeScope,
    KnowledgeValidationError,
    json_value_digest,
)
from ai_hardware_autolab.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer  # noqa: E402
from ai_hardware_autolab.knowledge.retriever import KnowledgeRetriever  # noqa: E402
from ai_hardware_autolab.knowledge.store import KnowledgeStore, KnowledgeStoreError  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate(
    evidence: list[dict],
    *,
    value: object = 5,
    statement: str = "The PRU interval is 5 ns under the cited condition.",
    disposition: str = "assert",
    project: str = "qkd-proj",
    subject_id: str = "pru",
) -> KnowledgeCandidate:
    return KnowledgeCandidate.parse(
        {
            "kind": "KnowledgeCandidate",
            "producer": "researcher",
            "scope": {
                "project_id": project,
                "hardware_id": "bbb-am335x",
                "revision": "rev-c",
            },
            "subject": {"kind": "component", "id": subject_id, "label": "PRU"},
            "relation": "has_interval",
            "object": {"kind": "literal", "value": value, "unit": "ns"},
            "statement": statement,
            "disposition": disposition,
            "evidence": evidence,
            "source_job_id": "job_123",
            "tags": ["timing", "pru"],
        }
    )


def _contract_evidence(root: Path, *, role: str = "supports") -> list[dict]:
    path = root / "contracts" / "timing.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"timing": {"interval_ns": 5}}), encoding="utf-8")
    return [
        {
            "kind": "contract",
            "role": role,
            "locator": "contracts/timing.json",
            "sha256": _sha(path),
            "json_pointer": "/timing/interval_ns",
            "value_sha256": json_value_digest(5),
        }
    ]


def test_candidate_is_closed_and_semantic_id_ignores_display_label(tmp_path: Path):
    first = _candidate(_contract_evidence(tmp_path))
    changed = first.to_dict()
    changed["subject"]["label"] = "Programmable real-time unit"
    second = KnowledgeCandidate.parse(changed)

    assert first.claim_id == second.claim_id
    assert first.version_id != second.version_id

    unknown = first.to_dict()
    unknown["confidence"] = 0.99
    with pytest.raises(KnowledgeValidationError, match="unknown fields"):
        KnowledgeCandidate.parse(unknown)


def test_revision_cannot_float_without_hardware():
    with pytest.raises(KnowledgeValidationError, match="requires hardware_id"):
        KnowledgeScope.parse({"project_id": "qkd-proj", "revision": "rev-c"})


def test_filesystem_reducer_promotes_only_pinned_json_value(tmp_path: Path):
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))
    candidate = _candidate(_contract_evidence(tmp_path))

    claim = reducer.reduce(candidate)

    assert claim.status == "verified"
    assert claim.checks[0].code == "verified_json_value"

    contract = tmp_path / "contracts" / "timing.json"
    contract.write_text(json.dumps({"timing": {"interval_ns": 10}}), encoding="utf-8")
    stale = reducer.reduce(candidate)
    assert stale.status == "proposed"
    assert stale.checks[0].code == "sha256_mismatch"


def test_missing_evidence_does_not_become_refutation(tmp_path: Path):
    evidence = _contract_evidence(tmp_path)
    (tmp_path / evidence[0]["locator"]).unlink()

    claim = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path)).reduce(_candidate(evidence))

    assert claim.status == "proposed"
    assert not claim.checks[0].ok


def test_refutation_requires_verified_refuting_evidence(tmp_path: Path):
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))
    refuted = reducer.reduce(
        _candidate(_contract_evidence(tmp_path, role="refutes"), disposition="refute")
    )
    mismatched = reducer.reduce(
        _candidate(_contract_evidence(tmp_path, role="supports"), disposition="refute")
    )

    assert refuted.status == "refuted"
    assert mismatched.status == "proposed"


def test_serialized_claim_cannot_lie_about_reducer_status(tmp_path: Path):
    claim = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path)).reduce(
        _candidate(_contract_evidence(tmp_path))
    )
    forged = claim.to_dict()
    forged["status"] = "refuted"

    from ai_hardware_autolab.knowledge.models import KnowledgeClaim

    with pytest.raises(KnowledgeValidationError, match="refuting evidence"):
        KnowledgeClaim.parse(forged)


def _reference(root: Path) -> tuple[str, str]:
    original = b"%PDF-1.7\nmanufacturer timing data"
    document_id = hashlib.sha256(original).hexdigest()
    directory = root / "references" / document_id
    directory.mkdir(parents=True)
    (directory / "original.pdf").write_bytes(original)
    text = "The programmable real-time unit executes one instruction per cycle."
    (directory / "manifest.json").write_text(
        json.dumps({"extraction": {"status": "ok"}}), encoding="utf-8"
    )
    (directory / "text.json").write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page": 42,
                        "text": text,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return document_id, text






def test_store_is_idempotent_searchable_and_scope_limited(tmp_path: Path):
    claim = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path)).reduce(
        _candidate(_contract_evidence(tmp_path))
    )
    store = KnowledgeStore(tmp_path / "state" / "knowledge.db")

    first = store.record(claim)
    repeated = store.record(claim)

    assert first.event_id == repeated.event_id
    assert [row.claim_id for row in store.search("PRU interval", scope=claim.candidate.scope)] == [
        claim.claim_id
    ]
    assert store.search(
        "PRU interval", scope=KnowledgeScope("another-project", "another-board", "rev-a")
    ) == []


def test_unverified_revision_does_not_downgrade_verified_claim(tmp_path: Path):
    evidence = _contract_evidence(tmp_path)
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))
    verified = reducer.reduce(_candidate(evidence))
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.record(verified)

    missing = copy.deepcopy(evidence)
    missing[0]["locator"] = "contracts/missing.json"
    proposed = reducer.reduce(_candidate(missing, statement="A later agent repeated the claim."))
    assert proposed.claim_id == verified.claim_id
    store.record(proposed)

    assert store.claim(verified.claim_id).status == "verified"
    assert len(store.events_since()) == 2


def test_supersession_and_rebuild_have_the_same_projection(tmp_path: Path):
    resolver = FilesystemEvidenceResolver(tmp_path)
    reducer = KnowledgeReducer(resolver)
    evidence = _contract_evidence(tmp_path)
    old = reducer.reduce(_candidate(evidence, value=5))
    new = reducer.reduce(_candidate(evidence, value=10, statement="A revised interpretation is 10 ns."))
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.record(old)
    store.record(new)
    store.supersede(old.claim_id, new.claim_id, "A newer reviewed interpretation replaced it.")

    assert store.claim(old.claim_id).status == "superseded"
    assert store.claim(new.claim_id).status == "verified"

    store.rebuild()
    assert store.claim(old.claim_id).status == "superseded"
    assert store.claim(new.claim_id).status == "verified"


def test_supersession_refuses_an_unverified_replacement(tmp_path: Path):
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))
    evidence = _contract_evidence(tmp_path)
    old = reducer.reduce(_candidate(evidence))
    proposed = reducer.reduce(_candidate([], value=10))
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.record(old)
    store.record(proposed)

    with pytest.raises(KnowledgeStoreError, match="verified evidence"):
        store.supersede(old.claim_id, proposed.claim_id, "replace")


def test_retriever_expands_one_hop_without_embeddings(tmp_path: Path):
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))
    evidence = _contract_evidence(tmp_path)
    timing = reducer.reduce(_candidate(evidence))
    firmware_data = _candidate(
        evidence,
        value="firmware.out",
        statement="The PRU produces the firmware artifact.",
        subject_id="pru",
    ).to_dict()
    firmware_data["relation"] = "produces"
    firmware_data["object"] = {
        "kind": "entity",
        "entity": {"kind": "artifact", "id": "pru-firmware", "label": "PRU firmware"},
    }
    firmware = reducer.reduce(KnowledgeCandidate.parse(firmware_data))
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.record(timing)
    store.record(firmware)

    context = KnowledgeRetriever(store).retrieve("interval", timing.candidate.scope)

    assert context.direct_count == 1
    assert {claim.claim_id for claim in context.claims} == {timing.claim_id, firmware.claim_id}
    assert context.to_dict()["claims"][0]["evidence"]
