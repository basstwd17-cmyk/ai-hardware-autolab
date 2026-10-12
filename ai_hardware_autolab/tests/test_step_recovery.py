"""No live device/model: crash boundaries, atomic resume and broker source checks."""
import copy
import json
from types import SimpleNamespace as S

import pytest

from ai_hardware_autolab.orchestrator.jobs import JobLedger, JobLedgerError
from ai_hardware_autolab.orchestrator.loop import Loop, LoopError
from ai_hardware_autolab.orchestrator.development import MODE
from ai_hardware_autolab.orchestrator.recovery import blocked_step
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.control import pending_steers
from ai_hardware_autolab.orchestrator.wire import WireSession
from ai_hardware_autolab.orchestrator.runtime_registry import RuntimeRegistry
from ai_hardware_autolab.orchestrator.runtime_events import RuntimeEventRecorder
from ai_hardware_autolab.tests.test_autonomous_flow_review import make_loop
from ai_hardware_autolab.tests.test_lightweight_execution import no_network, identity_setup
from ai_hardware_autolab.tests.test_scope_session_approval import setup_scope
from ai_hardware_autolab.tests.test_scenario_access import start
from ai_hardware_autolab.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerError
from ai_hardware_autolab.mcp_server.registry import ServerPolicy, build_tools
from ai_hardware_autolab.hwtools.result import sha256_file


def recorded_stop(tmp_path, state='running'):
    ledger = JobLedger(tmp_path/'jobs.db')
    job = ledger.create_job('__mission__', 'diagnose', brief={'kind':'Brief', 'agent':'human', 'scenario':'diagnose',
        'resources': {'development_mode':MODE, 'execution_mode':'hardware'}})
    ledger.transition(job.job_id, 'running')
    ledger.begin_iteration(job.job_id)
    plan = {'kind':'Plan', 'agent':'lead-agent', 'iteration':1, 'requirements_complete':True,
            'steps':[{'id':x, 'agent':'builder', 'tool':'measure_scope', 'touches_hardware':True} for x in ['first','stopped','later']]}
    ledger.record_handoff(job.job_id,'Plan','lead-agent',plan)
    ledger.record_handoff(job.job_id,'StepResult','builder',{'status':'done','runs':['actual-first'],'summary':'measured'},step_id='first')
    stop = ledger.record_handoff(job.job_id,'StepResult','builder',{'status':'blocked','repair_target':'platform_deployment',
        'hardware_contacted':False,'hardware_state_unknown':False,'runs':[]},step_id='stopped')
    if state == 'blocked': ledger.transition(job.job_id,'blocked',reason='platform_deployment: test')
    return ledger, job.job_id, stop


def test_crash_between_handoff_and_job_block_never_skips_device(tmp_path):
    ledger, job, _ = recorded_stop(tmp_path)
    calls=[]
    loop=make_loop(ledger, lambda *args:calls.append(args), tmp_path)
    assert loop.phase(job) == 'blocked'
    assert loop.advance(job).blocked
    assert ledger.job(job).state == 'blocked' and not calls
    fresh=make_loop(JobLedger(tmp_path/'jobs.db'), lambda *args:calls.append(args), tmp_path)
    assert fresh.advance(job).blocked and not calls


def test_dispatch_without_result_is_not_repeated_after_restart(tmp_path):
    ledger=JobLedger(tmp_path/'jobs.db')
    job=ledger.create_job('__mission__','test',brief={'resources':{'development_mode':MODE}})
    ledger.transition(job.job_id,'running'); ledger.begin_iteration(job.job_id)
    ledger.record_handoff(job.job_id,'Plan','lead-agent',{'steps':[{'id':'device'}]})
    ledger.append_event(job.job_id,'step.dispatch.started',{'iteration':1,'step_id':'device'})
    loop=make_loop(ledger, lambda *a:pytest.fail('replayed ambiguous device'), tmp_path)
    assert loop.advance(job.job_id).reason_code == 'hardware_state_unknown'
    with pytest.raises(LoopError,match='unrecorded'): loop.resume_step(job.job_id,'human','restarted')


def test_atomic_resume_preserves_old_evidence_and_rejects_double_click(tmp_path):
    ledger, job, stop=recorded_stop(tmp_path,'blocked')
    old=ledger.handoffs(job)
    ControlPlane(ledger).steer(job,'check the new cable','human')
    row=ledger.resume_pre_device_step(job,stop.handoff_id,'human','source applied')
    assert row.iteration==2 and row.state=='running'
    copied=ledger.handoffs(job,'StepResult',2)
    assert len(copied)==1 and copied[0].step_id=='first'
    assert copied[0].payload['runs']==['actual-first'] and copied[0].payload['reused_from']['sha256']==old[2].sha256
    assert [h.sha256 for h in ledger.handoffs(job) if h.iteration<=1]==[h.sha256 for h in old]
    with pytest.raises(JobLedgerError): ledger.resume_pre_device_step(job,stop.handoff_id,'human','second click')
    assert ledger.job(job).iteration==2
    assert make_loop(ledger,lambda *a:None,tmp_path).phase(job)=='execute'
    assert pending_steers(ledger,job)[0]['message']=='check the new cable'


def test_web_resume_uses_operator_auth_and_forwards_the_command(monkeypatch):
    from ai_hardware_autolab.tests.test_webui import _app, _client, BASE, TestClient
    app,session=_app(None)
    seen=[]
    monkeypatch.setattr(app.state.hub.subscription,'command',lambda frame:seen.append(frame) or {'ok':True})
    frame={'type':'job.resume','jobId':'j','requestedBy':'human','note':'resolved'}
    with TestClient(app,base_url=BASE) as http:
        assert http.post('/api/command',json=frame).status_code==401
    with _client(app,session) as http:
        assert http.post('/api/command',json=frame).status_code==200
    assert seen==[frame]


def test_resume_transaction_rolls_back_every_record_on_crash(tmp_path,monkeypatch):
    ledger,job,stop=recorded_stop(tmp_path,'blocked')
    before=ledger.latest_event_id()
    original=ledger._write_handoff
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(ledger,'_write_handoff',crash)
    with pytest.raises(RuntimeError):ledger.resume_pre_device_step(job,stop.handoff_id,'human','resolved')
    assert ledger.latest_event_id()==before
    assert ledger.job(job).state=='blocked' and ledger.job(job).iteration==1
    assert not ledger.handoffs(job,'Plan',2)


def test_wire_and_wrappers_forward_resume_check(tmp_path):
    ledger,job,_=recorded_stop(tmp_path,'blocked')
    checked=[]
    class Runner:
        def __call__(self,*args):pytest.fail('resume must not execute a device')
        def check_resume(self,*args):checked.append(args)
    runner=RuntimeEventRecorder(ledger).wrap(RuntimeRegistry(Runner()))
    loop=make_loop(ledger,runner,tmp_path)
    daemon=S(loop=loop,is_active=lambda _:False)
    sent=[]
    wire=WireSession(ledger,ControlPlane(ledger,daemon),sent.append)
    wire.handle({'type':'job.resume','commandId':'resume1','jobId':job,'requestedBy':'human','note':'fixed and restarted'})
    assert sent[-1]['ok'] is True, sent
    assert len(checked)==1 and ledger.job(job).iteration==2


@pytest.mark.parametrize('failure',['terminal','unknown','budget','unresolved'])
def test_resume_does_not_clear_other_boundaries(tmp_path,failure):
    ledger,job,stop=recorded_stop(tmp_path,'blocked')
    class Runner:
        def __call__(self,*args):pytest.fail('unexpected execution')
        def check_resume(self,*args):raise ValueError('source not applied')
    loop=make_loop(ledger,Runner(),tmp_path)
    if failure=='terminal':ledger.transition(job,'done','cancelled')
    if failure=='unknown':ledger.append_event(job,'step.dispatch.started',{'iteration':1,'step_id':'later'})
    if failure=='budget':loop.policy=__import__('dataclasses').replace(loop.policy,max_iterations=1)
    before=ledger.latest_event_id()
    with pytest.raises((LoopError,ValueError)):loop.resume_step(job,'human','please resume')
    assert ledger.latest_event_id()==before


def test_same_turn_driver_edit_is_stopped_at_bound_broker(setup_scope,monkeypatch):
    s=setup_scope
    steps,_,_,scopes=identity_setup(s,monkeypatch)
    step=steps[1]
    workspace=s.root/'workspace'
    file=workspace/'ai_hardware_autolab/hwtools/scope.py';file.parent.mkdir(parents=True);file.write_text('original')
    policy=ServerPolicy(role='builder',allow_hardware=True,project_root=workspace,device_root=s.root,
        contracts_dir=s.controller.contracts_dir,runs_dir=s.root/'runs',hardware_tool='measure_scope',
        hardware_contract=step['contract'],hardware_contract_sha256=step['contract_sha256'])
    tool=next(t for t in build_tools(policy) if t.name=='measure_scope')
    file.write_text('edited during model turn')
    answer=tool.handler({'contract':step['contract'],'identity_only':True})
    assert answer['execution_blocked']=='platform_deployment' and answer['hardware_contacted'] is False
    assert not scopes
    file.write_text('original')
    answer=tool.handler({'contract':step['contract'],'identity_only':True})
    assert answer['run_verdict']=='PASS' and len(scopes)==1  # denied preflight did not spend the device call


def test_source_map_covers_every_installed_builder_hardware_tool():
    from ai_hardware_autolab.orchestrator.execution_sources import _DRIVERS
    names={t.name for t in build_tools(ServerPolicy(role='builder',allow_hardware=True)) if t.touches_hardware}
    assert names <= _DRIVERS.keys()


def test_cli_resume_is_a_control_message_not_a_new_job(monkeypatch):
    from ai_hardware_autolab.orchestrator import __main__ as cli
    calls=[]
    monkeypatch.setattr(cli,'_send',lambda args,body:calls.append(body) or 0)
    assert cli.main(['resume-step','job_test','--by','human','--note','applied and restarted'])==0
    assert calls==[{'command':'resume-step','job_id':'job_test','requested_by':'human','note':'applied and restarted'}]


def test_restart_resume_runs_only_stopped_device_and_finishes(setup_scope,monkeypatch):
    s=setup_scope
    steps,template,ssh,scopes=identity_setup(s,monkeypatch)
    job,_=start(s,'BBB와 스코프의 연결 정보를 확인해줘')
    workspace_root=s.root/'workspaces'
    workspace=workspace_root/job.job_id
    path='ai_hardware_autolab/hwtools/scope.py'
    for root in (s.root,workspace):
        file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('installed v1')
    class Resolver:
        root=workspace_root
        def __call__(self,task):return self.root/task['job_id']
    def runtime():
        return CodexAgentRunner(template.config,hardware_approval=s.gate,workspace_resolver=Resolver(),client=template.client)
    current=[runtime()]
    class Runner:
        def check_resume(self,*args):current[0].check_resume(*args)
        def __call__(self,agent,task):
            if agent.name=='lead-agent':
                return {'kind':'Plan','agent':agent.name,'objective':task['scenario'],'requirements_complete':True,'steps':steps}
            if agent.name=='verifier':return {'requirements_met':True,'analysis':'Actual BBB result retained, new scope result verified'}
            result=current[0](agent,task)
            if task['step']['id']=='bbb-id':
                (workspace/path).write_text('reviewed v2')
                result['workspace_changes']={'workspace':str(workspace),'changes':[{'path':path}]}
            return result
    runner=Runner()
    s.loop.runner=runner
    s.loop.resource_refresher=lambda scenario,r:{**r,'available_tools':['run_bbb','measure_scope','codex_workspace']}
    s.loop.run(job.job_id)
    assert s.ledger.job(job.job_id).state=='blocked'
    assert len(ssh.scripts)==1 and not scopes
    first=s.ledger.handoffs(job.job_id,'StepResult',1)[0]
    with pytest.raises(CodexRunnerError,match='still not installed'):s.loop.resume_step(job.job_id,'human','not actually fixed')
    (s.root/path).write_text((workspace/path).read_text())
    with pytest.raises(CodexRunnerError,match='restart'):s.loop.resume_step(job.job_id,'human','applied but not restarted')
    current[0]=runtime()  # fresh process source baseline
    fresh=make_loop(JobLedger(s.ledger.path),runner,s.root,scenario_approver=s.loop.scenario_approver,auto_approver=s.loop.auto_approver)
    fresh.resource_refresher=s.loop.resource_refresher
    fresh.resume_step(job.job_id,'human','reviewed, applied and restarted')
    fresh.run(job.job_id)
    assert s.ledger.job(job.job_id).outcome=='satisfied'
    assert len(ssh.scripts)==1 and len(scopes)==1
    kept=s.ledger.handoffs(job.job_id,'StepResult',2)[0]
    assert kept.payload['runs']==first.payload['runs']
    assert kept.payload['reused_from']['handoff_id']==first.handoff_id
    assert not template.client.thread_params
