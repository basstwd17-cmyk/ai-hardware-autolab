"""Standing scope consent is opt-in; tests never connect to an instrument."""
import copy
from types import SimpleNamespace

import pytest
import yaml

from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.orchestrator import wire as W
from QKD_PROJ.orchestrator.campaign import CampaignController, CampaignPolicy, CampaignPolicyError
from QKD_PROJ.orchestrator.campaign_proposal import CampaignProposalService, CampaignProposalError
from QKD_PROJ.orchestrator.control import ControlPlane
from QKD_PROJ.orchestrator.daemon import HardwareApprovalGate
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.orchestrator.workflows import WorkflowRouter
from QKD_PROJ.tests.test_autonomous_flow_review import make_loop
from QKD_PROJ.tests.test_device_workflow_revision import multiple_devices
from QKD_PROJ.tests.test_campaign_proposal import NOW


@pytest.fixture
def setup_scope(tmp_path):
    ledger, _, old, _, compiled, _, resources = multiple_devices(tmp_path)
    calls = []
    loop = make_loop(ledger, lambda *args: calls.append(args), tmp_path)
    router = WorkflowRouter(ledger, loop, SimpleNamespace(), simple=loop)
    clock = [NOW]
    gate = HardwareApprovalGate(ledger, contracts_dir=old.contracts_dir, project_root=tmp_path, now=lambda: clock[0])
    controller = CampaignController(ledger, loop, gate, campaigns_dir=old.campaigns_dir,
        contracts_dir=old.contracts_dir, project_root=tmp_path, runs_dir=tmp_path/'runs',
        hardware_enabled=True, router=router, now=lambda: clock[0])
    service = CampaignProposalService(ledger, controller, project_root=tmp_path,
        contracts_dir=old.contracts_dir, campaigns_dir=old.campaigns_dir, now=lambda: clock[0])

    def job(scenario='measure CH3', mode='hardware'):
        row, _ = start_simple_mission(ledger, scenario, scenario, resources={**resources, 'execution_mode': mode})
        ledger.transition(row.job_id, 'running')
        ledger.begin_iteration(row.job_id)
        plan = copy.deepcopy(compiled.plan)
        plan['steps'] = plan['steps'][:1]
        ledger.record_handoff(row.job_id, 'Plan', 'lead-agent', plan, iteration=1)
        return ledger.job(row.job_id)

    return SimpleNamespace(ledger=ledger, loop=loop, router=router, gate=gate,
        controller=controller, service=service, clock=clock, job=job,
        origin=job(), step=compiled.plan['steps'][0], calls=calls, root=tmp_path)


def activate(s, mode='scope_session', maximum=24):
    p = s.service.preview(s.origin.job_id, authorization_scope=mode, max_auto_approvals=maximum)
    assert p.ready, p.blockers
    return s.service.approve(s.origin.job_id, 'human', p.review_sha256,
                            authorization_scope=mode, max_auto_approvals=maximum)


def require(s, job, step=None):
    step = step or s.step
    return s.loop._require_approval(s.ledger.job(job.job_id), step, step['id'], s.loop.agent('builder'))


def test_one_review_resolves_pending_and_reuses_on_new_job_and_step(setup_scope):
    s = setup_scope
    first = require(s, s.origin)
    assert first.blocked
    result = activate(s, maximum=3)
    assert result['campaign']['authorization_scope'] == 'scope_session'
    assert s.ledger.approval(first.approval_id).state == 'granted'
    assert require(s, s.origin) is None
    assert s.gate(s.loop.agent('builder'), {'job_id': s.origin.job_id, 'step': s.step}, 'measure_scope', {})
    # A consumed per-step grant renews within the same reviewed campaign.
    second = require(s, s.origin)
    assert not second.blocked
    other = s.job('calculate a different statistic from the same CH3 connection')
    changed_step = {**s.step, 'id': 'new-step-name'}
    third = require(s, other, changed_step)
    assert not third.blocked
    assert s.gate(s.loop.agent('builder'), {'job_id': other.job_id, 'step': changed_step}, 'measure_scope', {})
    assert s.controller.status()['auto_approvals_used'] == 3
    denied = require(s, s.job('another measurement'))
    assert denied.blocked  # budget is global, not reset per job
    assert not s.calls  # no model or instrument invoked by these permission checks


def test_default_is_not_a_reusable_grant_and_mode_is_signed(setup_scope):
    s = setup_scope
    normal = s.service.preview(s.origin.job_id)
    reusable = s.service.preview(s.origin.job_id, authorization_scope='scope_session')
    assert normal.review_sha256 != reusable.review_sha256
    with pytest.raises(CampaignProposalError, match='review changed'):
        s.service.approve(s.origin.job_id, 'human', normal.review_sha256, authorization_scope='scope_session')
    activate(s, 'scenario')
    assert require(s, s.job('different goal')).blocked


@pytest.mark.parametrize('mode', [True, None, 'all', 1, []])
def test_invalid_scope_is_not_coerced(setup_scope, mode):
    with pytest.raises(CampaignProposalError, match='authorization_scope'):
        setup_scope.service.preview(setup_scope.origin.job_id, authorization_scope=mode)


def test_expiry_and_offline_jobs_and_other_tools_remain_closed(setup_scope):
    s = setup_scope
    pending = require(s, s.origin)
    activate(s)
    a = s.loop.auto_approver
    builder = s.loop.agent('builder')
    assert not a.evaluate(s.job('offline', mode='offline'), s.step, builder).allowed
    assert not a.evaluate(s.origin, {**s.step, 'tool': 'scan_jtag'}, builder).allowed
    s.clock[0] = '2026-09-02T01:00:00+00:00'
    assert not a.evaluate(s.origin, s.step, builder).allowed
    # This pending request existed before activation, hence had no own expiry.
    assert s.ledger.approval(pending.approval_id).expires_at is None
    assert not s.gate(builder, {'job_id': s.origin.job_id, 'step': s.step}, 'measure_scope', {})
    assert 'expired' in s.gate.refusals[-1]


@pytest.mark.parametrize('changed', ['contract', 'connection'])
def test_scope_or_target_drift_invalidates_consent(setup_scope, changed):
    s = setup_scope
    activate(s)
    path = s.controller.contracts_dir / (s.step['contract'] if changed == 'contract' else 'board-map.yaml')
    path.write_text(path.read_text() + '\n# changed\n')
    assert require(s, s.job('same instrument')).blocked


def test_cancel_is_not_session_revocation_but_never_revives_cancelled_job(setup_scope):
    s = setup_scope
    activate(s)
    control = ControlPlane(s.ledger, campaign_controller=s.controller)
    control.cancel(s.origin.job_id, 'human')
    assert not s.loop.auto_approver.evaluate(s.ledger.job(s.origin.job_id), s.step, s.loop.agent('builder')).allowed
    # Simulate loss of in-memory state without writing a deactivation event.
    s.controller._clear()
    assert s.controller.recover()['active'] is True
    other = s.job('new goal, same approved instrument')
    request = require(s, other)
    assert not request.blocked
    s.controller.deactivate('human', 'finished using this instrument')
    assert not s.gate(s.loop.agent('builder'), {'job_id': other.job_id, 'step': s.step}, 'measure_scope', {})
    assert not s.controller.recover()['active']
    assert s.ledger.job(s.origin.job_id).outcome == 'cancelled'


def test_explicit_refusal_is_not_overridden_by_activation(setup_scope):
    s = setup_scope
    request = require(s, s.origin)
    s.ledger.decide_approval(request.approval_id, False, 'human')
    activate(s)
    assert require(s, s.origin).outcome == 'needs_approval'


def test_offline_daemon_does_not_restore_device_authority(setup_scope):
    s = setup_scope
    activate(s)
    s.controller._clear()
    s.controller.hardware_enabled = False
    assert not s.controller.recover()['active']


def test_wire_carries_opt_in_and_parser_rejects_unpinned_target(setup_scope):
    s = setup_scope
    sent = []
    control = ControlPlane(s.ledger, campaign_controller=s.controller, campaign_proposer=s.service)
    wire = W.WireSession(s.ledger, control, sent.append)
    frame = {'type': W.CAMPAIGN_BUNDLE_PREVIEW, 'commandId': 'p', 'jobId': s.origin.job_id,
             'authorizationScope': 'scope_session'}
    wire.handle(frame)
    assert sent[-1]['ok'], sent[-1]
    p = sent[-1]['proposal']
    assert p['authorization_scope'] == 'scope_session'
    wire.handle({**frame, 'type': W.CAMPAIGN_BUNDLE_APPROVE, 'commandId': 'a',
                 'approvedBy': 'human', 'reviewSha256': p['review_sha256']})
    assert sent[-1]['ok'], sent[-1]
    path = s.controller.campaigns_dir/sent[-1]['filename']
    data = yaml.safe_load(path.read_text())
    data['permissions'][0]['artifacts'] = {next(iter(data['permissions'][0]['artifacts'])): '0'*64}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(CampaignPolicyError):
        CampaignPolicy.load(path, sha256_file(path), contracts_dir=s.controller.contracts_dir, project_root=s.root)


def test_board_actions_cannot_be_promoted_to_scope_session(tmp_path):
    _, job, _, service, _, _, _ = multiple_devices(tmp_path)
    p = service.preview(job.job_id, authorization_scope='scope_session')
    assert not p.ready
    assert any('스코프 단독' in b for b in p.blockers)
