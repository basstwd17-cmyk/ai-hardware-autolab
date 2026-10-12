"""Replay saved conversation results; never invoke a model or device.

Prose stays proposed. Only a literal, host-authored observation of what a
device call returned is evidence-checked; it is not scenario acceptance.
"""
from __future__ import annotations

import logging
from .models import (EvidenceCheck, EvidenceRef, KnowledgeCandidate, KnowledgeClaim,
                     KnowledgeEntity, KnowledgeObject, KnowledgeScope, stable_digest, canonical_json)

LOG = logging.getLogger(__name__)
KINDS = {"conversation.turn.completed", "conversation.device.completed", "conversation.device.failed"}


def knowledge_notes(value):
    """Optional notes: malformed notes never fail an otherwise useful turn."""
    if not isinstance(value, list):
        return []
    return [{"topic": row["topic"].strip()[:200], "text": row["text"].strip()[:8000]}
            for row in value[:12] if isinstance(row, dict)
            and isinstance(row.get("topic"), str) and row["topic"].strip()
            and isinstance(row.get("text"), str) and row["text"].strip()]


class ConversationKnowledgeIngestor:
    def __init__(self, ledger, store, scope):
        self.ledger, self.store, self.scope = ledger, store, scope
        self.source = "conversation-v1:" + stable_digest(str(ledger.path.resolve()))
        self.last_error = None

    def sync(self, limit=500):
        """Bounded, restartable catch-up, including completed/failed old jobs."""
        count = 0
        try:
            cursor = self.store.ingestion_cursor(self.source)
            for event in self.ledger.events_since(cursor, limit=limit):
                if event.kind in KINDS:
                    for claim in self.claims(event):
                        self.store.record(claim)
                        count += 1
                self.store.advance_ingestion(self.source, event.event_id)
            self.last_error = None
        except Exception as exc:
            # Cursor remains before the failed event. Other work still runs.
            self.last_error = type(exc).__name__
            LOG.warning("knowledge ingestion deferred (%s)", self.last_error)
        return count

    def claims(self, event):
        briefs = self.ledger.handoffs(event.job_id, "Brief", 0)
        if len(briefs) != 1 or briefs[0].payload.get("workflow") != "conversation-prototype-v1":
            return []
        brief = briefs[0].payload
        inputs = dict(brief.get("inputs") or {})
        # Only authority effective at this event; later clarification cannot
        # relabel an earlier observation as belonging to a different board.
        after = 0
        while True:
            batch = self.ledger.events_since(after, event.job_id, limit=500)
            for prior in batch:
                if prior.event_id >= event.event_id:
                    break
                if prior.kind == "conversation.hardware.prepared" and prior.agent == "platform":
                    inputs.update(prior.payload.get("inputs") or {})
            if not batch or batch[-1].event_id >= event.event_id:
                break
            after = batch[-1].event_id
        board = inputs.get("selected_board") or inputs.get("hardware_profile") or {}
        # No default RTASE scope for an unselected/unknown board.
        project = inputs.get("project") or {}
        scope = KnowledgeScope(project.get("project_id", self.scope.project_id), board.get("board_id"), board.get("revision"))
        ref = EvidenceRef("handoff", "supports", f"{self.source}/{event.job_id}/{event.event_id}",
                          stable_digest(event.describe()))
        origin = {"event_id": event.event_id, "job_id": event.job_id, "at": event.at}

        def make(topic, text, value, relation, kind="software", observed=False):
            candidate = KnowledgeCandidate(
                producer=event.agent or "platform", scope=scope,
                subject=KnowledgeEntity(kind, "topic-" + stable_digest(topic)[:24], topic[:512]),
                relation=relation, object=KnowledgeObject(value={**origin, **value}, literal=True),
                statement=text[:10000], disposition="assert", evidence=(ref,),
                source_job_id=event.job_id, tags=("conversation", relation),
            )
            # Normalize bounded text before computing its persisted version.
            candidate = KnowledgeCandidate.parse(candidate.to_dict())
            check = EvidenceCheck(ref.evidence_id, observed,
                "ledger_observation" if observed else "agent_statement",
                "Exact host result from the saved event; not functional acceptance." if observed
                else "Saved agent statement and references; semantic verification is pending.")
            return KnowledgeClaim(candidate, "verified" if observed else "proposed", (check,))

        data = event.payload
        if event.kind == "conversation.turn.completed":
            reply = data.get("reply") or {}
            context = {"artifacts": data.get("artifacts", [])[:32],
                       "runs": reply.get("runs", [])[:32], "workspace": brief.get("workspace"),
                       "status": reply.get("status"), "summary": reply.get("summary"),
                       "sources": reply.get("sources", [])[:32], "project": project}
            notes = knowledge_notes(reply.get("knowledge"))
            if not notes:
                notes = [{"topic": str(brief.get("objective") or "작업 결과")[:200],
                          "text": str(reply.get("summary") or "작업 결과")[:8000]}]
            output = [make(n["topic"], n["text"], {**context, "note": n["text"]}, "work_note") for n in notes]
            for source in reply.get("sources", [])[:32]:
                output.append(make(source["title"], source["finding"],
                                   {"url": source["url"], "finding": source["finding"]},
                                   "research_source", "source"))
            return output
        # Host observations preserve failures/unknown outcomes as such. Never
        # infer hardware success from a transport exception or a PASS label.
        tool = str(data.get("tool") or "장비 실행")
        observed = {key: data[key] for key in (
            "tool", "allowed", "hardware_contacted", "hardware_state_unknown",
            "run", "run_verdict", "result", "inventory", "error_code", "error_type"
        ) if key in data}
        text = canonical_json(observed)
        # Large waveform arrays remain in the original event/run, not map prose.
        if len(text) > 7000:
            observed = {k: v for k, v in observed.items() if k not in {"result", "inventory"}}
            observed["details"] = "See source event for full observation."
            text = canonical_json(observed)
        failed = event.kind.endswith(".failed")
        return [make(tool, ("장비 실행 결과 불명: " if failed else "장비 반환 기록: ") + text,
                     {"observation": observed}, "device_observation", "run",
                     observed=event.agent == "platform" and not failed)]
