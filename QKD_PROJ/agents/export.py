"""Render the runtime-neutral definitions for one CLI.

The definitions in this directory are markdown plus frontmatter and belong to
no vendor. A runtime needs them in its own shape: Claude Code takes an
``--agents`` JSON object or files under ``.claude/agents/``; Codex takes a
prompt on stdin. This module does that translation and nothing else -- no
definition content is invented here, so a runtime cannot quietly gain a tool
that its definition does not list.

**What the export can and cannot enforce.** Claude Code accepts a per-agent
tool allowlist, so the ``tools`` field really does bound what that agent can
call. Codex has no equivalent per-agent list, so on that runtime the bound is
the MCP server's launch profile: start the server without ``--allow-hardware``
and the board-touching tools are not registered at all, for any agent. Two
different mechanisms, one rule -- which is why the profile, not the definition,
is what a person chooses at launch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.agents.schema import AgentDefinition, ValidationError, load_all

MCP_SERVER_NAME = "rtase-hwtools"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_AGENTS_DIR = PROJECT_ROOT / ".claude" / "agents"
CODEX_PROMPTS_DIR = Path(__file__).resolve().parent / "codex"

GENERATED_NOTE = (
    "Generated from QKD_PROJ/agents/{source}. Edit that file, then run\n"
    "`py -3 -m QKD_PROJ.agents.export --runtime claude --profile hardware --write`.\n"
    "tests/test_agents.py fails if this copy drifts from the definition."
)

PROFILES = ("offline", "hardware")


def qualify(tool: str, server: str = MCP_SERVER_NAME) -> str:
    """An MCP tool name as a runtime addresses it."""
    return f"mcp__{server}__{tool}"


def select(agents: Iterable[AgentDefinition], profile: str) -> list[AgentDefinition]:
    """`offline` drops every agent that would need the board.

    This is not a convenience. An unattended run is allowed to do offline work
    only, so the unattended profile must not contain an agent whose whole
    purpose is to change hardware state -- even a well-behaved one that would
    have stopped at the approval check.
    """
    if profile not in PROFILES:
        raise ValidationError(f"profile must be one of {PROFILES}, got {profile!r}")
    agents = list(agents)
    if profile == "hardware":
        return agents
    return [a for a in agents if not a.touches_hardware]


# -- Claude Code -------------------------------------------------------------


def claude_tools(agent: AgentDefinition) -> list[str]:
    return [*agent.local_tools, *(qualify(t) for t in agent.mcp_tools)]


def render_claude_json(agents: Iterable[AgentDefinition]) -> dict[str, Any]:
    """The object `claude -p --agents` takes."""
    rendered: dict[str, Any] = {}
    for agent in agents:
        entry: dict[str, Any] = {
            "description": agent.description,
            "prompt": agent.prompt,
            "tools": claude_tools(agent),
        }
        if agent.model:
            entry["model"] = agent.model
        rendered[agent.name] = entry
    return rendered


def render_claude_file(agent: AgentDefinition) -> str:
    """One `.claude/agents/<name>.md`, frontmatter in that runtime's dialect."""
    lines = [
        "---",
        f"name: {agent.name}",
        f"description: {json.dumps(agent.description, ensure_ascii=False)}",
        f"tools: {', '.join(claude_tools(agent))}",
    ]
    if agent.model:
        lines.append(f"model: {agent.model}")
    lines += ["---", ""]
    source = agent.source.name if agent.source else f"{agent.name}.md"
    lines.append("<!-- " + GENERATED_NOTE.format(source=source) + " -->")
    lines += ["", agent.prompt.rstrip(), ""]
    return "\n".join(lines)


# -- Codex -------------------------------------------------------------------


def render_codex_prompt(agent: AgentDefinition) -> str:
    """A prompt for `codex exec`, with its permissions stated in the text.

    Codex cannot be handed a per-agent tool allowlist, so the list is written
    into the prompt as a boundary the agent is told to respect, and the real
    enforcement stays where it already is: tools absent from the server's
    profile, and SafetyGate underneath them.
    """
    tools = ", ".join(agent.mcp_tools) or "none"
    header = [
        f"# Agent: {agent.name} ({agent.role})",
        "",
        agent.description,
        "",
        f"MCP server: {MCP_SERVER_NAME}. Tools you may call: {tools}.",
        "Call no other tool on that server. If a tool you need is not listed, stop and",
        "say which one and why -- do not substitute a different tool for it.",
    ]
    if agent.touches_hardware:
        header += [
            "",
            "This agent changes or observes real hardware. Execute one step, then return.",
        ]
    if agent.native_web:
        header += [
            "",
            "Native web search is enabled only for this role. Search results are discovery, not evidence.",
            "Use collect_source, search_reference and read_reference_page to turn a selected PDF",
            "into immutable, page-cited evidence before making a SourcePacket claim.",
        ]
    if agent.skills:
        header += [
            "",
            "Load these skills from the MCP server's prompts before you start: "
            + ", ".join(agent.skills)
            + ".",
        ]
    return "\n".join([*header, "", "---", "", agent.prompt.rstrip(), ""])


# -- validation --------------------------------------------------------------


def registry_tool_names(allow_hardware: bool = True) -> set[str]:
    from QKD_PROJ.mcp_server.registry import ServerPolicy, build_tools

    return {t.name for t in build_tools(ServerPolicy(allow_hardware=allow_hardware))}


def hardware_tool_names() -> set[str]:
    return registry_tool_names(True) - registry_tool_names(False)


def check(agents: Iterable[AgentDefinition]) -> list[str]:
    """Problems that would make an export lie about what an agent can do."""
    agents = list(agents)
    known = registry_tool_names(True)
    hardware = hardware_tool_names()
    problems: list[str] = []

    for agent in agents:
        for tool in agent.mcp_tools:
            if tool not in known:
                problems.append(
                    f"{agent.name}: unknown MCP tool {tool!r}; the server registers {sorted(known)}"
                )
        used_hardware = sorted(set(agent.mcp_tools) & hardware)
        if used_hardware and not agent.touches_hardware:
            problems.append(
                f"{agent.name}: touches_hardware is false but lists board tools {used_hardware}"
            )
        if agent.touches_hardware and not used_hardware:
            problems.append(
                f"{agent.name}: touches_hardware is true but lists no board tool; "
                "an agent should not carry a permission it does not use"
            )
        if agent.role == "verify" and set(agent.local_tools) & {"Write", "Edit", "NotebookEdit"}:
            problems.append(
                f"{agent.name}: a verifying agent must not be able to write. Its verdict is "
                "only worth reading because it cannot change what it judges."
            )
        if agent.native_web and agent.role != "research":
            problems.append(f"{agent.name}: native_web is reserved for the research role")

    produced = {kind for agent in agents for kind in agent.emits}
    for agent in agents:
        for kind in agent.consumes:
            if kind not in produced and kind != "Brief":
                problems.append(
                    f"{agent.name}: consumes {kind!r}, which no agent emits -- the loop has a gap"
                )
    return problems


# -- CLI ---------------------------------------------------------------------


def _write(path: Path, text: str, dry_run: bool) -> str:
    # newline="\n" on purpose: the definitions are LF and the export is compared
    # against them by a test. Letting the platform decide would make a checkout
    # on Windows look like a drifted export.
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render agent definitions for one runtime.")
    parser.add_argument("--runtime", choices=("claude", "codex", "json"), default="json")
    parser.add_argument("--profile", choices=PROFILES, default="offline")
    parser.add_argument("--write", action="store_true", help="write files instead of printing")
    parser.add_argument("--check-only", action="store_true", help="validate and exit")
    args = parser.parse_args(argv)

    try:
        agents = load_all()
    except ValidationError as exc:
        print(f"invalid definition: {exc}", file=sys.stderr)
        return 2

    problems = check(agents)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    if args.check_only:
        print(f"ok: {len(agents)} definitions, no problems")
        return 0

    selected = select(agents, args.profile)
    if args.runtime == "claude" and args.write:
        for agent in selected:
            print(_write(CLAUDE_AGENTS_DIR / f"{agent.name}.md", render_claude_file(agent), False))
    elif args.runtime == "codex" and args.write:
        for agent in selected:
            print(_write(CODEX_PROMPTS_DIR / f"{agent.name}.md", render_codex_prompt(agent), False))
    elif args.runtime == "claude":
        print(json.dumps(render_claude_json(selected), ensure_ascii=False, indent=2))
    elif args.runtime == "codex":
        for agent in selected:
            print(render_codex_prompt(agent))
    else:
        print(json.dumps([a.describe() for a in selected], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
