"""User submission -> loop -> real approval gate. No model or hardware calls."""
import hashlib
from types import SimpleNamespace

import pytest
import yaml

from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.orchestrator.scenario_access import ScenarioAutoApprover, submission_access
from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
from QKD_PROJ.tests.test_scope_session_approval import setup_scope, require


def start(s, scenario='오실로스코프 CH3만 측정해줘', *, mode='hardware', consent=True, catalog=None):
    old = s.ledger.handoffs(s.origin.job_id, 'Brief', 0)[0].payload['resources']
    resources = {**old, 'execution_mode': mode}
    if consent:
        resources['scenario_access'] = submission_access(scenario, project_root=s.root,
            contracts_dir=s.controller.contracts_dir, board_catalog=catalog, now=lambda: s.clock[0])
    job, _ = start_simple_mission(s.ledger, scenario, scenario, resources=resources)
    s.ledger.transition(job.job_id, 'running')
    s.ledger.begin_iteration(job.job_id)
    approver = ScenarioAutoApprover(s.ledger, project_root=s.root, contracts_dir=s.controller.contracts_dir,
        runs_dir=s.root/'runs', enabled=True, now=lambda: s.clock[0])
    s.loop.scenario_approver = approver
    s.gate.scenario_authorizer = approver
    return s.ledger.job(job.job_id), approver


def test_submission_repeats_measurements_without_campaign_click(setup_scope):
    s = setup_scope
    job, policy = start(s)
    first = require(s, job)
    assert not first.blocked, first.detail
    approval = s.ledger.approval(first.approval_id)
    assert approval.decided_by == 'scenario:' + job.job_id
    assert not s.ledger.pending_approvals(job.job_id)
    assert s.gate(s.loop.agent('builder'), {'job_id': job.job_id, 'step': s.step}, 'measure_scope', {})
    # New step name, same user and device scope: no second manual review.
    second = require(s, job, {**s.step, 'id': 'remeasure'})
    assert not second.blocked
    assert s.gate(s.loop.agent('builder'), {'job_id': job.job_id, 'step': {**s.step, 'id': 'remeasure'}}, 'measure_scope', {})
    assert not s.controller.status()['active']
    assert not s.calls


@pytest.mark.parametrize('text', ['스코프 사용하지 말고 BBB만 확인해줘', 'BBB와 스코프를 비교만 해줘', 'BBB 프로그램해줘'])
def test_mentions_are_not_blanket_permission(setup_scope, text):
    s = setup_scope
    job, _ = start(s, text)
    assert require(s, job).blocked


def test_excluding_board_does_not_exclude_scope_in_the_next_clause(setup_scope):
    s = setup_scope
    job, _ = start(s, 'BBB는 건드리지 말고 오실로스코프 CH3만 측정해줘')
    assert not require(s, job).blocked


@pytest.mark.parametrize('mode,consent', [('offline', True), ('hardware', False)])
def test_offline_and_historical_jobs_do_not_gain_consent(setup_scope, mode, consent):
    s = setup_scope
    job, policy = start(s, mode=mode, consent=consent)
    assert not policy.applies(job.job_id)
    assert require(s, job).blocked


@pytest.mark.parametrize('scenario', [
    '오실로스코프로 측정해줘', '오실로스코프 CH3만 측정해줘',
    '오실로스코프 CH1, CH2만 사용해줘', '스코프 CH3에서만 측정해줘',
])
def test_scope_submission_delegates_device_not_channel_recipe(setup_scope, scenario):
    s = setup_scope
    job, policy = start(s, scenario)
    devices = policy.source(job.job_id)['devices']
    assert [device['kind'] for device in devices] == ['scope']
    assert devices[0]['channels'] == [1, 2, 3, 4]


@pytest.mark.parametrize('channels', [[1], [2], [3], [4], [1, 2, 3, 4]])
def test_new_contract_same_device_and_changed_channels_are_automatic(setup_scope, channels):
    s = setup_scope
    job, policy = start(s)
    source = s.controller.contracts_dir / s.step['contract']
    target = source.with_name('revised-scope.yaml')
    target.write_text(source.read_text() + '\n# new analysis requirements\n')
    revised = {**s.step, 'id': 'different-method', 'contract': target.name,
               'contract_sha256': sha256_file(target), 'artifacts': {}}
    assert not require(s, job, revised).blocked
    data = yaml.safe_load(target.read_text())
    channel = data['hardware_safety']['scope_channels'][0]
    data['hardware_safety']['scope_channels'] = [{**channel, 'channel': ch} for ch in channels]
    target.write_text(yaml.safe_dump(data))
    changed = {**revised, 'contract_sha256': sha256_file(target)}
    result = policy.evaluate(job, changed, s.loop.agent('builder'))
    assert result.allowed, result.reason
    changed['id'] = 'changed-channels'
    request = require(s, job, changed)
    assert not request.blocked, request.detail
    assert not s.ledger.pending_approvals(job.job_id)
    assert s.gate(s.loop.agent('builder'), {'job_id': job.job_id, 'step': changed}, 'measure_scope', {})
    assert not s.calls  # authorization check only: no physical acquisition


def test_expiry_cancel_and_offline_restart_close_issued_grant(setup_scope):
    s = setup_scope
    job, policy = start(s)
    request = require(s, job)
    actor, task = s.loop.agent('builder'), {'job_id': job.job_id, 'step': s.step}
    policy.enabled = False
    assert not s.gate(actor, task, 'measure_scope', {})
    policy.enabled = True
    s.clock[0] = '2030-01-01T00:00:00+00:00'
    assert not s.gate(actor, task, 'measure_scope', {})
    s.clock[0] = '2026-09-04T00:00:00+00:00'
    ControlPlane(s.ledger).cancel(job.job_id, 'human')
    assert s.ledger.approval(request.approval_id).state == 'withdrawn'
    assert not s.gate(actor, task, 'measure_scope', {})


def test_map_drift_and_denial_are_not_automatically_overridden(setup_scope):
    s = setup_scope
    job, policy = start(s)
    pending = s.ledger.request_approval(job.job_id, s.step['contract'], 'no', requested_by='builder', step_id=s.step['id'])
    s.ledger.decide_approval(pending.approval_id, False, 'human')
    assert not policy.evaluate(job, s.step, s.loop.agent('builder')).allowed
    path = s.controller.contracts_dir/'board-map.yaml'
    path.write_text(path.read_text() + '\n# changed endpoint binding\n')
    assert not policy.evaluate(job, {**s.step, 'id': 'other'}, s.loop.agent('builder')).allowed


def test_two_native_devices_can_share_submission_without_device_whitelist_steps(setup_scope, monkeypatch):
    s = setup_scope
    for key in ['BB_HOST', 'BB_PORT', 'BB_USER']:
        monkeypatch.delenv(key, raising=False)
    path = s.controller.contracts_dir/'board-map.yaml'
    data = yaml.safe_load(path.read_text())
    data.update(cpld={'device': 'MachXO2'}, bbb={'access': {'host': 'bbb-local', 'user': 'user', 'port': 22}})
    path.write_text(yaml.safe_dump(data))
    job, policy = start(s, 'BBB와 CPLD를 사용하고 오실로스코프로 측정해줘')
    for name, tool, ops in [('bbb.yaml', 'run_bbb', ['bbb_ssh_command']), ('scan.yaml', 'scan_jtag', ['jtag_scan'])]:
        contract = {'status': 'active', 'allowed_operations': ops,
                    'bbb': {'target': {'host': 'bbb-local', 'user': 'user', 'port': 22}}}
        file = s.controller.contracts_dir/name
        file.write_text(yaml.safe_dump(contract))
        step = {'id': tool, 'agent': 'builder', 'tool': tool, 'contract': name, 'contract_sha256': sha256_file(file), 'touches_hardware': True}
        evaluated = policy.evaluate(job, step, s.loop.agent('builder'))
        assert evaluated.allowed, evaluated.reason


def test_explicit_campaign_revocation_never_falls_back_to_submission(setup_scope):
    s = setup_scope
    job, policy = start(s)
    issued = require(s, job)
    s.ledger.append_event(job.job_id, 'campaign.deactivated', {'reason': 'human revoked'}, 'human')
    assert not policy.evaluate(job, s.step, s.loop.agent('builder')).allowed
    assert not s.gate(s.loop.agent('builder'), {'job_id': job.job_id, 'step': s.step}, 'measure_scope', {})


def test_configured_board_alias_matches_without_task_routing(setup_scope):
    s = setup_scope
    path = s.controller.contracts_dir/'board-map.yaml'
    catalog = SimpleNamespace(profiles=[SimpleNamespace(board_id='example', aliases=['내 개발보드'],
        board_map=path.relative_to(s.root).as_posix())])
    source = submission_access('내 개발보드 펌웨어를 작성하고 업로드해줘', project_root=s.root,
        contracts_dir=s.controller.contracts_dir, board_catalog=catalog, now=lambda: s.clock[0])
    assert [d['kind'] for d in source['devices']] == ['board']


def test_real_control_front_door_records_consent_not_model_resources(setup_scope):
    s = setup_scope
    control = ControlPlane(s.ledger, campaign_controller=s.controller,
        preflight_engine=ScenarioPreflight(s.controller.contracts_dir))
    scenario = 'Execution scope: hardware; submitting this scenario delegates control of the devices requested below, within their electrical safety bounds. Other devices are not authorized.\n\n오실로스코프 CH3만 측정해줘'
    response = control.legacy_mission(scenario, scenario, hashlib.sha256(scenario.encode()).hexdigest())
    job_id = response['job']['job_id']
    brief = s.ledger.handoffs(job_id, 'Brief', 0)[0].payload
    access = brief['resources']['scenario_access']
    assert access['source'] == 'user_scenario'
    assert access['devices'][0]['kind'] == 'scope'
    assert brief['resources']['approval_required'] is False
    # A bad client digest cannot mint source consent.
    with pytest.raises(ValueError):
        control.legacy_mission(scenario, scenario, '0'*64)


def test_unknown_scope_profiles_do_not_hide_the_real_scope(setup_scope):
    s = setup_scope
    unknown = s.root/'unknown-map.yaml'
    unknown.write_text('scope: {model: UNVERIFIED, host: UNVERIFIED, port: UNVERIFIED}')
    catalog = SimpleNamespace(profiles=[SimpleNamespace(board_id='new-board', aliases=['new board'], board_map=unknown.name)])
    source = submission_access('오실로스코프 측정해줘', project_root=s.root, contracts_dir=s.controller.contracts_dir,
        board_catalog=catalog, now=lambda: s.clock[0])
    assert len(source['devices']) == 1


def test_approval_budget_counts_consumed_calls_and_survives_recreation(setup_scope, monkeypatch):
    s = setup_scope
    job, policy = start(s)
    # Exercise the final issued grant: counting it must not invalidate its use.
    monkeypatch.setattr('QKD_PROJ.orchestrator.campaign.CampaignAutoApprover._grant_counts', lambda *_: (255, 255))
    result = require(s, job)
    assert not result.blocked
    monkeypatch.setattr('QKD_PROJ.orchestrator.campaign.CampaignAutoApprover._grant_counts', lambda *_: (256, 256))
    s.gate.scenario_authorizer = ScenarioAutoApprover(s.ledger, project_root=s.root,
        contracts_dir=s.controller.contracts_dir, runs_dir=s.root/'runs', enabled=True, now=lambda: s.clock[0])
    assert s.gate(s.loop.agent('builder'), {'job_id': job.job_id, 'step': s.step}, 'measure_scope', {})
    assert not policy.evaluate(job, {**s.step, 'id': 'after-limit'}, s.loop.agent('builder')).allowed
