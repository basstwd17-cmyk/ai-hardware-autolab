from pathlib import Path
from types import SimpleNamespace

from QKD_PROJ.knowledge.conversation_ingestor import ConversationKnowledgeIngestor, knowledge_notes
from QKD_PROJ.knowledge.models import KnowledgeScope
from QKD_PROJ.knowledge.store import KnowledgeStore
from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.service import Service


def setup(tmp_path, board='other-board'):
    ledger = JobLedger(tmp_path / 'jobs.db')
    store = KnowledgeStore(tmp_path / 'knowledge.db')
    runtime = lambda role, ctx: {'status':'satisfied' if role == 'verifier' else 'ready',
                                'summary':'PWM implementation requires a fresh measurement',
                                'knowledge':[{'topic':'PWM', 'text':'Use a timer; output not yet measured.'}]}
    flow = ConversationFlow(ledger, runtime)
    job = flow.create('Develop PWM', tmp_path, inputs={'selected_board':{'board_id':board,'revision':'r2'}})
    scope = KnowledgeScope('proj', 'rtase-v1', 'v1.0')
    return ledger, store, flow, job.job_id, scope


def test_auto_ingestion_next_role_and_finished_job(tmp_path):
    ledger, store, flow, job_id, scope = setup(tmp_path)
    observed = []
    original = flow.runtime
    def runtime(role, ctx):
        observed.append(store.latest_event_id())
        return original(role, ctx)
    flow.runtime = runtime
    daemon = SimpleNamespace(tick=lambda _:flow.advance(job_id))
    service = Service(ledger, daemon, knowledge_store=store, knowledge_scope=scope)
    for _ in range(3): service.tick_once()
    assert observed == [0,1,2]
    claims = store.events_since(0,100)
    assert len(claims) == 3
    for event in claims:
        assert event.payload['status'] == 'proposed'
        assert event.payload['candidate']['scope'] == {'project_id':'proj','hardware_id':'other-board','revision':'r2'}
    # Terminal jobs are still captured on an idle service restart.
    restarted = ConversationKnowledgeIngestor(ledger,store,scope)
    restarted.sync()
    assert store.latest_event_id() == 3


def test_restart_after_partial_write_and_no_device_or_model_replay(tmp_path, monkeypatch):
    ledger, store, flow, job_id, scope = setup(tmp_path)
    flow.advance(job_id)
    sync = ConversationKnowledgeIngestor(ledger,store,scope)
    advance = store.advance_ingestion
    def crash(source, event_id):
        if store.latest_event_id(): raise OSError('disk unavailable')
        advance(source,event_id)
    monkeypatch.setattr(store,'advance_ingestion',crash)
    sync.sync()
    assert sync.last_error == 'OSError' and store.latest_event_id() == 1
    monkeypatch.setattr(store,'advance_ingestion',advance)
    flow.runtime = lambda *_: (_ for _ in ()).throw(AssertionError('no runtime replay'))
    sync.sync()
    assert sync.last_error is None and store.latest_event_id() == 1
    assert store.ingestion_cursor(sync.source) == ledger.latest_event_id()


def test_host_observation_is_exact_not_inferred_success(tmp_path):
    ledger, store, flow, job_id, scope = setup(tmp_path)
    ledger.append_event(job_id,'conversation.device.completed',{
        'tool':'measure_scope','run':'capture-1','run_verdict':'FAIL',
        'result':{'ok':False,'measurements':{'frequency_hz':123}},
    },'platform')
    ledger.append_event(job_id,'conversation.device.failed',{
        'hardware_state_unknown':True,'error_type':'TimeoutError'},'platform')
    sync = ConversationKnowledgeIngestor(ledger,store,scope)
    sync.sync()
    events = store.events_since(0,100)
    assert [e.payload['status'] for e in events] == ['verified','proposed']
    observed = events[0].payload['candidate']['object']['value']['observation']
    assert observed['result']['measurements']['frequency_hz'] == 123
    assert observed['run_verdict'] == 'FAIL'
    assert 'not functional acceptance' in events[0].payload['checks'][0]['detail']


def test_bad_optional_notes_and_knowledge_failure_do_not_block_job(tmp_path, monkeypatch):
    assert knowledge_notes([{}, {'topic':'PWM','text':'measured'},None]) == [{'topic':'PWM','text':'measured'}]
    ledger, store, flow, job_id, scope = setup(tmp_path)
    monkeypatch.setattr(store,'record',lambda *_: (_ for _ in ()).throw(OSError('readonly')))
    service = Service(ledger,SimpleNamespace(tick=lambda _:flow.advance(job_id)),
                      knowledge_store=store,knowledge_scope=scope)
    for _ in range(3): service.tick_once()
    assert ledger.job(job_id).state == 'done'
    assert service.knowledge_ingestor.last_error == 'OSError'


def test_sources_artifacts_and_revision_scopes_are_preserved(tmp_path):
    ledger, store, flow, job_id, scope = setup(tmp_path)
    (tmp_path/'code.c').write_text('code')
    flow.runtime = lambda *_:{'status':'ready','summary':'PWM is implemented, untested',
        'artifacts':['code.c'], 'sources':[{'title':' Timer manual\n','url':'https://example.com/timer','finding':' Use PWM mode.\n'}]}
    flow.advance(job_id)
    sync = ConversationKnowledgeIngestor(ledger,store,scope)
    sync.sync(limit=1)
    while store.ingestion_cursor(sync.source) < ledger.latest_event_id(): sync.sync(limit=1)
    events = store.events_since(0,100)
    assert len(events) == 2 and all(e.payload['status'] == 'proposed' for e in events)
    values = [e.payload['candidate']['object']['value'] for e in events]
    assert values[0]['artifacts'][0]['path'] == 'code.c'
    assert len(values[0]['artifacts'][0]['sha256']) == 64
    assert values[1]['url'] == 'https://example.com/timer'


def test_without_board_does_not_inherit_rtase(tmp_path):
    ledger, store, flow, _, scope = setup(tmp_path)
    job = flow.create('Source research', tmp_path)
    flow.advance(job.job_id)
    ConversationKnowledgeIngestor(ledger,store,scope).sync()
    assert store.events_since(0,100)[0].payload['candidate']['scope'] == {'project_id':'proj'}
