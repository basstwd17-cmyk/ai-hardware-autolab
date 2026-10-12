# AI Hardware Autolab source navigation

Read README.md and STATUS.md before broad exploration.
Current execution is orchestrator/conversation_flow.py, conversation_codex.py
and conversation_service.py: Lead → Builder → Verifier.

- The selected project's source and user scenario define the work.
- New jobs do not require execution contracts, compiled Plans or Campaigns.
- ProjectRegistry and ProjectWorkspaces in orchestrator/projects.py pin each
  job's project settings and develop its source in an isolated Git workspace.
- Preserve original source, active job workspaces, SQLite state and knowledge.
  Do not run git clean -fdX. Shared runtime stores are not source packages.
- Generic jobs have no default board, lab IP, device or vendor toolchain.
- Agent messages carry design intent, artifacts, concrete checks and sources.
- Stored agent notes stay unverified; verify references before reusing as facts.
- Keep package imports and project-relative paths consistent with
  ai_hardware_autolab. Do not recursively search other job workspaces/runs.
- Native optional driver interfaces remain internal extension points. No
  hardware example or external board repository is required for normal startup.
- Historical modules and documents are compatibility/reference material, not
  requirements for connecting a new project. Check consumers before removal.
