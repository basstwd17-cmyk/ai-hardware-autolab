# Current platform source navigation

Read README.md and STATUS.md before broad source exploration.
New missions use orchestrator/conversation_flow.py::INSTRUCTIONS:
Lead, Builder and Verifier through conversation_codex.py and conversation_service.py.

- The current scenario, feedback and selected physical setup define the work.
  New agents exchange design intent, implementation artifacts and evidence; they
  do not create execution contracts, compiled Plans or Campaigns.
- Reuse source selected by the board profile or capability catalog, checking
  its path/hash and relevance. Newest mtime or a Gate filename is not proof.
- For conversation jobs these catalogs are advisory, not a mandatory route.
  Agents determine implementation; the host binds physical device access. Do not
  create an unrelated board profile, force all skills, or require a coding
  contract. Builder edits ordinary source in its isolated workspace and leaves
  changes for final review rather than publishing them automatically.
- docs/history/, dated design notes, agents/simple/, agents/codex/, and root agents/*.md are
  historical/compatibility material (except agents/README.md, the current guide).
  Consult for a specific evidence gap or compatibility task, not by default.
- Do not recursively search another job's workspaces, generated caches, or
  all run/reference stores. Use bounded tools and exact relevant paths.
- BBB/PRU sources also live in ../BeagleBone-Black; STM32 code lives in
  ../STM32Cube. Select the exact board/revision before reusing them.
- Jobs select their own registered hardware setup. Start with inputs.selected_board
  source_paths/datasheets; do not import RTASE pins, SRAM restrictions or knowledge
  into another board. USB/COM presence does not verify its model or firmware.
  Registry/discovery code is in boards/registry.py and boards/discovery.py;
  SelectedHardwareExecutor binds each job to its stored board/revision/hash.
- Preserve contracts, runs, references, candidates, and SQLite state. They are
  not disposable just because Git ignores them. Never use git clean -fdX here.
- Existing source/contract/capability paths are evidence links. Do not move
  them without migrating and testing every consumer.

.rgignore controls ordinary search, not access permissions or safety.
