"""Real failure shape, synthetic endpoints; never connects to equipment."""
import copy
import json

import pytest
import yaml

from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate, SafetyViolation
from ai_hardware_autolab.orchestrator.boundary_normalization import normalize_boundary
from ai_hardware_autolab.orchestrator.contract_compiler import ContractCompiler, ContractCompileError
from ai_hardware_autolab.tests.test_autonomous_development import plan, resources


def proposal():
    return {
        "configuration": {
            "device_map": "contracts/board-map.yaml",
            "bbb": {"target": {"host": "192.0.2.2", "user": "test"}},
            "scope": {
                "target": {"host": "192.0.2.3", "port": 5025, "model": "RTM3004"},
                "channels": [{"channel": n, "source": pin, "signal": signal,
                              "termination": "1_Mohm", "probe_attenuation": 10,
                              "max_abs_input_v": 3.3}
                             for n, pin, signal in [(1,"P9_17","CS"), (2,"P9_22","CLK"),
                                                    (3,"P9_18","MOSI"), (4,"P9_21","MISO")]],
                "acquisition": {"timebase_s_per_div": 0.002},
            },
        },
        "allowed_operations": ["oscilloscope_capture", "bbb_ssh_command"],
        "forbidden_operations": ["nonvolatile_flash"],
        "hardware_safety": {"stop_conditions": ["Stop on unexpected identity or unsafe input"]},
    }


def compile_proposal(tmp_path, value):
    contracts = tmp_path / "contracts"
    contracts.mkdir(exist_ok=True)
    (contracts / "board-map.yaml").write_text(yaml.safe_dump({"scope": proposal()["configuration"]["scope"]["target"]}))
    p = plan()
    p["hardware_boundaries"] = {"spi_gpio_scope": value}
    p["steps"][0].update(tool="measure_scope", contract="$spi_gpio_scope", touches_hardware=True,
                         agent="builder", execution="agent")
    result = ContractCompiler(contracts, project_root=tmp_path).compile(
        p, job_id="job_test", iteration=1, scenario_sha256="",
        resources={**resources(), "execution_mode": "hardware", "available_tools": ["measure_scope"]})
    return result, Contracts.load(result.filename, contracts, project_root=tmp_path)


def test_reported_nested_scope_shape_compiles_without_new_permission(tmp_path):
    p = proposal()
    before = copy.deepcopy(p)
    result, contracts = compile_proposal(tmp_path, p)
    doc = contracts.test_spec
    assert p == before
    assert doc["scope"] == p["configuration"]["scope"]
    assert doc["bbb"] == p["configuration"]["bbb"]
    assert doc["hardware_safety"]["scope_channels"] == doc["scope"]["channels"]
    assert doc["allowed_operations"] == p["allowed_operations"]
    assert doc["forbidden_operations"] == p["forbidden_operations"]
    assert len(SafetyGate(contracts).instrument_scope()["scope_channels"]) == 4
    assert doc["status"] == "proposed_hardware_boundary"
    with pytest.raises(SafetyViolation):
        SafetyGate(contracts).require("oscilloscope_capture")
    with pytest.raises(SafetyViolation):
        SafetyGate(contracts, allow_hardware=True).require("nonvolatile_flash")
    assert result.plan["steps"][0]["contract_sha256"] == result.sha256
    second, _ = compile_proposal(tmp_path, p)
    assert second.sha256 == result.sha256


@pytest.mark.parametrize("conflict", ["host", "port", "channels", "limit", "map"])
def test_conflicting_declarations_are_not_overwritten(conflict):
    p = normalize_boundary(proposal())
    if conflict in {"host", "port"}:
        p["hardware_safety"]["devices"][0][conflict] = "different"
    elif conflict == "channels":
        p["hardware_safety"]["scope_channels"].pop()
    elif conflict == "limit":
        p["hardware_safety"]["scope_channels"][0]["max_abs_input_v"] = 50
    else:
        p["device_map"] = "somewhere-else.yaml"
    with pytest.raises(SafetyViolation, match="conflict"):
        normalize_boundary(p)


@pytest.mark.parametrize("missing", ["target", "limit"])
def test_missing_safety_information_is_not_invented(tmp_path, missing):
    p = proposal()
    if missing == "target":
        del p["configuration"]["scope"]["target"]
    else:
        del p["configuration"]["scope"]["channels"][0]["max_abs_input_v"]
    with pytest.raises(ContractCompileError, match="repair hardware configuration"):
        compile_proposal(tmp_path, p)


def test_normalization_is_idempotent_and_retains_other_devices():
    p = proposal()
    p["hardware_safety"]["devices"] = [{"kind": "bbb", "host": "192.0.2.2"}]
    n = normalize_boundary(p)
    assert normalize_boundary(n) == n
    assert n["hardware_safety"]["devices"][0] == p["hardware_safety"]["devices"][0]


def test_untyped_conflicting_endpoint_is_not_silently_added_as_another_device():
    p = proposal()
    p["hardware_safety"]["devices"] = [{"host": "192.0.2.99", "model": "other", "port": 5025}]
    with pytest.raises(SafetyViolation, match="ambiguous"):
        normalize_boundary(p)


def test_endpoint_mismatch_still_fails_after_normalization(tmp_path):
    p = proposal()
    p["configuration"]["scope"]["target"]["host"] = "192.0.2.99"
    with pytest.raises(ContractCompileError, match="endpoint"):
        compile_proposal(tmp_path, p)


@pytest.mark.parametrize("wire_format", ["direct", "object_wrapper", "legacy_string"])
def test_runner_accepts_single_plan_and_legacy_formats(wire_format):
    from ai_hardware_autolab.agents.schema import load_simple_team
    from ai_hardware_autolab.orchestrator.codex_runner import CodexAgentRunner
    from ai_hardware_autolab.tests.test_codex_runner import FakeClient
    p = plan()
    p["hardware_boundaries"] = {"spi_gpio_scope": proposal()}
    p["steps"][0]["instructions"] = 'Use "quoted values" and C:\\work\\file.py; hand off normally.'
    answer = p if wire_format == "direct" else {
        "kind": "Plan", "agent": "lead-agent", "handoff": p if wire_format == "object_wrapper" else json.dumps(p)}
    client = FakeClient(answer)
    runner = CodexAgentRunner(client=client)
    task = {"kind": "Brief", "iteration": 1, "resources": resources()}
    lead = next(a for a in load_simple_team() if a.name == "lead-agent")
    result = runner(lead, task)
    assert result["steps"] == p["steps"]
    assert result["hardware_boundaries"] == p["hardware_boundaries"]
    params = client.turn_params[0]
    assert "outputSchema" not in params
    assert "ONE JSON object" in params["input"][0]["text"]
    assert "JSON-encoded string" not in params["input"][0]["text"]
    assert params["sandboxPolicy"]["type"] == "readOnly"
