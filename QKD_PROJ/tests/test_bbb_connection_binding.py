"""Configured endpoint -> new job -> approval -> recorded SSH, entirely offline."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from QKD_PROJ.hwtools import Contracts, SafetyGate, run_bbb
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.hwtools.ssh import SshError, SshTarget, bbb_endpoint
from QKD_PROJ.mcp_server import registry
from QKD_PROJ.mcp_server.protocol import ToolError
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.tests.test_bbb import FakeSsh, transport_for
from QKD_PROJ.tests.test_scenario_access import start
from QKD_PROJ.tests.test_scope_session_approval import setup_scope, require


ENDPOINT = {"host": "bbb-fixture", "user": "fixture-user", "port": 2222}
SECRET = "test-only-not-a-real-password"


@pytest.fixture(autouse=True)
def no_live_connection(monkeypatch):
    for key in ["BB_HOST", "BB_USER", "BB_PORT", "BB_PW"]:
        monkeypatch.delenv(key, raising=False)

    def forbid(*args, **kwargs):
        raise AssertionError("a test tried to create a live SSH connection")

    monkeypatch.setattr("QKD_PROJ.hwtools.ssh.ParamikoClient", forbid)


def configure(s, endpoint=ENDPOINT):
    path = s.controller.contracts_dir / "board-map.yaml"
    data = yaml.safe_load(path.read_text())
    data["bbb"] = {"access": copy.deepcopy(endpoint)}
    path.write_text(yaml.safe_dump(data))
    script = s.root / "check.sh"
    script.write_text("printf 'device-work-finished\\n'\n")
    contract = s.controller.contracts_dir / "bbb.yaml"
    contract.write_text(yaml.safe_dump({
        "allowed_operations": ["bbb_ssh_command"],
        "bbb": {"target_model_substring": "AM335x BeagleBone Black"},
        "scripts": {"check": {"path": "check.sh", "sha256": sha256_file(script), "effect_class": "read"}},
    }))
    step = {"id": "read-bbb", "agent": "builder", "tool": "run_bbb", "contract": contract.name,
            "contract_sha256": sha256_file(contract), "touches_hardware": True}
    return data, step


def test_registered_rtase_endpoint_is_backed_by_successful_run():
    root = Path(__file__).resolve().parents[1]
    data = yaml.safe_load((root / "contracts/board-map.yaml").read_text(encoding="utf-8"))
    access = data["bbb"]["access"]
    recorded = json.loads((root / "runs" / access["evidence_run"] / "manifest.json").read_text(encoding="utf-8"))
    successful = [s for s in recorded["steps"] if s["tool"] == "bbb" and s["ok"]]
    assert any(s["measurements"]["target"] == bbb_endpoint(data, {}) for s in successful)
    assert bbb_endpoint(data, {}) == {"host": "192.168.50.2", "user": "xilinx", "port": 22}


def test_selected_board_and_environment_use_one_secret_free_resolver():
    data = {"bbb": {"access": ENDPOINT}}
    env = {"BB_PW": SECRET}
    assert bbb_endpoint(data, env) == ENDPOINT
    target = SshTarget.from_env(env, board_map=data)
    assert target.describe() == ENDPOINT
    assert SECRET not in repr(target)
    assert SECRET not in json.dumps(bbb_endpoint(data, env))
    env.update(BB_HOST=" second-board ", BB_USER=" second-user ", BB_PORT="22")
    assert bbb_endpoint(data, env) == {"host": "second-board", "user": "second-user", "port": 22}


@pytest.mark.parametrize("port", [True, 0, 65536, "UNVERIFIED", "22.5", 22.5])
def test_invalid_ports_do_not_become_ssh_targets(port):
    data = {"bbb": {"access": {**ENDPOINT, "port": port}}}
    assert bbb_endpoint(data, {})["port"] is None
    with pytest.raises(SshError, match="BB_PORT"):
        SshTarget.from_env({"BB_PW": SECRET}, board_map=data)


def test_missing_endpoint_and_missing_auth_are_different_errors():
    with pytest.raises(SshError, match="BB_HOST"):
        SshTarget.from_env({"BB_PW": SECRET}, board_map={})
    with pytest.raises(SshError, match="authentication BB_PW"):
        SshTarget.from_env({}, board_map={"bbb": {"access": ENDPOINT}})
    assert bbb_endpoint({"bbb": {"access": {"host": "UNVERIFIED", "user": "UNKNOWN"}}}, {}) == {
        "host": None, "user": None, "port": 22,
    }


@pytest.mark.parametrize("auth_source", ["env", "vault"])
def test_new_job_map_approval_mcp_and_actual_transport_agree(setup_scope, monkeypatch, auth_source):
    s = setup_scope
    data, step = configure(s)
    job, authorizer = start(s, "BBB의 기존 진단을 실행해줘")
    source = authorizer.source(job.job_id)
    assert source["devices"][0]["target"] == ENDPOINT
    # No password needed to plan/approve, and no duplicate bbb.target required.
    approval = require(s, job, step)
    assert not approval.blocked, approval.detail
    assert s.gate(s.loop.agent("builder"), {"job_id": job.job_id, "step": step}, "run_bbb", {})
    fake = FakeSsh()
    targets = []

    def connect(target, **kwargs):
        targets.append(target.describe())
        return fake

    if auth_source == "env":
        monkeypatch.setenv("BB_PW", SECRET)
    else:
        monkeypatch.setattr("QKD_PROJ.hwtools.local_credentials.read_password",
                            lambda endpoint: SECRET if endpoint == ENDPOINT else None)
    monkeypatch.setattr("QKD_PROJ.hwtools.ssh.ParamikoClient", connect)
    policy = registry.ServerPolicy(project_root=s.root, contracts_dir=s.controller.contracts_dir,
        runs_dir=s.root / "runs", role="builder", allow_hardware=True, hardware_tool="run_bbb",
        hardware_contract=step["contract"], hardware_contract_sha256=step["contract_sha256"])
    tool = next(t for t in registry.build_tools(policy) if t.name == "run_bbb")
    payload = tool.handler({"contract": step["contract"], "script": "check"})
    assert payload["result"]["ok"], payload["result"].get("error")
    assert payload["result"]["measurements"]["target"] == source["devices"][0]["target"]
    assert targets == [ENDPOINT]
    assert "/proc/device-tree/model" in fake.scripts[0]
    assert "device-work-finished" in fake.scripts[1]
    assert SECRET not in json.dumps(payload)
    assert SECRET not in json.dumps(source)
    # A later environment change cannot use that job/grant for another device.
    monkeypatch.setenv("BB_HOST", "unapproved-board")
    assert not authorizer.evaluate(job, step, s.loop.agent("builder")).allowed
    with pytest.raises(ToolError, match="no device contacted"):
        tool.handler({"contract": step["contract"], "script": "check"})
    assert targets == [ENDPOINT]


def test_wrong_live_model_stops_before_running_script(setup_scope):
    s = setup_scope
    _, step = configure(s)
    gate = SafetyGate(Contracts.load(step["contract"], s.controller.contracts_dir), allow_hardware=True)
    fake = FakeSsh([("/proc/device-tree/model", (0, "Xilinx Zynq\nlinux\n", ""))])
    result = run_bbb(gate, "check", transport=transport_for(fake), repo_root=s.root)
    assert not result.ok
    assert result.measurements["failed_stage"] == "verify_target"
    assert len(fake.scripts) == 1
    assert "device-work-finished" not in fake.scripts[0]


def test_registration_does_not_rewrite_existing_job_authority(setup_scope):
    s = setup_scope
    configure(s, {"host": "UNVERIFIED", "user": "UNVERIFIED", "port": 22})
    old, authorizer = start(s, "BBB 진단해줘")
    before = copy.deepcopy(authorizer.source(old.job_id))
    _, step = configure(s)
    # Identical active scenarios deduplicate to the old job. Finish/cancel it
    # before resubmitting, rather than silently upgrading its frozen authority.
    ControlPlane(s.ledger).cancel(old.job_id, "test-user")
    new, newer = start(s, "BBB 진단해줘")
    assert newer.source(new.job_id)["devices"][0]["target"] == ENDPOINT
    assert authorizer.source(old.job_id) == before
    assert not authorizer.evaluate(old, step, s.loop.agent("builder")).allowed
    assert newer.evaluate(new, step, s.loop.agent("builder")).allowed
