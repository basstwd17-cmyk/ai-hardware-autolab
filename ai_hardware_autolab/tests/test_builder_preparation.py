"""Short Lead brief / failed input preparation -> Builder -> validated execution."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from ai_hardware_autolab.orchestrator.candidate_reference import resolve_candidate_reference
from ai_hardware_autolab.orchestrator.development import normalize_plan
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission
from ai_hardware_autolab.hwtools.safety import SafetyViolation
from ai_hardware_autolab.tests.test_autonomous_flow_review import make_loop
from ai_hardware_autolab.tests.test_autonomous_development import plan, resources
from ai_hardware_autolab.tests.test_handoff_preparation import clone_program_inputs
from ai_hardware_autolab.orchestrator.contract_compiler import ContractCompiler


def rejected_plan():
    return json.loads((Path(__file__).parent/'fixtures/job_967e57107747_rejected_plan.json').read_text(encoding='utf-8'))


def record(root, name):
    path = root / 'runs' / name
    path.mkdir(parents=True)
    (path / 'manifest.json').write_text(json.dumps({'verdict': 'PASS', 'steps': [{
        'tool': 'codex_workspace', 'measurements': {'execution_integrity': True, 'last_command_exit_code': 0}}]}))
    return name


def test_candidate_reference_imports_identity_not_historical_approval(tmp_path):
    contracts, name, xcf = clone_program_inputs(tmp_path)
    original = (contracts/name).read_bytes()
    p = resolve_candidate_reference({'candidate_contract': name}, contracts)
    assert p['candidate_reference']['authority_inherited'] is False
    assert p['sram_program_candidate']['immutable'] is True
    assert not any(k.startswith(('approved', 'approval')) for k in p['sram_program_candidate'])
    assert 'allowed_operations' not in p and 'execution_result' not in p
    assert (contracts/name).read_bytes() == original
    # Compile the resolved identity through the real SRAM readiness checks.
    p.update(status='proposed_hardware_boundary', allowed_operations=['sram_fast_program', 'jtag_scan'],
             hardware_safety=rejected_plan()['hardware_boundaries']['gate11_safe_sram']['hardware_safety'])
    step_plan = plan()
    step_plan['hardware_boundaries'] = {'candidate': p}
    step_plan['steps'][0].update(tool='program_cpld_sram', touches_hardware=True,
                                contract='$candidate', tool_args={'program_xcf': xcf})
    compiled = ContractCompiler(contracts, project_root=tmp_path).compile(step_plan,
        job_id='test', iteration=1, scenario_sha256='', resources={**resources(), 'execution_mode': 'hardware'})
    assert any(ref.endswith('.bit') for ref in compiled.plan['steps'][0]['artifacts'])


def test_actual_rejected_job_plan_compiles_from_referenced_metadata_offline(tmp_path):
    contracts, _, _ = clone_program_inputs(tmp_path)
    p = rejected_plan()
    before = copy.deepcopy(p)
    compiled = ContractCompiler(contracts, project_root=tmp_path).compile(p,
        job_id='offline-replay', iteration=1, scenario_sha256='',
        resources={**resources(), 'execution_mode': 'hardware'})
    assert p == before
    assert len(compiled.plan['steps']) == 3
    step = compiled.plan['steps'][1]
    assert any(ref.endswith('.bit') for ref in step['artifacts'])
    spec = yaml.safe_load((contracts/step['contract']).read_text())
    assert spec['candidate_reference']['authority_inherited'] is False
    assert 'approved_by' not in spec['sram_program_candidate']


@pytest.mark.parametrize('value', ['../other.yaml', 'missing.yaml'])
def test_candidate_reference_does_not_guess_or_escape(tmp_path, value):
    with pytest.raises((SafetyViolation, FileNotFoundError)):
        resolve_candidate_reference({'candidate_contract': value}, tmp_path)


def test_conflicting_candidate_and_stale_contract_are_not_silently_replaced(tmp_path):
    contracts, name, _ = clone_program_inputs(tmp_path)
    with pytest.raises(SafetyViolation, match='conflicts'):
        resolve_candidate_reference({'configuration': {'candidate': {
            'source_contract': name, 'bitstream_sha256': 'a'*64}}}, contracts)
    with pytest.raises(SafetyViolation, match='changed'):
        resolve_candidate_reference({'candidate_contract': name, 'candidate_contract_sha256': 'a'*64}, contracts)


def test_brief_handoff_needs_no_steps_hashes_or_hardware_document():
    brief = {'kind': 'Plan', 'agent': 'lead-agent', 'objective': 'Measure existing SPI',
             'approach': 'Reuse working implementation', 'references': ['existing.py'],
             'acceptance_criteria': ['four registers match'], 'unknowns': ['current image']}
    normalized = normalize_plan(brief, resources())
    assert normalized['preparation_context']['original_plan'] == brief
    assert normalized['steps'][0]['touches_hardware'] is False
    assert normalized['requirements_complete'] is False
    assert 'steps' not in brief


@pytest.mark.parametrize('broken_device_plan', [False, True])
def test_builder_prepares_then_execution_continues_without_second_lead_or_repeated_prep(tmp_path, broken_device_plan):
    ledger = JobLedger(tmp_path/'jobs.db')
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['codex_workspace', 'program_cpld_sram']}
    job, _ = start_simple_mission(ledger, 'test', 'implement request', resources=r)
    calls = []
    def runner(agent, task):
        calls.append((agent.name, task['iteration']))
        if agent.name == 'lead-agent':
            if broken_device_plan:
                p = plan()
                p['steps'][0].update(tool='program_cpld_sram', touches_hardware=True, contract='missing.yaml')
                return p
            return {'kind': 'Plan', 'agent': 'lead-agent', 'objective': 'Implement requested function',
                    'approach': 'Reuse existing code', 'acceptance_criteria': ['requested behavior works']}
        if agent.name == 'builder':
            if task['iteration'] == 1:
                assert task['plan']['builder_preparation']
                assert task['step']['tool'] == 'codex_workspace' and task['step']['touches_hardware'] is False
                if broken_device_plan:
                    assert 'missing.yaml' in task['plan']['preparation_context']['reason']
                return {'status': 'done', 'runs': [record(tmp_path, 'prepared')], 'prepared_plan': plan()}
            assert task['iteration'] == 2
            return {'status': 'done', 'runs': [record(tmp_path, 'implemented')]}
        return {'requirements_met': True, 'analysis': 'checked current execution against original request'}
    make_loop(ledger, runner, tmp_path).run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied'
    assert calls == [('lead-agent', 1), ('builder', 1), ('builder', 2), ('verifier', 2)]
    events = ledger.events_since(0, job_id=job.job_id)
    assert any(e.kind == 'plan.preparation.required' for e in events)
    assert any(e.kind == 'plan.prepared.selected' for e in events)
    assert not ledger.pending_approvals(job.job_id)


def test_cancelled_preparation_never_continues(tmp_path):
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, 'test', 'code', resources=resources())
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        return {'objective': 'implement', 'approach': 'reuse'}
    loop = make_loop(ledger, runner, tmp_path)
    loop.advance(job.job_id)
    ledger.transition(job.job_id, 'done', outcome='cancelled', reason='user cancelled')
    loop.advance(job.job_id)
    assert calls == ['lead-agent']
    assert ledger.job(job.job_id).outcome == 'cancelled'


def test_feedback_after_preparation_is_not_bypassed_by_prepared_plan(tmp_path):
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, 'test', 'implement request', resources=resources())
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        if agent.name == 'builder':
            return {'status': 'done', 'runs': [record(tmp_path, 'prepared')], 'prepared_plan': plan()}
        if task['iteration'] == 2:
            assert task['steering'][0]['message'] == 'Only report the result; do not operate devices'
            return {**plan(), 'iteration': 2}
        return {'objective': 'implement', 'approach': 'reuse'}
    loop = make_loop(ledger, runner, tmp_path)
    loop.advance(job.job_id)  # Lead
    loop.advance(job.job_id)  # Builder preparation recorded
    ledger.append_event(job.job_id, 'control.steer',
        {'message': 'Only report the result; do not operate devices', 'by': 'human'}, 'human')
    loop.advance(job.job_id)  # deterministic preparation reduction
    loop.advance(job.job_id)  # next iteration
    loop.advance(job.job_id)  # feedback must go to Lead, not bypass it
    assert calls == ['lead-agent', 'builder', 'lead-agent']
    assert not any(e.kind == 'plan.prepared.selected' for e in ledger.events_since(0, job_id=job.job_id))


def test_missing_direct_arguments_go_to_preparation_not_fake_dispatch():
    p = plan()
    p['steps'][0].update(tool='program_cpld_sram', touches_hardware=True, execution='direct', tool_args='not arguments')
    result = normalize_plan(p, {**resources(), 'execution_mode': 'hardware'})
    assert result['builder_preparation']
    assert result['steps'][0]['touches_hardware'] is False
    assert 'Direct execution needs preparation' in result['preparation_context']['reason']
