"""Recorded diagnosis can finish on a device FAIL, never on invented evidence."""
import json

import pytest

from ai_hardware_autolab.hwtools.safety import Contracts, SafetyGate
from ai_hardware_autolab.hwtools.scope import identify_scope
from ai_hardware_autolab.hwtools.bbb import identify_bbb
from ai_hardware_autolab.hwtools.scpi import ScpiError
from ai_hardware_autolab.hwtools.instruments import RTM3004
from ai_hardware_autolab.orchestrator.diagnostics import complete_diagnostic
from ai_hardware_autolab.orchestrator.development import MODE, normalize_plan, validate_development_plan
from ai_hardware_autolab.orchestrator.execution_sources import unapplied_device_edits
from ai_hardware_autolab.orchestrator.results import build_result
from ai_hardware_autolab.agents.schema import ValidationError
from ai_hardware_autolab.tests.test_lightweight_execution import identity_setup, direct_task, no_network
from ai_hardware_autolab.tests.test_scope_session_approval import setup_scope, require
from ai_hardware_autolab.tests.test_scenario_access import start


def gate_for(s, step, allowed=True):
    return SafetyGate(Contracts.load(step['contract'], s.controller.contracts_dir), allow_hardware=allowed)


@pytest.mark.parametrize('phase', ['tcp_connect', 'scpi_identification', 'model_validation', 'option_query'])
def test_scope_records_exact_failure_phase_without_settings(setup_scope, monkeypatch, phase):
    s = setup_scope
    steps, _, _, _ = identity_setup(s, monkeypatch)
    commands = []
    scope = RTM3004(host='fake', mock=True)
    def query(command):
        commands.append(command)
        if phase == 'scpi_identification' or command == '*OPT?':
            raise ScpiError('timed out waiting for reply')
        return 'maker,' + ('wrong' if phase == 'model_validation' else 'RTM3004') + ',serial,firmware'
    scope.scpi.query = query
    def factory(**kwargs):
        if phase == 'tcp_connect':
            raise TimeoutError('no TCP response')
        return scope
    monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', factory)
    result = identify_scope(gate_for(s, steps[1]))
    assert result.verdict == 'FAIL'
    assert result.measurements['failure_stage'] == phase
    assert result.measurements['failure_error']
    assert result.measurements['diagnostic_complete'] is True
    assert commands == ([] if phase == 'tcp_connect' else ['*IDN?', '*OPT?'] if phase == 'option_query' else ['*IDN?'])


@pytest.mark.parametrize('failure', ['authorization', 'internal', 'cleanup'])
def test_guard_and_internal_errors_are_not_completed_diagnoses(setup_scope, monkeypatch, failure):
    s = setup_scope
    steps, _, _, _ = identity_setup(s, monkeypatch)
    scope = RTM3004(host='fake', mock=True, mock_responses={'*IDN?': 'maker,RTM3004,s,f', '*OPT?': ''})
    def error(*args, **kwargs):
        raise AttributeError('broken driver')
    if failure == 'internal':
        monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', error)
    elif failure == 'cleanup':
        scope.close = error
        monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', lambda **kwargs: scope)
    gate = gate_for(s, steps[1])
    if failure == 'authorization':
        gate._forbidden.add('scope_identify')
    result = identify_scope(gate)
    assert result.verdict == 'FAIL'
    assert result.measurements['diagnostic_complete'] is False


@pytest.mark.parametrize('failure', ['credentials', 'ssh', 'model', 'kernel'])
def test_bbb_failure_stage_and_redaction(setup_scope, monkeypatch, failure):
    s = setup_scope
    steps, _, ssh, _ = identity_setup(s, monkeypatch)
    if failure == 'credentials':
        monkeypatch.delenv('BB_PW')
    elif failure == 'ssh':
        from ai_hardware_autolab.hwtools.ssh import SshError
        from ai_hardware_autolab.tests.test_bbb_connection_binding import SECRET
        def error(*args, **kwargs):
            raise SshError('authentication failed: ' + SECRET)
        monkeypatch.setattr('ai_hardware_autolab.hwtools.ssh.SshTransport.connect', error)
    else:
        output = 'wrong board\nkernel\n' if failure == 'model' else 'TI AM335x BeagleBone Black\n'
        ssh.rules = [('/proc/device-tree/model', (0, output, ''))]
    result = identify_bbb(gate_for(s, steps[0]))
    assert result.verdict == 'FAIL'
    assert result.measurements['diagnostic_complete'] is (failure != 'credentials')
    expected = 'connection_configuration' if failure == 'credentials' else 'ssh_connection_or_authentication' if failure == 'ssh' else 'model_and_kernel_read'
    assert result.measurements['failure_stage'] == expected
    from ai_hardware_autolab.tests.test_bbb_connection_binding import SECRET
    assert SECRET not in str(result.measurements) + str(result.error)


@pytest.mark.parametrize('review_accepts', [True, False])
def test_report_finishes_once_with_real_scope_fail_and_bbb_pass(setup_scope, monkeypatch, review_accepts):
    from dataclasses import replace
    s = setup_scope
    s.loop.policy = replace(s.loop.policy, max_iterations=1)
    steps, runtime, ssh, _ = identity_setup(s, monkeypatch)
    for step in steps:
        step['completion'] = 'diagnostic_report'
        step.pop('advance_when')
    attempts = []
    def unavailable(**kwargs):
        attempts.append(1)
        raise TimeoutError('no TCP response')
    monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', unavailable)
    job, _ = start(s, 'BBB와 스코프 연결 상태만 확인하고 실패하면 어느 단계인지 보고하고 끝내줘')
    s.loop.resource_refresher = lambda scenario, r: {**r, 'available_tools': ['run_bbb', 'measure_scope', 'codex_workspace']}
    models = []
    def runner(agent, task):
        if agent.name == 'builder':
            return runtime(agent, task)
        models.append(agent.name)
        if agent.name == 'lead-agent':
            return {'kind': 'Plan', 'agent': agent.name, 'objective': task['scenario'], 'requirements_complete': True, 'steps': steps}
        results = task['step_results']
        assert [r['verdict'] for r in results] == ['PASS', 'FAIL']
        assert all(r['status'] == 'done' and r['diagnostic_report']['complete'] for r in results)
        assert 'no TCP response' in results[1]['execution_errors'][0]['error']
        return {'requirements_met': review_accepts, 'analysis': '진단과 원래 요청을 독립적으로 비교했습니다.'}
    s.loop.runner = runner
    s.loop.run(job.job_id)
    assert s.ledger.job(job.job_id).outcome == ('satisfied' if review_accepts else 'exhausted')
    assert s.ledger.job(job.job_id).iteration == 1
    assert attempts == [1] and len(ssh.scripts) == 1
    assert models == ['lead-agent', 'verifier'] and not runtime.client.thread_params
    results = [h.payload for h in s.ledger.handoffs(job.job_id, 'StepResult')]
    scope_run = s.root / 'runs' / results[1]['runs'][0] / 'manifest.json'
    assert json.loads(scope_run.read_text())['verdict'] == 'FAIL'
    payload, _ = build_result(s.ledger, job.job_id, runs_dir=s.root/'runs', project_root=s.root)
    assert ('진단 완료' if review_accepts else '진단 결과') in payload['summary']
    assert 'scope: FAIL (tcp_connect)' in payload['summary']


def test_connection_success_request_keeps_failed_status(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, _, _ = identity_setup(s, monkeypatch)
    job, _ = start(s, '스코프를 연결해서 정상 동작하게 고쳐줘')
    def unavailable(**kwargs):
        raise TimeoutError('no TCP response')
    monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', unavailable)
    assert not require(s, job, steps[1]).blocked
    result = runtime(s.loop.agent('builder'), direct_task(s, job, steps[1]))
    assert result['status'] == 'failed' and result['verdict'] == 'FAIL'
    assert result['diagnostic_report']['complete'] is False


@pytest.mark.parametrize('changes', [{'tool': 'codex_workspace'}, {'tool_args': {'identity_only': False}}, {'entrypoint': 'custom.py:run'}])
def test_report_mode_cannot_relabel_mutating_operations(changes):
    step = {'id': 'id', 'agent': 'builder', 'tool': 'measure_scope', 'contract': 'scope.yaml',
            'touches_hardware': True, 'completion': 'diagnostic_report', 'tool_args': {'identity_only': True}, **changes}
    resources = {'development_mode': MODE, 'execution_mode': 'hardware'}
    plan = normalize_plan({'kind': 'Plan', 'agent': 'lead-agent', 'gate': '__mission__', 'iteration': 1,
        'objective': 'diagnose', 'requirements_complete': True, 'steps': [step]}, resources)
    with pytest.raises(ValidationError):
        validate_development_plan(plan, resources, available_capabilities=[])


@pytest.mark.parametrize('rows', [[], [None], [{'measurements': 'bad'}], [{'tool': 'scope', 'action': 'identify',
    'verdict': 'FAIL', 'measurements': {'identity_only': True, 'diagnostic_complete': True}}]])
def test_incomplete_evidence_is_not_a_diagnosis(rows):
    assert not complete_diagnostic({'steps': rows})


def test_explicit_success_condition_is_not_removed_for_report_mode():
    condition = {'field': 'measurements.identity_verified', 'expect': True, 'source': 'user requires connection'}
    plan = {'steps': [{'tool': 'measure_scope', 'completion': 'diagnostic_report',
                      'tool_args': {'identity_only': True}, 'advance_when': [condition]}]}
    result = normalize_plan(plan, {'development_mode': MODE})
    assert result['steps'][0]['advance_when'] == [condition]


def test_unapplied_driver_stops_before_authorization_or_device(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, ssh, scopes = identity_setup(s, monkeypatch)
    job, _ = start(s, '스코프 연결을 확인해줘')
    workspace = s.root / 'isolated'
    path = 'ai_hardware_autolab/hwtools/scope.py'
    for root, code in [(s.root, 'installed'), (workspace, 'fixed in isolation')]:
        source = root/path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(code)
    runtime.workspace_resolver = lambda task: workspace
    def forbidden(*args):
        pytest.fail('source guard must precede hardware approval callback')
    runtime.hardware_approval = forbidden
    task = direct_task(s, job, steps[1])
    task['unpublished_platform_paths'] = [path]
    result = runtime(s.loop.agent('builder'), task)
    assert result['status'] == 'blocked' and result['repair_target'] == 'platform_deployment'
    assert result['runs'] == [] and result['hardware_contacted'] is False
    assert {row['path'] for row in result['unapplied_sources']} == {path}
    assert not scopes and not ssh.scripts and not runtime.client.thread_params


def test_source_guard_ignores_tests_other_device_and_identical_copy(tmp_path):
    workspace, installed = tmp_path/'workspace', tmp_path/'host'
    paths = ['ai_hardware_autolab/tests/test_scope.py', 'ai_hardware_autolab/hwtools/bbb.py', 'ai_hardware_autolab/hwtools/scope.py']
    for path in paths:
        for root in [workspace, installed]:
            file = root/path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('same' if path.endswith('/scope.py') else str(root))
    task = {'step': {'tool': 'measure_scope'}, 'unpublished_platform_paths': paths}
    assert unapplied_device_edits(task, workspace, installed) == []
    (workspace/paths[-1]).write_text('new driver')
    assert [row['path'] for row in unapplied_device_edits(task, workspace, installed)] == [paths[-1]]


def test_loop_carries_audited_edits_and_stops_without_retry(setup_scope, monkeypatch):
    s = setup_scope
    steps, runtime, _, scopes = identity_setup(s, monkeypatch)
    path = 'ai_hardware_autolab/hwtools/scope.py'
    workspace = s.root/'isolated'
    for root, code in [(s.root, 'installed'), (workspace, 'workspace fix')]:
        source = root/path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(code)
    runtime.workspace_resolver = lambda task: workspace
    job, _ = start(s, '스코프 연결 오류를 수정하고 재확인해줘')
    s.loop.resource_refresher = lambda scenario, r: {**r, 'available_tools': ['measure_scope', 'codex_workspace']}
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        if agent.name == 'lead-agent':
            return {'kind': 'Plan', 'agent': agent.name, 'objective': task['scenario'], 'requirements_complete': True,
                'steps': [{'id': 'fix', 'tool': 'codex_workspace', 'instructions': 'fix isolated driver'}, steps[1]]}
        assert agent.name == 'builder'
        if task['step']['id'] == 'fix':
            run = s.root/'runs/fix-run'
            run.mkdir(parents=True)
            (run/'manifest.json').write_text(json.dumps({'verdict': 'PASS', 'steps': [
                {'tool': 'codex_workspace', 'measurements': {'execution_integrity': True}}]}))
            return {'status': 'done', 'runs': ['fix-run'], 'workspace_changes': {'changes': [{'path': path}]}}
        assert task['unpublished_platform_paths'] == [path]
        return runtime(agent, task)
    s.loop.runner = runner
    s.loop.run(job.job_id)
    assert s.ledger.job(job.job_id).state == 'blocked'
    assert not scopes and not runtime.client.thread_params
    last = s.ledger.handoffs(job.job_id, 'StepResult')[-1].payload
    assert last['repair_target'] == 'platform_deployment' and last['runs'] == []
    previous_calls = list(calls)
    assert s.loop.advance(job.job_id).blocked
    assert calls == previous_calls
