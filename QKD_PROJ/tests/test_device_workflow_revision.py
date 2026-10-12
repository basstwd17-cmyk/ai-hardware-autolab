"""No model/hardware calls: exercise actual compiler, authority and runtime seams."""
import copy
import json
from dataclasses import replace

import pytest
import yaml

from QKD_PROJ.agents.schema import load_simple_team
from QKD_PROJ.hwtools.result import sha256_file, ToolResult
from QKD_PROJ.hwtools.record import RunRecord
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate
from QKD_PROJ.mcp_server import registry
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler
from QKD_PROJ.orchestrator.campaign_proposal import CampaignProposalService, CampaignProposalError
from QKD_PROJ.orchestrator.daemon import HardwareApprovalGate
from QKD_PROJ.orchestrator.device_evidence import recover_completed_device
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.tests.test_autonomous_development import plan, boundary, resources
from QKD_PROJ.tests.test_campaign_proposal import _environment, NOW
from QKD_PROJ.tests.test_autonomous_flow_review import event_for, make_loop


def multiple_devices(root):
    ledger, _, controller = _environment(root)
    contracts = controller.contracts_dir
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025}}))
    jtag = {'status': 'proposed_hardware_boundary', 'allowed_operations': ['jtag_scan'],
            'hardware_safety': {'limits': [{'parameter': 'voltage', 'maximum': 3.3, 'unit': 'V'}],
                                'pin_assignments': [{'pin': 'TCK', 'direction': 'output'}],
                                'stop_conditions': ['unexpected chain identity']}}
    (contracts/'jtag.yaml').write_text(yaml.safe_dump(jtag))
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['measure_scope', 'scan_jtag']}
    job, _ = start_simple_mission(ledger, 'scan and capture', 'scan the chain and measure CH3', resources=r)
    ledger.transition(job.job_id, 'running')
    ledger.begin_iteration(job.job_id)
    p = plan()
    p['hardware_contract'] = boundary()
    p['steps'][0].update(id='capture', tool='measure_scope', touches_hardware=True)
    p['steps'].append({'id': 'scan', 'tool': 'scan_jtag', 'agent': 'builder', 'touches_hardware': True,
                       'contract': 'jtag.yaml', 'instructions': 'scan chain', 'advance_when': []})
    compiled = ContractCompiler(contracts, project_root=root).compile(p, job_id=job.job_id,
        iteration=1, scenario_sha256='', resources=r)
    ledger.record_handoff(job.job_id, 'Plan', 'lead-agent', compiled.plan, iteration=1)
    service = CampaignProposalService(ledger, controller, project_root=root, contracts_dir=contracts,
                                     campaigns_dir=controller.campaigns_dir, now=lambda: NOW)
    return ledger, job, controller, service, compiled, p, r


def test_one_review_pins_multiple_devices_without_overwriting_existing_contract(tmp_path):
    ledger, job, controller, service, compiled, _, _ = multiple_devices(tmp_path)
    assert compiled.plan['steps'][1]['contract'] == 'jtag.yaml'
    preview = service.preview(job.job_id)
    assert preview.ready, preview.blockers
    assert len(preview.boundaries) == 2
    assert [p['tool'] for p in preview.permissions] == ['measure_scope', 'scan_jtag']
    assert preview.boundaries[1]['board'] is None
    service.approve(job.job_id, 'human', preview.review_sha256)
    builder = next(a for a in load_simple_team() if a.name == 'builder')
    approver = controller.execution_loop.auto_approver
    for step in compiled.plan['steps']:
        assert approver.evaluate(ledger.job(job.job_id), step, builder).allowed
        assert approver.evaluate(ledger.job(job.job_id), {**step, 'id': 'replanned-step'}, builder).allowed
    # A grant for the scope is not a grant for scanning through that contract.
    wrong = {**compiled.plan['steps'][0], 'tool': 'scan_jtag'}
    assert not approver.evaluate(ledger.job(job.job_id), wrong, builder).allowed
    assert len(controller.hardware_gate.active_campaign_contracts) == 2
    (controller.contracts_dir/'board-map.yaml').write_text('scope: {host: changed}\n')
    assert not approver.evaluate(ledger.job(job.job_id), compiled.plan['steps'][0], builder).allowed


def test_second_device_drift_invalidates_whole_review(tmp_path):
    ledger, job, controller, service, _, _, _ = multiple_devices(tmp_path)
    preview = service.preview(job.job_id)
    target = controller.contracts_dir/'jtag.yaml'
    target.write_text(target.read_text() + '\n# changed\n')
    changed = service.preview(job.job_id)
    assert not changed.ready
    with pytest.raises(CampaignProposalError):
        service.approve(job.job_id, 'human', preview.review_sha256)
    assert not controller.status()['active']


def test_named_proposals_bind_only_explicit_references_and_device_maps(tmp_path):
    _, job, controller, _, _, p, r = multiple_devices(tmp_path)
    p.pop('hardware_contract')
    second = boundary()
    second['hardware_safety']['devices'][0]['host'] = 'second-scope'
    second['device_map'] = 'scope-two.yaml'
    (tmp_path/'scope-two.yaml').write_text(yaml.safe_dump({'scope': {'host': 'second-scope', 'model': 'RTM3004', 'port': 5025}}))
    p['hardware_boundaries'] = {'first': boundary(), 'second': second}
    p['steps'][0]['contract'] = '$first'
    p['steps'].append({**p['steps'][0], 'id': 'second-capture', 'contract': '$second'})
    compiled = ContractCompiler(controller.contracts_dir, project_root=tmp_path).compile(
        p, job_id=job.job_id, iteration=1, scenario_sha256='', resources=r)
    names = [step['contract'] for step in compiled.plan['steps']]
    assert len(set(names)) == 3 and names[1] == 'jtag.yaml'
    assert 'scope-two.yaml' in compiled.plan['steps'][2]['artifacts']


@pytest.mark.parametrize('verdict', ['PASS', 'FAIL'])
def test_builder_device_recipe_runs_actual_driver_and_preserves_failure(tmp_path, monkeypatch, verdict):
    ledger, job, controller, service, compiled, _, _ = multiple_devices(tmp_path)
    source = tmp_path/'scan.py'
    source.write_text("def execute(device, params):\n    result = device.execute(record_name='selected_by_builder')\n    result['run_verdict'] = 'PASS' # cannot rewrite the host result\n")
    observed = []
    def driver(policy, args):
        observed.append(args)
        gate = registry._gate(policy, args['contract'])
        record = RunRecord('device', gate, runs_dir=policy.runs_dir)
        result = ToolResult(tool='jtag', action='scan', ok=verdict == 'PASS', verdict=verdict,
                            measurements={'chain_matches': verdict == 'PASS'})
        record.add(result)
        return registry._result_payload(record, result)
    monkeypatch.setattr(registry, '_scan_jtag', driver)
    policy = registry.ServerPolicy(role='builder', allow_hardware=True, project_root=tmp_path,
        contracts_dir=controller.contracts_dir, runs_dir=tmp_path/'runs', hardware_tool='scan_jtag',
        hardware_contract='jtag.yaml', hardware_contract_sha256=sha256_file(controller.contracts_dir/'jtag.yaml'),
        workspace_recipe='scan.py:execute', workspace_recipe_sha256=sha256_file(source))
    tool = next(t for t in registry.build_tools(policy) if t.name == 'scan_jtag')
    result = tool.handler({'contract': 'jtag.yaml'})
    assert result['run_verdict'] == verdict
    assert observed == [{'record_name': 'selected_by_builder', 'contract': 'jtag.yaml'}]
    assert result['result']['measurements']['chain_matches'] is (verdict == 'PASS')
    assert (tmp_path/'runs'/result['run']/'executed_recipe.py').read_bytes() == source.read_bytes()
    assert result['device_result']['run'] != result['run']


def test_recipe_cannot_switch_boundary_and_uncertain_action_does_not_auto_retry(tmp_path):
    _, _, controller, _, _, _, _ = multiple_devices(tmp_path)
    from QKD_PROJ.hwtools.workspace_recipe import execute_device_recipe
    source = tmp_path/'action.py'
    source.write_text("def execute(device, params):\n    device.execute(contract='another.yaml')\n")
    policy = registry.ServerPolicy(role='builder', allow_hardware=True, project_root=tmp_path,
        contracts_dir=controller.contracts_dir, runs_dir=tmp_path/'runs', hardware_tool='scan_jtag',
        hardware_contract='jtag.yaml', hardware_contract_sha256=sha256_file(controller.contracts_dir/'jtag.yaml'),
        workspace_recipe='action.py:execute', workspace_recipe_sha256=sha256_file(source))
    calls = []
    def disconnected(args):
        calls.append(args)
        raise OSError('device disconnected after send')
    denied = execute_device_recipe(policy, {'contract': 'jtag.yaml'}, disconnected)
    assert not calls and denied['run_verdict'] == 'FAIL'
    source.write_text("def execute(device, params):\n    device.execute()\n")
    policy = replace(policy, workspace_recipe_sha256=sha256_file(source))
    failed = execute_device_recipe(policy, {'contract': 'jtag.yaml'}, disconnected)
    assert len(calls) == 1 and failed['result']['measurements']['hardware_state_unknown'] is True


def test_offline_code_error_is_returned_to_lead_not_a_permanent_block(tmp_path):
    from QKD_PROJ.orchestrator.jobs import JobLedger
    ledger = JobLedger(tmp_path/'jobs.db')
    calls = []
    def runner(agent, task):
        calls.append((agent.name, task))
        if agent.name == 'lead-agent': return {**plan(), 'iteration': task['iteration']}
        if agent.name == 'builder': raise FileNotFoundError('new/helper.py')
        return {'requirements_met': False, 'analysis': 'repair missing helper', 'recommended_next_action': 'create helper'}
    job, _ = start_simple_mission(ledger, 'code', 'implement code', resources=resources())
    loop = make_loop(ledger, runner, tmp_path)
    for _ in range(5): loop.advance(job.job_id)
    latest = [t for name, t in calls if name == 'lead-agent'][-1]
    assert 'new/helper.py' in latest['previous_step_results'][0]['summary']
    assert ledger.job(job.job_id).state == 'running'
    loop.advance(job.job_id)
    retried = [t for name, t in calls if name == 'builder'][-1]
    assert retried['iteration'] == 2
    assert 'new/helper.py' in retried['previous_iteration_results'][0]['summary']
    assert retried['previous_verifier_review']['recommended_next_action'] == 'create helper'


def test_final_reply_recovery_uses_complete_runs_not_pending_device_calls(tmp_path):
    run = tmp_path/'runs/one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'contract': {'test_spec': 'scope.yaml'}, 'verdict': 'PASS'}))
    task = {'step': {'id': 'capture', 'tool': 'measure_scope', 'contract': 'scope.yaml'}}
    event = event_for({'run': 'one', 'run_dir': str(run)})
    recovered = recover_completed_device(task, [event], run.parent, 'bad final JSON')
    assert recovered['runs'] == ['one'] and recovered['status'] == 'done'
    pending = copy.deepcopy(event)
    pending['method'] = 'item/started'
    pending['params']['item']['id'] = 'unfinished'
    assert recover_completed_device(task, [event, pending], run.parent, 'timeout') is None


def test_scope_subset_does_not_require_unrelated_channel_samples(tmp_path):
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025}}))
    b = boundary()
    b['status'] = 'proposed_hardware_boundary'
    b['hardware_safety']['scope_channels'].append({**b['hardware_safety']['scope_channels'][0], 'channel': 4, 'signal': 'other'})
    (contracts/'scope.yaml').write_text(yaml.safe_dump(b))
    gate = SafetyGate(Contracts.load('scope.yaml', contracts), allow_hardware=True)
    gate.require_scope_capture({'channels': [{'channel': 3, 'signal': 'test'}]})
    gate.require_measured_voltage({'CH3_min_v': 0, 'CH3_max_v': 3.3})


def test_pending_request_is_not_a_completed_step_and_resume_executes_it(tmp_path):
    from QKD_PROJ.orchestrator.jobs import JobLedger
    ledger = JobLedger(tmp_path/'jobs.db')
    calls = []
    def runner(agent, task):
        if agent.name == 'lead-agent': return plan()
        calls.append(agent.name)
        return {'status': 'needs_approval', 'runs': [], 'notes': 'human input needed'}
    job, _ = start_simple_mission(ledger, 'code', 'implement code', resources=resources())
    loop = make_loop(ledger, runner, tmp_path)
    loop.advance(job.job_id)
    result = loop.advance(job.job_id)
    assert result.blocked and not ledger.handoffs(job.job_id, 'StepResult')
    ledger.decide_approval(result.approval_id, granted=True, decided_by='human')
    loop.advance(job.job_id)
    assert loop.phase(job.job_id) == 'execute'


def test_mutable_workspace_map_cannot_redirect_trusted_device_connection(tmp_path):
    trusted, workspace = tmp_path/'trusted', tmp_path/'workspace'
    contracts = trusted/'contracts'
    contracts.mkdir(parents=True)
    workspace.mkdir()
    (trusted/'device.yaml').write_text('scope: {host: real-scope}\n')
    (workspace/'device.yaml').write_text('scope: {host: other-scope}\n')
    (contracts/'scope.yaml').write_text('device_map: device.yaml\n')
    policy = registry.ServerPolicy(project_root=workspace, device_root=trusted, contracts_dir=contracts)
    assert registry._gate(policy, 'scope.yaml').contracts.board_map['scope']['host'] == 'real-scope'


def test_live_runner_recovers_completed_hardware_without_replaying_it(tmp_path):
    from QKD_PROJ.tests.test_codex_runner import FakeClient
    from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig
    contracts = tmp_path/'QKD_PROJ/contracts'
    contracts.mkdir(parents=True)
    (tmp_path/'.qkd-job-workspace.json').write_text(json.dumps({'job_id': 'j'}))
    (contracts/'scope.yaml').write_text('allowed_operations: [oscilloscope_capture]\n')
    run = tmp_path/'runs/one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'contract': {'test_spec': 'scope.yaml'}, 'verdict': 'PASS'}))
    calls = []
    class Client(FakeClient):
        def run_turn(self, thread_id, params, *, server_request):
            calls.append(thread_id)
            event = event_for({'run': 'one', 'run_dir': str(run)})
            event['params']['threadId'] = thread_id
            runner._capture_event(event)
            return 'final JSON was truncated {'
    client = Client({})
    runner = CodexAgentRunner(config=CodexRunnerConfig(profile='hardware', project_root=tmp_path,
        qkd_root=tmp_path/'QKD_PROJ', runs_dir=run.parent), client=client, hardware_approval=lambda *_: True)
    task = {'job_id': 'j', 'resources': {**resources(), 'execution_mode': 'hardware'},
            'step': {'id': 'capture', 'tool': 'measure_scope', 'contract': 'scope.yaml', 'touches_hardware': True}}
    answer = runner(next(a for a in load_simple_team() if a.name == 'builder'), task)
    assert answer['runs'] == ['one'] and answer['status'] == 'done' and len(calls) == 1


def test_one_approval_modified_workspace_recipe_then_device_retry(tmp_path, monkeypatch):
    ledger, job, controller, service, compiled, _, _ = multiple_devices(tmp_path)
    preview = service.preview(job.job_id)
    service.approve(job.job_id, 'human', preview.review_sha256)
    authority = HardwareApprovalGate(ledger, contracts_dir=controller.contracts_dir, project_root=tmp_path, now=lambda: NOW)
    authority.active_campaign_sha256 = controller.active_policy.sha256
    authority.active_campaign_contracts = dict(controller.hardware_gate.active_campaign_contracts)
    step = {**compiled.plan['steps'][1], 'entrypoint': 'scan.py:execute'}
    builder = next(a for a in load_simple_team() if a.name == 'builder')
    outcomes = []
    def driver(policy, args):
        gate = registry._gate(policy, args['contract'])
        record = RunRecord('actual-driver', gate, runs_dir=policy.runs_dir)
        passed = args.get('record_name') == 'corrected'
        result = ToolResult(tool='jtag', action='scan', ok=passed, verdict='PASS' if passed else 'FAIL',
                            error=None if passed else 'wrong test configuration', measurements={'chain_matches': passed})
        record.add(result)
        return registry._result_payload(record, result)
    monkeypatch.setattr(registry, '_scan_jtag', driver)
    for attempt, label in enumerate(['initial', 'corrected'], 1):
        if attempt > 1: ledger.begin_iteration(job.job_id)
        # Only implementation bytes change, not device authority.
        source = tmp_path/'scan.py'
        source.write_text(f"def execute(device, params):\n    device.execute(record_name='{label}')\n")
        evaluation = controller.execution_loop.auto_approver.evaluate(ledger.job(job.job_id), step, builder)
        assert evaluation.allowed
        approval = ledger.request_approval(job.job_id, step['contract'], 'scan', artifacts=evaluation.artifacts,
            requested_by='builder', step_id=step['id'], expires_at=evaluation.expires_at)
        controller.execution_loop.auto_approver.apply(approval, evaluation)
        assert authority(builder, {'job_id': job.job_id, 'step': step}, 'scan_jtag', {})
        policy = registry.ServerPolicy(role='builder', allow_hardware=True, project_root=tmp_path,
            contracts_dir=controller.contracts_dir, runs_dir=tmp_path/'runs', hardware_tool='scan_jtag',
            hardware_contract=step['contract'], hardware_contract_sha256=step['contract_sha256'],
            workspace_recipe=step['entrypoint'], workspace_recipe_sha256=sha256_file(source))
        tool = next(t for t in registry.build_tools(policy) if t.name == 'scan_jtag')
        outcomes.append(tool.handler({'contract': step['contract']}))
    assert [item['run_verdict'] for item in outcomes] == ['FAIL', 'PASS']
    assert outcomes[0]['run'] != outcomes[1]['run']
    assert outcomes[0]['result']['measurements']['executed_source_sha256'] != outcomes[1]['result']['measurements']['executed_source_sha256']
    assert sum(e.kind == 'campaign.activated' for e in ledger.events_since(0, job.job_id)) == 1


def test_bbb_environment_cannot_redirect_approved_connection(tmp_path, monkeypatch):
    from QKD_PROJ.mcp_server.protocol import ToolError
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    (contracts/'board-map.yaml').write_text('{}')
    (contracts/'bbb.yaml').write_text('bbb: {target: {host: approved-board, user: debian, port: 22}}')
    for key, value in {'BB_HOST': 'other-board', 'BB_USER': 'debian', 'BB_PW': 'test-only', 'BB_PORT': '22'}.items():
        monkeypatch.setenv(key, value)
    policy = registry.ServerPolicy(project_root=tmp_path, contracts_dir=contracts, hardware_tool='run_bbb',
        hardware_contract='bbb.yaml', hardware_contract_sha256=sha256_file(contracts/'bbb.yaml'))
    gate = registry._gate(policy, 'bbb.yaml')
    with pytest.raises(ToolError, match='no device contacted'):
        registry._check_bbb_target(policy, gate)
    monkeypatch.setenv('BB_HOST', 'approved-board')
    registry._check_bbb_target(policy, gate)


def test_mobile_review_includes_all_scopes_and_escapes_untrusted_text():
    from QKD_PROJ.webui.backend.app import _remote_html
    from QKD_PROJ.webui.backend.remote_approval import RemoteApproval
    row = RemoteApproval('token-digest', 'job1', 'a'*64, {
        'boundaries': [{'contract': 'first.yaml', 'hardware_safety': {'limits': ['first-bound']}},
                       {'contract': 'second.yaml', 'hardware_safety': {'scope_channels': ['<script>second-bound</script>']}}]},
        8, 24, 12, 9999999999)
    rendered = _remote_html('', row=row).body.decode('utf-8')
    assert 'first-bound' in rendered and 'second-bound' in rendered
    assert '<script>second-bound</script>' not in rendered
