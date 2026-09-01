"""Offline tests for the Codex App Server AgentRunner implementation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from QKD_PROJ.agents.schema import AgentDefinition, load_all, validate_handoff
from QKD_PROJ.orchestrator.codex_runner import (
    CodexAgentRunner,
    CodexApprovalRequired,
    CodexRunnerConfig,
    CodexRunnerError,
    CodexTurnFailed,
    StdioAppServer,
    build_app_server_command,
    handoff_output_schema,
    sandbox_policy,
)

ROOT = Path(__file__).resolve().parents[2]
QKD_ROOT = ROOT / "QKD_PROJ"


def agent(name: str) -> AgentDefinition:
    return next(value for value in load_all() if value.name == name)


class FakeClient:
    def __init__(self, answer: object):
        self.answer = answer
        self.thread_params: list[dict] = []
        self.turn_params: list[dict] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    def start_thread(self, params):
        self.thread_params.append(params)
        return "thr_1"

    def run_turn(self, thread_id, params, *, server_request):
        assert thread_id == "thr_1"
        self.turn_params.append(params)
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)

    def unsubscribe(self, thread_id):
        self.unsubscribed.append(thread_id)

    def close(self):
        self.closed = True


def test_runner_starts_a_fresh_ephemeral_thread_and_returns_one_envelope():
    handoff = {
            "kind": "Plan",
            "agent": "gate-architect",
            "gate": "g.yaml",
            "iteration": 1,
            "steps": [],
            "blocked_on": "contract_gap",
    }
    client = FakeClient(
        {"kind": "Plan", "agent": "gate-architect", "handoff": json.dumps(handoff)}
    )
    runner = CodexAgentRunner(client=client)

    envelope = runner(agent("gate-architect"), {"kind": "Brief", "iteration": 1})

    assert envelope["kind"] == "Plan"
    assert client.thread_params[0]["ephemeral"] is True
    assert "# Agent: gate-architect" in client.thread_params[0]["developerInstructions"]
    assert client.turn_params[0]["outputSchema"] == handoff_output_schema(
        "Plan", "gate-architect"
    )
    assert client.turn_params[0]["sandboxPolicy"]["type"] == "readOnly"
    assert client.unsubscribed == ["thr_1"]


def test_runner_resolves_one_code_workspace_per_job_but_keeps_shared_evidence(tmp_path):
    workspace = tmp_path / "workspaces" / "job_123456789abc"
    (workspace / "QKD_PROJ").mkdir(parents=True)
    shared_runs = tmp_path / "shared-runs"
    shared_runs.mkdir()
    handoff = {
        "kind": "Plan",
        "agent": "gate-architect",
        "gate": "g.yaml",
        "iteration": 1,
        "steps": [],
        "blocked_on": "contract_gap",
    }
    client = FakeClient(
        {"kind": "Plan", "agent": "gate-architect", "handoff": json.dumps(handoff)}
    )
    runner = CodexAgentRunner(
        config=CodexRunnerConfig(runs_dir=shared_runs),
        workspace_resolver=lambda task: workspace
        if task["job_id"] == "job_123456789abc"
        else pytest.fail("wrong job"),
        client=client,
    )

    runner(
        agent("gate-architect"),
        {"kind": "Brief", "iteration": 1, "job_id": "job_123456789abc"},
    )

    assert client.thread_params[0]["cwd"] == str(workspace.resolve())
    assert client.turn_params[0]["cwd"] == str(workspace.resolve())
    assert runner.config.runs_dir == shared_runs.resolve()


def test_discovery_handoff_is_a_structured_object_not_an_escaped_json_string(tmp_path):
    digest = "a" * 64
    handoff = {
        "kind": "SourcePacket",
        "agent": "researcher",
        "scenario_sha256": digest,
        "queries": [],
        "documents": [],
        "claims": [],
        "open_questions": ["unanswered-question"],
    }
    client = FakeClient(
        {"kind": "SourcePacket", "agent": "researcher", "handoff": handoff}
    )
    runner = CodexAgentRunner(
        config=CodexRunnerConfig(reference_root=tmp_path / "references"),
        client=client,
    )

    task = {
        "scenario_sha256": digest,
        "research_plan": {
            "search_budget": 1,
            "questions": [{"id": "unanswered-question"}],
        },
    }
    assert runner(agent("researcher"), task) == handoff
    schema = client.turn_params[0]["outputSchema"]["properties"]["handoff"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["open_questions"]["items"]["type"] == "string"
    assert schema["properties"]["open_questions"]["items"]["enum"] == [
        "unanswered-question"
    ]
    assert schema["properties"]["scenario_sha256"]["enum"] == [digest]
    assert "value" in schema["properties"]["claims"]["items"]["required"]
    assert "not a string" in client.turn_params[0]["input"][0]["text"]


def test_change_analysis_schema_requires_lowercase_machine_identifiers():
    digest = "a" * 64
    schema = handoff_output_schema(
        "ChangeAnalysis",
        "change-analyst",
        {
            "scenario_sha256": digest,
            "mission_plan": {
                "baseline_contract": "gate12-pru-dac1-plan.yaml",
                "execution_agents": ["design-builder"],
            },
            "source_packet": {"claims": [{"id": "pin-map"}]},
        },
    )["properties"]["handoff"]
    finding = schema["properties"]["findings"]["items"]

    assert finding["properties"]["id"]["pattern"] == "^[a-z0-9][a-z0-9_.-]*$"
    assert finding["properties"]["area"]["pattern"] == "^[a-z0-9][a-z0-9_.-]*$"


@pytest.mark.parametrize(
    "answer, message",
    [
        ("not json", "not one JSON object"),
        (
            {"kind": "Report", "agent": "gate-architect", "handoff": "{}"},
            "must emit Plan",
        ),
        (
            {"kind": "Plan", "agent": "someone-else", "handoff": "{}"},
            "returned agent",
        ),
    ],
)
def test_runner_rejects_ambiguous_or_misattributed_final_answers(answer, message):
    with pytest.raises(CodexTurnFailed, match=message):
        CodexAgentRunner(client=FakeClient(answer))(
            agent("gate-architect"), {"kind": "Brief", "iteration": 1}
        )


def test_offline_profile_refuses_to_start_a_hardware_agent():
    client = FakeClient({})
    with pytest.raises(CodexRunnerError, match="offline MCP profile"):
        CodexAgentRunner(client=client)(agent("board-operator"), {"kind": "Plan"})
    assert client.thread_params == []


def test_role_policy_derives_only_the_contracts_build_source_directory():
    policy = sandbox_policy(
        agent("design-builder"),
        {
            "step": {
                "contract": "gate12-pru-dac1-plan.yaml",
            }
        },
        CodexRunnerConfig(),
    )
    assert policy == {
        "type": "workspaceWrite",
        "writableRoots": [str(QKD_ROOT / "gate12_pru_dac1")],
        "networkAccess": False,
    }
    assert sandbox_policy(agent("reporter"), {}, CodexRunnerConfig())["writableRoots"] == [
        str(QKD_ROOT / "docs")
    ]


def test_hardware_mcp_call_needs_an_injected_durable_approval():
    board = agent("board-operator")
    task = {"step": {"id": "program", "contract": "g.yaml"}}
    runner = CodexAgentRunner(config=CodexRunnerConfig(profile="hardware"), client=FakeClient({}))
    params = {
        "serverName": "rtase-hwtools",
        "message": 'Allow the rtase-hwtools MCP server to run tool "program_cpld_sram"?',
    }
    with pytest.raises(CodexApprovalRequired, match="no durable approval"):
        runner._server_request(board, task, "mcpServer/elicitation/request", params)

    approved = CodexAgentRunner(
        config=CodexRunnerConfig(profile="hardware"),
        client=FakeClient({}),
        hardware_approval=lambda actual_agent, actual_task, tool, _params: (
            actual_agent.name == "board-operator"
            and actual_task is task
            and tool == "program_cpld_sram"
        ),
    )
    assert approved._server_request(
        board, task, "mcpServer/elicitation/request", params
    ) == {"action": "accept", "content": {}}


def test_agent_cannot_call_a_tool_outside_its_definition_even_in_hardware_profile():
    params = {
        "serverName": "rtase-hwtools",
        "message": 'Allow the rtase-hwtools MCP server to run tool "program_cpld_sram"?',
    }
    runner = CodexAgentRunner(
        config=CodexRunnerConfig(profile="hardware"),
        client=FakeClient({}),
        hardware_approval=lambda *_: True,
    )
    with pytest.raises(CodexApprovalRequired, match="outside its definition"):
        runner._server_request(
            agent("measurement"), {}, "mcpServer/elicitation/request", params
        )


def test_default_command_pins_the_mcp_profile_at_process_launch():
    offline = build_app_server_command(
        CodexRunnerConfig(profile="offline"), role="gate-architect"
    )
    hardware = build_app_server_command(
        CodexRunnerConfig(profile="hardware"), role="board-operator"
    )
    assert offline[:3] == (offline[0], "app-server", "--stdio")
    assert "--allow-hardware" not in " ".join(offline)
    assert "--allow-hardware" in " ".join(hardware)
    offline_args = json.loads(next(part for part in offline if ".args=" in part).split("=", 1)[1])
    hardware_args = json.loads(next(part for part in hardware if ".args=" in part).split("=", 1)[1])
    assert offline_args[3:5] == ["--role", "gate-architect"]
    assert hardware_args[3:5] == ["--role", "board-operator"]
    assert offline_args[offline_args.index("--project-root") + 1] == str(ROOT)
    assert offline_args[offline_args.index("--contracts-dir") + 1] == str(QKD_ROOT / "contracts")
    assert offline_args[offline_args.index("--runs-dir") + 1] == str(QKD_ROOT / "runs")
    assert hardware_args[-1] == "--allow-hardware"
    assert not any("auth" in part.lower() for part in offline)
    assert 'web_search="disabled"' in offline

    research = build_app_server_command(
        CodexRunnerConfig(profile="offline"), role="researcher", enable_web=True
    )
    assert 'web_search="live"' in research


def test_researcher_gets_native_collection_tools_but_other_roles_do_not(tmp_path):
    runner = CodexAgentRunner(
        config=CodexRunnerConfig(reference_root=tmp_path / "references"),
        client=FakeClient({}),
    )
    researcher = agent("researcher")
    task = {"research_plan": {"search_budget": 3}}
    with runner._research_context(researcher, task):
        params = runner._thread_params(researcher)
        assert {tool["name"] for tool in params["dynamicTools"]} == {
            "collect_source",
            "search_reference",
            "read_reference_page",
        }
        refused = runner._server_request(
            researcher,
            task,
            "item/tool/call",
            {"tool": "collect_source", "arguments": {"url": "https://example.test/x.pdf"}},
        )
        assert refused["success"] is False
    assert "dynamicTools" not in runner._thread_params(agent("gate-architect"))


def test_stdio_client_performs_handshake_answers_requests_and_extracts_final_item():
    events: list[dict] = []
    fixture = Path(__file__).with_name("fixtures") / "fake_codex_app_server.py"
    with StdioAppServer(
        command=(sys.executable, "-u", str(fixture)),
        cwd=ROOT,
        timeout_seconds=5,
        client_info={"name": "test", "title": "Test", "version": "1"},
        event_sink=events.append,
    ) as client:
        thread_id = client.start_thread({"ephemeral": True})
        text = client.run_turn(
            thread_id,
            {"input": [{"type": "text", "text": "test"}]},
            server_request=lambda method, _params: (
                {"action": "accept", "content": {}}
                if method == "mcpServer/elicitation/request"
                else pytest.fail(method)
            ),
        )
        client.unsubscribe(thread_id)

    assert json.loads(text)["kind"] == "Plan"
    assert [event["method"] for event in events] == [
        "thread/started",
        "item/completed",
        "turn/completed",
    ]


def test_stdio_client_opts_into_experimental_api_for_dynamic_tools():
    fixture = Path(__file__).with_name("fixtures") / "fake_codex_app_server.py"
    with StdioAppServer(
        command=(sys.executable, "-u", str(fixture)),
        cwd=ROOT,
        timeout_seconds=5,
        client_info={"name": "test", "title": "Test", "version": "1"},
        experimental_api=True,
    ) as client:
        thread_id = client.start_thread(
            {
                "ephemeral": True,
                "dynamicTools": [
                    {
                        "name": "collect_source",
                        "description": "Collect one source",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        client.unsubscribe(thread_id)


@pytest.mark.skipif(
    os.environ.get("QKD_CODEX_LIVE") != "1",
    reason="uses the signed-in Codex CLI and subscription quota",
)
def test_live_codex_returns_a_valid_gate_architect_envelope():
    task = {
        "kind": "Brief",
        "job_id": "live-smoke-not-recorded",
        "gate": "gate9-bbb-host-tooling.yaml",
        "objective": "Plan one offline evidence check; do not execute or modify anything.",
        "iteration": 1,
        "strikes": 0,
        "previous_verdict": None,
    }
    with CodexAgentRunner(
        config=CodexRunnerConfig(profile="offline", timeout_seconds=240)
    ) as runner:
        envelope = runner(agent("gate-architect"), task)
    assert validate_handoff(envelope)["agent"] == "gate-architect"
