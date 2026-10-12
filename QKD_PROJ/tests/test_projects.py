"""Real Git snapshots and scheduler; paid model/device boundaries stay fake."""
import hashlib
import subprocess
from pathlib import Path

import pytest

from QKD_PROJ.orchestrator.projects import ProjectRegistry, ProjectWorkspaces
from QKD_PROJ.orchestrator.workspace import WorkspaceManager
from QKD_PROJ.tests.test_conversation_service import setup_flow, SCENARIO
from QKD_PROJ.knowledge.conversation_ingestor import ConversationKnowledgeIngestor
from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.knowledge.store import KnowledgeStore
from QKD_PROJ.orchestrator.wire import WireSession


def repository(parent, name):
    root = parent / name
    root.mkdir()
    (root / 'README.md').write_text('Independent research project', encoding='utf-8')
    (root / 'src').mkdir()
    (root / 'src/base.py').write_text('value = 1', encoding='utf-8')
    for args in [('init',), ('add', '.'), ('-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
                  '-c', 'maintenance.auto=false', '-c', 'gc.auto=0', 'commit', '-m', 'initial')]:
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'src/new.py').write_text('uncommitted = True', encoding='utf-8')
    return root


def test_project_registry_restarts_and_refuses_non_repository(tmp_path):
    source=repository(tmp_path, 'analysis')
    registry=ProjectRegistry(tmp_path/'state/projects.json', source, default_id='analysis')
    row=registry.register({'root':str(source), 'name':'Data analysis', 'test_command':'python -m pytest'})
    again=ProjectRegistry(registry.path, source, default_id='analysis')
    assert again.resolve(row['project_id'])['test_command']=='python -m pytest'
    assert len(again.catalog()['projects'])==1
    with pytest.raises(ValueError, match='최상위'):
        registry.register({'root':str(source/'src')})
    with pytest.raises(ValueError, match='등록'):
        registry.resolve('does-not-exist')


def test_jobs_develop_selected_project_and_ingest_separate_knowledge(tmp_path):
    calls=[]
    def run(role, context):
        project=context['inputs']['project']
        workspace=Path(context['workspace'])
        calls.append((role, project['project_id']))
        assert (workspace/'src/base.py').is_file()
        assert (workspace/'src/new.py').is_file()
        assert not (workspace/'QKD_PROJ/base.py').exists()
        assert 'selected_board' not in context['inputs']
        assert context['knowledge_context']['scope']['project_id']==project['project_id']
        if role=='builder':
            (workspace/'result.py').write_text('answer = 5', encoding='utf-8')
        return {'status':'satisfied' if role=='verifier' else 'ready', 'summary':'Calculated five.',
                'artifacts':['result.py'] if role!='lead' else [],
                'knowledge':[{'topic':'Calculation', 'text':'Use addition. Result: 5. Verify with the project test.'}]}
    default, ledger, flow, daemon, control=setup_flow(tmp_path, run)
    first=repository(tmp_path, 'research-one')
    second=repository(tmp_path, 'research-two')
    registry=ProjectRegistry(tmp_path/'projects.json', default)
    rows=[registry.register({'root':str(root), 'name':root.name}) for root in (first,second)]
    managers=ProjectWorkspaces(flow.manager, registry)
    flow.manager=flow.runtime.manager=managers
    control.project_registry=registry
    store=KnowledgeStore(tmp_path/'knowledge.db')
    from QKD_PROJ.knowledge.retriever import KnowledgeRetriever
    flow.runtime.knowledge_retriever=KnowledgeRetriever(store)
    flow.runtime.knowledge_scope=KnowledgeScope('qkd-proj','rtase-v1','v1.0')
    digest=hashlib.sha256(SCENARIO.encode()).hexdigest()
    jobs=[control.mission('Implement addition',SCENARIO,digest,project_id=row['project_id'])['job']['job_id'] for row in rows]
    assert jobs[0]!=jobs[1]
    assert control.mission('Implement addition',SCENARIO,digest,project_id=rows[0]['project_id'])['reused']
    # A guide update affects new intake, not the existing job's root/guide.
    registry.register({'root':str(first), 'name':'Changed guide'})
    assert flow._brief(jobs[0])['inputs']['project']['name']=='research-one'
    for _ in range(6):
        daemon.tick()
    assert all(ledger.job(job).outcome=='satisfied' for job in jobs)
    assert all(not (root/'result.py').exists() for root in (first,second))
    for row in rows:
        assert [role for role, project in calls if project==row['project_id']]==['lead','builder','verifier']
    ingestor=ConversationKnowledgeIngestor(ledger,store,KnowledgeScope('qkd-proj','rtase-v1','v1.0'))
    assert ingestor.sync()>0
    for row in rows:
        claims=store.search('Calculation',scope=KnowledgeScope(row['project_id']),statuses=('proposed',),limit=20)
        assert claims
        assert all(c.candidate.scope.project_id==row['project_id'] and c.candidate.scope.hardware_id is None for c in claims)
        assert all(c.candidate.object.value['project']['project_id']==row['project_id'] for c in claims)
    assert ingestor.sync()==0


def test_web_frames_register_select_and_reject_missing_projects(tmp_path):
    default,ledger,flow,daemon,control=setup_flow(tmp_path,lambda *_:pytest.fail('no model calls'))
    target=repository(tmp_path,'web-project')
    registry=ProjectRegistry(tmp_path/'projects.json',default)
    managers=ProjectWorkspaces(flow.manager,registry)
    flow.manager=flow.runtime.manager=managers
    control.project_registry=registry
    frames=[]
    wire=WireSession(ledger,control,frames.append)
    wire.handle({'type':'projects.register','commandId':'register','project':{'root':str(target),'name':'Web research'}})
    assert frames[-1]['ok']
    project_id=frames[-1]['project']['project_id']
    wire.handle({'type':'projects.catalog','commandId':'catalog'})
    assert len(frames[-1]['projects'])==2
    wire.handle({'type':'job.mission','commandId':'mission','objective':'Implement addition','scenario':SCENARIO,
                  'scenarioSha256':hashlib.sha256(SCENARIO.encode()).hexdigest(),'searchPolicy':'refresh','projectId':project_id})
    assert frames[-1]['ok']
    brief=flow._brief(frames[-1]['job']['job_id'])
    assert brief['inputs']['project']['project_id']==project_id
    assert (Path(brief['workspace'])/'src/new.py').is_file()
    wire.handle({'type':'job.mission','commandId':'bad','objective':'Implement addition','scenario':SCENARIO,
                  'scenarioSha256':hashlib.sha256(SCENARIO.encode()).hexdigest(),'searchPolicy':'refresh','projectId':'unknown'})
    assert not frames[-1]['ok']


def test_production_daemon_uses_external_default_project_without_board(tmp_path):
    from types import SimpleNamespace
    from QKD_PROJ.orchestrator.__main__ import build_service
    source=repository(tmp_path,'external-default')
    args=SimpleNamespace(ledger=str(tmp_path/'daemon/jobs.db'),runs=str(tmp_path/'runs'),
                         knowledge_db=str(tmp_path/'knowledge.db'),knowledge_project='analysis',
                         profile='offline',dry_run=False,project_root=str(source),workspace_root=str(tmp_path/'workspaces'),
                         backoff=1.0,retries=2,interval=0.1,pipe=None)
    service,_=build_service(args)
    calls=[]
    class Runtime:
        def __init__(self,config,event_sink): pass
        def __call__(self,role,context):
            calls.append(role)
            assert context['inputs']['project']['root']==str(source.resolve())
            assert 'selected_board' not in context['inputs']
            assert (Path(context['workspace'])/'src/new.py').is_file()
            return {'status':'satisfied' if role=='verifier' else 'ready','summary':'External project inspected.'}
    service.daemon.loop.conversation.runtime.factory=Runtime
    job=service.control.mission('Inspect source',SCENARIO,hashlib.sha256(SCENARIO.encode()).hexdigest())['job']['job_id']
    for _ in range(3): service.tick_once()
    assert service.ledger.job(job).outcome=='satisfied'
    assert calls==['lead','builder','verifier']
    assert service.knowledge_store.claims(scope=KnowledgeScope('analysis'),statuses=('proposed',),limit=20)
