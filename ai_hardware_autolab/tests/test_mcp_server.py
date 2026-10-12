"""Offline tests for the MCP server.

No hardware, no network, no MCP SDK. The protocol is exercised the way a
client speaks it -- JSON in, JSON out -- so a passing suite means a real
client can connect, not just that the functions are callable.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.mcp_server.protocol import DEFAULT_PROTOCOL_VERSION, METHOD_NOT_FOUND  # noqa: E402
from ai_hardware_autolab.mcp_server.registry import ServerPolicy  # noqa: E402
from ai_hardware_autolab.mcp_server.server import build_server  # noqa: E402

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def offline():
    return build_server(ServerPolicy(allow_hardware=False))


@pytest.fixture
def unlocked():
    return build_server(ServerPolicy(allow_hardware=True))


def request(server, method: str, params: dict | None = None, message_id: int = 1):
    return server.handle(
        {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
    )


def call(server, name: str, arguments: dict | None = None) -> tuple[dict, bool]:
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    result = response["result"]
    payload = json.loads(result["content"][0]["text"])
    return payload, result["isError"]


# -- handshake ---------------------------------------------------------------


def test_initialize_echoes_a_supported_protocol_version(offline) -> None:
    result = request(offline, "initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "rtase-hwtools"
    assert set(result["capabilities"]) == {"tools", "prompts", "resources"}
    assert "contract" in result["instructions"]


def test_unknown_protocol_version_falls_back_to_ours(offline) -> None:
    result = request(offline, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION


def test_notifications_get_no_response(offline) -> None:
    assert offline.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_a_protocol_error(offline) -> None:
    response = request(offline, "resources/subscribe")
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_ping(offline) -> None:
    assert request(offline, "ping")["result"] == {}


# -- the tool list is the safety boundary ------------------------------------


HARDWARE_TOOLS = {
    "scan_jtag", "program_cpld_sram", "measure_gate1_gpio", "run_bbb",
    "deploy_pru", "run_board_stage",
    "measure_board_scope", "measure_scope",
}


def test_board_touching_tools_are_absent_without_allow_hardware(offline) -> None:
    names = {tool["name"] for tool in request(offline, "tools/list")["result"]["tools"]}
    assert HARDWARE_TOOLS.isdisjoint(names)
    assert {"list_contracts", "read_contract", "build_cpld"} <= names


def test_allow_hardware_adds_them_and_nothing_else(offline, unlocked) -> None:
    before = {t["name"] for t in request(offline, "tools/list")["result"]["tools"]}
    after = {t["name"] for t in request(unlocked, "tools/list")["result"]["tools"]}
    assert after - before == HARDWARE_TOOLS


def test_a_locked_server_cannot_be_talked_into_a_hardware_tool(offline) -> None:
    response = request(offline, "tools/call", {"name": "program_cpld_sram", "arguments": {}})
    assert "error" in response and "Unknown tool" in response["error"]["message"]


def test_every_tool_declares_an_object_schema(unlocked) -> None:
    for tool in request(unlocked, "tools/list")["result"]["tools"]:
        schema = tool["inputSchema"]
        assert schema["type"] == "object", tool["name"]
        assert tool["description"].strip(), tool["name"]


# -- offline tools -----------------------------------------------------------


def test_list_contracts_reports_permissions(offline) -> None:
    payload, is_error = call(offline, "list_contracts")
    assert not is_error
    by_file = {entry["file"]: entry for entry in payload["contracts"]}
    assert "board-map.yaml" in by_file
    gate9 = by_file["gate9-bbb-host-tooling.yaml"]
    assert gate9["test_id"] == "gate9-bbb-host-tooling"
    assert "sram_fast_program" in gate9["forbidden_operations"]
    assert payload["policy"]["allow_hardware"] is False


def test_read_contract_returns_the_yaml(offline) -> None:
    payload, is_error = call(offline, "read_contract", {"name": "gate1-test-spec.yaml"})
    assert not is_error
    assert "test_id: gate1-jtag-sram-gpio" in payload["text"]


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "..\\board-map.yaml", "contracts/board-map.yaml", "board-map.txt"],
)
def test_contract_arguments_cannot_escape_the_contracts_directory(offline, name) -> None:
    payload, is_error = call(offline, "read_contract", {"name": name})
    assert is_error
    assert "Paths are not accepted" in payload["error"] or "No such contract" in payload["error"]


def test_missing_argument_is_reported_not_crashed(offline) -> None:
    payload, is_error = call(offline, "read_contract", {})
    assert is_error and "Missing required argument" in payload["error"]


def test_list_runs_reads_the_evidence_chain(offline, tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "20260820T000000Z_demo").mkdir(parents=True)
    (runs / "20260820T000000Z_demo" / "run_status.json").write_text(
        json.dumps({"run": "demo", "verdict": "PASS", "steps": [{"tool": "cpld", "action": "build"}]}),
        encoding="utf-8",
    )
    server = build_server(ServerPolicy(runs_dir=runs))
    payload, is_error = call(server, "list_runs")
    assert not is_error
    assert payload["runs"][0] == {
        "run": "20260820T000000Z_demo",
        "verdict": "PASS",
        "steps": ["cpld.build"],
    }


def test_build_cpld_refuses_a_contract_that_does_not_describe_a_design(offline) -> None:
    """Fails on the contract, before Diamond is ever invoked."""
    payload, is_error = call(offline, "build_cpld", {"contract": "gate9-bbb-host-tooling.yaml"})
    assert is_error
    assert "test_design" in payload["error"]


# -- prompts and resources ---------------------------------------------------


def test_skills_are_exposed_as_prompts(offline) -> None:
    names = {p["name"] for p in request(offline, "prompts/list")["result"]["prompts"]}
    assert {"scope-measure", "build-cpld"} <= names


def test_prompt_body_is_the_skill_without_its_frontmatter(offline) -> None:
    result = request(offline, "prompts/get", {"name": "scope-measure"})["result"]
    text = result["messages"][0]["content"]["text"]
    assert text.startswith("# RTM3004 measurement")
    assert "name: scope-measure" not in text
    assert "DCLimit" in text  # the trap the skill exists to carry


def test_contracts_are_readable_as_resources(offline) -> None:
    uris = {r["uri"] for r in request(offline, "resources/list")["result"]["resources"]}
    assert "contract://board-map.yaml" in uris
    assert "doc://STATUS.md" in uris
    contents = request(offline, "resources/read", {"uri": "contract://board-map.yaml"})
    assert "part: LCMXO2-4000HC-4TG144C" in contents["result"]["contents"][0]["text"]


def test_unknown_resource_is_refused(offline) -> None:
    response = request(offline, "resources/read", {"uri": "contract://nope.yaml"})
    assert "error" in response


# -- transport ---------------------------------------------------------------


def test_stdio_framing_round_trip(offline) -> None:
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "not json at all",
    ]
    out = io.StringIO()
    offline.serve(io.StringIO("\n".join(lines) + "\n"), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r.get("id") for r in responses] == [1, 2, None]
    assert responses[1]["result"]["tools"]
    assert responses[2]["error"]["code"] == -32700


def test_a_handler_crash_becomes_a_tool_error_not_a_dead_connection(offline) -> None:
    offline.tools["list_runs"] = offline.tools["list_runs"].__class__(
        name="list_runs",
        description="boom",
        input_schema={"type": "object"},
        handler=lambda args: (_ for _ in ()).throw(RuntimeError("exploded")),
    )
    payload, is_error = call(offline, "list_runs")
    assert is_error
    assert "exploded" in payload["error"]
    # the server is still usable afterwards
    assert request(offline, "ping")["result"] == {}


def test_the_command_in_mcp_json_actually_serves(tmp_path: Path) -> None:
    """Launch the server the way a client launches it.

    This is the only test that covers stdout hygiene and the direct-script
    sys.path fallback. Both fail silently in unit tests and loudly in a client:
    one stray print on stdout corrupts the stream and the server looks broken.
    """
    import subprocess

    config = json.loads((PLATFORM_ROOT.parent / ".mcp.json").read_text(encoding="utf-8"))
    entry = config["mcpServers"]["rtase-hwtools"]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    completed = subprocess.run(
        [entry["command"], *entry["args"]],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert [line.get("id") for line in lines] == [1, 2]  # the notification is silent
    assert lines[0]["result"]["serverInfo"]["name"] == "rtase-hwtools"
    assert {t["name"] for t in lines[1]["result"]["tools"]}.isdisjoint(HARDWARE_TOOLS)
