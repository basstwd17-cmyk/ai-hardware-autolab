"""Offline tests for the agent definitions and their handoff envelopes.

What can actually be tested here is a boundary, not a behaviour. Nothing
asserts that a model obeys its prompt. What is asserted is that the permission
a definition grants matches the permission its prompt describes, that the loop
has no gap between what one agent emits and the next consumes, and that an
envelope which would let an unevidenced claim through is rejected before it
reaches the next agent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.agents import export, schema  # noqa: E402
from ai_hardware_autolab.agents.schema import ValidationError, load_agent, load_all  # noqa: E402

AGENTS_DIR = Path(__file__).resolve().parents[1] / "agents"


@pytest.fixture(scope="module")
def agents():
    return load_all(AGENTS_DIR)


# -- the definitions load and mean what they say -----------------------------


def test_every_definition_loads(agents):
    assert {a.name for a in agents} == {
        "architecture-selector",
        "change-analyst",
        "contract-drafter",
        "contract-reviewer",
        "system-architect",
        "mission-architect",
        "researcher",
        "gate-architect",
        "design-builder",
        "firmware-builder",
        "board-operator",
        "measurement",
        "verifier",
        "reporter",
    }


def test_roles_cover_the_whole_loop(agents):
    assert {a.role for a in agents} == set(schema.ROLES)
    assert [a.role for a in agents] == sorted(
        (a.role for a in agents), key=schema.ROLES.index
    )


def test_historical_stages_fold_into_three_user_facing_agents(agents):
    teams = {schema.team_for_agent(agent.name) for agent in agents}
    assert teams == set(schema.TEAM_ORDER)
    assert schema.team_for_agent("mission-architect") == "lead"
    assert schema.team_for_agent("gate-architect") == "lead"
    assert schema.team_for_agent("firmware-builder") == "builder"
    assert schema.team_for_agent("reporter") == "verifier"


def test_definitions_pass_the_registry_check(agents):
    assert export.check(agents) == []


def test_every_mcp_tool_exists_on_the_server(agents):
    known = export.registry_tool_names(allow_hardware=True)
    for agent in agents:
        assert set(agent.mcp_tools) <= known, agent.name


def test_offline_agents_cannot_name_a_board_tool(agents):
    hardware = export.hardware_tool_names()
    for agent in agents:
        if not agent.touches_hardware:
            assert not set(agent.mcp_tools) & hardware, agent.name


def test_the_verifier_has_no_way_to_write(agents):
    verifier = next(a for a in agents if a.role == "verify")
    assert not set(verifier.local_tools) & {"Write", "Edit", "NotebookEdit"}
    assert not set(verifier.mcp_tools) & export.hardware_tool_names()


def test_the_architect_cannot_write_a_contract(agents):
    """The plan is reviewed before anything happens, so the planner writes nothing."""
    architect = next(a for a in agents if a.role == "plan")
    assert not set(architect.local_tools) & {"Write", "Edit", "NotebookEdit"}


def test_only_the_operator_changes_board_state(agents):
    """`measurement` observes; changing the board is one agent's job.

    If the agent that programmed the board were also the one reporting on it,
    the evidence chain would close on itself.
    """
    changing = {"program_cpld_sram", "deploy_pru"}
    for agent in agents:
        if agent.name != "board-operator":
            assert not set(agent.mcp_tools) & changing, agent.name


def test_the_loop_has_no_gap(agents):
    """Every kind consumed is produced by somebody, except the human's Brief."""
    produced = {kind for a in agents for kind in a.emits} | {"Brief"}
    for agent in agents:
        assert set(agent.consumes) <= produced, agent.name


def test_offline_profile_drops_the_hardware_agents(agents):
    offline = export.select(agents, "offline")
    assert all(not a.touches_hardware for a in offline)
    assert {a.name for a in offline} == {
        "architecture-selector",
        "change-analyst",
        "contract-drafter",
        "contract-reviewer",
        "system-architect",
        "mission-architect",
        "researcher",
        "gate-architect",
        "design-builder",
        "firmware-builder",
        "verifier",
        "reporter",
    }
    assert len(export.select(agents, "hardware")) == len(agents)


def test_unknown_profile_is_refused(agents):
    with pytest.raises(ValidationError):
        export.select(agents, "everything")


# -- malformed definitions are refused ---------------------------------------


def _write(tmp_path: Path, name: str, front: str, body: str = "do the thing") -> Path:
    path = tmp_path / f"{name}.md"
    path.write_text(f"---\n{front}\n---\n\n{body}\n", encoding="utf-8")
    return path


def test_missing_frontmatter_is_refused(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(ValidationError, match="frontmatter"):
        load_agent(path)


def test_unclosed_frontmatter_is_refused(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("---\nname: x\nrole: plan\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="not closed"):
        load_agent(path)


def test_name_must_match_the_file(tmp_path):
    path = _write(tmp_path, "planner", "name: architect\nrole: plan\ndescription: d\nemits: [Plan]")
    with pytest.raises(ValidationError, match="must match the file name"):
        load_agent(path)


def test_unknown_role_is_refused(tmp_path):
    path = _write(tmp_path, "x", "name: x\nrole: supervise\ndescription: d\nemits: [Plan]")
    with pytest.raises(ValidationError, match="role must be one of"):
        load_agent(path)


def test_unknown_handoff_kind_is_refused(tmp_path):
    path = _write(tmp_path, "x", "name: x\nrole: plan\ndescription: d\nemits: [Blueprint]")
    with pytest.raises(ValidationError, match="unknown handoff kind"):
        load_agent(path)


def test_native_web_flag_must_be_a_real_boolean(tmp_path):
    path = _write(
        tmp_path,
        "x",
        "name: x\nrole: research\ndescription: d\nemits: [SourcePacket]\nnative_web: 'false'",
    )
    with pytest.raises(ValidationError, match="native_web must be a boolean"):
        load_agent(path)


def test_an_agent_that_emits_nothing_is_refused(tmp_path):
    path = _write(tmp_path, "x", "name: x\nrole: plan\ndescription: d")
    with pytest.raises(ValidationError, match="emits nothing"):
        load_agent(path)


def test_an_empty_prompt_is_refused(tmp_path):
    path = _write(tmp_path, "x", "name: x\nrole: plan\ndescription: d\nemits: [Plan]", body="")
    with pytest.raises(ValidationError, match="must not be empty"):
        load_agent(path)


def test_check_catches_an_undeclared_hardware_tool(tmp_path):
    path = _write(
        tmp_path,
        "sneaky",
        "name: sneaky\nrole: build\ndescription: d\nemits: [StepResult]\n"
        "mcp_tools: [program_cpld_sram]",
    )
    problems = export.check([load_agent(path)])
    assert any("touches_hardware is false" in p for p in problems)


def test_check_catches_an_unknown_tool(tmp_path):
    path = _write(
        tmp_path,
        "sneaky",
        "name: sneaky\nrole: build\ndescription: d\nemits: [StepResult]\n"
        "mcp_tools: [program_the_flash]",
    )
    assert any("unknown MCP tool" in p for p in export.check([load_agent(path)]))


# -- handoff envelopes -------------------------------------------------------

VALID_PLAN = {
    "kind": "Plan",
    "agent": "gate-architect",
    "gate": "gate12-pru-dac1-plan.yaml",
    "iteration": 1,
    "steps": [
        {
            "id": "s1",
            "agent": "design-builder",
            "tool": "build_cpld",
            "contract": "gate12-pru-dac1-plan.yaml",
            "advance_when": [
                {
                    "field": "measurements.drc_errors",
                    "expect": 0,
                    "source": "contracts/gate12-pru-dac1-plan.yaml pass_criteria",
                }
            ],
        }
    ],
}


def test_a_valid_plan_passes():
    assert schema.validate_handoff(json.loads(json.dumps(VALID_PLAN)))


def test_a_step_without_an_advance_condition_is_refused():
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["steps"][0]["advance_when"] = []
    with pytest.raises(ValidationError, match="advance_when"):
        schema.validate_handoff(plan)


def test_a_threshold_without_a_source_is_refused():
    """The rule that keeps invented numbers out of a plan."""
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["steps"][0]["advance_when"][0]["source"] = "   "
    with pytest.raises(ValidationError, match="source is empty"):
        schema.validate_handoff(plan)


def test_duplicate_step_ids_are_refused():
    plan = json.loads(json.dumps(VALID_PLAN))
    plan["steps"].append(json.loads(json.dumps(plan["steps"][0])))
    with pytest.raises(ValidationError, match="ids must be unique"):
        schema.validate_handoff(plan)


def test_a_done_step_with_no_run_is_refused():
    with pytest.raises(ValidationError, match="did not happen"):
        schema.validate_handoff(
            {
                "kind": "StepResult",
                "agent": "design-builder",
                "step_id": "s1",
                "status": "done",
                "runs": [],
            }
        )


def test_a_blocked_step_may_have_no_run():
    assert schema.validate_handoff(
        {
            "kind": "StepResult",
            "agent": "board-operator",
            "step_id": "s3",
            "status": "needs_approval",
            "runs": [],
        }
    )


def test_an_invented_verdict_is_refused():
    with pytest.raises(ValidationError, match="copied from a ToolResult"):
        schema.validate_handoff(
            {
                "kind": "StepResult",
                "agent": "measurement",
                "step_id": "s5",
                "status": "done",
                "runs": ["20260821T111203Z_x"],
                "verdict": "looks fine",
            }
        )


def test_a_met_condition_needs_evidence():
    with pytest.raises(ValidationError, match="met by a run record"):
        schema.validate_handoff(
            {
                "kind": "Verdict",
                "agent": "verifier",
                "gate": "g.yaml",
                "iteration": 1,
                "outcome": "satisfied",
                "conditions": [{"field": "measurements.drc_errors", "met": True, "evidence": []}],
            }
        )


def test_satisfied_cannot_close_over_an_open_condition():
    with pytest.raises(ValidationError, match="may not close over an open condition"):
        schema.validate_handoff(
            {
                "kind": "Verdict",
                "agent": "verifier",
                "gate": "g.yaml",
                "iteration": 2,
                "outcome": "satisfied",
                "conditions": [
                    {"field": "a", "met": True, "evidence": ["run1"]},
                    {"field": "b", "met": False, "evidence": ["run2"]},
                ],
            }
        )


def test_an_unmet_condition_closes_as_exhausted_not_satisfied():
    assert schema.validate_handoff(
        {
            "kind": "Verdict",
            "agent": "verifier",
            "gate": "g.yaml",
            "iteration": 3,
            "outcome": "exhausted",
            "conditions": [{"field": "b", "met": False, "evidence": ["run2"]}],
        }
    )


def test_an_unknown_envelope_kind_is_refused():
    with pytest.raises(ValidationError, match="unknown handoff kind"):
        schema.validate_handoff({"kind": "Opinion", "agent": "x"})


def test_a_handoff_must_be_an_object():
    with pytest.raises(ValidationError, match="must be a JSON object"):
        schema.validate_handoff("done, it passed")


# -- rendering ---------------------------------------------------------------


def test_claude_json_qualifies_mcp_tool_names(agents):
    rendered = export.render_claude_json(agents)
    verifier = rendered["verifier"]
    assert "mcp__rtase-hwtools__read_run" in verifier["tools"]
    assert "Write" not in verifier["tools"]
    assert verifier["prompt"].strip()


def test_claude_files_round_trip_the_prompt(agents):
    for agent in agents:
        text = export.render_claude_file(agent)
        assert text.startswith("---\n")
        assert agent.prompt.rstrip() in text
        assert "Generated from ai_hardware_autolab/agents/" in text


def test_codex_prompt_states_its_tool_boundary(agents):
    operator = next(a for a in agents if a.name == "board-operator")
    text = export.render_codex_prompt(operator)
    assert "program_cpld_sram" in text
    assert "Call no other tool" in text
    assert "one step, then return" in text


def test_codex_prompt_names_the_skills_to_load(agents):
    measure = next(a for a in agents if a.name == "measurement")
    assert "scope-measure" in export.render_codex_prompt(measure)


def test_only_researcher_receives_native_web(agents):
    enabled = [agent.name for agent in agents if agent.native_web]
    assert enabled == ["researcher"]
    prompt = export.render_codex_prompt(next(a for a in agents if a.name == "researcher"))
    assert "Search results are discovery, not evidence" in prompt


def test_export_cli_check_only_succeeds(capsys):
    assert export.main(["--check-only"]) == 0
    assert "no problems" in capsys.readouterr().out


# -- the definitions reach a runtime that is not Claude Code -----------------


def _prompt_names(allow_hardware: bool) -> list[str]:
    from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_prompts

    return [p.name for p in build_prompts(ServerPolicy(allow_hardware=allow_hardware))]


def test_agents_are_served_as_mcp_prompts():
    names = _prompt_names(allow_hardware=True)
    assert "agent-verifier" in names
    assert "build-cpld" in names, "skills must still be served alongside them"


def test_a_locked_server_withholds_the_hardware_agents():
    """Same rule as the tool list: what cannot be called is not offered."""
    locked = _prompt_names(allow_hardware=False)
    assert "agent-board-operator" not in locked
    assert "agent-measurement" not in locked
    assert "agent-verifier" in locked


def test_the_exported_copies_are_current(agents):
    """A stale export is a silent lie: the runtime reads it, the reviewer reads the source."""
    for agent in agents:
        claude = export.CLAUDE_AGENTS_DIR / f"{agent.name}.md"
        assert claude.is_file(), f"missing export: {claude}"
        assert claude.read_text(encoding="utf-8") == export.render_claude_file(agent), (
            f"{claude} is stale; re-run "
            "`py -3 -m ai_hardware_autolab.agents.export --runtime claude --profile hardware --write`"
        )
        codex = export.CODEX_PROMPTS_DIR / f"{agent.name}.md"
        assert codex.is_file(), f"missing export: {codex}"
        assert codex.read_text(encoding="utf-8") == export.render_codex_prompt(agent), (
            f"{codex} is stale; re-run "
            "`py -3 -m ai_hardware_autolab.agents.export --runtime codex --profile hardware --write`"
        )


def test_a_served_agent_prompt_carries_its_body():
    from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_prompts

    prompt = next(
        p for p in build_prompts(ServerPolicy(allow_hardware=True)) if p.name == "agent-verifier"
    )
    body = prompt.loader({})
    assert "A claim in a StepResult is not evidence" in body
    assert "read_run" in body
