"""Offline tests for the Codex App Server AgentRunner implementation."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from ai_hardware_autolab.agents.schema import AgentDefinition, load_all, load_simple_team, validate_handoff
from ai_hardware_autolab.orchestrator.codex_runner import (
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
PLATFORM_ROOT = ROOT / "ai_hardware_autolab"


def agent(name: str) -> AgentDefinition:
    return next(
        value for value in (*load_all(), *load_simple_team()) if value.name == name
    )


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


def test_runner_interrupts_the_active_job_turn():
    entered = threading.Event()
    interrupted = threading.Event()

    class BlockingClient(FakeClient):
        def run_turn(self, thread_id, params, *, server_request):
            entered.set()
            interrupted.wait(timeout=2)
            raise CodexTurnFailed("interrupted")

        def interrupt_turn(self, thread_id):
            assert thread_id == "thr_1"
            interrupted.set()
            return True

    client = BlockingClient({})
    runner = CodexAgentRunner(client=client)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            runner(
                agent("gate-architect"),
                {"kind": "Brief", "iteration": 1, "job_id": "job_live"},
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(timeout=1)
    assert runner.cancel("job_live") is True
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors and isinstance(errors[0], CodexTurnFailed)


def test_prompt_omits_only_identical_knowledge_copy_without_mutating_task():
    knowledge = {'facts': [{'claim': 'known signal', 'evidence': 'runs/example'}] * 40}
    task = {'kind': 'Brief', 'scenario': 'measure requested instrument',
            'resources': {'knowledge_context': knowledge, 'scenario_access': {'devices': ['scope']}},
            'knowledge_context': knowledge, 'steering': [{'message': 'keep CH3 wiring'}]}
    before = json.dumps(task, ensure_ascii=False, sort_keys=True)
    runner = CodexAgentRunner(client=FakeClient({}))
    params = runner._turn_params(agent('gate-architect'), task, 'Plan')
    payload = params['input'][0]['text'].split('TASK:\n', 1)[1]
    sent = json.loads(payload)
    assert sent['resources'] == task['resources']
    assert sent['scenario'] == task['scenario']
    assert sent['steering'] == task['steering']
    assert 'knowledge_context' not in sent
    assert len(payload) < len(json.dumps(task, ensure_ascii=False, indent=2)) / 2
    assert json.dumps(task, ensure_ascii=False, sort_keys=True) == before
    task['knowledge_context'] = {'facts': ['different, do not discard']}
    params = runner._turn_params(agent('gate-architect'), task, 'Plan')
    sent = json.loads(params['input'][0]['text'].split('TASK:\n', 1)[1])
    assert sent['knowledge_context'] == task['knowledge_context']


def test_autonomous_lead_hands_preparation_to_builder_without_changing_task_or_sandbox(tmp_path):
    from ai_hardware_autolab.agents.schema import load_simple_team
    from ai_hardware_autolab.orchestrator.development import MODE, LEAD_HANDOFF_GUIDANCE
    team = {a.name: a for a in load_simple_team()}
    task = {'kind': 'Brief', 'scenario': 'Implement a new STM32 board; do not enable a new rail',
            'resources': {'development_mode': MODE, 'available_tools': [],
                          'scenario_access': {'devices': ['stm32'], 'allow_flash': False}}}
    original = json.dumps(task, sort_keys=True)
    runner = CodexAgentRunner(client=FakeClient({}))
    params = runner._turn_params(team['lead-agent'], task, 'Plan')
    prompt = params['input'][0]['text']
    assert LEAD_HANDOFF_GUIDANCE in prompt
    sent = json.loads(prompt.split('TASK:\n', 1)[1])
    assert sent['scenario'] == task['scenario']
    assert sent['resources']['scenario_access'] == task['resources']['scenario_access']
    assert params['sandboxPolicy']['type'] == 'readOnly'
    assert json.dumps(task, sort_keys=True) == original
    (tmp_path / '.qkd-job-workspace.json').write_text(json.dumps({'job_id': 'job_test'}))
    builder_task = {**task, 'job_id': 'job_test'}
    config = CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path / 'ai_hardware_autolab')
    builder = runner._turn_params(team['builder'], builder_task, 'StepResult', config)
    assert LEAD_HANDOFF_GUIDANCE not in builder['input'][0]['text']


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
    (workspace / "ai_hardware_autolab").mkdir(parents=True)
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
        "writableRoots": [str(PLATFORM_ROOT / "gate12_pru_dac1")],
        "networkAccess": False,
    }
    assert sandbox_policy(agent("reporter"), {}, CodexRunnerConfig())["writableRoots"] == [
        str(PLATFORM_ROOT / "docs")
    ]


def test_builder_uses_plan_write_scope_and_measurement_only_step_is_read_only(tmp_path):
    project = tmp_path / "project"
    qkd = project / "ai_hardware_autolab"
    contracts = qkd / "contracts"
    contracts.mkdir(parents=True)
    (contracts / "generated.yaml").write_text(
        "orchestration:\n  write_scope:\n    - generated/feature.py\n",
        encoding="utf-8",
    )
    config = CodexRunnerConfig(project_root=project, qkd_root=qkd)
    builder = next(value for value in load_all(PLATFORM_ROOT / "agents" / "simple") if value.name == "builder")

    writable = sandbox_policy(
        builder,
        {
            "step": {"contract": "generated.yaml", "tool": "codex_workspace"},
            "resources": {"write_scope": ["generated/feature.py"]},
        },
        config,
    )
    assert writable["type"] == "workspaceWrite"
    assert writable["writableRoots"] == [str(project / "generated")]

    measurement = sandbox_policy(
        builder,
        {
            "step": {"contract": "generated.yaml", "tool": "measure_board_scope"},
            "resources": {"write_scope": []},
        },
        config,
    )
    assert measurement == {"type": "readOnly", "networkAccess": False}


def test_workspace_turn_is_recorded_from_app_server_exit_code_not_model_claim(tmp_path):
    project = tmp_path / "project"
    qkd = project / "ai_hardware_autolab"
    contracts = qkd / "contracts"
    runs = qkd / "runs"
    contracts.mkdir(parents=True)
    runs.mkdir()
    (contracts / "board-map.yaml").write_text("{}\n", encoding="utf-8")
    (contracts / "generated.yaml").write_text(
        "test_id: generated\nstatus: reviewed_generated_offline\n"
        "allowed_operations: [workspace_development]\n"
        "forbidden_operations: []\npass_criteria: {}\n",
        encoding="utf-8",
    )
    runner = CodexAgentRunner(
        config=CodexRunnerConfig(
            project_root=project,
            qkd_root=qkd,
            runs_dir=runs,
        )
    )
    envelope = runner._record_workspace_run(
        {
            "kind": "StepResult",
            "agent": "builder",
            "step_id": "test",
            "status": "done",
            "runs": ["invented-by-model"],
        },
        {"step": {"id": "test", "contract": "generated.yaml"}},
        runner.config,
        {"changes": [{"path": "generated/feature.py"}], "outside_write_scope": []},
        [
            {
                "method": "item/completed",
                "params": {"threadId": "thr", "item": {
                    "type": "commandExecution", "command": "openocd -f board.cfg",
                    "status": "declined", "exitCode": None,
                }},
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thr",
                    "item": {
                        "type": "commandExecution",
                        "command": "py -3 -m pytest generated",
                        "exitCode": 0,
                    },
                },
            }
        ],
    )

    assert envelope["runs"] != ["invented-by-model"]
    manifest = json.loads((runs / envelope["runs"][0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verdict"] == "PASS"
    assert manifest["steps"][0]["measurements"]["last_command_exit_code"] == 0
    assert manifest["steps"][0]["measurements"]["workspace_change_count"] == 1
    assert manifest["steps"][0]["measurements"]["declined_command_count"] == 1
    assert manifest["steps"][0]["measurements"]["hardware_command_findings"] == []
    evidence = json.loads((runs / envelope["runs"][0] / "workspace_evidence.json").read_text(encoding="utf-8"))
    assert evidence["declined_commands"][0]["status"] == "declined"


def test_builder_recovers_a_malformed_final_json_without_replaying_file_changes(tmp_path):
    project = tmp_path / "project"
    workspace = project / "ai_hardware_autolab" / "workspaces" / "job_123456789abc"
    qkd = workspace / "ai_hardware_autolab"
    contracts = qkd / "contracts"
    runs = project / "ai_hardware_autolab" / "runs"
    contracts.mkdir(parents=True)
    runs.mkdir(parents=True)
    (contracts / "board-map.yaml").write_text("{}\n", encoding="utf-8")
    (contracts / "generated.yaml").write_text(
        "test_id: generated\nstatus: reviewed_generated_offline\n"
        "allowed_operations: [workspace_development]\n"
        "forbidden_operations: []\npass_criteria: {}\n",
        encoding="utf-8",
    )

    class Resolver:
        def __init__(self):
            self.audits = 0
            self.publishes = 0

        def __call__(self, task):
            return workspace

        def audit(self, task, agent_name):
            self.audits += 1
            return {"changes": [], "outputs": [], "outside_write_scope": []}

        def publish(self, task, audit):
            self.publishes += 1
            return {"job_id": task["job_id"], "published": []}

    class MalformedAfterWork(CodexAgentRunner):
        def _invoke(self, agent_definition, task, runtime_config):
            raise CodexTurnFailed(
                "final JSON was truncated",
                final_text='{\"kind\": \"StepResult\"',
                events=[{
                    "method": "item/completed",
                    "params": {"item": {
                        "type": "commandExecution",
                        "command": "py -3 -m pytest",
                        "exitCode": 0,
                    }},
                }],
            )

    resolver = Resolver()
    builder = next(
        value
        for value in load_all(PLATFORM_ROOT / "agents" / "simple")
        if value.name == "builder"
    )
    runner = MalformedAfterWork(
        config=CodexRunnerConfig(
            project_root=project,
            qkd_root=project / "ai_hardware_autolab",
            runs_dir=runs,
        ),
        workspace_resolver=resolver,
        client=FakeClient({}),
    )

    envelope = runner(builder, {
        "job_id": "job_123456789abc",
        "iteration": 1,
        "step": {
            "id": "implement",
            "tool": "codex_workspace",
            "contract": "generated.yaml",
        },
        "resources": {"write_scope": []},
    })

    assert envelope["status"] == "done"
    assert envelope["verdict"] == "PASS"
    assert envelope["handoff_recovery"]["reason"] == "final JSON was truncated"
    assert resolver.audits == 1
    assert resolver.publishes == 1


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


def test_builder_runtime_uses_untrusted_command_approval_policy():
    runner = CodexAgentRunner(client=FakeClient({}))
    builder = agent("builder")
    assert runner._thread_params(builder)["approvalPolicy"] == "untrusted"
    task = {
        "step": {
            "id": "build",
            "tool": "codex_workspace",
            "touches_hardware": False,
        },
        "resources": {"write_scope": ["ai_hardware_autolab/generated"]},
    }
    assert runner._turn_params(builder, task, "StepResult")["approvalPolicy"] == "untrusted"


def test_builder_command_approval_accepts_offline_work_and_refuses_hardware():
    runner = CodexAgentRunner(client=FakeClient({}))
    builder = agent("builder")
    safe = {"item": {"command": "py -3 -m pytest ai_hardware_autolab/tests/test_evidence.py"}}
    assert runner._server_request(
        builder, {}, "item/commandExecution/requestApproval", safe
    ) == {"decision": "accept"}

    assert runner._server_request(
            builder,
            {},
            "item/commandExecution/requestApproval",
            {"command": "openocd -f interface.cfg"},
        ) == {"decision": "decline"}
    # A refusal does not tear down the turn: subsequent development is allowed.
    assert runner._server_request(
        builder, {}, "item/commandExecution/requestApproval",
        {"command": r"Get-Content ai_hardware_autolab\hwtools\instruments\rtm3004.py"},
    ) == {"decision": "accept"}


@pytest.mark.parametrize("command,decision", [
    ("python -m pytest ai_hardware_autolab/tests/test_workspace_gpio_spi_scope_capture.py -q", "accept"),
    ("git diff --check; git diff --stat; Get-FileHash -Algorithm SHA256 "
     "ai_hardware_autolab/workspace_recipes/gpio_spi_scope_capture.py, "
     "ai_hardware_autolab/workspace_recipes/gpio_spi_bbb_preflight.py, "
     "BeagleBone-Black/host/rtase_spi.py | Format-Table -AutoSize", "accept"),
    ("git diff --check; ssh board reboot", "decline"),
    ("python ai_hardware_autolab/workspace_recipes/gpio_spi_scope_capture.py", "decline"),
])
@pytest.mark.parametrize("as_argv", [False, True])
def test_live_shell_command_approval_regression(command, decision, as_argv):
    runner = CodexAgentRunner(client=FakeClient({}))
    shell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    value = [shell, "-Command", command] if as_argv else f'"{shell}" -Command \'{command}\''
    assert runner._server_request(
        agent("builder"), {}, "item/commandExecution/requestApproval",
        {"item": {"command": value}},
    ) == {"decision": decision}


def test_non_builder_cannot_receive_command_or_file_change_authority():
    runner = CodexAgentRunner(client=FakeClient({}))
    lead = agent("lead-agent")
    with pytest.raises(CodexApprovalRequired, match="outside the Builder role"):
        runner._server_request(
            lead,
            {},
            "item/commandExecution/requestApproval",
            {"command": "dir"},
        )
    with pytest.raises(CodexApprovalRequired, match="outside the Builder role"):
        runner._server_request(lead, {}, "item/fileChange/requestApproval", {})


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
    assert offline_args[offline_args.index("--contracts-dir") + 1] == str(PLATFORM_ROOT / "contracts")
    assert offline_args[offline_args.index("--runs-dir") + 1] == str(PLATFORM_ROOT / "runs")
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
