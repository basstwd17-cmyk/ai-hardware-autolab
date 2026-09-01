"""Agent definitions, and the envelope they hand to each other.

Two things live here.

**The definition format.** An agent is a markdown file with YAML frontmatter.
The frontmatter is the enforceable part -- which tools the agent may call,
whether it is allowed near the board -- and the body is the prompt. Keeping the
two in one file is what makes a definition reviewable: the permission and the
reason for it are on the same page.

**The handoff envelope.** Agents in a loop have to pass work to each other, and
prose does not survive that trip. A measurement described in a sentence loses
its units, its run directory, and the difference between "the tool judged this
FAIL" and "the model thinks this looks wrong". So every agent returns one JSON
object of a known kind, and the field carrying a PASS/FAIL is copied out of a
``ToolResult`` rather than written by the model.

There is no ``jsonschema`` dependency on purpose: the rest of the platform
installs with PyYAML alone, and these shapes are small enough to check by hand.
"""

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


# -- handoff envelopes -------------------------------------------------------

_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "Brief": ("kind", "agent", "scenario", "resources"),
    "Plan": ("kind", "agent", "gate", "iteration", "steps"),
    "StepResult": ("kind", "agent", "step_id", "status", "runs"),
    "Verdict": ("kind", "agent", "gate", "iteration", "outcome", "conditions"),
    "Report": ("kind", "agent", "gate", "outcome", "document"),
}


def _require(envelope: dict[str, Any], kind: str) -> None:
    missing = [f for f in _REQUIRED_FIELDS[kind] if f not in envelope]
    if missing:
        raise ValidationError(f"{kind} is missing required fields: {', '.join(missing)}")


def _check_condition(condition: Any, where: str) -> None:
    """An advance condition has to be checkable against a recorded field.

    ``field`` names a key inside the ToolResult the step will produce,
    ``expect`` is the value it must hold, and ``source`` says which datasheet
    page, contract field or earlier run supplies that number. The last one is
    not paperwork: a threshold nobody can trace is worse than no threshold,
    because it looks like it was measured.
    """
    if not isinstance(condition, dict):
        raise ValidationError(f"{where} must be an object")
    for key in ("field", "expect", "source"):
        if key not in condition:
            raise ValidationError(f"{where} is missing {key!r}")
    if not str(condition["source"]).strip():
        raise ValidationError(
            f"{where}.source is empty. Name the datasheet, contract field or run that "
            "supplies this number, or say the step measures it for the first time."
        )


def _check_step(step: Any, index: int, known_agents: set[str] | None = None) -> None:
    if not isinstance(step, dict):
        raise ValidationError(f"Plan.steps[{index}] must be an object")
    for key in ("id", "agent", "tool", "contract", "advance_when"):
        if key not in step:
            raise ValidationError(f"Plan.steps[{index}] is missing {key!r}")
    if known_agents is not None and step["agent"] not in known_agents:
        raise ValidationError(
            f"Plan.steps[{index}].agent names {step['agent']!r}; known agents: "
            f"{', '.join(sorted(known_agents))}"
        )
    conditions = step["advance_when"]
    if not isinstance(conditions, list) or not conditions:
        raise ValidationError(
            f"Plan.steps[{index}].advance_when must be a non-empty list. A step with no "
            "advance condition cannot be verified, and so cannot be planned."
        )
    for j, condition in enumerate(conditions):
        _check_condition(condition, f"Plan.steps[{index}].advance_when[{j}]")


def validate_handoff(
    envelope: Any, *, known_agents: Iterable[str] | None = None
) -> dict[str, Any]:
    """Raise unless the envelope is one the next agent in the loop can read."""
    if not isinstance(envelope, dict):
        raise ValidationError("a handoff must be a JSON object")
    kind = envelope.get("kind")
    if kind not in HANDOFF_KINDS:
        raise ValidationError(f"unknown handoff kind: {kind!r}; expected one of {HANDOFF_KINDS}")
    if kind in (
        "MissionPlan",
        "ResearchPlan",
        "SourcePacket",
        "ChangeAnalysis",
        "ArchitectureDecision",
        "ContractDraft",
        "ContractReview",
    ):
        try:
            from QKD_PROJ.research.architecture import (
                ArchitectureDecision,
                ContractDraft,
                ContractReview,
            )
            from QKD_PROJ.research.models import (
                ResearchPlan,
                ResearchValidationError,
                SourcePacket,
            )
            from QKD_PROJ.research.mission import ChangeAnalysis, MissionPlan

            if kind == "MissionPlan":
                MissionPlan.parse(envelope)
            elif kind == "ResearchPlan":
                ResearchPlan.parse(envelope)
            elif kind == "SourcePacket":
                SourcePacket.parse(envelope)
            elif kind == "ChangeAnalysis":
                ChangeAnalysis.parse(envelope)
            elif kind == "ArchitectureDecision":
                ArchitectureDecision.parse(envelope)
            elif kind == "ContractDraft":
                ContractDraft.parse(envelope)
            else:
                ContractReview.parse(envelope)
        except ResearchValidationError as exc:
            raise ValidationError(str(exc)) from exc
        return envelope
    _require(envelope, kind)

    if kind == "Plan":
        steps = envelope["steps"]
        if not isinstance(steps, list):
            raise ValidationError("Plan.steps must be a list")
        # A Plan that stops -- `blocked_on: contract_gap`, or `exhausted` -- is a
        # legitimate plan with nothing in it. Demanding a step would push the
        # architect into inventing one rather than refusing.
        if not steps and not envelope.get("blocked_on"):
            raise ValidationError(
                "Plan.steps is empty and blocked_on is not set. A plan with no steps and no "
                "reason for having none cannot be executed or reviewed."
            )
        ids = [s.get("id") for s in steps if isinstance(s, dict)]
        if len(set(ids)) != len(ids):
            raise ValidationError("Plan.steps ids must be unique")
        agent_names = set(known_agents) if known_agents is not None else None
        for index, step in enumerate(steps):
            _check_step(step, index, agent_names)

    elif kind == "StepResult":
        status = envelope["status"]
        if status not in STATUSES:
            raise ValidationError(f"StepResult.status must be one of {STATUSES}, got {status!r}")
        if not isinstance(envelope["runs"], list):
            raise ValidationError("StepResult.runs must be a list of run directory names")
        if status in ("done", "failed") and not envelope["runs"]:
            raise ValidationError(
                f"StepResult.status is {status!r} with no run. Every tool call writes a "
                "RunRecord; a step with no run has no evidence and did not happen."
            )
        verdict = envelope.get("verdict")
        if verdict is not None and verdict not in TOOL_VERDICTS:
            raise ValidationError(
                f"StepResult.verdict must be copied from a ToolResult {TOOL_VERDICTS}, "
                f"got {verdict!r}"
            )

    elif kind == "Verdict":
        outcome = envelope["outcome"]
        if outcome not in ITERATION_OUTCOMES:
            raise ValidationError(
                f"Verdict.outcome must be one of {ITERATION_OUTCOMES}, got {outcome!r}"
            )
        conditions = envelope["conditions"]
        if not isinstance(conditions, list) or not conditions:
            raise ValidationError("Verdict.conditions must be a non-empty list")
        for index, condition in enumerate(conditions):
            if not isinstance(condition, dict):
                raise ValidationError(f"Verdict.conditions[{index}] must be an object")
            for key in ("field", "met", "evidence"):
                if key not in condition:
                    raise ValidationError(f"Verdict.conditions[{index}] is missing {key!r}")
            if condition["met"] and not condition["evidence"]:
                raise ValidationError(
                    f"Verdict.conditions[{index}] is met with no evidence. A condition is "
                    "met by a run record, not by an agent saying so."
                )
        if outcome == "satisfied" and not all(c.get("met") for c in conditions):
            raise ValidationError(
                "Verdict.outcome is 'satisfied' while a condition is unmet. The loop may "
                "not close over an open condition."
            )

    elif kind == "Report":
        # Terminal outcomes only: a report describes a loop that has stopped.
        if envelope["outcome"] not in LOOP_OUTCOMES:
            raise ValidationError(
                f"Report.outcome must be one of {LOOP_OUTCOMES}, got {envelope['outcome']!r}"
            )

    return envelope
