"""Configured-device observation and its real UI/agent entry points; no hardware."""
import hashlib
import threading
from pathlib import Path

import pytest
import yaml

from QKD_PROJ.boards.connections import HardwareConnections
from QKD_PROJ.boards.hardware import HardwareProfile
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.service import _LockedControl
from QKD_PROJ.orchestrator.wire import WireSession

ALL_DEVICES = ['bbb', 'scope', 'cpld', 'serial']


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / 'QKD_PROJ/boards/test/1'
    path.mkdir(parents=True)
    (path / 'device-map.yaml').write_text('{}')
    (path / 'hardware.yaml').write_text(yaml.safe_dump({
        'schema_version': 1, 'board_id': 'test', 'revision': '1',
        'device_map': 'QKD_PROJ/boards/test/1/device-map.yaml',
        'devices': [{'kind': kind, 'model': kind} for kind in ('bbb', 'scope', 'cpld', 'serial')],
        'tools': {'run_bbb': {'approval': 'automatic'}},
    }))
    return HardwareProfile.load(tmp_path, 'test', '1')


def observation(kind, *, ok=True, verified=True):
    return {'run': f'connection_{kind}', 'run_verdict': 'PASS' if ok else 'FAIL',
            'result': {'ok': ok, 'measurements': {
                'chain_verified' if kind == 'cpld' else 'identity_verified': verified,
                'failure_stage': None if ok else 'tcp_connect',
            }}}


def finish(checker):
    with checker._changed:
        assert checker._changed.wait_for(lambda: checker._snapshot['status'] != 'checking', timeout=5)
    return checker.snapshot()


def test_per_device_results_do_not_promote_configured_or_unverified_devices(profile, tmp_path):
    calls = []
    def probe(kind):
        calls.append(kind)
        return observation(kind, ok=kind != 'scope', verified=kind != 'cpld')
    checker = HardwareConnections(profile, tmp_path / 'runs', probe=probe)
    before = profile.path.read_bytes()
    assert checker.snapshot()['status'] == 'not_checked' and not calls
    result = checker.check(ALL_DEVICES)
    assert calls == ['bbb', 'scope', 'cpld']
    assert [r['status'] for r in result['devices']] == ['connected', 'unavailable', 'unavailable', 'unsupported']
    assert result['devices'][1]['failure_stage'] == 'tcp_connect'
    assert result['checked_at'] and all(r['checked_at'] for r in result['devices'])
    result['devices'][0]['status'] = 'changed'
    assert checker.snapshot()['devices'][0]['status'] == 'connected'
    assert profile.path.read_bytes() == before


def test_probe_exception_isolated_and_does_not_publish_arbitrary_exception_text(profile, tmp_path):
    def probe(kind):
        if kind == 'bbb':
            raise RuntimeError('secret-from-transport')
        return observation(kind)
    result = HardwareConnections(profile, tmp_path, probe=probe).check(ALL_DEVICES)
    assert [r['status'] for r in result['devices']][:3] == ['error', 'connected', 'connected']
    assert 'secret-from-transport' not in str(result)


def test_profile_change_stops_all_device_access(profile, tmp_path):
    checker = HardwareConnections(profile, tmp_path, probe=lambda _: pytest.fail('must not contact device'))
    profile.path.write_text(profile.path.read_text() + '\n# changed\n')
    result = checker.check(['bbb'])
    assert result['status'] == 'error'
    assert all(r['status'] == 'not_checked' for r in result['devices'])


@pytest.mark.parametrize('devices', [None, [], 'bbb', ['unknown'], [1]])
def test_missing_or_invalid_selection_never_scans_any_device(profile, tmp_path, devices):
    calls = []
    checker = HardwareConnections(profile, tmp_path, probe=lambda kind: calls.append(kind))
    with pytest.raises(ValueError):
        checker.check(devices)
    assert not calls and checker.snapshot()['status'] == 'not_checked'


def test_different_requests_return_their_own_results_and_keep_other_observations(profile, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls, responses = [], []
    def probe(kind):
        calls.append(kind)
        if kind == 'bbb':
            entered.set()
            assert release.wait(3)
        result = observation(kind)
        result['run'] += f'_{len(calls)}'
        return result
    checker = HardwareConnections(profile, tmp_path, probe=probe)
    checker.start(['bbb'])
    assert entered.wait(3)
    scope_request = threading.Thread(target=lambda: responses.append(checker.check(['scope'])))
    scope_request.start()
    release.set()
    scope_request.join(5)
    assert not scope_request.is_alive()
    assert [r['kind'] for r in responses[0]['devices']] == ['scope']
    assert calls == ['bbb', 'scope']
    before = checker.snapshot()['devices'][1]
    assert checker.snapshot()['devices'][2]['status'] == 'not_checked'
    assert checker.check(['bbb'])['devices'][0]['run'] == 'connection_bbb_3'
    assert checker.snapshot()['devices'][1] == before


@pytest.mark.parametrize('role', ['lead', 'builder', 'verifier'])
def test_connection_call_returns_to_its_requester_and_is_reused_after_restart(tmp_path, role):
    from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
    ledger = JobLedger(tmp_path / 'roles.db')
    contexts, requests = [], []
    class Runtime:
        def __call__(self, current, context):
            contexts.append(current)
            if current == role and not context['device_results']:
                return {'status': 'device', 'summary': 'Only scope identification is needed.',
                        'device_call': {'tool': 'check_connections', 'arguments': {'devices': ['scope']}}}
            if context['device_results']:
                assert context['inputs']['hardware_connections']['devices'][0]['run'] == 'scope-identity'
            return {'status': 'satisfied' if current == 'verifier' else 'ready',
                    'summary': 'The requested identity report is complete.',
                    'runs': ['scope-identity'] if context['device_results'] else []}

        def execute_device(self, _job, _turn, request):
            requests.append(request)
            return {'tool': 'check_connections', 'inventory': {'devices': [
                {'kind': 'scope', 'status': 'connected', 'checked_at': '2026-09-30T01:00:00Z', 'run': 'scope-identity'}]}}
    runtime = Runtime()
    job = ConversationFlow(ledger, runtime).create('Report scope identity', tmp_path, execution_mode='hardware',
        inputs={'hardware_profile': {'sha256': 'a' * 64}, 'device_tools': ['check_connections']})
    for _ in range(5):
        state = ConversationFlow(JobLedger(ledger.path), runtime).advance(job.job_id)
    assert state.done and len(requests) == 1
    expected = ['lead', 'builder', 'verifier']
    expected.insert(expected.index(role), role)
    assert contexts == expected


@pytest.mark.parametrize('role', ['lead', 'verifier'])
def test_connection_access_does_not_grant_programming_to_other_roles(tmp_path, role):
    from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
    with pytest.raises(ValueError, match='not available'):
        ConversationFlow.validate_reply(role, {'status': 'device', 'summary': 'Unexpected programming request',
            'device_call': {'tool': 'program_cpld_sram', 'arguments': {'program_xcf': 'image.xcf'}}}, tmp_path,
            device_tools=['check_connections', 'program_cpld_sram'])


def test_lead_corrects_a_bad_selection_without_handing_the_error_to_builder(profile, tmp_path):
    from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
    from QKD_PROJ.orchestrator.conversation_hardware import ConversationHardwareConfig, ConversationHardwareExecutor
    ledger = JobLedger(tmp_path / 'repair.db')
    executor = ConversationHardwareExecutor(ledger, profile,
        ConversationHardwareConfig(profile.project_root, tmp_path / 'runs'))
    calls = []
    executor.connections._probe = lambda kind: calls.append(kind) or observation(kind)
    class Runtime:
        def __call__(self, role, context):
            results = context['device_results']
            if role == 'lead' and len(results) < 2:
                if results:
                    assert results[0]['error_code'] == 'device_input_invalid'
                    assert results[0]['repair_target'] == 'lead'
                    assert not calls
                return {'status': 'device', 'summary': 'Check the BBB requested by the user.',
                        'device_call': {'tool': 'check_connections',
                                        'arguments': {'devices': ['bbb'] if results else []}}}
            return {'status': 'satisfied' if role == 'verifier' else 'ready',
                    'summary': 'BBB identity reported.', 'runs': ['connection_bbb']}

        execute_device = staticmethod(executor)
    flow = ConversationFlow(ledger, Runtime())
    job = flow.create('Identify BBB', tmp_path, execution_mode='hardware', inputs={
        'hardware_profile': {'sha256': profile.sha256}, 'device_tools': ['check_connections']})
    assert flow.run(job.job_id).done
    assert calls == ['bbb']


def test_concurrent_requests_join_one_scan_and_wait_for_device_execution(profile, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def probe(kind):
        calls.append(kind)
        entered.set()
        assert release.wait(3)
        return observation(kind)
    checker = HardwareConnections(profile, tmp_path, probe=probe)
    with checker.device_lock:
        assert checker.start(['bbb'])['status'] == 'checking'
        assert checker.start(['bbb'])['status'] == 'checking'
        assert not entered.is_set()
    try:
        assert entered.wait(3)
        assert checker.start(['bbb'])['status'] == 'checking'
        with pytest.raises(ValueError, match='another device check'):
            checker.start(['scope'])
    finally:
        release.set()
    assert finish(checker)['status'] == 'checked'
    assert calls == ['bbb']


def test_cancelled_job_does_not_probe_more_devices(profile, tmp_path):
    cancelled = threading.Event()
    calls = []
    def probe(kind):
        calls.append(kind)
        cancelled.set()
        return observation(kind)
    checker = HardwareConnections(profile, tmp_path, probe=probe)
    checker.start(ALL_DEVICES, cancelled=cancelled.is_set)
    assert finish(checker)['status'] == 'cancelled'
    assert calls == ['bbb']


def test_cancel_during_connection_dispatch_remains_terminal(tmp_path):
    from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
    ledger = JobLedger(tmp_path / 'cancel.db')
    class Runtime:
        def __call__(self, role, context):
            return {'status': 'device', 'summary': 'Only BBB is needed.',
                    'device_call': {'tool': 'check_connections', 'arguments': {'devices': ['bbb']}}}

        def execute_device(self, job_id, *_):
            flow.cancel(job_id)
            raise RuntimeError('job stopped during connection check')
    flow = ConversationFlow(ledger, Runtime())
    job = flow.create('Identify BBB', tmp_path, execution_mode='hardware',
        inputs={'hardware_profile': {'sha256': 'a' * 64}, 'device_tools': ['check_connections']})
    flow.advance(job.job_id)
    assert flow.advance(job.job_id).done
    assert ledger.job(job.job_id).outcome == 'cancelled'


def test_wire_status_and_refresh_are_nonblocking_and_offline_cannot_probe(profile, tmp_path):
    entered, release = threading.Event(), threading.Event()
    def probe(kind):
        entered.set()
        assert release.wait(3)
        return observation(kind)
    checker = HardwareConnections(profile, tmp_path, probe=probe)
    ledger = JobLedger(tmp_path / 'jobs.db')
    control = ControlPlane(ledger, hardware_enabled=False, hardware_connections=checker)
    sent = []
    wire = WireSession(ledger, _LockedControl(control, threading.RLock()), sent.append)
    wire.handle({'type': 'hardware.check', 'commandId': 'disabled', 'devices': ['bbb']})
    assert not sent[-1]['ok'] and not entered.is_set()
    control.hardware_enabled = True
    wire.handle({'type': 'hardware.status', 'commandId': 'read'})
    assert sent[-1]['inventory']['status'] == 'not_checked'
    wire.handle({'type': 'hardware.check', 'commandId': 'missing-selection'})
    assert not sent[-1]['ok'] and not entered.is_set()
    wire.handle({'type': 'hardware.check', 'commandId': 'refresh', 'devices': ['bbb']})
    try:
        assert sent[-1]['ok'] and sent[-1]['inventory']['status'] == 'checking'
        assert entered.wait(3)
        wire.handle({'type': 'hardware.status', 'commandId': 'progress'})
        assert sent[-1]['inventory']['status'] == 'checking'
    finally:
        release.set()
    finish(checker)


def test_real_driver_dispatch_is_identity_only_and_records_runs(tmp_path, monkeypatch):
    from QKD_PROJ.hwtools import bbb, scope, jtag
    from QKD_PROJ.hwtools.result import ToolResult
    installed = HardwareProfile.load(Path(__file__).resolve().parents[2], 'rtase-v1', 'v1.0')
    calls = []
    def identify(kind):
        def run(gate, record, **kwargs):
            calls.append((kind, kwargs))
            result = ToolResult(tool=kind, action='identify', verdict='PASS',
                                measurements=observation(kind)['result']['measurements'])
            record.add(result)
            return result
        return run
    monkeypatch.setattr(bbb, 'identify_bbb', identify('bbb'))
    monkeypatch.setattr(scope, 'identify_scope', identify('scope'))
    monkeypatch.setattr(jtag, 'scan_jtag', identify('cpld'))
    result = HardwareConnections(installed, tmp_path / 'runs').check(['bbb', 'scope', 'cpld'])
    assert all(row['status'] == 'connected' for row in result['devices'])
    assert calls[0] == ('bbb', {'timeout_s': 10.0})
    assert calls[1] == ('scope', {})
    assert calls[2][1]['timeout_s'] == 15.0
    assert calls[2][1]['xcf_path'].name == 'jtag-scan.xcf'
    assert all((tmp_path / 'runs' / row['run'] / 'manifest.json').is_file() for row in result['devices'])


@pytest.mark.parametrize('refresh', [False, True])
def test_agents_select_only_bbb_reuse_results_and_can_request_a_fresh_check(tmp_path, refresh):
    from QKD_PROJ.tests.test_conversation_service import setup_flow
    from QKD_PROJ.orchestrator.conversation_hardware import ConversationHardwareConfig, ConversationHardwareExecutor
    calls, contexts = [], []
    installed = HardwareProfile.load(Path(__file__).resolve().parents[2], 'rtase-v1', 'v1.0')
    def run(role, context):
        contexts.append(context)
        if role == 'lead' and not context['device_results']:
            assert not calls  # No startup connection check before scenario analysis.
            assert 'hardware_connections' not in context['inputs']
            assert {row['kind'] for row in context['inputs']['hardware_devices']} == {'bbb', 'scope', 'cpld'}
            assert context['inputs']['device_tools'] == ['check_connections']
            return {'status': 'device', 'summary': 'This scenario needs only BBB identity.',
                    'device_call': {'tool': 'check_connections', 'arguments': {'devices': ['bbb']}}}
        rows = context['inputs']['hardware_connections']['devices']
        assert len(rows) == 1 and rows[0]['kind'] == 'bbb'
        assert rows[0]['run'] == f'connection_bbb_{len(calls)}'
        if refresh and role == 'builder' and len(calls) == 1:
            return {'status': 'device', 'summary': 'Request fresh BBB identity for the requested recheck.',
                    'device_call': {'tool': 'check_connections', 'arguments': {'devices': ['bbb']}}}
        if role == 'verifier':
            return {'status': 'satisfied', 'summary': 'The requested BBB identity was observed.',
                    'runs': [rows[0]['run']]}
        return {'status': 'ready', 'summary': 'Reuse the observed BBB identity; no other device needed.'}
    _, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    executor = ConversationHardwareExecutor(ledger, installed,
        ConversationHardwareConfig(installed.project_root, tmp_path / 'runs'))
    def probe(kind):
        calls.append(kind)
        result = observation(kind)
        result['run'] += f'_{len(calls)}'
        (tmp_path / 'runs' / result['run']).mkdir(parents=True)
        return result
    executor.connections._probe = probe
    flow.runtime.device_executor = executor
    control.hardware_profile, control.hardware_enabled = installed, True
    scenario = 'Execution scope: hardware. Identify only the BBB.' + (' Recheck it once.' if refresh else '')
    job_id = control.mission('Identify BBB', scenario, hashlib.sha256(scenario.encode()).hexdigest())['job']['job_id']
    for _ in range(7 if refresh else 5):
        daemon.tick()
    assert [ctx['role'] for ctx in contexts] == ['lead', 'lead', 'builder'] + (['builder'] if refresh else []) + ['verifier']
    assert calls == ['bbb'] * (2 if refresh else 1)
    assert not [e for e in ledger.events_since(0, job_id) if e.kind == 'conversation.hardware.checked']
    assert ledger.job(job_id).outcome == 'satisfied'
    from QKD_PROJ.orchestrator.results import build_result
    result, artifacts = build_result(ledger, job_id, runs_dir=tmp_path / 'runs', project_root=tmp_path)
    assert result['artifacts'][0]['artifactId'] == f'run:connection_bbb_{len(calls)}'


def test_source_only_hardware_job_finishes_without_a_connection_check(tmp_path):
    from QKD_PROJ.tests.test_conversation_service import setup_flow
    from QKD_PROJ.orchestrator.conversation_hardware import ConversationHardwareConfig, ConversationHardwareExecutor
    installed = HardwareProfile.load(Path(__file__).resolve().parents[2], 'rtase-v1', 'v1.0')
    calls = []
    def run(role, context):
        assert 'hardware_connections' not in context['inputs'] and not context['device_results']
        return {'status': 'satisfied' if role == 'verifier' else 'ready', 'summary': 'Explained the existing source.'}
    _, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    executor = ConversationHardwareExecutor(ledger, installed,
        ConversationHardwareConfig(installed.project_root, tmp_path / 'runs'))
    executor.connections._probe = lambda kind: calls.append(kind) or observation(kind)
    flow.runtime.device_executor = executor
    control.hardware_profile, control.hardware_enabled = installed, True
    scenario = 'Execution scope: hardware. Explain the existing source; device access is unnecessary.'
    job_id = control.mission('Explain source', scenario, hashlib.sha256(scenario.encode()).hexdigest())['job']['job_id']
    for _ in range(3):
        daemon.tick()
    assert not calls and ledger.job(job_id).outcome == 'satisfied'


def test_http_commands_reach_the_real_daemon_without_waiting_for_an_agent_turn(profile, tmp_path):
    from QKD_PROJ.orchestrator.daemon import Daemon
    from QKD_PROJ.orchestrator.workflows import WorkflowRouter
    from QKD_PROJ.orchestrator.service import Service, ServiceConfig, SocketTransport
    from QKD_PROJ.tests.test_webui import _app, _client

    calls = []
    checker = HardwareConnections(profile, tmp_path / 'runs',
                                  probe=lambda kind: calls.append(kind) or observation(kind))
    ledger = JobLedger(tmp_path / 'http.db')
    service = Service(ledger, Daemon(ledger, WorkflowRouter(ledger)),
        config=ServiceConfig(install_signal_handlers=False, runtime_profile='hardware'),
        transport=SocketTransport(), hardware_profile=profile, hardware_connections=checker)
    with service:
        app, session = _app(service.transport.address)
        with _client(app, session) as http:
            # HTTP and pipe handlers run in different threads from this lock.
            with service._turn:
                status = http.post('/api/command', json={'type': 'hardware.status'})
                assert status.status_code == 200 and status.json()['inventory']['status'] == 'not_checked'
                assert not calls
                missing = http.post('/api/command', json={'type': 'hardware.check'})
                assert missing.status_code == 409 and not calls
                started = http.post('/api/command', json={'type': 'hardware.check', 'devices': ['scope']})
                assert started.status_code == 200
            finish(checker)
            checked = http.post('/api/command', json={'type': 'hardware.status'})
            assert checked.json()['inventory']['devices'][0]['status'] == 'not_checked'
            assert checked.json()['inventory']['devices'][1]['status'] == 'connected'
            assert calls == ['scope']
