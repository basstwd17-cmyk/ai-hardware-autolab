"""Production loop/broker connections with a mock device, no LLM or hardware."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from ai_hardware_autolab.agents.schema import load_simple_team
from ai_hardware_autolab.hwtools.result import sha256_file
from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_tools
from ai_hardware_autolab.orchestrator.campaign import CampaignController
from ai_hardware_autolab.orchestrator.campaign_proposal import CampaignProposalService
from ai_hardware_autolab.orchestrator.contract_compiler import ContractCompiler, ContractCompileError
from ai_hardware_autolab.orchestrator.daemon import HardwareApprovalGate
from ai_hardware_autolab.orchestrator.development import MODE
from ai_hardware_autolab.orchestrator.device_evidence import bind_device_evidence
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.loop import Loop, LoopPolicy
from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission, hardware_completion_gap
from ai_hardware_autolab.tests.test_autonomous_development import boundary, plan, resources


def make_loop(ledger, runner, root, **kwargs):
    return Loop(ledger, runner, runs_dir=root/'runs', agents=load_simple_team(),
        planner_agent='lead-agent', verifier_agent='verifier',
        policy=LoopPolicy(max_iterations=2, report_on_finish=False),
        require_plan_governance=True, repair_invalid_plans=True, reasoning_verifier=True,
        completion_guard=hardware_completion_gap,
        contract_compiler=ContractCompiler(root/'contracts', project_root=root), **kwargs)


def test_last_allowed_iteration_runs_and_review_does_not_echo_the_table(tmp_path):
    ledger = JobLedger(tmp_path/'jobs.db')
    run = tmp_path/'runs/run-one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'verdict': 'PASS', 'steps': [
        {'tool': 'codex_workspace', 'measurements': {'last_command_exit_code': 0}}]}))
    calls = []
    def runner(agent, task):
        calls.append((agent.name, task['iteration']))
        if agent.name == 'lead-agent':
            if task['iteration'] == 2:
                assert task['previous_plan']['iteration'] == 1
                assert task['previous_plan']['objective'] == plan()['objective']
                assert task['previous_plan']['steps'][0]['id'] == 'implement'
            return {**plan(), 'iteration': task['iteration']}
        if agent.name == 'builder':
            return {'kind': 'StepResult', 'status': 'done', 'runs': ['run-one']}
        return {'requirements_met': task['iteration'] == 2, 'analysis': 'reviewed actual implementation',
                'recommended_next_action': 'fix the edge case' if task['iteration'] == 1 else ''}
    job, _ = start_simple_mission(ledger, 'code', 'implement the requested behavior', resources=resources())
    loop = make_loop(ledger, runner, tmp_path)
    loop.run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied'
    assert calls == [(name, iteration) for iteration in (1, 2) for name in ('lead-agent', 'builder', 'verifier')]


@pytest.mark.parametrize('bad_review', [{'requirements_met': 'true'}, {'requirements_met': True, 'outcome': 'fabricated'}])
def test_review_format_repair_does_not_repeat_builder(tmp_path, bad_review):
    ledger = JobLedger(tmp_path/'jobs.db')
    run = tmp_path/'runs/run-one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'verdict': 'PASS', 'steps': [
        {'tool': 'codex_workspace', 'measurements': {'last_command_exit_code': 0}}]}))
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        if agent.name == 'lead-agent': return plan()
        if agent.name == 'builder': return {'status': 'done', 'runs': ['run-one']}
        if 'format_correction' not in task: return bad_review
        return {'requirements_met': True, 'analysis': 'checked current run and changed source'}
    job, _ = start_simple_mission(ledger, 'code', 'implement the requested behavior', resources=resources())
    make_loop(ledger, runner, tmp_path).run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied'
    assert calls == ['lead-agent', 'builder', 'verifier', 'verifier']
    verdict = ledger.handoffs(job.job_id, 'Verdict')[0].payload
    assert verdict['outcome'] == 'satisfied' and verdict['conditions'][0]['observed'] == 0


def test_bad_device_configuration_returns_to_lead_before_approval(tmp_path):
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025}}))
    ledger = JobLedger(tmp_path/'jobs.db')
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['measure_scope']}
    p = plan()
    p['hardware_contract'] = boundary()
    p['steps'][0].update(tool='measure_scope', touches_hardware=True)
    bad = copy.deepcopy(p)
    bad['hardware_contract']['hardware_safety']['devices'][0]['host'] = 'wrong-device'
    with pytest.raises(ContractCompileError, match='repair hardware configuration'):
        ContractCompiler(contracts, project_root=tmp_path).compile(bad, job_id='job_1', iteration=1, scenario_sha256='', resources=r)
    calls = []
    def runner(agent, task):
        assert agent.name == 'lead-agent'
        calls.append(task)
        return p if 'format_correction' in task else bad
    job, _ = start_simple_mission(ledger, 'scope', 'measure approved CH3', resources=r)
    loop = make_loop(ledger, runner, tmp_path)
    assert not loop.advance(job.job_id).blocked
    assert len(calls) == 2
    assert ledger.job(job.job_id).state == 'running'
    assert not ledger.pending_approvals(job.job_id)


def event_for(payload, contract='scope.yaml'):
    return {'method': 'item/completed', 'params': {'item': {
        'type': 'mcpToolCall', 'status': 'completed', 'server': 'rtase-hwtools', 'tool': 'measure_scope',
        'arguments': {'contract': contract}, 'result': {'content': [{'type': 'text', 'text': json.dumps(payload)}]}}}}


def test_one_campaign_measure_failure_replan_measure_success(tmp_path, monkeypatch):
    from ai_hardware_autolab.hwtools.instruments import RTM3004
    from ai_hardware_autolab.tests.test_measure_scope import RESPONSES, PARAMS
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    qkd = Path(__file__).parents[1]
    (contracts/'scope.yaml').write_bytes((qkd/'contracts/scope-selftest-spec.yaml').read_bytes())
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025}}))
    source = tmp_path/'capture.py'
    source.write_bytes((qkd/'hwtools/recipes/probe_comp_check.py').read_bytes())
    ledger = JobLedger(tmp_path/'jobs.db')
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['measure_scope']}
    job, _ = start_simple_mission(ledger, 'scope', 'Measure the approved CH3 probe signal with complete statistics', resources=r)
    now = lambda: '2026-09-04T00:00:00+00:00'
    authority = HardwareApprovalGate(ledger, contracts_dir=contracts, project_root=tmp_path, now=now)
    calls, captured = [], []
    def screenshot(self, path):
        path.write_bytes(b'fixture')
        return path
    monkeypatch.setattr(RTM3004, 'save_screenshot', screenshot)
    def runner(agent, task):
        calls.append(agent.name)
        if agent.name == 'lead-agent':
            if task['iteration'] == 2:
                assert task['previous_verifier_review']['requirements_met'] is False
                assert task['previous_step_results'][0]['status'] == 'failed'
            return {'objective': 'reuse the probe recipe', 'requirements_complete': True, 'steps': [{
                'id': 'capture', 'agent': 'builder', 'tool': 'measure_scope', 'touches_hardware': True,
                'contract': 'scope.yaml', 'entrypoint': 'capture.py:probe_comp_check',
                'instructions': 'Measure CH3; preserve statistics'}]}
        if agent.name == 'verifier':
            return {'requirements_met': task['iteration'] == 2, 'analysis': 'The first capture was short; the second has full statistics.',
                    'recommended_next_action': 'retry acquisition with sufficient wait' if task['iteration'] == 1 else ''}
        assert authority(agent, task, 'measure_scope', {}), authority.refusals
        responses = dict(RESPONSES)
        if task['iteration'] == 1: responses['MEAS1:RES:WFMC?'] = '37'
        monkeypatch.setattr('ai_hardware_autolab.hwtools.scope.RTM3004', lambda **_: RTM3004(host='mock', mock=True, mock_responses=responses))
        policy = ServerPolicy(allow_hardware=True, role='builder', project_root=tmp_path, contracts_dir=contracts,
            runs_dir=tmp_path/'runs', hardware_tool='measure_scope', hardware_contract='scope.yaml',
            hardware_contract_sha256=sha256_file(contracts/'scope.yaml'),
            workspace_recipe='capture.py:probe_comp_check', workspace_recipe_sha256=sha256_file(source))
        tool = next(t for t in build_tools(policy) if t.name == 'measure_scope')
        payload = tool.handler({'contract': 'scope.yaml', 'params': PARAMS})
        captured.append(payload)
        # Model forgot the run and claimed success. Broker evidence wins.
        return bind_device_evidence({'runs': [], 'status': 'done'}, task, [event_for(payload)], tmp_path/'runs')
    loop = make_loop(ledger, runner, tmp_path)
    controller = CampaignController(ledger, loop, authority, campaigns_dir=tmp_path/'campaigns',
        contracts_dir=contracts, project_root=tmp_path, runs_dir=tmp_path/'runs', hardware_enabled=True, now=now)
    service = CampaignProposalService(ledger, controller, project_root=tmp_path, contracts_dir=contracts,
        campaigns_dir=tmp_path/'campaigns', now=now)
    loop.advance(job.job_id)
    preview = service.preview(job.job_id, max_iterations=2)
    assert preview.ready, preview.blockers
    service.approve(job.job_id, 'test-human', preview.review_sha256, max_iterations=2)
    loop.run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied'
    assert calls == ['lead-agent', 'builder', 'verifier'] * 2
    assert [p['run_verdict'] for p in captured] == ['FAIL', 'PASS']
    assert [h.payload['outcome'] for h in ledger.handoffs(job.job_id, 'Verdict')] == ['open', 'satisfied']
    events = ledger.events_since(0, job.job_id)
    assert sum(e.kind == 'campaign.activated' for e in events) == 1
    assert sum(e.kind == 'approval.authorized' for e in events) == 2
    assert all((Path(p['run_dir'])/'executed_recipe.py').read_bytes() == source.read_bytes() for p in captured)


def test_recipe_cannot_replace_host_computed_samples_or_frontend_readbacks():
    from ai_hardware_autolab.hwtools.instruments.rtm3004 import Waveform
    from ai_hardware_autolab.hwtools.scope import RecipeOutcome
    from ai_hardware_autolab.tests.test_measure_scope import run, selftest_gate
    def recipe(*_):
        return RecipeOutcome(measurements={'sample_count': 4, 'retrieval_path': 'fixture',
            'CH3_max_v': 0, 'CH3_termination': '50_Ohm'}, probed_channels=(3,),
            waveforms=(Waveform(3, 0, 3e-9, 4, 1, [0, 3.3, 0, 3.3]),))
    result = run(recipe, {}, selftest_gate())
    assert result.measurements['CH3_max_v'] == 3.3
    # Actual frontend value is whatever the driver reads, not recipe metadata.
    assert result.measurements['CH3_termination'] == '1_Mohm'


def test_bad_mcp_argument_can_be_corrected_without_spending_device_attempt(tmp_path, monkeypatch):
    from ai_hardware_autolab.mcp_server import registry as registry
    from ai_hardware_autolab.mcp_server.protocol import ToolError
    contract = tmp_path/'scope.yaml'
    contract.write_text('allowed_operations: [oscilloscope_capture]\n')
    calls = []
    monkeypatch.setattr(registry, '_measure_scope', lambda _, args: calls.append(args) or {'ok': True})
    policy = ServerPolicy(allow_hardware=True, role='builder', contracts_dir=tmp_path,
        hardware_tool='measure_scope', hardware_contract=contract.name, hardware_contract_sha256=sha256_file(contract))
    tool = next(t for t in build_tools(policy) if t.name == 'measure_scope')
    for args in ({'contract': contract.name}, {'contract': contract.name, 'params': 'CH3'}):
        with pytest.raises(ToolError, match='correct the call'):
            tool.handler(args)
    assert not calls
    assert tool.handler({'contract': contract.name, 'params': {}})['ok']
    with pytest.raises(ToolError, match='consumed'):
        tool.handler({'contract': contract.name, 'params': {}})
    assert len(calls) == 1


def test_phase_does_not_refresh_all_catalogs_on_each_poll(tmp_path):
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, 'code', 'code', resources=resources())
    ledger.transition(job.job_id, 'running')
    ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id, 'Plan', 'lead-agent', plan(), iteration=1)
    loop = make_loop(ledger, lambda *_: None, tmp_path,
                     resource_refresher=lambda *_: pytest.fail('phase only reads durable mode'))
    assert loop.phase(job.job_id) == 'execute'


def test_real_runner_binds_broker_run_when_builder_omits_it(tmp_path):
    from ai_hardware_autolab.tests.test_codex_runner import FakeClient
    from ai_hardware_autolab.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig
    contracts = tmp_path/'ai_hardware_autolab/contracts'
    contracts.mkdir(parents=True)
    (contracts/'scope.yaml').write_text('allowed_operations: [oscilloscope_capture]\n')
    (tmp_path/'.qkd-job-workspace.json').write_text(json.dumps({'job_id': 'job_test'}))
    run = tmp_path/'runs/run-one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'contract': {'test_spec': 'scope.yaml'}, 'verdict': 'PASS'}))
    payload = {'run': run.name, 'run_dir': str(run), 'run_verdict': 'PASS'}
    class Client(FakeClient):
        def run_turn(self, thread_id, params, *, server_request):
            event = event_for(payload)
            event['params']['threadId'] = thread_id
            runner._capture_event(event)
            return super().run_turn(thread_id, params, server_request=server_request)
    client = Client({'kind': 'StepResult', 'agent': 'builder', 'handoff': {
        'kind': 'StepResult', 'agent': 'builder', 'step_id': 'capture', 'status': 'done', 'runs': []}})
    runner = CodexAgentRunner(config=CodexRunnerConfig(profile='hardware', project_root=tmp_path,
        qkd_root=tmp_path/'ai_hardware_autolab', runs_dir=tmp_path/'runs'), client=client, hardware_approval=lambda *_: True)
    task = {'job_id': 'job_test', 'resources': {**resources(), 'execution_mode': 'hardware'},
            'step': {'id': 'capture', 'tool': 'measure_scope', 'contract': 'scope.yaml', 'touches_hardware': True}}
    result = runner(next(a for a in load_simple_team() if a.name == 'builder'), task)
    assert result['runs'] == ['run-one'] and result['status'] == 'done'


@pytest.mark.parametrize('problem', ['other-server', 'other-contract', 'missing-file', 'outside-root', 'invalid-manifest'])
def test_unrelated_or_missing_evidence_is_not_attached(tmp_path, problem):
    run = tmp_path/'runs/run-one'
    run.mkdir(parents=True)
    (run/'manifest.json').write_text(json.dumps({'contract': {'test_spec': 'scope.yaml'}, 'verdict': 'PASS'}))
    if problem == 'invalid-manifest':
        (run/'manifest.json').write_text(json.dumps({'contract': 'invalid', 'verdict': 'PASS'}))
    payload = {'run': run.name, 'run_dir': str(run)}
    if problem == 'missing-file': payload.update(run='missing', run_dir=str(run.parent/'missing'))
    event = event_for(payload)
    if problem == 'other-server': event['params']['item']['server'] = 'other-server'
    if problem == 'other-contract': event['params']['item']['arguments']['contract'] = 'different.yaml'
    root = tmp_path/'other-runs' if problem == 'outside-root' else run.parent
    task = {'step': {'tool': 'measure_scope', 'contract': 'scope.yaml'}}
    assert bind_device_evidence({'runs': []}, task, [event], root) == {'runs': []}
