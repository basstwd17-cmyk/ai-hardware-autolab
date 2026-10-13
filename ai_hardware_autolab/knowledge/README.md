# Project knowledge

ConversationKnowledgeIngestor reads role results from the job ledger into KnowledgeStore.
Reusable notes preserve project scope, source references, file hashes and recorded checks.
A persisted cursor catches up after restart; failed ingestion is retried without stopping development.

Agent notes are proposed knowledge, not proof of successful implementation. FilesystemEvidenceResolver
checks explicitly supplied references before KnowledgeReducer can mark a claim verified.
KnowledgeRetriever searches within the selected project and returns source/evidence metadata.
The web knowledge map exposes the full text and actual recorded details.

SQLite stores and job workspaces are local installation data and are excluded from Git.
