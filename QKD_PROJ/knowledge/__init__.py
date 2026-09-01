"""Evidence-backed long-term memory for the agent platform."""

from .models import (
    CLAIM_STATUSES,
    ENTITY_KINDS,
    EvidenceCheck,
    EvidenceRef,
    KnowledgeCandidate,
    KnowledgeClaim,
    KnowledgeEntity,
    KnowledgeObject,
    KnowledgeScope,
    KnowledgeValidationError,
    canonical_json,
    json_value_digest,
    stable_digest,
)
from .reducer import EvidenceResolver, FilesystemEvidenceResolver, KnowledgeReducer
from .retriever import KnowledgeContext, KnowledgeRetriever
from .store import KnowledgeEvent, KnowledgeStore, KnowledgeStoreError

__all__ = [
    "CLAIM_STATUSES",
    "ENTITY_KINDS",
    "EvidenceCheck",
    "EvidenceRef",
    "KnowledgeCandidate",
    "KnowledgeClaim",
    "KnowledgeEntity",
    "KnowledgeObject",
    "KnowledgeScope",
    "KnowledgeValidationError",
    "canonical_json",
    "json_value_digest",
    "stable_digest",
    "EvidenceResolver",
    "FilesystemEvidenceResolver",
    "KnowledgeReducer",
    "KnowledgeContext",
    "KnowledgeRetriever",
    "KnowledgeEvent",
    "KnowledgeStore",
    "KnowledgeStoreError",
]
