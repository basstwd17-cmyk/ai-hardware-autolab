# Platform development guide

Read README.md and STATUS.md first.

- Current execution: orchestrator/conversation_flow.py, conversation_codex.py and conversation_service.py.
- The selected project and user scenario define the work. Lead → Builder → Verifier exchange messages and actual artifacts.
- ProjectRegistry and ProjectWorkspaces pin project settings and keep source changes in isolated Git workspaces.
- New jobs have no execution contracts, Campaigns or installed board adapters.
- Preserve original project source, active workspaces, SQLite state and knowledge. Do not run git clean -fdX.
- Agent notes remain unverified. Cite actual sources and check evidence before reusing as facts.
- Keep platform imports self-contained. Do not scan all runs or another project's workspaces.
- Test project separation, scheduling, feedback/cancellation, model transport, artifact access and knowledge ingestion with temporary projects and mock models.
