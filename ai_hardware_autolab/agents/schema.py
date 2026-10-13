"""Display definitions for the three scenario roles."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

AGENTS_DIR = Path(__file__).resolve().parent

# Roles, in the order the loop runs them. A role is not decoration: export.py
# uses it to decide which runtime profile an agent belongs to, and the split
# between `operate` and `measure` is what keeps the agent that changed the
# board from being the one that reports on it.
ROLES = ("discover", "research", "plan", "build", "operate", "measure", "verify", "report")

# User-facing teams. New missions use these exact three agents. Historical
# stage names remain mapped below so old ledger rows still render coherently.
TEAM_ORDER = ("lead", "builder", "verifier")

TEAM_BY_AGENT = {
    "lead-agent": "lead",
    "mission-architect": "lead",
    "gate-architect": "lead",
    "researcher": "lead",
    "system-architect": "lead",
    "change-analyst": "lead",
    "architecture-selector": "lead",
    "contract-drafter": "lead",
    "contract-reviewer": "lead",
    "builder": "builder",
    "design-builder": "builder",
    "firmware-builder": "builder",
    "board-operator": "builder",
    "measurement": "builder",
    "verifier": "verifier",
    "reporter": "verifier",
}


def team_for_agent(name: str) -> str:
    """Return the stable user-facing team for one internal runtime stage."""

    try:
        return TEAM_BY_AGENT[name]
    except KeyError as exc:
        raise ValidationError(f"agent {name!r} has no user-facing team") from exc

# The kinds of envelope an agent may return.
HANDOFF_KINDS = (
    "Brief",
    "MissionPlan",
    "ResearchPlan",
    "SourcePacket",
    "ChangeAnalysis",
    "ArchitectureDecision",
    "ContractDraft",
    "ContractReview",
    "Plan",
    "StepResult",
    "Verdict",
    "Report",
)

# Verdicts a tool can produce. An agent may copy one of these out of a
# ToolResult. It may never invent one.
TOOL_VERDICTS = ("PASS", "FAIL", "WARNING")

# How one step ended.
STATUSES = (
    "done",            # the step ran and its ToolResult was recorded
    "failed",          # the step ran and the tool judged it FAIL
    "blocked",         # the step could not run, and the obstacle is not a person
    "needs_approval",  # the step needs a human signature before it may run
)

# How the outer loop ended. `satisfied` is the only success. A Report carries
# one of these, because a report is written when the loop has stopped.
LOOP_OUTCOMES = (
    "satisfied",           # every advance condition verified from run evidence
    "needs_approval",      # waiting on a person
    "contract_gap",        # the plan needs authority no contract grants
    "exhausted",           # the same failure repeated with no new evidence
    "primary_run_missing",  # a run the gate depends on is not on disk
)

# What one Verdict can say. A Verdict is about a single iteration, and an
# iteration can end without the loop ending -- `open` means the evidence
# disagreed and there is still something new to try. Keeping the two lists
# apart is what stops a Report from claiming a loop finished as `open`.
ITERATION_OUTCOMES = LOOP_OUTCOMES + ("open",)

CONTRACT_ACTIONS = ("reuse", "amend", "new")
IMPLEMENTATION_STRATEGIES = ("reuse", "configure", "extend", "replace", "new")


class ValidationError(ValueError):
    """A definition or a handoff does not match the shape the loop expects."""


@dataclass(frozen=True)
class AgentDefinition:
    """One agent: its permissions, and the prompt that explains them."""

    name: str
    role: str
    description: str
    prompt: str
    mcp_tools: tuple[str, ...] = ()
    local_tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    emits: tuple[str, ...] = ()
    touches_hardware: bool = False
    native_web: bool = False
    model: str | None = None
    source: Path | None = field(default=None, compare=False)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "team": team_for_agent(self.name),
            "role": self.role,
            "description": self.description,
            "mcp_tools": list(self.mcp_tools),
            "local_tools": list(self.local_tools),
            "skills": list(self.skills),
            "consumes": list(self.consumes),
            "emits": list(self.emits),
            "touches_hardware": self.touches_hardware,
            "native_web": self.native_web,
        }


# -- definition loading ------------------------------------------------------


def _tuple(data: dict[str, Any], key: str, source: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError(f"{source.name}: {key} must be a list of strings")
    return tuple(value)


def _strict_bool(data: dict[str, Any], key: str, source: Path) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ValidationError(f"{source.name}: {key} must be a boolean")
    return value


def split_frontmatter(text: str, source: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). The body keeps its markdown untouched."""
    if not text.startswith("---"):
        raise ValidationError(f"{source.name}: a definition must open with YAML frontmatter")
    _, _, rest = text.partition("---")
    front, marker, body = rest.partition("\n---")
    if not marker:
        raise ValidationError(f"{source.name}: the frontmatter is not closed with ---")
    try:
        data = yaml.safe_load(front) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - message wording varies
        raise ValidationError(f"{source.name}: unparsable frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{source.name}: frontmatter must be a mapping")
    return data, body.lstrip("\n")


def load_agent(path: Path) -> AgentDefinition:
    path = Path(path)
    data, body = split_frontmatter(path.read_text(encoding="utf-8"), path)

    name = str(data.get("name") or path.stem)
    if name != path.stem:
        raise ValidationError(f"{path.name}: frontmatter name {name!r} must match the file name")

    role = str(data.get("role") or "")
    if role not in ROLES:
        raise ValidationError(f"{path.name}: role must be one of {ROLES}, got {role!r}")

    description = str(data.get("description") or "").strip()
    if not description:
        raise ValidationError(
            f"{path.name}: description is required; it is how a runtime routes work to this agent"
        )

    if not body.strip():
        raise ValidationError(f"{path.name}: the body is the prompt and must not be empty")

    emits = _tuple(data, "emits", path)
    consumes = _tuple(data, "consumes", path)
    for kind in emits + consumes:
        if kind not in HANDOFF_KINDS:
            raise ValidationError(f"{path.name}: unknown handoff kind {kind!r}")
    if not emits:
        raise ValidationError(f"{path.name}: an agent that emits nothing cannot be part of the loop")

    return AgentDefinition(
        name=name,
        role=role,
        description=description,
        prompt=body,
        mcp_tools=_tuple(data, "mcp_tools", path),
        local_tools=_tuple(data, "local_tools", path),
        skills=_tuple(data, "skills", path),
        consumes=consumes,
        emits=emits,
        touches_hardware=_strict_bool(data, "touches_hardware", path),
        native_web=_strict_bool(data, "native_web", path),
        model=str(data["model"]) if data.get("model") else None,
        source=path,
    )


def load_all(directory: Path | None = None) -> list[AgentDefinition]:
    """Every definition, in loop order, alphabetically inside a role."""
    directory = Path(directory or AGENTS_DIR)
    agents = [load_agent(p) for p in sorted(directory.glob("*.md")) if p.stem != "README"]
    seen: set[str] = set()
    for agent in agents:
        if agent.name in seen:
            raise ValidationError(f"duplicate agent name: {agent.name}")
        seen.add(agent.name)
    return sorted(agents, key=lambda a: (ROLES.index(a.role), a.name))


def load_simple_team() -> list[AgentDefinition]:
    """Return the three-agent team used by every new scenario mission."""

    agents = load_all(AGENTS_DIR / "simple")
    expected = {"lead-agent", "builder", "verifier"}
    found = {agent.name for agent in agents}
    if found != expected:
        raise ValidationError(
            f"simple team must contain exactly {sorted(expected)}, found {sorted(found)}"
        )
    return agents
