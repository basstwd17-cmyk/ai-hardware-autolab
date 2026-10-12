"""Offline syntax repair and planning-only recovery; no real model or device."""
import copy
import json
from dataclasses import replace

import pytest

from QKD_PROJ.orchestrator.plan_format import decode_json, PlanFormatError
from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig, CodexTurnFailed
from QKD_PROJ.orchestrator.jobs import JobLedger, JobLedgerError
from QKD_PROJ.orchestrator.pagination import iter_events
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission
from QKD_PROJ.tests.test_codex_runner import FakeClient, agent
from QKD_PROJ.tests.test_autonomous_development import plan, resources
from QKD_PROJ.tests.test_autonomous_flow_review import make_loop


def test_comma_repair_preserves_all_plan_values_and_is_bounded():
    expected = plan()
    expected['hardware_boundaries'] = {'scope': {'allowed_operations': ['scope_identify'], 'max_voltage': 3.3}}
    encoded = json.dumps(expected)
    malformed = encoded.replace(', "agent"', ' "agent"', 1)
    edits = []
    assert decode_json(malformed, edits) == expected
    assert len(edits) == 1 and edits[0]['operation'] == 'insert_comma'
    with pytest.raises(json.JSONDecodeError):
        decode_json(malformed, edits)


@pytest.mark.parametrize('text', ['{"a":1', '{"a": "unterminated}', '{"a":1 "b":2 "c":3}',
                                   '{"allow":false "allow":true}', '{"a":1,}'])
def test_ambiguous_or_multi_edit_input_is_not_guessed(text):
    with pytest.raises(json.JSONDecodeError):
        decode_json(text, [])


def test_actual_runner_repairs_nested_plan_without_another_model_turn(tmp_path):
    p = plan()
    wire = {'kind': 'Plan', 'agent': 'lead-agent',
            'handoff': json.dumps(p).replace(', "agent"', ' "agent"', 1)}
    client = FakeClient(wire)
    runner = CodexAgentRunner(config=CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path/'QKD_PROJ', runs_dir=tmp_path/'runs'), client=client)
    result = runner(replace(agent('lead-agent'), native_web=False), {'iteration': 1})
    recovery = result.pop('_format_recovery')
    assert recovery['edits'][0]['operation'] == 'insert_comma'
    assert json.loads(recovery['raw_response']) == wire
    assert result == p
    assert len(client.turn_params) == 1


def test_builder_bad_json_is_never_automatically_replayed(tmp_path):
    client = FakeClient('{"kind":"StepResult" "agent":"builder"}')
    runner = CodexAgentRunner(config=CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path/'QKD_PROJ'), client=client)
    with pytest.raises(CodexTurnFailed):
        runner(replace(agent('builder'), touches_hardware=False), {'step': {'touches_hardware': False}, 'resources': {'write_scope': []}})
    assert len(client.turn_params) == 1


def setup(tmp_path):
    ledger = JobLedger(tmp_path/'jobs.db')
    job, _ = start_simple_mission(ledger, 'code', 'implement algorithm', resources=resources())
    return ledger, job.job_id


def test_failed_plan_is_preserved_then_explicit_retry_keeps_job_and_disables_search(tmp_path):
    ledger, job = setup(tmp_path)
    calls = []
    def runner(a, task):
        calls.append(copy.deepcopy(task))
        if len(calls) == 1:
            raise PlanFormatError('syntax error', '{"kind":"Plan","steps":[')
        assert a.name == 'lead-agent'
        return plan()
    loop = make_loop(ledger, runner, tmp_path)
    assert loop.advance(job).blocked
    failure = [e for e in iter_events(ledger, job) if e.kind == 'plan.format_failed'][0]
    assert failure.payload['raw_response'] == '{"kind":"Plan","steps":['
    assert failure.payload['iteration'] == 1
    before = ledger.handoffs(job)[0].sha256
    assert loop.resume_step(job, 'operator', 'reuse local materials')['resumed_step'] == 'plan'
    with pytest.raises(Exception): loop.resume_step(job, 'operator', 'double click')
    assert not loop.advance(job).blocked
    assert ledger.handoffs(job, 'Plan', 1)
    assert calls[1]['research_plan']['search_budget'] == 0
    assert calls[1]['plan_recovery']['original_response_available'] is True
    assert ledger.handoffs(job)[0].sha256 == before
    assert not ledger.handoffs(job, 'StepResult')
    assert not [e for e in iter_events(ledger, job) if e.kind == 'step.dispatch.started']


def legacy_stop(ledger, job):
    ledger.transition(job, 'running'); ledger.begin_iteration(job)
    ledger.transition(job, 'blocked', reason="runtime_failure: lead-agent could not run while planning: lead-agent returned an invalid handoff JSON string: Expecting ',' delimiter")


def test_legacy_clipped_response_is_explicitly_a_replan_not_a_fake_repair(tmp_path):
    ledger, job = setup(tmp_path)
    legacy_stop(ledger, job)
    calls = []
    loop = make_loop(ledger, lambda a,t: calls.append(t) or plan(), tmp_path)
    loop.resume_step(job, 'operator', 'replan')
    loop.advance(job)
    assert calls[0]['plan_recovery']['original_response_available'] is False
    assert calls[0]['plan_recovery']['rejected_response'] == ''
    assert ledger.job(job).iteration == 1


@pytest.mark.parametrize('guard', ['Plan', 'StepResult', 'dispatch', 'cancel'])
def test_resume_cannot_replay_device_work_or_cancelled_job(tmp_path, guard):
    ledger, job = setup(tmp_path)
    legacy_stop(ledger, job)
    if guard in ('Plan', 'StepResult'):
        ledger.record_handoff(job, guard, 'lead-agent' if guard == 'Plan' else 'builder', {})
    elif guard == 'dispatch':
        ledger.append_event(job, 'step.dispatch.started', {'iteration': 1, 'step_id': 'device'})
    else:
        ledger.transition(job, 'done', 'cancelled')
    before = ledger.latest_event_id()
    with pytest.raises(JobLedgerError): ledger.resume_plan_format(job, 'operator', 'try')
    assert ledger.latest_event_id() == before


def test_retry_attempt_consumed_before_call_and_not_replayed_after_crash(tmp_path):
    ledger, job = setup(tmp_path)
    legacy_stop(ledger, job)
    def crash(*a): raise KeyboardInterrupt('simulated process death')
    loop = make_loop(ledger, crash, tmp_path)
    loop.resume_step(job, 'operator', 'retry')
    with pytest.raises(KeyboardInterrupt): loop.advance(job)
    fresh = make_loop(JobLedger(tmp_path/'jobs.db'), lambda *a:pytest.fail('replayed'), tmp_path)
    assert fresh.advance(job).blocked
    with pytest.raises(JobLedgerError): fresh.resume_step(job, 'operator', 'again')


def test_resume_transaction_rolls_back_on_failure(tmp_path, monkeypatch):
    ledger, job = setup(tmp_path)
    legacy_stop(ledger, job)
    original = ledger._append
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('transaction crash')
    monkeypatch.setattr(ledger, '_append', crash)
    before = ledger.latest_event_id()
    with pytest.raises(RuntimeError): ledger.resume_plan_format(job, 'operator', 'retry')
    assert ledger.latest_event_id() == before and ledger.job(job).state == 'blocked'


def test_repaired_plan_passes_normal_compiler_and_keeps_auditable_original(tmp_path):
    ledger, job = setup(tmp_path)
    p = plan()
    encoded = json.dumps(p).replace(', "agent"', ' "agent"', 1)
    client = FakeClient({'kind': 'Plan', 'agent': 'lead-agent', 'handoff': encoded})
    runner = CodexAgentRunner(config=CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path/'QKD_PROJ', runs_dir=tmp_path/'runs'), client=client)
    loop = make_loop(ledger, runner, tmp_path)
    loop.agents['lead-agent'] = replace(loop.agents['lead-agent'], native_web=False)
    assert not loop.advance(job).blocked
    assert len(client.turn_params) == 1
    stored = ledger.handoffs(job, 'Plan', 1)[0].payload
    assert '_format_recovery' not in stored
    assert stored['steps'][0]['contract'] == '__workspace__'
    event = next(e for e in iter_events(ledger, job) if e.kind == 'plan.format_repaired')
    assert json.loads(event.payload['raw_response'])['handoff'] == encoded


def test_planning_retry_disables_native_web_at_runtime(tmp_path):
    p = plan()
    client = FakeClient({'kind':'Plan','agent':'lead-agent','handoff':json.dumps(p)})
    runner = CodexAgentRunner(config=CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path/'QKD_PROJ'), client=client)
    runner(agent('lead-agent'), {'research_plan': {'search_budget':0}, 'plan_recovery': {'mode':'replan_from_saved_context'}})
    assert 'dynamicTools' not in client.thread_params[0]
    assert len(client.turn_params) == 1


def test_one_replan_cannot_start_another_schema_correction_turn(tmp_path):
    ledger, job = setup(tmp_path)
    legacy_stop(ledger, job)
    calls = []
    loop = make_loop(ledger, lambda a,t: calls.append(t) or {'kind':'Plan','agent':'lead-agent','steps':'invalid'}, tmp_path)
    loop.resume_step(job, 'operator', 'try once')
    assert loop.advance(job).blocked
    assert len(calls) == 1
