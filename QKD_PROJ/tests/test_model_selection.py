"""Offline model selection tests: no real model turns, accounts or hardware."""
from __future__ import annotations

import copy
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from QKD_PROJ.agents.schema import load_simple_team
from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig, StdioAppServer, CodexProtocolError
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.model_selection import ModelCatalog, normalize_selection, selection_for
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.orchestrator.service import _LockedControl
from QKD_PROJ.orchestrator.wire import WireSession
from QKD_PROJ.tests.test_scenario_preflight import write_contract


def metadata():
    return [dict(model=name, displayName=name, defaultReasoningEffort=default,
                 supportedReasoningEfforts=[{"reasoningEffort": e} for e in efforts])
            for name, default, efforts in [("small", "low", ["low", "medium"]),
                                          ("large", "high", ["high", "xhigh"])]]


def test_catalog_cache_is_copy_safe_and_defaults_are_model_specific():
    calls = []
    catalog = ModelCatalog(lambda: calls.append(1) or metadata())
    catalog.models()[0]["efforts"].clear()
    selection = {"default": {"model": "small"}, "builder": {"model": "large"}}
    frozen = catalog.validate(selection)
    assert frozen["default"]["effort"] == "low"
    assert frozen["builder"]["effort"] == "high"
    assert "effort" not in selection["builder"]
    assert calls == [1]
    assert selection_for({"model_selection": frozen}, "builder") == frozen["builder"]
    assert selection_for({"model_selection": frozen}, "verifier") == frozen["default"]


@pytest.mark.parametrize("value", [[], "small", {"provider": "claude"}, {"builder": "small"},
    {"default": {"effort": "low"}}, {"default": {"model": "small", "shell": "anything"}},
    {"default": {"model": "--config"}}, {"default": {"model": "small", "effort": False}}])
def test_invalid_choice_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_selection(value)


@pytest.mark.parametrize("choice", [{"model": "missing"}, {"model": "small", "effort": "xhigh"}])
def test_unavailable_model_or_effort_is_not_silently_replaced(choice):
    with pytest.raises(ValueError):
        ModelCatalog(metadata).validate({"default": choice})


def test_failed_catalog_never_leaks_stderr_and_defaults_still_work():
    def fail():
        raise RuntimeError("secret-token-in-stderr")
    catalog = ModelCatalog(fail)
    assert catalog.validate(None) == {}
    assert catalog.validate({}) == {}
    with pytest.raises(ValueError) as exc:
        catalog.models()
    assert "secret" not in str(exc.value)


def test_stdio_catalog_reads_all_pages_without_any_turn(tmp_path):
    peer = Path(__file__).parent / "fixtures" / "fake_codex_app_server.py"
    with StdioAppServer(command=(sys.executable, "-u", str(peer)), cwd=tmp_path,
                        timeout_seconds=3, client_info={"name": "models_test", "version": "1"}) as client:
        rows = client.list_models()
        assert [row["model"] for row in rows] == ["test-small", "test-large"]
        assert client._active_turns == {}


def test_bad_pagination_stops():
    client = object.__new__(StdioAppServer)
    client.timeout_seconds = 3
    client._request = lambda *_: {"data": [], "nextCursor": "same"}
    with pytest.raises(CodexProtocolError, match="pagination"):
        client.list_models()


def test_wire_submission_persists_models_in_brief_and_refuses_invalid(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    write_contract(contracts, "parser.yaml", test_id="parser", purpose=["Parse text offline."])
    control = ControlPlane(ledger, preflight_engine=ScenarioPreflight(contracts_dir=contracts),
                           model_catalog=ModelCatalog(metadata))
    sent = []
    wire = WireSession(ledger, _LockedControl(control, threading.RLock()), sent.append)
    wire.handle({"type": "models.list", "commandId": "catalog"})
    assert sent[-1]["ok"] and len(sent[-1]["models"]) == 2
    scenario = "Execution scope: offline; no hardware access is permitted.\nWrite a text parser."
    preflight = control.preflight(scenario)
    frame = {"type": "job.mission", "commandId": "start", "scenario": scenario,
             "scenarioSha256": preflight["scenario_sha256"], "objective": "text parser", "searchPolicy": "refresh",
             "modelSelection": {"default": {"model": "small"}, "verifier": {"model": "large"}}}
    wire.handle(frame)
    assert sent[-1]["ok"], sent[-1]
    job_id = sent[-1]["job"]["job_id"]
    resources = ledger.handoffs(job_id, "Brief", 0)[0].payload["resources"]
    assert resources["model_selection"]["verifier"] == {"model": "large", "effort": "high"}
    assert resources["execution_mode"] == "offline"
    assert not ledger.pending_approvals()
    # Restart/reopen has the user's immutable selection; no localStorage dependency.
    reopened = JobLedger(tmp_path / "jobs.db")
    assert reopened.handoffs(job_id, "Brief", 0)[0].payload["resources"] == resources
    wire.handle({**frame, "commandId": "bad", "modelSelection": {"default": {"model": "missing"}}})
    assert not sent[-1]["ok"]
    assert len(ledger.jobs()) == 1


def test_duplicate_does_not_change_running_jobs_model(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.db")
    resources = {"model_selection": {"default": {"model": "small", "effort": "low"}}}
    job, _ = start_simple_mission(ledger, "parse", "parse text", resources=resources)
    duplicate, reused = start_simple_mission(ledger, "parse", "parse text", resources=resources)
    assert reused and duplicate.job_id == job.job_id
    with pytest.raises(ValueError, match="모델 설정"):
        start_simple_mission(ledger, "parse", "parse text", resources={})
    assert len(ledger.jobs()) == 1


def test_runner_applies_models_per_role_without_changing_permissions_or_config(monkeypatch, tmp_path):
    selection = ModelCatalog(metadata).validate({"default": {"model": "small"}, "builder": {"model": "large"}})
    configs = []
    runner = CodexAgentRunner(config=CodexRunnerConfig(model="daemon-default", effort="medium"))
    def invoke(agent, task, config):
        configs.append((agent.name, config))
        # Inspect actual outgoing protocol parameters, not just the config object.
        thread = runner._thread_params(agent, config)
        turn = runner._turn_params(agent, task, "Plan" if agent.name == "lead-agent" else
                                   "StepResult" if agent.name == "builder" else "Verdict", config)
        assert thread["model"] == config.model == turn["model"]
        assert turn["effort"] == config.effort
        return {"kind": "Plan", "steps": []}, []
    monkeypatch.setattr(runner, "_invoke", invoke)
    task = {"resources": {"model_selection": selection, "write_scope": []}, "step": {"touches_hardware": False}}
    before = copy.deepcopy(task)
    for agent in load_simple_team():
        runner(replace(agent, native_web=False), task)
    assert {role: cfg.model for role, cfg in configs} == {"lead-agent": "small", "builder": "large", "verifier": "small"}
    assert all(cfg.profile == "offline" and cfg.hardware_tool is None for _, cfg in configs)
    assert runner.config.model == "daemon-default" and runner.config.effort == "medium"
    assert task == before
    configs.clear()
    runner(replace(load_simple_team()[0], native_web=False), {})
    assert configs[0][1].model == "daemon-default"


def test_models_metadata_does_not_wait_for_an_active_tick(tmp_path):
    lock = threading.RLock()
    control = _LockedControl(ControlPlane(JobLedger(tmp_path / "jobs.db"), model_catalog=ModelCatalog(metadata)), lock)
    done = threading.Event()
    with lock:
        worker = threading.Thread(target=lambda: (control.handle({"command": "models-list"}), done.set()))
        worker.start()
        assert done.wait(1)
    worker.join(1)


def test_browser_models_command_uses_the_existing_auth_boundary(monkeypatch):
    from QKD_PROJ.tests.test_webui import _app, _client, BASE
    from fastapi.testclient import TestClient
    app, session = _app(None)
    seen = []
    def command(frame):
        seen.append(frame)
        return {"ok": True, "provider": "codex", "models": ModelCatalog(metadata).models()}
    monkeypatch.setattr(app.state.hub.subscription, "command", command)
    frame = {"type": "models.list", "commandId": "m"}
    with TestClient(app, base_url=BASE) as client:
        assert client.post("/api/command", json=frame).status_code in (401, 403)
    assert not seen
    with _client(app, session) as client:
        answer = client.post("/api/command", json=frame)
        assert answer.status_code == 200
        assert len(answer.json()["models"]) == 2
    assert seen == [frame]
