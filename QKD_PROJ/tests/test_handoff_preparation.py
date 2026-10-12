"""Plan -> preparation -> real approval gate -> mock device -> recorded verdict.

Only the instrument/programmer boundary is mocked. Never connects to hardware.
"""
import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from QKD_PROJ.agents.schema import load_simple_team
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler, ContractCompileError
from QKD_PROJ.orchestrator.daemon import HardwareApprovalGate
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.scenario_access import ScenarioAutoApprover, submission_access
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.tests.test_autonomous_development import plan, resources
from QKD_PROJ.tests.test_autonomous_flow_review import make_loop
from QKD_PROJ.tests.test_boundary_normalization import proposal

ROOT = Path(__file__).resolve().parents[1]


def scope_setup(tmp_path):
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    p = proposal()
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': p['configuration']['scope']['target']}))
    p['status'] = 'proposed_hardware_boundary'
    p['allowed_operations'] = ['oscilloscope_capture']
    (contracts/'old-draft.yaml').write_text(yaml.safe_dump(p))
    scenario = 'oscilloscope capture'
    r = {**resources(), 'execution_mode': 'hardware', 'available_tools': ['measure_scope']}
    r['scenario_access'] = submission_access(scenario, project_root=tmp_path, contracts_dir=contracts)
    p = plan()
    p['steps'][0].update(tool='measure_scope', contract='old-draft.yaml', touches_hardware=True,
                         agent='builder', advance_when=[{'field': 'measurements.sample_count', 'expect': 4, 'source':'mock capture'}])
    return contracts, r, p, scenario


def test_reused_scope_contract_reaches_verifier_without_manual_click(tmp_path):
    contracts, r, p, scenario = scope_setup(tmp_path)
    original = (contracts/'old-draft.yaml').read_bytes()
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, scenario, scenario, resources=r)
    approver = ScenarioAutoApprover(ledger, project_root=tmp_path, contracts_dir=contracts,
                                  runs_dir=tmp_path/'runs', enabled=True)
    gate = HardwareApprovalGate(ledger, project_root=tmp_path, contracts_dir=contracts)
    gate.scenario_authorizer = approver
    calls = []
    def runner(agent, task):
        calls.append(agent.name)
        if agent.name == 'lead-agent': return p
        if agent.name == 'builder':
            assert task['step']['contract'] != 'old-draft.yaml'
            assert gate(agent, task, 'measure_scope', {})
            bound = Contracts.load(task['step']['contract'], contracts, project_root=tmp_path)
            assert len(SafetyGate(bound).instrument_scope()['scope_channels']) == 4
            run = tmp_path/'runs/mock-capture'
            run.mkdir(parents=True)
            (run/'manifest.json').write_text(json.dumps({'verdict':'PASS','steps':[
                {'tool':'scope','measurements':{'sample_count':4}}]}))
            return {'kind':'StepResult','agent':'builder','status':'done','runs':[run.name]}
        return {'requirements_met':True,'analysis':'mock capture has four samples'}
    loop = make_loop(ledger, runner, tmp_path, scenario_approver=approver)
    loop.run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied', [e.payload for e in ledger.events_since(0, job_id=job.job_id) if e.kind == 'job.blocked']
    assert calls == ['lead-agent','builder','verifier']
    assert not ledger.pending_approvals(job.job_id)
    assert (contracts/'old-draft.yaml').read_bytes() == original
    assert any(e.kind == 'approval.authorized' for e in ledger.events_since(0, job_id=job.job_id))


def test_incomplete_native_plan_returns_to_lead_before_any_approval(tmp_path):
    contracts, r, p, scenario = scope_setup(tmp_path)
    bad = copy.deepcopy(p)
    (contracts/'program-draft.yaml').write_text(yaml.safe_dump({
        'status':'proposed_hardware_boundary','allowed_operations':['jtag_scan','sram_fast_program'],
        'configuration':{'candidate':{'bitstream':'not-ready.bit','sha256':'a'*64}},
        'hardware_safety':{'stop_conditions':['stop on mismatch']}}))
    bad['steps'][0].update(tool='program_cpld_sram', contract='program-draft.yaml',
                           tool_args={'program_xcf':'candidate.xcf'})
    r['available_tools'].append('program_cpld_sram')
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, scenario, scenario, resources=r)
    calls = []
    def runner(agent, task):
        assert agent.name == 'lead-agent'
        calls.append(task)
        if len(calls) == 2:
            assert 'repair hardware configuration' in task['format_correction']['error']
        return bad
    loop = make_loop(ledger, runner, tmp_path)
    loop.advance(job.job_id)
    assert len(calls) == 2
    assert ledger.job(job.job_id).state == 'blocked'
    assert not ledger.pending_approvals(job.job_id)
    assert not ledger.handoffs(job.job_id, 'Plan')
    assert not any(e.kind == 'step.dispatch.started' for e in ledger.events_since(0, job_id=job.job_id))


def clone_program_inputs(tmp_path):
    contracts = tmp_path/'QKD_PROJ/contracts'
    contracts.mkdir(parents=True)
    name = 'gate11-gpio-spi-code-registers.yaml'
    xcf = 'gate11-gpio-spi-code-registers-sram-program.xcf'
    data = yaml.safe_load((ROOT/'contracts'/name).read_text())
    for file in (name, xcf, 'board-map.yaml'):
        shutil.copy2(ROOT/'contracts'/file, contracts/file)
    candidate = data['sram_program_candidate']
    paths = [str(Path(candidate['directory'])/candidate['bitstream']), *data['test_design']['sources'].values()]
    for ref in paths:
        target = tmp_path/ref
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT.parent/ref,target)
    return contracts, name, xcf


def test_program_candidate_is_pinned_before_approval_and_changed_bytes_are_refused(tmp_path):
    contracts, name, xcf = clone_program_inputs(tmp_path)
    p = plan()
    p['steps'][0].update(tool='program_cpld_sram', contract=name, touches_hardware=True,
                         agent='builder', execution='direct', tool_args={'program_xcf':xcf})
    r = {**resources(),'execution_mode':'hardware','available_tools':['program_cpld_sram']}
    compiled = ContractCompiler(contracts,project_root=tmp_path).compile(p,job_id='test',iteration=1,scenario_sha256='',resources=r)
    step = compiled.plan['steps'][0]
    assert any(ref.endswith('.bit') for ref in step['artifacts'])
    assert any(ref.endswith('.xcf') for ref in step['artifacts'])
    assert any(ref.endswith('.v') for ref in step['artifacts'])
    ledger = JobLedger(tmp_path/'jobs.db')
    job,_ = start_simple_mission(ledger,'program','CPLD program',resources=r)
    request = ledger.request_approval(job.job_id,name,'one SRAM program',artifacts=step['artifacts'],
                                     requested_by='builder',step_id=step['id'])
    ledger.decide_approval(request.approval_id,True,'test-human')
    bit = next(tmp_path/ref for ref in step['artifacts'] if ref.endswith('.bit'))
    bit.write_bytes(bit.read_bytes()+b'changed')
    gate = HardwareApprovalGate(ledger,project_root=tmp_path,contracts_dir=contracts)
    builder = next(a for a in load_simple_team() if a.name=='builder')
    assert not gate(builder,{'job_id':job.job_id,'step':step},'program_cpld_sram',step['tool_args'])
    assert 'not what was signed' in gate.refusals[-1]
    assert not gate(builder,{'job_id':job.job_id,'step':step},'program_cpld_sram',{'program_xcf':'other.xcf'})
    assert 'actual program_xcf differs' in gate.refusals[-1]
    with pytest.raises(ContractCompileError,match='repair hardware configuration'):
        ContractCompiler(contracts,project_root=tmp_path).compile(p,job_id='test',iteration=1,scenario_sha256='',resources=r)


def test_unready_drafts_are_hidden_but_still_readable(tmp_path):
    from QKD_PROJ.mcp_server.registry import ServerPolicy, _list_contracts, _read_contract
    contracts,r,p,scenario = scope_setup(tmp_path)
    bad = {'status':'proposed_hardware_boundary','allowed_operations':['sram_fast_program'],
           'hardware_safety':{'stop_conditions':['stop']}}
    path=contracts/'unready.yaml'
    path.write_text(yaml.safe_dump(bad))
    before=path.read_bytes()
    policy=ServerPolicy(contracts_dir=contracts,project_root=tmp_path)
    visible=_list_contracts(policy,{})
    assert 'unready.yaml' not in {r['file'] for r in visible['contracts']}
    assert visible['omitted_unready']==1
    assert 'unready.yaml' in {r['file'] for r in _list_contracts(policy,{'include_unready':True})['contracts']}
    assert _read_contract(policy,{'name':'unready.yaml'})['reuse']['status']=='needs_preparation'
    assert path.read_bytes()==before


@pytest.mark.parametrize('reference_mode', [False, True])
def test_sram_plan_to_mock_program_run_to_verifier_uses_one_automatic_grant(tmp_path, monkeypatch, reference_mode):
    from QKD_PROJ import hwtools
    from QKD_PROJ.hwtools import sram
    from QKD_PROJ.mcp_server.registry import ServerPolicy, _program_cpld_sram
    from QKD_PROJ.tests.test_sram_program import passing_scan, successful_runner
    contracts,name,xcf = clone_program_inputs(tmp_path)
    r = {**resources(),'execution_mode':'hardware','available_tools':['program_cpld_sram']}
    scenario = 'RTASE V1 CPLD SRAM program'
    r['scenario_access'] = submission_access(scenario,project_root=tmp_path,contracts_dir=contracts)
    p = plan()
    p['steps'][0].update(tool='program_cpld_sram',contract=name,touches_hardware=True,agent='builder',
        execution='agent',tool_args={'program_xcf':xcf},
        advance_when=[{'field':'measurements.program_success','expect':True,'source':'mock programmer'}])
    if reference_mode:
        fixture = json.loads((ROOT/'tests/fixtures/job_967e57107747_rejected_plan.json').read_text(encoding='utf-8'))
        p['hardware_boundaries'] = {'candidate': {
            'candidate_contract': name, 'allowed_operations': ['jtag_scan', 'sram_fast_program'],
            'hardware_safety': fixture['hardware_boundaries']['gate11_safe_sram']['hardware_safety']}}
        p['steps'][0]['contract'] = '$candidate'
    ledger=JobLedger(tmp_path/'jobs.db')
    job,_=start_simple_mission(ledger,scenario,scenario,resources=r)
    approver=ScenarioAutoApprover(ledger,project_root=tmp_path,contracts_dir=contracts,runs_dir=tmp_path/'runs',enabled=True)
    gate=HardwareApprovalGate(ledger,project_root=tmp_path,contracts_dir=contracts)
    gate.scenario_authorizer=approver
    scans=[]
    monkeypatch.setattr(hwtools,'scan_jtag',lambda *_a,**_kw: scans.append('mock') or passing_scan())
    monkeypatch.setattr(sram,'_find_pgrcmd',lambda *_: tmp_path/'mock/bin/pgrcmd.exe')
    monkeypatch.setattr(hwtools,'program_cpld_sram',lambda *a,**kw: sram.program_cpld_sram(*a,**kw,runner=successful_runner))
    calls=[]
    def runner(agent,task):
        calls.append(agent.name)
        if agent.name=='lead-agent': return p
        if agent.name=='builder':
            step=task['step']
            assert gate(agent,task,'program_cpld_sram',step['tool_args']), gate.refusals
            policy=ServerPolicy(project_root=tmp_path,contracts_dir=contracts,runs_dir=tmp_path/'runs',
                allow_hardware=True,role='builder',hardware_tool='program_cpld_sram',
                hardware_contract=step['contract'],hardware_contract_sha256=step['contract_sha256'])
            result=_program_cpld_sram(policy,{'contract':step['contract'],**step['tool_args']})
            assert result['run_verdict']=='PASS', result
            return {'kind':'StepResult','agent':'builder','status':'done','runs':[result['run']]}
        return {'requirements_met':True,'analysis':'verified mocked SRAM program run'}
    loop=make_loop(ledger,runner,tmp_path,scenario_approver=approver)
    # Fixture uses the real QKD_PROJ layout for the native candidate reader.
    loop.contract_compiler=ContractCompiler(contracts,project_root=tmp_path)
    loop.run(job.job_id)
    assert ledger.job(job.job_id).outcome=='satisfied', [e.payload for e in ledger.events_since(0,job_id=job.job_id) if e.kind=='job.blocked']
    assert calls==['lead-agent','builder','verifier']
    assert scans==['mock']
    events=ledger.events_since(0,job_id=job.job_id)
    assert sum(e.kind=='approval.authorized' for e in events)==1
    assert not ledger.pending_approvals(job.job_id)


def test_program_input_failure_precedes_even_the_jtag_scan(tmp_path, monkeypatch):
    from QKD_PROJ import hwtools
    from QKD_PROJ.hwtools.sram import SramProgramError
    from QKD_PROJ.mcp_server.registry import ServerPolicy, _program_cpld_sram
    contracts,name,xcf=clone_program_inputs(tmp_path)
    data=yaml.safe_load((contracts/name).read_text())
    bit=tmp_path/data['sram_program_candidate']['directory']/data['sram_program_candidate']['bitstream']
    bit.write_bytes(b'bad')
    monkeypatch.setattr(hwtools,'scan_jtag',lambda *_a,**_kw: pytest.fail('must not touch JTAG'))
    policy=ServerPolicy(project_root=tmp_path,contracts_dir=contracts,runs_dir=tmp_path/'runs',
        allow_hardware=True,role='builder',hardware_tool='program_cpld_sram',
        hardware_contract=name,hardware_contract_sha256=sha256_file(contracts/name))
    with pytest.raises(SramProgramError):
        _program_cpld_sram(policy,{'contract':name,'program_xcf':xcf})
    assert not (tmp_path/'runs').exists()


def test_failed_later_step_leaves_no_new_or_normalized_contract_files(tmp_path):
    contracts,r,p,scenario=scope_setup(tmp_path)
    before={path.name:path.read_bytes() for path in contracts.iterdir()}
    p['hardware_boundaries']={'bad':{
        'allowed_operations':['jtag_scan','sram_fast_program'],
        'hardware_safety':{'stop_conditions':['stop on discrepancy']}}}
    p['steps'].append({'id':'program','agent':'builder','tool':'program_cpld_sram',
        'contract':'$bad','touches_hardware':True,'instructions':'program candidate',
        'tool_args':{'program_xcf':'candidate.xcf'}})
    r['available_tools'].append('program_cpld_sram')
    with pytest.raises(ContractCompileError,match='repair hardware configuration'):
        ContractCompiler(contracts,project_root=tmp_path).compile(p,job_id='test',iteration=1,scenario_sha256='',resources=r)
    assert {path.name:path.read_bytes() for path in contracts.iterdir()}==before
