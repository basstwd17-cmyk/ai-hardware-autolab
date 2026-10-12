"""One UI submission through the real scheduler, isolated Git workspace and results.

Only the paid model boundary is replaced. No operating DB or hardware is used.
"""
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_hardware_autolab.orchestrator.conversation_codex import ConversationCodexConfig
from ai_hardware_autolab.orchestrator.conversation_service import WebConversationFlow, WorkspaceConversationRuntime
from ai_hardware_autolab.orchestrator.control import ControlPlane
from ai_hardware_autolab.orchestrator.daemon import Daemon
from ai_hardware_autolab.orchestrator.jobs import JobLedger
from ai_hardware_autolab.orchestrator.results import build_result
from ai_hardware_autolab.orchestrator.scenario_preflight import ScenarioPreflight
from ai_hardware_autolab.orchestrator.service import _LockedControl
from ai_hardware_autolab.orchestrator.workspace import WorkspaceManager
from ai_hardware_autolab.orchestrator.workflows import WorkflowRouter
from ai_hardware_autolab.tests.test_workspace import _source_repo


SCENARIO = 'Execution scope: offline; no hardware access is permitted.\n\nImplement addition.'


def setup_flow(tmp_path, run, *, dry_run=False):
    source = _source_repo(tmp_path)
    ledger = JobLedger(tmp_path / 'jobs.db')
    manager = WorkspaceManager(source / 'ai_hardware_autolab' / 'workspaces', source)

    class Runtime:
        def __init__(self, config, event_sink):
            self.config, self.sink = config, event_sink

        def __call__(self, role, context):
            self.sink({'method': 'item/completed', 'job_id': context['job_id'],
                       'agent': role, 'attempt_id': context['attempt_id'],
                       'params': {'item': {'type': 'agentMessage', 'text': 'Working on addition'}}})
            return run(role, context)

    runtime = WorkspaceConversationRuntime(ledger, manager, ConversationCodexConfig(manager.root),
                                          factory=Runtime, dry_run=dry_run)
    flow = WebConversationFlow(ledger, runtime, manager)
    router = WorkflowRouter(ledger, conversation=flow)
    daemon = Daemon(ledger, router)
    control = ControlPlane(ledger, daemon, preflight_engine=ScenarioPreflight(contracts_dir=tmp_path / 'no-contracts'))
    return source, ledger, flow, daemon, control


def submit(control):
    return control.mission('Implement addition', SCENARIO, hashlib.sha256(SCENARIO.encode()).hexdigest())['job']['job_id']


def test_web_submission_needs_no_contract_and_finishes_in_three_ticks(tmp_path):
    calls = []

    def run(role, context):
        calls.append(role)
        assert 'plan' not in context and 'contract' not in context
        root = Path(context['workspace'])
        assert (root / 'ai_hardware_autolab/base.py').is_file()
        if role == 'builder':
            (root / 'result.txt').write_text('5', encoding='utf-8')
        return {'status': 'satisfied' if role == 'verifier' else 'ready',
                'summary': '5 is the requested result', 'artifacts': [] if role == 'lead' else ['result.txt']}

    source, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    job_id = submit(control)
    for _ in range(3):
        assert daemon.tick().describe()['advanced']['job_id'] == job_id
    assert calls == ['lead', 'builder', 'verifier']
    assert ledger.job(job_id).outcome == 'satisfied'
    assert not ledger.handoffs(job_id, 'Plan') and not (source / 'result.txt').exists()
    payload, artifacts = build_result(ledger, job_id, runs_dir=tmp_path, project_root=source)
    assert payload['summary'] == '5 is the requested result'
    assert len(artifacts) == 1 and Path(artifacts[0].target_path).read_text() == '5'
    runtime_event = next(e for e in ledger.events_since(0, job_id) if e.kind == 'runtime.item/completed')
    assert runtime_event.payload['params']['item']['text'] == 'Working on addition'
    assert runtime_event.payload['item_type'] == 'agentMessage'
    Path(artifacts[0].target_path).write_text('changed')
    assert build_result(ledger, job_id, runs_dir=tmp_path, project_root=source)[1] == []


def test_question_feedback_returns_to_lead_not_legacy_plan(tmp_path):
    calls = []
    def run(role, context):
        calls.append(role)
        if not context['feedback']:
            return {'status': 'needs_user', 'summary': 'Which input values?'}
        assert context['feedback'][0]['message'] == '2 and 3'
        return {'status': 'ready', 'summary': 'Use 2 and 3'}
    _, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    job_id = submit(control)
    daemon.tick()
    assert daemon.waiting_for(job_id).detail == 'user_input: Which input values?'
    result = control.steer(job_id, '2 and 3', 'local-operator')
    assert result['will_be_delivered'] and ledger.job(job_id).state == 'running'
    daemon.tick()
    assert calls == ['lead', 'lead']
    feedback = [e for e in ledger.events_since(0, job_id) if e.kind == 'conversation.feedback']
    assert feedback[0].agent == 'human'
    assert not control.job(job_id)['pending_steers']
    assert not control.job(job_id)['interrupted']


def test_cancel_committed_before_interrupt_and_never_reopened(tmp_path):
    _, ledger, flow, daemon, control = setup_flow(tmp_path, lambda *_: pytest.fail('must not run'))
    job_id = submit(control)
    def interrupt(identifier):
        assert ledger.job(identifier).outcome == 'cancelled'
        return True
    proxy = _LockedControl(control, threading.RLock(), interrupt)
    proxy.cancel(job_id, 'human')
    assert daemon.tick().advanced is None
    with pytest.raises(Exception, match='finished'):
        proxy.steer(job_id, 'try again', 'human')


def test_midturn_feedback_does_not_wait_for_turn_lock(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def run(role, context):
        entered.set()
        assert release.wait(5)
        return {'status': 'ready', 'summary': 'Initial approach'}
    _, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    job_id = submit(control)
    turn_lock = threading.RLock()
    proxy = _LockedControl(control, turn_lock)
    def tick():
        with turn_lock:
            daemon.tick()
    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert entered.wait(5)
        result = proxy.steer(job_id, 'Use an integer result', 'human')
        assert result['will_be_delivered']
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and flow.phase(job_id).role == 'lead'


def test_dry_run_and_runtime_failure_do_not_auto_replay(tmp_path):
    _, ledger, flow, daemon, control = setup_flow(tmp_path, lambda *_: pytest.fail('dry-run'), dry_run=True)
    job_id = submit(control)
    daemon.tick()
    assert ledger.job(job_id).state == 'blocked'
    assert not control.steer(job_id, 'continue', 'human')['will_be_delivered']
    assert daemon.tick().advanced is None


def test_prose_only_work_does_not_require_a_dummy_file(tmp_path):
    def run(role, context):
        return {'status': 'satisfied' if role == 'verifier' else 'ready', 'summary': 'Explained the existing code.'}
    _, ledger, flow, daemon, control = setup_flow(tmp_path, run)
    job_id = submit(control)
    for _ in range(3):
        daemon.tick()
    assert ledger.job(job_id).outcome == 'satisfied'


def test_production_assembly_completes_without_constructing_legacy_engines(tmp_path, monkeypatch):
    from ai_hardware_autolab.orchestrator import __main__ as entrypoint
    from ai_hardware_autolab.orchestrator.campaign import CampaignController
    from ai_hardware_autolab.orchestrator.contract_compiler import ContractCompiler
    from ai_hardware_autolab.orchestrator.codex_runner import CodexAgentRunner
    from ai_hardware_autolab.orchestrator.loop import Loop
    from ai_hardware_autolab.boards.profiles import BoardCatalog

    def retired(*args, **kwargs):
        pytest.fail('production must not initialize a retired execution engine')

    for kind in (CampaignController, ContractCompiler, CodexAgentRunner, Loop):
        monkeypatch.setattr(kind, '__init__', retired)
    monkeypatch.setattr(BoardCatalog, 'load_default', retired)
    source = _source_repo(tmp_path)
    monkeypatch.setattr(entrypoint, 'PLATFORM_ROOT', source / 'ai_hardware_autolab')
    args = SimpleNamespace(ledger=str(tmp_path / 'production.db'), runs=str(tmp_path / 'runs'),
        profile='offline', dry_run=False, workspace_root=str(source / 'ai_hardware_autolab/workspaces'),
        backoff=1.0, retries=2, interval=0.1, pipe=None)
    service, raw = entrypoint.build_service(args)
    calls = []

    class Runtime:
        def __init__(self, config, event_sink):
            pass

        def __call__(self, role, context):
            calls.append(role)
            assert 'contract' not in context and 'plan' not in context
            return {'status': 'satisfied' if role == 'verifier' else 'ready',
                    'summary': 'The requested architecture explanation is complete.'}

    service.daemon.loop.conversation.runtime.factory = Runtime
    job_id = submit(service.control)
    for _ in range(3):
        service.tick_once()
    assert raw is None and service.campaign_controller is None
    assert calls == ['lead', 'builder', 'verifier']
    assert service.ledger.job(job_id).outcome == 'satisfied'
    assert not service.ledger.handoffs(job_id, 'Plan')
    assert not service.ledger.approvals(job_id)


def test_retired_job_stays_readable_and_does_not_starve_current_work(tmp_path):
    from ai_hardware_autolab.orchestrator.daemon import WAITING_PERSON
    from ai_hardware_autolab.orchestrator.jobs import JobLedger
    from ai_hardware_autolab.orchestrator.workflows import WorkflowRouter

    ledger = JobLedger(tmp_path / 'history.db')
    old = ledger.create_job('old.yaml', 'Historical unfinished job')
    done = ledger.create_job('old.yaml', 'Historical finished job')
    ledger.transition(done.job_id, 'done', 'cancelled', reason='historical cancellation')
    fresh = ledger.create_job('conversation', 'Current scenario')
    seen = []
    conversation = SimpleNamespace(owns=lambda job_id: job_id == fresh.job_id,
                                  advance=lambda job_id: seen.append(job_id))
    router = WorkflowRouter(ledger, conversation=conversation)
    daemon = Daemon(ledger, router)
    before = ledger.latest_event_id()
    assert daemon.waiting_for(old.job_id).status == WAITING_PERSON
    assert daemon.waiting_for(done.job_id).status == 'finished'
    assert router.phase(done.job_id) == 'finished'
    assert daemon.runnable() == [fresh.job_id]
    daemon.tick()
    assert seen == [fresh.job_id]
    assert ledger.latest_event_id() == before
    assert ControlPlane(ledger, daemon).job(old.job_id)['job']['job_id'] == old.job_id
    with pytest.raises(ValueError, match='retired'):
        router.resume_step(old.job_id, 'operator', 'do not replay history')
