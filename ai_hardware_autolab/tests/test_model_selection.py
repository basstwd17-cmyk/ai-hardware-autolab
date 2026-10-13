"""Offline model selection tests: no real model turns, accounts or hardware."""
from __future__ import annotations

import copy
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from ai_hardware_autolab.agents.schema import load_simple_team
from ai_hardware_autolab.orchestrator.codex_transport import StdioAppServer, CodexProtocolError
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.model_selection import ModelCatalog, normalize_selection, selection_for
from ai_hardware_autolab.orchestrator.scenario_preflight import ScenarioPreflight
from ai_hardware_autolab.orchestrator.service import _LockedControl
from ai_hardware_autolab.orchestrator.wire import WireSession


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
    from ai_hardware_autolab.webui.backend.app import create_app, DEFAULT_PORT
    from ai_hardware_autolab.webui.backend.security import Session
    BASE=f'http://127.0.0.1:{DEFAULT_PORT}'
    def _app(address):
        session=Session(file=None)
        return create_app(address=address,session=session,port=DEFAULT_PORT),session
    def _client(app,session):
        return TestClient(app,base_url=BASE,headers={'Authorization':f'Bearer {session.token}'})
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

def test_model_choice_is_frozen_and_part_of_duplicate_submission(tmp_path):
    import hashlib
    from ai_hardware_autolab.tests.test_conversation_service import setup_flow, SCENARIO
    source,ledger,flow,daemon,control=setup_flow(tmp_path,lambda role,ctx: {})
    control.model_catalog=ModelCatalog(metadata)
    digest=hashlib.sha256(SCENARIO.encode()).hexdigest()
    selected={'builder':{'model':'large','effort':'high'}}
    first=control.mission('Build',SCENARIO,digest,model_selection=selected)
    same=control.mission('Build',SCENARIO,digest,model_selection=selected)
    other=control.mission('Build',SCENARIO,digest,model_selection={'builder':{'model':'small'}})
    selected['builder']['model']='small'
    assert same['reused'] and same['job']['job_id']==first['job']['job_id']
    assert other['job']['job_id']!=first['job']['job_id']
    assert flow._brief(first['job']['job_id'])['inputs']['model_selection']['builder']['model']=='large'
