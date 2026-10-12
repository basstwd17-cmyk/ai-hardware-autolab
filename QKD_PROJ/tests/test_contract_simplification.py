"""Integrated plans, effect-local checks and durable reasoning context. Offline only."""
import copy

import pytest
import yaml

from QKD_PROJ.hwtools.safety import Contracts, SafetyGate, SafetyViolation
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler
from QKD_PROJ.tests.test_autonomous_development import boundary, plan, resources


def mixed_gate(tmp_path):
    spec = boundary()
    spec['status'] = 'proposed_hardware_boundary'
    spec['allowed_operations'] += ['bbb_ssh_command']
    spec['hardware_safety']['devices'].append({'kind': 'bbb', 'host': '192.168.50.2', 'port': 22})
    spec['hardware_safety']['scope_channels'][0].update(signal='SPI.CLK', termination='1_Mohm')
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025}}))
    (contracts/'mixed.yaml').write_text(yaml.safe_dump(spec))
    return SafetyGate(Contracts.load('mixed.yaml', contracts), allow_hardware=True)


def test_combined_document_projects_only_scope_and_keeps_limits(tmp_path):
    gate = mixed_gate(tmp_path)
    scope = gate.instrument_scope()
    assert len(scope['devices']) == 1 and scope['devices'][0]['model'] == 'RTM3004'
    assert 'limits' not in scope
    gate.require('oscilloscope_capture')
    gate.require_scope_capture({'channels': [{'channel': 3, 'signal': 'CLK'}]})
    gate.require_measured_voltage({'CH3_min_v': 0, 'CH3_max_v': 3.3})
    with pytest.raises(SafetyViolation):
        gate.require_measured_voltage({'CH3_min_v': 0, 'CH3_max_v': 100})
    # Passive validation does not make the BBB safe/authorized to drive pins.
    with pytest.raises(SafetyViolation, match='electrical limits'):
        gate.require('bbb_ssh_command')
    with pytest.raises(SafetyViolation):
        gate.require('flash_program')


@pytest.mark.parametrize('rows', [
    [{'channel': 3, 'signal': 'MISO'}],
    [{'channel': 2, 'signal': 'CLK'}],
    [{'channel': 3, 'signal': 'CLK'}, {'channel': 3, 'signal': 'CLK'}],
    [{'channel': True, 'signal': 'CLK'}],
])
def test_label_normalization_never_changes_wiring(tmp_path, rows):
    gate = mixed_gate(tmp_path)
    with pytest.raises(SafetyViolation):
        gate.require_scope_capture({'channels': rows})


@pytest.mark.parametrize('mutation', ['endpoint', 'termination', 'forbidden', 'duplicate_scope'])
def test_mixed_document_still_rejects_unsafe_or_ambiguous_scope(tmp_path, mutation):
    gate = mixed_gate(tmp_path)
    safety = gate.contracts.test_spec['hardware_safety']
    if mutation == 'endpoint': safety['devices'][0]['host'] = 'wrong'
    if mutation == 'termination': safety['scope_channels'][0]['termination'] = '50ohm'
    if mutation == 'forbidden': gate._forbidden.add('oscilloscope_capture')
    if mutation == 'duplicate_scope': safety['devices'].append(dict(safety['devices'][0]))
    with pytest.raises(SafetyViolation): gate.instrument_scope()


def test_shared_boundary_compiles_without_splitting_leads_plan(tmp_path):
    gate = mixed_gate(tmp_path)
    # This case executes BBB as well as observing the scope. Unlike the
    # passive-only tests above, supply BBB's own preparation data explicitly.
    gate.contracts.test_spec['bbb'] = {'target': {'host':'192.0.2.2','port':22,'user':'test'}}
    gate.contracts.test_spec['hardware_safety'].update(
        limits=[{'parameter':'voltage','max':3.3,'unit':'V','source':'fixture'},
                {'parameter':'current','max':4,'unit':'mA','source':'fixture'}],
        pin_assignments=[{'pin':'P9_22','direction':'output','source':'fixture'}])
    p = plan()
    p['hardware_boundaries'] = {'combined': gate.contracts.test_spec}
    p['steps'] = [
        {'id': 'write', 'agent': 'builder', 'tool': 'run_bbb', 'touches_hardware': True,
         'contract': '$combined', 'instructions': 'write and read the registers'},
        {'id': 'capture', 'agent': 'builder', 'tool': 'measure_scope', 'touches_hardware': True,
         'contract': '$combined', 'instructions': 'capture CLK'},
    ]
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['run_bbb', 'measure_scope']}
    result = ContractCompiler(tmp_path/'contracts', project_root=tmp_path).compile(
        p, job_id='job_test', iteration=1, scenario_sha256='', resources=r)
    assert [s['id'] for s in result.plan['steps']] == ['write', 'capture']
    assert len(result.plan['hardware_contracts']) == 1
    assert result.plan['steps'][0]['contract'] == result.plan['steps'][1]['contract']
    assert p['steps'][0]['contract'] == '$combined'


def test_rejected_plan_is_durable_but_not_executable(tmp_path):
    from QKD_PROJ.orchestrator.jobs import JobLedger
    from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
    from QKD_PROJ.tests.test_autonomous_flow_review import make_loop
    mixed_gate(tmp_path)
    bad = plan()
    bad['hardware_contract'] = boundary()
    bad['hardware_contract']['hardware_safety']['devices'][0]['host'] = 'wrong'
    bad['steps'][0].update(tool='measure_scope', touches_hardware=True)
    tasks = []
    def runner(agent, task):
        tasks.append(task)
        return copy.deepcopy(bad)
    ledger = JobLedger(tmp_path/'jobs.db')
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['measure_scope']}
    job, _ = start_simple_mission(ledger, 'capture', 'measure the actual clock', resources=r)
    assert make_loop(ledger, runner, tmp_path).advance(job.job_id).blocked
    assert len(tasks) == 2
    assert tasks[1]['research_plan']['search_budget'] == 0
    assert tasks[1]['format_correction']['rejected_handoff']['objective'] == bad['objective']
    events = [e for e in ledger.events_since(0, job.job_id) if e.kind == 'plan.validation_failed']
    assert len(events) == 2 and events[-1].payload['correction'] is True
    assert events[0].payload['rejected_plan']['hardware_contract'] == bad['hardware_contract']
    assert len(events[0].payload['sha256']) == 64
    assert not ledger.handoffs(job.job_id, 'Plan')
    assert not ledger.pending_approvals(job.job_id)


def test_bound_sram_dispatch_uses_host_grant_not_legacy_approval_fields():
    from QKD_PROJ.hwtools.sram import _approved_candidate, SramProgramError
    from QKD_PROJ.tests.test_sram_program import gate as candidate_gate
    gate = candidate_gate(live=True)
    with pytest.raises(SramProgramError, match='Approval 2'):
        _approved_candidate(gate)
    gate.delegated_operation = 'sram_fast_program'
    candidate = _approved_candidate(gate)
    assert candidate.authorization_source == 'bound_device_dispatch'
    assert candidate.approved_by is None and candidate.execution_number is None
    # A delegated operation is not permission to lose source/image identity.
    gate.contracts.test_spec['sram_program_candidate']['bitstream_sha256'] = 'bad'
    with pytest.raises(SramProgramError, match='SHA-256'):
        _approved_candidate(gate)


def test_registry_sram_grant_requires_matching_bound_contract():
    from dataclasses import replace
    from QKD_PROJ.mcp_server.registry import ServerPolicy, _gate, ToolError
    from QKD_PROJ.tests.test_sram_program import ROOT
    name = 'gate1-test-spec.yaml'
    policy = ServerPolicy(allow_hardware=True, role='builder', contracts_dir=ROOT/'contracts',
                          project_root=ROOT.parent)
    assert _gate(policy, name).delegated_operation is None
    bound = replace(policy, hardware_tool='program_cpld_sram', hardware_contract=name,
                    hardware_contract_sha256=sha256_file(ROOT/'contracts'/name))
    assert _gate(bound, name).delegated_operation == 'sram_fast_program'
    with pytest.raises(ToolError): _gate(replace(bound, hardware_contract_sha256='0'*64), name)


def test_campaign_keeps_each_device_view_for_shared_document(tmp_path, monkeypatch):
    from QKD_PROJ.tests.test_campaign_proposal import _environment, NOW
    from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
    from QKD_PROJ.orchestrator.campaign_proposal import CampaignProposalService
    for key in ('BB_HOST', 'BB_USER', 'BB_PORT'):
        monkeypatch.delenv(key, raising=False)
    ledger, _, controller = _environment(tmp_path)
    contracts = controller.contracts_dir
    data = boundary()
    data['status'] = 'proposed_hardware_boundary'
    data['allowed_operations'].append('bbb_identify')
    (contracts/'combined.yaml').write_text(yaml.safe_dump(data))
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({
        'scope': {'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025},
        'bbb': {'access': {'host': '192.168.50.2', 'user': 'test', 'port': 22}},
    }))
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['run_bbb', 'measure_scope']}
    p = plan()
    p['steps'] = [{'id': tool, 'agent': 'builder', 'tool': tool, 'touches_hardware': True,
                   'contract': 'combined.yaml', 'instructions': 'use the relevant device',
                   **({'tool_args': {'identity_only': True}} if tool == 'run_bbb' else {})}
                  for tool in ('measure_scope', 'run_bbb')]
    job, _ = start_simple_mission(ledger, 'multi', 'identify BBB and capture scope', resources=r)
    ledger.transition(job.job_id, 'running')
    ledger.begin_iteration(job.job_id)
    compiled = ContractCompiler(contracts, project_root=tmp_path).compile(
        p, job_id=job.job_id, iteration=1, scenario_sha256='', resources=r)
    ledger.record_handoff(job.job_id, 'Plan', 'lead-agent', compiled.plan, iteration=1)
    service = CampaignProposalService(ledger, controller, project_root=tmp_path, contracts_dir=contracts,
                                     campaigns_dir=controller.campaigns_dir, now=lambda: NOW)
    preview = service.preview(job.job_id)
    assert preview.ready, preview.blockers
    assert len(preview.boundaries) == 2 and len(preview.permissions) == 2
    assert {b['tool'] for b in preview.boundaries} == {'measure_scope', 'run_bbb'}
    assert preview.boundaries[0]['hardware_safety']['devices'][0]['model'] == 'RTM3004'
    assert preview.boundaries[1]['hardware_safety']['devices'][0]['model'] == 'BBB SSH'


def test_agent_context_preserves_design_without_repeating_installed_yaml():
    from QKD_PROJ.orchestrator.loop import Loop
    original = {'objective': 'whole scenario', 'architecture': {'buffer': 'ring'},
                'steps': [{'contract': 'combined.yaml'}], 'hardware_boundaries': {'combined': boundary()},
                'hardware_contracts': [{'name': 'combined.yaml', 'sha256': 'a'*64}]}
    context = Loop._agent_plan(original)
    assert context['architecture'] == original['architecture']
    assert context['steps'] == original['steps']
    assert 'hardware_boundaries' not in context
    assert 'hardware_boundaries' in original
    uncompiled = {**original, 'hardware_contracts': []}
    assert Loop._agent_plan(uncompiled) == uncompiled
