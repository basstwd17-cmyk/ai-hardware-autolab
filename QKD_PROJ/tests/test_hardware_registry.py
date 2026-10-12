"""Other-board registration, per-job routing and transport; physical boundaries are mocked."""
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from QKD_PROJ.boards.discovery import discover_local, matching_device
from QKD_PROJ.boards.registry import HardwareRegistry
from QKD_PROJ.boards.hardware import HardwareProfile
from QKD_PROJ.orchestrator.conversation_hardware import SelectedHardwareExecutor, ConversationHardwareConfig
from QKD_PROJ.orchestrator.conversation_service import prepare_intake, WebConversationFlow
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
from QKD_PROJ.mcp_server.protocol import DevicePreparationError

SERIAL = {"kind": "serial", "port": "COM8", "description": "USB Serial", "vid": 0x483,
          "pid": 0x5740, "serial_number": "test-board-a", "location": "1-2"}
USB = {"kind": "usb", "description": "Test probe", "instance_id": "USB\\VID_0483&PID_374B\\TEST", "vid": 0x483, "pid": 0x374B}


def discovery():
    return discover_local(serial_reader=lambda: [SERIAL], usb_reader=lambda: [USB])


def registration(board="other-board", **extra):
    return {"board_id": board, "revision": "v1", "title": board, "model": "User-declared board model", **extra}


def registry(tmp_path):
    return HardwareRegistry(tmp_path, default={"board_id": "other-board", "revision": "v1"}, discover=discovery)


def test_discovery_is_passive_partial_and_keeps_observations_separate_from_models(monkeypatch):
    import serial
    monkeypatch.setattr(serial, "Serial", lambda *a, **kw: pytest.fail("discovery must not open a port"))
    result = discovery()
    assert {r["kind"] for r in result["devices"]} == {"serial", "usb"}
    assert all("model" not in row and row["evidence"] == "host_enumeration" for row in result["devices"])
    assert result == {**discovery(), "checked_at": result["checked_at"]}
    def failed():
        raise RuntimeError("private host details")
    partial = discover_local(serial_reader=lambda: [SERIAL], usb_reader=failed)
    assert len(partial["devices"]) == 1 and partial["errors"] == [{"kind": "usb", "error": "RuntimeError"}]
    assert "private host details" not in str(partial)
    assert matching_device(SERIAL, [{**SERIAL, "serial_number": "different-board"}]) is None
    assert matching_device(SERIAL, [{**SERIAL, "port": "COM9"}]) is None


def test_registration_persists_retries_and_does_not_inherit_rtase(tmp_path):
    store = registry(tmp_path)
    (tmp_path / "firmware").mkdir()
    found = store.discover()
    candidate = next(r for r in found["devices"] if r["kind"] == "serial")
    request = registration(candidate_ids=[candidate["candidate_id"]], source_paths=["firmware"],
                           datasheets=["https://example.org/datasheet"], notes="User wiring note")
    first = store.register(request)
    assert store.register(request) == first
    reopened = registry(tmp_path)
    profile = reopened.resolve()
    assert profile.public_description() == first
    assert profile.data["source_paths"] == ["firmware"]
    assert not {"bbb", "jtag", "scripts", "forbidden_operations", "device_map"} & set(profile.data)
    assert set(profile.tools) == {"serial_exchange"}
    assert len(reopened.catalog()["setups"]) == 1
    with pytest.raises(ValueError, match="revision exists"):
        store.register({**request, "serial_port": "COM9"})
    assert reopened.resolve().sha256 == profile.sha256


@pytest.mark.parametrize("change", [
    {"board_id": "../escape"}, {"revision": ".."}, {"board_id": "con"}, {"revision": "nul.txt"},
    {"source_paths": ["../outside"]}, {"source_paths": [".codex"]},
    {"datasheets": ["https://user:secret@example.org/doc"]}, {"password": "do not store"},
    {"candidate_ids": ["invented"]}, {"serial_port": "socket://remote:23"},
])
def test_invalid_registrations_do_not_create_profiles(tmp_path, change):
    store = registry(tmp_path)
    with pytest.raises(ValueError):
        store.register(registration(**change))
    assert store.catalog()["setups"] == []


def make_control(tmp_path, store):
    ledger = JobLedger(tmp_path / "jobs.db")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    flow = WebConversationFlow(ledger, None, SimpleNamespace(prepare=lambda _: workspace))
    executor = SelectedHardwareExecutor(ledger, store, ConversationHardwareConfig(tmp_path, tmp_path / "runs"))
    control = ControlPlane(ledger, SimpleNamespace(loop=SimpleNamespace(conversation=flow)),
        preflight_engine=ScenarioPreflight(tmp_path / "no-contracts"), hardware_enabled=True,
        hardware_registry=store, hardware_executor=executor)
    return ledger, flow, executor, control


def submit(control, board):
    scenario = "Execution scope: hardware. Inspect the selected board connection."
    return control.mission("Inspect board", scenario, hashlib.sha256(scenario.encode()).hexdigest(),
                           hardware_selection={"board_id": board, "revision": "v1"})


def test_same_scenario_on_two_boards_routes_by_job_even_after_restart(tmp_path, monkeypatch):
    from QKD_PROJ.hwtools import local_board
    store = registry(tmp_path)
    store.register(registration(serial_port="COM8"))
    store.register(registration("second-board", serial_port="COM9"))
    ledger, flow, executor, control = make_control(tmp_path, store)
    first, second = submit(control, "other-board"), submit(control, "second-board")
    a, b = first["job"]["job_id"], second["job"]["job_id"]
    assert a != b and submit(control, "other-board")["job"]["job_id"] == a
    assert flow._brief(a)["inputs"]["hardware_selection"]["board_id"] == "other-board"
    assert flow._brief(b)["inputs"]["hardware_devices"][0]["port"] == "COM9"
    calls = []
    monkeypatch.setattr(local_board, "serial_exchange", lambda profile, args, runs: calls.append(profile.board_id) or {"result": {"ok": True}})
    restarted = SelectedHardwareExecutor(ledger, registry(tmp_path), executor.config)
    for job in (b, a):
        ledger.transition(job, "running")
        restarted(job, 1, {"tool": "serial_exchange", "arguments": {"device": "serial-manual"}})
    assert calls == ["second-board", "other-board"]
    assert restarted.connections_for({"board_id": "second-board", "revision": "v1"}).device_lock is restarted.connections_for().device_lock
    assert not ledger.handoffs(a, "Plan") and not ledger.approvals(a)
    profile = store.resolve()
    profile.path.write_text(profile.path.read_text() + "\n")
    with pytest.raises(DevicePreparationError, match="changed"):
        restarted(a, 1, {"tool": "serial_exchange", "arguments": {"device": "serial-manual"}})
    assert len(calls) == 2
    with pytest.raises(ValueError):
        submit(control, "does-not-exist")


def test_offline_board_selection_carries_sources_but_no_device_tools(tmp_path):
    store = registry(tmp_path)
    store.register(registration(serial_port="COM8"))
    preview, inputs = prepare_intake("Read the board source", ScenarioPreflight(tmp_path),
        execution_mode="offline", hardware_profile=store.resolve(), hardware_enabled=True)
    assert inputs["selected_board"]["board_id"] == "other-board"
    assert "device_tools" not in inputs and preview["execution_mode"] == "offline"


def test_usb_presence_is_not_board_identity_and_multiple_device_results_survive(tmp_path, monkeypatch):
    from QKD_PROJ.hwtools import local_board
    store = registry(tmp_path)
    found = store.discover()
    store.register(registration(candidate_ids=[r["candidate_id"] for r in found["devices"]], serial_port="COM9"))
    ledger, flow, executor, control = make_control(tmp_path, store)
    job = submit(control, "other-board")["job"]["job_id"]
    ledger.transition(job, "running")
    monkeypatch.setattr(local_board, "discover_local", lambda: discover_local(
        serial_reader=lambda: [SERIAL, {**SERIAL, "port": "COM9", "serial_number": "second"}], usb_reader=lambda: [USB]))
    profile = store.resolve()
    for device in profile.data["devices"]:
        result = executor(job, 1, {"tool": "check_connections", "arguments": {"devices": [device["id"]]}})
        assert result["inventory"]["devices"][0]["status"] == "present"
        ledger.append_event(job, "conversation.device.completed", result, "platform")
        manifest = json.loads((tmp_path / "runs" / result["inventory"]["devices"][0]["run"] / "manifest.json").read_text())
        assert manifest["steps"][0]["measurements"]["identity_verified"] is False
        assert "contract" not in manifest
    context = flow._context(job, "lead")
    assert len(context["inputs"]["hardware_connections"]["devices"]) == 3
    assert context["inputs"]["device_tools"] == ["check_connections"]


def test_serial_transport_uses_selected_identity_and_records_actual_bytes(tmp_path, monkeypatch):
    import serial
    from QKD_PROJ.hwtools import local_board
    store = registry(tmp_path)
    found = store.discover()
    store.register(registration(candidate_ids=[found["devices"][0]["candidate_id"]]))
    profile = store.resolve()
    calls = []
    class Port:
        def __init__(self, **kwargs): calls.append(kwargs)
        def open(self): calls.append((self.port, self.dtr, self.rts))
        def write(self, value): calls.append(value); return len(value)
        def read(self, count): return b"OK\n"
        def close(self): calls.append("closed")
    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(local_board, "serial_devices", lambda: [SERIAL])
    args = {"device": profile.data["devices"][0]["id"], "write_hex": "0102", "read_bytes": 3}
    result = local_board.serial_exchange(profile, args, tmp_path / "runs")
    assert result["result"]["measurements"]["read_hex"] == "4f4b0a"
    assert calls[1] == ("COM8", False, False) and calls[2] == b"\x01\x02"
    assert result["hardware_contacted"] and result["run_verdict"] == "PASS"
    monkeypatch.setattr(local_board, "serial_devices", lambda: [{**SERIAL, "serial_number": "replacement"}])
    with pytest.raises(DevicePreparationError, match="identity"):
        local_board.serial_exchange(profile, args, tmp_path / "runs")
    assert len(calls) == 4
    for bad in ({"read_bytes": 100000}, {"timeout_s": 60}, {"write_hex": "zz"}, {"baudrate": True}, {"port": "COM9"}):
        with pytest.raises(DevicePreparationError):
            local_board.serial_exchange(profile, {**args, **bad}, tmp_path / "runs")


def test_scope_reuse_does_not_copy_old_board_wiring_or_programming_restrictions(tmp_path, monkeypatch):
    original = HardwareProfile.load(Path(__file__).resolve().parents[2], "rtase-v1", "v1.0")
    source_dir = tmp_path / "QKD_PROJ/boards/scope-fixture/v1"
    source_dir.mkdir(parents=True)
    data = {**original.data, "board_id": "scope-fixture", "revision": "v1", "device_map": "QKD_PROJ/boards/scope-fixture/v1/device-map.yaml"}
    (source_dir / "device-map.yaml").write_bytes(original.device_map_path.read_bytes())
    (source_dir / "hardware.yaml").write_text(yaml.safe_dump(data))
    store = registry(tmp_path)
    store.register(registration(scope_from={"board_id": "scope-fixture", "revision": "v1"}))
    profile = store.resolve()
    assert "forbidden_operations" not in profile.data and "jtag" not in profile.data
    assert all(row["signal"].startswith("UNASSIGNED") for row in profile.data["scope"]["channels"])
    gate = profile.compatibility_gate(scope_connections=[{"channel": 1, "signal": "PWM"}])
    assert gate.instrument_scope()["devices"][0]["model"] == "RTM3004"
    assert gate.contracts.board_map["scope"]["host"] == "192.168.50.3"
    gate.require_scope_capture({"channels": [{"channel": 1, "signal": "PWM"}]})
    ledger, _, executor, control = make_control(tmp_path, store)
    job = submit(control, "other-board")["job"]["job_id"]
    with pytest.raises(DevicePreparationError, match="wiring"):
        executor(job, 1, {"tool": "measure_scope", "arguments": {"params": {"channels": [{"channel": 1, "signal": "PWM"}]}}})
    for malformed in ({"params": []}, {"params": {"channels": ["CH1"]}}, {"params": {"channels": [{"channel": []}]}}):
        with pytest.raises(DevicePreparationError):
            executor(job, 1, {"tool": "measure_scope", "arguments": malformed})
    # A user-supplied mapping enables the existing instrument driver on a new board.
    from QKD_PROJ.hwtools import scope
    from QKD_PROJ.hwtools.result import ToolResult
    calls = []
    def measure(recipe, params, gate, record):
        calls.append((params, gate.contracts.board_map["scope"]))
        result = ToolResult("scope", "capture", verdict="PASS", measurements={"frequency_hz": 1000})
        record.add(result)
        return result
    monkeypatch.setattr(scope, "measure_scope", measure)
    scenario = "Execution scope: hardware. Measure PWM with CH1=PWM."
    job = control.mission("Measure PWM", scenario, hashlib.sha256(scenario.encode()).hexdigest())["job"]["job_id"]
    ledger.transition(job, "running")
    result = executor(job, 1, {"tool": "measure_scope", "arguments": {"params": {"channels": [{"channel": 1, "signal": "PWM"}]}}})
    assert result["result"]["ok"] is True and len(calls) == 1
    assert calls[0][1]["host"] == "192.168.50.3"


def test_http_discovery_registration_selection_and_checks_reach_real_daemon(tmp_path, monkeypatch):
    from QKD_PROJ.hwtools import local_board
    from QKD_PROJ.orchestrator.daemon import Daemon
    from QKD_PROJ.orchestrator.workflows import WorkflowRouter
    from QKD_PROJ.orchestrator.service import Service, ServiceConfig, SocketTransport
    from QKD_PROJ.tests.test_webui import _app, _client
    store = registry(tmp_path)
    ledger, flow, executor, _ = make_control(tmp_path, store)
    monkeypatch.setattr(local_board, "discover_local", discovery)
    service = Service(ledger, Daemon(ledger, WorkflowRouter(ledger, conversation=flow)),
        config=ServiceConfig(install_signal_handlers=False, runtime_profile="hardware"),
        transport=SocketTransport(), hardware_registry=store, hardware_executor=executor)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            # Registration/discovery are independent of a long model turn.
            with service._turn:
                found = http.post("/api/command", json={"type": "hardware.discover"})
                assert found.status_code == 200
                candidate = found.json()["discovery"]["devices"][0]["candidate_id"]
                registered = http.post("/api/command", json={"type": "hardware.register", "setup": registration(candidate_ids=[candidate])})
                assert registered.status_code == 200, registered.text
                catalog = http.post("/api/command", json={"type": "hardware.catalog"})
                assert catalog.json()["setups"][0]["board_id"] == "other-board"
            selected = {"board_id": "other-board", "revision": "v1"}
            scenario = "Execution scope: hardware. Read board status."
            submitted = http.post("/api/command", json={"type": "job.mission", "objective": "Read status", "scenario": scenario,
                "searchPolicy": "refresh",
                "scenarioSha256": hashlib.sha256(scenario.encode()).hexdigest(), "hardwareSelection": selected})
            assert submitted.status_code == 200, submitted.text
            job = submitted.json()["job"]["job_id"]
            assert flow._brief(job)["inputs"]["hardware_selection"] == selected
            device = store.resolve().data["devices"][0]["id"]
            with service._turn:
                started = http.post("/api/command", json={"type": "hardware.check", "hardwareSelection": selected, "devices": [device]})
                assert started.status_code == 200, started.text
            checker = executor.connections_for(selected)
            with checker._changed:
                assert checker._changed.wait_for(lambda: checker._active is None, timeout=5)
            status = http.post("/api/command", json={"type": "hardware.status", "hardwareSelection": selected})
            assert status.json()["inventory"]["devices"][0]["status"] == "present"
            rejected = http.post("/api/command", json={"type": "hardware.status", "hardwareSelection": {"board_id": "missing", "revision": "v1"}})
            assert rejected.status_code == 409


def test_production_flow_uses_selected_board_sources_knowledge_and_tools(tmp_path, monkeypatch):
    import serial
    from QKD_PROJ.hwtools import local_board
    from QKD_PROJ.knowledge.models import KnowledgeScope
    from QKD_PROJ.orchestrator import __main__ as entrypoint
    from QKD_PROJ.tests.test_workspace import _source_repo
    source = _source_repo(tmp_path)
    store = registry(source)
    store.register(registration(serial_port="COM8", source_paths=["QKD_PROJ/base.py"]))
    monkeypatch.setattr(entrypoint, "QKD_ROOT", source / "QKD_PROJ")
    args = SimpleNamespace(ledger=str(tmp_path / "production.db"), runs=str(tmp_path / "runs"),
        profile="hardware", dry_run=False, workspace_root=str(source / "QKD_PROJ/workspaces"),
        hardware_board="other-board", hardware_revision="v1", knowledge_hardware="rtase-v1", knowledge_revision="v1.0",
        backoff=1.0, retries=2, interval=0.1, pipe=None)
    service, _ = entrypoint.build_service(args)
    runtime = service.daemon.loop.conversation.runtime
    contexts, scopes, calls = [], [], []
    class Knowledge:
        def retrieve(self, objective, scope, limit):
            scopes.append(scope)
            return SimpleNamespace(to_dict=lambda: {"claims": []})
    runtime.knowledge_retriever = Knowledge()
    runtime.knowledge_scope = KnowledgeScope(project_id="test", hardware_id="rtase-v1", revision="v1.0")
    class Model:
        def __init__(self, config, event_sink): pass
        def __call__(self, role, context):
            contexts.append(role)
            board = context["inputs"]["selected_board"]
            assert board["board_id"] == "other-board" and board["source_paths"] == ["QKD_PROJ/base.py"]
            assert (Path(context["workspace"]) / board["source_paths"][0]).exists()
            assert "run_bbb" not in context["inputs"]["device_tools"]
            if role == "lead" and not context["device_results"]:
                assert not calls
                return {"status": "device", "summary": "Check the selected board's serial connection.",
                        "device_call": {"tool": "check_connections", "arguments": {"devices": ["serial-manual"]}}}
            if role == "builder" and len(context["device_results"]) == 1:
                return {"status": "device", "summary": "Read board status with the documented serial protocol.",
                        "device_call": {"tool": "serial_exchange", "arguments": {"device": "serial-manual", "write_hex": "01", "read_bytes": 2}}}
            if role == "verifier":
                assert context["device_results"][-1]["result"]["measurements"]["read_text"] == "OK"
                return {"status": "satisfied", "summary": "The simulated board returned its expected status.",
                        "runs": [context["device_results"][-1]["run"]]}
            return {"status": "ready", "summary": "Continue with the selected board and existing observation."}
    class Port:
        def __init__(self, **kw): pass
        def open(self): calls.append(self.port)
        def write(self, data): return len(data)
        def read(self, count): return b"OK"
        def close(self): pass
    runtime.factory = Model
    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(local_board, "serial_devices", lambda: [SERIAL])
    monkeypatch.setattr(local_board, "discover_local", discovery)
    job = submit(service.control, "other-board")["job"]["job_id"]
    for _ in range(10):
        if service.ledger.job(job).state == "done": break
        service.tick_once()
    assert service.ledger.job(job).outcome == "satisfied"
    assert contexts == ["lead", "lead", "builder", "builder", "verifier"]
    assert calls == ["COM8"]
    assert scopes and all(scope.hardware_id == "other-board" and scope.revision == "v1" for scope in scopes)
    assert not service.ledger.handoffs(job, "Plan") and not service.ledger.approvals(job)


def test_cancel_while_waiting_for_another_setup_prevents_serial_dispatch(tmp_path, monkeypatch):
    from QKD_PROJ.hwtools import local_board
    store = registry(tmp_path)
    store.register(registration(serial_port="COM8"))
    ledger, flow, executor, control = make_control(tmp_path, store)
    job = submit(control, "other-board")["job"]["job_id"]
    ledger.transition(job, "running")
    monkeypatch.setattr(local_board, "serial_exchange", lambda *args: pytest.fail("cancelled job must not touch a port"))
    entered, failures = threading.Event(), []
    def request():
        entered.set()
        try:
            executor(job, 1, {"tool": "serial_exchange", "arguments": {"device": "serial-manual"}})
        except DevicePreparationError as exc:
            failures.append(str(exc))
    with executor.device_lock:
        worker = threading.Thread(target=request)
        worker.start()
        assert entered.wait(2)
        flow.cancel(job)
    worker.join(5)
    assert not worker.is_alive() and failures == ["job stopped before device dispatch"]


def test_serial_read_failure_keeps_sent_bytes_and_does_not_retry(tmp_path, monkeypatch):
    import serial
    from QKD_PROJ.hwtools import local_board
    store = registry(tmp_path)
    store.register(registration(serial_port="COM8"))
    writes = []
    class Port:
        def __init__(self, **kw): pass
        def open(self): pass
        def write(self, data): writes.append(data); return len(data)
        def read(self, count): raise OSError("transport details")
        def close(self): pass
    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(local_board, "serial_devices", lambda: [SERIAL])
    result = local_board.serial_exchange(store.resolve(), {"device": "serial-manual", "write_hex": "0102"}, tmp_path / "runs")
    assert writes == [b"\x01\x02"] and not result["result"]["ok"] and result["hardware_contacted"]
    manifest = json.loads((tmp_path / "runs" / result["run"] / "manifest.json").read_text())
    assert len(manifest["steps"]) == 1 and manifest["steps"][0]["measurements"]["written_bytes"] == 2
    assert "transport details" not in str(result)
