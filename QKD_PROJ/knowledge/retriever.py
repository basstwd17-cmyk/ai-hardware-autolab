"""Bounded lexical retrieval plus one-hop graph expansion for agent context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import KnowledgeClaim, KnowledgeScope
from .store import KnowledgeStore, KnowledgeStoreError


@dataclass(frozen=True)
class KnowledgeContext:
    query: str
    scope: KnowledgeScope
    direct_count: int
    claims: tuple[KnowledgeClaim, ...]

    def to_dict(self) -> dict:
        """The bounded, observable context later injected into an architect turn."""

        return {
            "kind": "KnowledgeContext",
            "query": self.query,
            "scope": self.scope.to_dict(),
            "direct_count": self.direct_count,
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "version_id": claim.version_id,
                    "scope": claim.candidate.scope.to_dict(),
                    "status": claim.status,
                    "statement": claim.candidate.statement,
                    "subject": claim.candidate.subject.to_dict(),
                    "relation": claim.candidate.relation,
                    "object": claim.candidate.object.to_dict(),
                    "evidence": [
                        {
                            "kind": evidence.kind,
                            "locator": evidence.locator,
                            "sha256": evidence.sha256,
                            "json_pointer": evidence.json_pointer,
                            "value_sha256": evidence.value_sha256,
                            "page": evidence.page,
                            "role": evidence.role,
                        }
                        for evidence in claim.candidate.evidence
                    ],
                    "source_job_id": claim.candidate.source_job_id,
                }
                for claim in self.claims
            ],
        }


class KnowledgeRetriever:
    def __init__(self, store: KnowledgeStore) -> None:
        self.store = store

    def retrieve(
        self,
        query: str,
        scope: KnowledgeScope,
        *,
        seed_entities: Sequence[str] = (),
        limit: int = 16,
        neighbor_limit: int = 8,
    ) -> KnowledgeContext:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
            raise KnowledgeStoreError("retrieval limit must be from 1 to 64")
        direct = self.store.search(query, scope=scope, limit=limit)
        collected: dict[str, KnowledgeClaim] = {claim.claim_id: claim for claim in direct}
        seeds = list(seed_entities)
        for claim in direct:
            seeds.append(claim.candidate.subject.key)
            if claim.candidate.object.entity is not None:
                seeds.append(claim.candidate.object.entity.key)
        for entity_key in dict.fromkeys(seeds):
            if len(collected) >= limit:
                break
            for claim in self.store.neighbors(
                entity_key, scope=scope, limit=min(neighbor_limit, limit - len(collected))
            ):
                collected.setdefault(claim.claim_id, claim)
                if len(collected) >= limit:
                    break
        return KnowledgeContext(query.strip(), scope, len(direct), tuple(collected.values()))
