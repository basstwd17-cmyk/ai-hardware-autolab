"""Agent definitions: who plans, who touches the board, who may judge.

Layer 1 of the three-layer design. Layer 3 (``hwtools``) decides what is
permitted and returns a ``ToolResult``; layer 2 (``orchestrator``) counts
approvals; this package says which agent may call which tool, and in what
order they hand work to each other.

Definitions are markdown with YAML frontmatter -- the same shape as
``.claude/skills/*/SKILL.md`` -- so nothing here belongs to one CLI.
``export.py`` renders them for whichever runtime is being used.
"""

from .schema import (  # noqa: F401
    AgentDefinition,
    HANDOFF_KINDS,
    ITERATION_OUTCOMES,
    LOOP_OUTCOMES,
    ROLES,
    STATUSES,
    ValidationError,
    load_agent,
    load_all,
    validate_handoff,
)
