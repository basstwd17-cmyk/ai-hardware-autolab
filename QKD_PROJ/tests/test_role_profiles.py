"""Role profiles: who may call what, and which scripts count as touching the board.

The agent definitions already say `measurement` observes and `board-operator`
acts. Until now that was enforced by the definitions alone -- both roles carry
`run_bbb`, so a measurement agent that ran a mutating diagnostic would have been
stopped by its prompt and by nothing else.

These tests pin the two locks that replace the prompt. A tool outside a role's
row is not registered, so no prompt can reach it. And a script the contract has
not classified as `read` is refused to any role that may not mutate -- including
scripts nobody has classified at all, which is the case that has to fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from QKD_PROJ.mcp_server.protocol import ToolError  # noqa: E402
from QKD_PROJ.mcp_server.registry import (  # noqa: E402
    MUTATE,
    READ,
    ROLE_EFFECTS,
    ROLE_TOOLS,
    ServerPolicy,
    build_prompts,
    build_tools,
    script_effect,
)

QKD_ROOT = Path(__file__).resolve().parents[1]

STATE_CHANGING = {"program_cpld_sram", "deploy_pru", "scan_jtag"}


def tools_for(role: str | None, allow_hardware: bool = True) -> set[str]:
    return {t.name for t in build_tools(ServerPolicy(allow_hardware=allow_hardware, role=role))}


# -- the matrix --------------------------------------------------------------


def test_every_role_has_both_a_tool_row_and_an_effect_row():
    """One matrix. A role in one table and not the other is a hole."""
    assert set(ROLE_TOOLS) == set(ROLE_EFFECTS)


def test_role_names_cover_runtime_and_legacy_agent_names():
    """The server recognises the new team and historical job definitions."""
    from QKD_PROJ.agents import load_all
    from QKD_PROJ.agents.schema import load_simple_team

    assert set(ROLE_TOOLS) == {
        a.name for a in [*load_all(), *load_simple_team()]
    }


def test_only_the_builder_or_legacy_operator_may_change_the_board():
    for role, tools in ROLE_TOOLS.items():
        overlap = tools & STATE_CHANGING
        if role in {"builder", "board-operator"}:
            assert overlap == STATE_CHANGING
        else:
            assert overlap == set(), f"{role} carries {overlap}"


def test_only_the_builder_or_legacy_operator_may_run_mutating_scripts():
    for role, effects in ROLE_EFFECTS.items():
        if role in {"builder", "board-operator"}:
            assert MUTATE in effects
        else:
            assert effects == frozenset({READ}), role


def test_measurement_gets_its_observing_tools_and_nothing_that_acts():
    measurement = tools_for("measurement")
    assert {
        "measure_gate1_gpio", "measure_board_scope", "run_bbb", "run_board_stage"
    } <= measurement
    assert not measurement & STATE_CHANGING
    assert "build_cpld" not in measurement


def test_the_read_only_roles_get_no_hardware_tool_at_all():
    for role in (
        "architecture-selector",
        "system-architect",
        "researcher",
        "contract-drafter",
        "contract-reviewer",
        "gate-architect",
        "verifier",
        "reporter",
    ):
        assert tools_for(role) == {"list_contracts", "read_contract", "list_runs", "read_run"}

    assert tools_for("firmware-builder") == {
        "list_contracts", "read_contract", "list_runs", "read_run", "run_board_build"
    }


def test_a_role_still_needs_the_hardware_flag():
    """Two locks, and they compose. The role narrows; the flag unlocks."""
    locked = tools_for("board-operator", allow_hardware=False)
    assert not locked & STATE_CHANGING
    assert "run_bbb" not in locked
    assert "list_runs" in locked


def test_no_role_means_the_historical_profile():
    everything = tools_for(None)
    assert STATE_CHANGING <= everything
    assert {"build_cpld", "measure_gate1_gpio", "run_bbb"} <= everything


def test_an_unknown_role_is_refused_at_launch():
    with pytest.raises(ToolError, match="Unknown role"):
        ServerPolicy(role="intern")


def test_a_roled_server_serves_only_that_role_definition():
    names = [p.name for p in build_prompts(ServerPolicy(allow_hardware=True, role="measurement"))]
    assert "agent-measurement" in names
    assert "agent-board-operator" not in names
    assert "build-cpld" in names, "skills are still served"


# -- classifying a script ----------------------------------------------------


def test_an_unclassified_script_counts_as_mutating():
    """Fail closed: a script nobody classified is a script nobody thought about."""
    assert script_effect({"path": "x.sh", "sha256": "0" * 64}) == MUTATE
    assert script_effect({"effect": "reads a few registers"}) == MUTATE
    assert script_effect(None) == MUTATE
    assert script_effect("not a mapping") == MUTATE


def test_an_unrecognised_class_counts_as_mutating():
    assert script_effect({"effect_class": "mostly harmless"}) == MUTATE
    assert script_effect({"effect_class": ""}) == MUTATE


def test_a_declared_read_is_honoured():
    assert script_effect({"effect_class": "read"}) == READ
    assert script_effect({"effect_class": "  READ "}) == READ


def test_a_prose_effect_does_not_classify_by_itself():
    """The sentence is worth keeping and cannot be enforced; effect_class is beside it."""
    assert script_effect({"effect": "read_only"}) == MUTATE


# -- the effect check on run_bbb ---------------------------------------------


def _call_run_bbb(role: str | None, contract: str, script: str, contracts_dir: Path):
    """Call the handler with runs/ pointed somewhere disposable.

    Pointing it at the real runs/ would file evidence for a test, and the audit
    would later report those directories as runs no contract cites -- which is
    true, and which nobody would be able to explain.
    """
    from QKD_PROJ.mcp_server.registry import _run_bbb

    policy = ServerPolicy(
        allow_hardware=True,
        role=role,
        contracts_dir=contracts_dir,
        runs_dir=contracts_dir.parent / "runs",
    )
    return _run_bbb(policy, {"contract": contract, "script": script})


@pytest.fixture()
def contracts(tmp_path):
    directory = tmp_path / "contracts"
    directory.mkdir()
    (directory / "board-map.yaml").write_text("nets: {}\n", encoding="utf-8")
    (directory / "t.yaml").write_text(
        "schema_version: 1\n"
        "test_id: t\n"
        "status: test\n"
        "allowed_operations: [bbb_ssh_command]\n"
        "forbidden_operations: []\n"
        "scripts:\n"
        "  observe:\n"
        "    path: BeagleBone-Black/diagnostics/mcspi_kernel_state.sh\n"
        "    sha256: '" + "0" * 64 + "'\n"
        "    effect: read_only\n"
        "    effect_class: read\n"
        "  disturb:\n"
        "    path: BeagleBone-Black/diagnostics/p9_21_gpio_loopback_probe.sh\n"
        "    sha256: '" + "0" * 64 + "'\n"
        "    effect: temporarily muxes pins then restores\n"
        "    effect_class: mutate\n"
        "  unclassified:\n"
        "    path: BeagleBone-Black/diagnostics/mcspi_ch0conf.sh\n"
        "    sha256: '" + "0" * 64 + "'\n"
        "    effect: reads controller registers\n",
        encoding="utf-8",
    )
    return directory


def test_measurement_cannot_run_a_mutating_script(contracts):
    """The lock that the prompt used to be."""
    with pytest.raises(ToolError, match="classified 'mutate'"):
        _call_run_bbb("measurement", "t.yaml", "disturb", contracts)


def test_measurement_cannot_run_an_unclassified_script(contracts):
    with pytest.raises(ToolError, match="counts as 'mutate' until a person reviews it"):
        _call_run_bbb("measurement", "t.yaml", "unclassified", contracts)


def test_the_refusal_names_the_role_and_what_it_may_run(contracts):
    with pytest.raises(ToolError) as caught:
        _call_run_bbb("measurement", "t.yaml", "disturb", contracts)
    message = str(caught.value)
    assert "role 'measurement'" in message
    assert "['read']" in message


def test_the_operator_is_not_stopped_by_the_effect_check(contracts):
    payload = _call_run_bbb("board-operator", "t.yaml", "disturb", contracts)
    assert payload["result"]["measurements"]["failed_stage"] == "script_hash"


def test_measurement_is_not_stopped_from_observing(contracts):
    payload = _call_run_bbb("measurement", "t.yaml", "observe", contracts)
    assert payload["result"]["measurements"]["failed_stage"] == "script_hash"


def test_an_unroled_server_runs_any_classification(contracts):
    payload = _call_run_bbb(None, "t.yaml", "unclassified", contracts)
    assert payload["result"]["measurements"]["failed_stage"] == "script_hash"


# -- the real corpus ---------------------------------------------------------


def test_no_real_script_is_classified_read_without_being_reviewed():
    """Today nothing carries effect_class, so measurement can run none of them.

    That is the intended state until a person signs the classification. The
    proposal is in contracts/proposals/; this test fails the day someone adds
    effect_class without meaning to.
    """
    import yaml

    contract = yaml.safe_load(
        (QKD_ROOT / "contracts" / "gate9-bbb-host-tooling.yaml").read_text(encoding="utf-8")
    )
    classified = {
        name: entry.get("effect_class")
        for name, entry in contract["scripts"].items()
        if isinstance(entry, dict) and entry.get("effect_class")
    }
    for name, entry in contract["scripts"].items():
        if name not in classified:
            assert script_effect(entry) == MUTATE, name
