"""Regression of the actual stalled path. No model requests or real devices."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from QKD_PROJ.agents.schema import load_simple_team
from QKD_PROJ.hwtools.result import sha256_file
from QKD_PROJ.hwtools.safety import Contracts, SafetyGate, SafetyViolation
from QKD_PROJ.hwtools.workspace_recipe import CaptureCommands, load_recipe
from QKD_PROJ.mcp_server.registry import ServerPolicy, build_tools
from QKD_PROJ.orchestrator.codex_runner import CodexAgentRunner, CodexRunnerConfig, build_app_server_command
from QKD_PROJ.orchestrator.contract_compiler import ContractCompiler
from QKD_PROJ.orchestrator.daemon import HardwareApprovalGate
from QKD_PROJ.orchestrator.development import MODE, normalize_plan
from QKD_PROJ.orchestrator.jobs import JobLedger
from QKD_PROJ.orchestrator.loop import Loop
from QKD_PROJ.orchestrator.simple_mission import start_simple_mission


def event(command, code, identifier=None):
    return {"method": "item/completed", "params": {"item": {
        "id": identifier, "type": "commandExecution", "command": command, "exitCode": code}}}


@pytest.mark.parametrize("commands,checks,expected", [
    ([event('python -m pytest target -q', 0), event('icacls tmp', 1)], [], "PASS"),
    ([event('python -m pytest target -q', 1), event('echo done', 0)], [], "FAIL"),
    ([event('python -m pytest target -q', 1), event('python -m pytest target -q', 0)], [], "PASS"),
    ([event('echo pytest passed', 0)], [], "WARNING"),
    ([event('echo done', 0)], ['python -m pytest target -q'], "WARNING"),
    ([event('pytest target', 0)], ['pytest target', 'pytest missing'], "WARNING"),
    ([], [], "WARNING"),
    ([event('python custom_check.py', 0, 'actual-check'), event('icacls tmp', 1)], ['actual-check'], "PASS"),
])
def test_verification_not_last_shell_exit(tmp_path, commands, checks, expected):
    config = CodexRunnerConfig(project_root=tmp_path, qkd_root=tmp_path/'QKD_PROJ', runs_dir=tmp_path/'runs')
    runner = CodexAgentRunner(config=config)
    answer = runner._record_workspace_run({'status': 'done', 'verification_commands': checks},
        {'resources': {'development_mode': MODE}, 'step': {'id': 'work', 'contract': '__workspace__'}},
        config, {'changes': [], 'outputs': [], 'outside_write_scope': []}, commands)
    assert answer['verdict'] == expected
    assert answer['status'] == ('failed' if expected == 'FAIL' else 'done')
    manifest = json.loads((config.runs_dir/answer['runs'][0]/'manifest.json').read_text())
    assert manifest['steps'][0]['measurements']['execution_integrity'] is (expected != 'FAIL')


def test_tests_are_stale_after_a_new_edit(tmp_path):
    from QKD_PROJ.orchestrator.workspace_checks import verification_evidence
    evidence = verification_evidence({}, [event('pytest target', 0),
        {'method': 'item/completed', 'params': {'item': {'type': 'fileChange', 'status': 'completed'}}}])
    assert evidence['passed'] is None


def test_plain_expectations_are_reviewed_not_looked_up_as_fake_metrics():
    p = normalize_plan({'steps': [{'id': 'report', 'tool': 'codex_workspace',
        'advance_when': [{'field': 'report.completeness', 'expect': 'explain findings', 'source': 'scenario'}]}]},
        {'development_mode': MODE})
    assert p['steps'][0]['advance_when'][0]['field'] == 'measurements.execution_integrity'
    assert p['steps'][0]['acceptance_criteria'][0]['expect'] == 'explain findings'


def environment(tmp_path):
    contracts = tmp_path/'contracts'
    contracts.mkdir()
    original = Path(__file__).parents[1]/'contracts/scope-selftest-spec.yaml'
    (contracts/'scope.yaml').write_bytes(original.read_bytes())
    (contracts/'board-map.yaml').write_text(yaml.safe_dump({'scope': {
        'model': 'RTM3004', 'host': '127.0.0.1', 'port': 5025, 'resource': 'fixture-only'}}))
    ledger = JobLedger(tmp_path/'jobs.db')
    r = {'development_mode': MODE, 'execution_mode': 'hardware', 'available_tools': ['measure_scope', 'codex_workspace']}
    job, _ = start_simple_mission(ledger, 'scope', 'Capture the approved scope channel only', resources=r)
    ledger.transition(job.job_id, 'running')
    ledger.begin_iteration(job.job_id)
    p = {'kind': 'Plan', 'agent': 'lead-agent', 'gate': '__mission__', 'iteration': 1,
         'objective': 'Measure only CH3', 'requirements_complete': True,
         'steps': [{'id': 'capture', 'agent': 'builder', 'tool': 'measure_scope', 'contract': 'scope.yaml',
                    'touches_hardware': True, 'instructions': 'capture', 'entrypoint': 'capture.py:measure'}]}
    plan = ContractCompiler(contracts, project_root=tmp_path).compile(p, job_id=job.job_id, iteration=1, scenario_sha256='', resources=r).plan
    ledger.record_handoff(job.job_id, 'Plan', 'lead-agent', plan, iteration=1)
    return ledger, job, plan, contracts, r


def test_one_human_approval_executes_actual_workspace_recipe_and_records_source(tmp_path, monkeypatch):
    ledger, job, plan, contracts, r = environment(tmp_path)
    builder = next(a for a in load_simple_team() if a.name == 'builder')
    loop = Loop(ledger, lambda *_: pytest.fail('cannot run model before approval'), agents=load_simple_team())
    pending = loop.advance(job.job_id)
    approval = ledger.approval(pending.approval_id)
    assert set(approval.artifacts) == {'contracts/scope.yaml', 'contracts/board-map.yaml'}
    ledger.decide_approval(approval.approval_id, granted=True, decided_by='human')
    gate = HardwareApprovalGate(ledger, contracts_dir=contracts, project_root=tmp_path)
    assert gate(builder, {'job_id': job.job_id, 'step': plan['steps'][0]}, 'measure_scope', {}), gate.refusals
    assert not gate(builder, {'job_id': job.job_id, 'step': plan['steps'][0]}, 'measure_scope', {})

    # A previously valid recipe is replaced by Builder in the job workspace.
    source = tmp_path/'capture.py'
    source.write_text('raise AssertionError("stale recipe")')
    source.write_text('from . import __name__ as trusted_package\n'
                      'from ..scope import RecipeOutcome\n'
                      'def measure(scope, params, gate):\n'
                      '    scope.scpi.write("CHAN3:COUP DCL")\n'
                      '    f = scope.front_end(3)\n'
                      '    return RecipeOutcome(measurements={"sample_count": 1, "retrieval_path": "fixture", "revision": "builder-new"}, probed_channels=(3,))\n')
    from QKD_PROJ.hwtools.instruments import RTM3004
    from QKD_PROJ.tests.test_measure_scope import RESPONSES
    monkeypatch.setattr('QKD_PROJ.hwtools.scope.RTM3004', lambda **_: RTM3004(host='mock', mock=True, mock_responses=RESPONSES))
    def screenshot(self, path):
        path.write_bytes(b'fixture screenshot')
        return path
    monkeypatch.setattr(RTM3004, 'save_screenshot', screenshot)
    policy = ServerPolicy(allow_hardware=True, project_root=tmp_path, contracts_dir=contracts, runs_dir=tmp_path/'runs',
        role='builder', hardware_tool='measure_scope', hardware_contract='scope.yaml',
        hardware_contract_sha256=sha256_file(contracts/'scope.yaml'), workspace_recipe='capture.py:measure',
        workspace_recipe_sha256=sha256_file(source))
    tool = next(t for t in build_tools(policy) if t.name == 'measure_scope')
    captured = tool.handler({'contract': 'scope.yaml', 'params': {'channel': 3}})
    assert captured['result']['ok'], captured
    assert captured['result']['measurements']['revision'] == 'builder-new'
    run = Path(captured['run_dir'])
    assert (run/'executed_recipe.py').read_bytes() == source.read_bytes()
    manifest = json.loads((run/'manifest.json').read_text())
    assert manifest['steps'][0]['measurements']['executed_source_sha256'] == sha256_file(source)
    assert manifest['steps'][0]['measurements']['execution_completed'] is True
    assert len(manifest['steps']) == 1
    assert not (tmp_path/'bitstream').exists()
    with pytest.raises(Exception, match='consumed'):
        tool.handler({'contract': 'scope.yaml', 'params': {'channel': 3}})


def test_changed_device_settings_invalidate_approval(tmp_path):
    ledger, job, plan, contracts, r = environment(tmp_path)
    builder = next(a for a in load_simple_team() if a.name == 'builder')
    loop = Loop(ledger, lambda *_: None, agents=load_simple_team())
    pending = loop.advance(job.job_id)
    ledger.decide_approval(pending.approval_id, granted=True, decided_by='human')
    (contracts/'board-map.yaml').write_text('scope: {host: other}\n')
    gate = HardwareApprovalGate(ledger, contracts_dir=contracts, project_root=tmp_path)
    assert not gate(builder, {'job_id': job.job_id, 'step': plan['steps'][0]}, 'measure_scope', {})
    assert 'hashes' in gate.refusals[-1]


def test_scope_capture_cannot_enable_outputs_or_other_channels():
    class Transport:
        def write(self, command):
            raise AssertionError('forbidden command must not reach transport')
    guard = CaptureCommands(Transport(), {3})
    for command in ('WGEN:OUTP ON', 'CHAN3:COUP DC', 'CHAN2:STAT ON', '*RST', 'TRIG:A:SOUR CH2', 'STOP;WGEN:OUTP ON'):
        with pytest.raises(SafetyViolation):
            guard.write(command)


def test_recipe_version_change_is_not_silently_executed(tmp_path):
    p = tmp_path/'capture.py'
    p.write_text('def measure(*args): return 1')
    digest = sha256_file(p)
    p.write_text('def measure(*args): return 2')
    with pytest.raises(ValueError, match='changed'):
        load_recipe(tmp_path, 'capture.py:measure', digest)


def test_failed_step_goes_to_review_not_later_hardware(tmp_path):
    ledger, job, p, contracts, r = environment(tmp_path)
    ledger.record_handoff(job.job_id, 'StepResult', 'builder', {'kind': 'StepResult', 'agent': 'builder',
                          'step_id': 'earlier-work', 'status': 'failed', 'runs': ['failed-run']}, iteration=1, step_id='earlier-work')
    loop = Loop(ledger, lambda *_: pytest.fail('must not execute later hardware'), agents=load_simple_team())
    assert loop.phase(job.job_id) == 'reduce'
    ledger.record_handoff(job.job_id, 'Verdict', 'verifier', {'outcome': 'open'}, iteration=1)
    assert loop.phase(job.job_id) == 'decide'


def test_missing_recipe_is_repairable_without_consuming_device_authority(tmp_path):
    builder = next(a for a in load_simple_team() if a.name == 'builder')
    config = CodexRunnerConfig(profile='hardware', project_root=tmp_path, runs_dir=tmp_path/'runs')
    runner = CodexAgentRunner(config=config, hardware_approval=lambda *_: pytest.fail('no device authority should be consumed'))
    result, _ = runner._invoke(builder, {'resources': {'development_mode': MODE}, 'step': {
        'id': 'capture', 'tool': 'measure_scope', 'contract': 'scope.yaml', 'touches_hardware': True,
        'entrypoint': 'missing.py:measure'}}, config)
    assert result['status'] == 'failed' and result['runs']
    assert 'no hardware contacted' in result['summary']


def test_existing_probe_recipe_can_be_reused_through_workspace_boundary(tmp_path):
    from QKD_PROJ.hwtools.workspace_recipe import bounded_recipe
    from QKD_PROJ.tests.test_measure_scope import PARAMS, run, selftest_gate
    source = Path(__file__).parents[1]/'hwtools/recipes/probe_comp_check.py'
    destination = tmp_path/'probe_comp_check.py'
    destination.write_bytes(source.read_bytes())
    recipe, _ = load_recipe(tmp_path, 'probe_comp_check.py:probe_comp_check', sha256_file(destination))
    result = run(bounded_recipe(recipe, {3}), PARAMS, selftest_gate())
    assert result.ok, result.error
    assert result.verdict == 'PASS'
    assert 'MEAS1:MAIN FREQ' in result.commands


def test_recipe_binding_reaches_the_mcp_subprocess(tmp_path):
    config = CodexRunnerConfig(codex_command=('codex',), profile='hardware', project_root=tmp_path,
        hardware_tool='measure_scope', hardware_contract='scope.yaml', hardware_contract_sha256='a'*64,
        workspace_recipe='capture.py:measure', workspace_recipe_sha256='b'*64)
    command = build_app_server_command(config, role='builder')
    arguments = next(item.split('=', 1)[1] for item in command if '.args=' in item)
    args = json.loads(arguments)
    assert args[args.index('--workspace-recipe')+1] == 'capture.py:measure'
    assert args[args.index('--workspace-recipe-sha256')+1] == 'b'*64


def test_consumed_campaign_grant_is_renewed_without_a_second_human(tmp_path):
    from QKD_PROJ.tests.test_campaign import make_policy, prepared_job, hardware_step, AGENTS, NOW
    from QKD_PROJ.orchestrator.campaign import CampaignAutoApprover
    from dataclasses import replace
    policy, _, _ = make_policy(tmp_path)
    policy = replace(policy, permissions=(replace(policy.permissions[0], max_approvals=2),))
    ledger = JobLedger(tmp_path/'jobs.db')
    job = prepared_job(ledger)
    approver = CampaignAutoApprover(ledger, policy, now=lambda: NOW)
    loop = Loop(ledger, lambda *_: pytest.fail('only approval scheduling'), auto_approver=approver)
    step, agent = hardware_step(), AGENTS['board-operator']
    first = loop._require_approval(job, step, 's1', agent)
    gate = HardwareApprovalGate(ledger, contracts_dir=tmp_path/'contracts', project_root=tmp_path,
        active_campaign_sha256=policy.sha256,
        active_campaign_contracts={'g.yaml': policy.permissions[0].contract_sha256}, now=lambda: NOW)
    assert gate(agent, {'job_id': job.job_id, 'step': step}, 'program_cpld_sram', {})
    second = loop._require_approval(job, step, 's1', agent)
    assert first.approval_id != second.approval_id
    renewed = ledger.approval(second.approval_id)
    assert renewed.state == 'granted' and renewed.decided_by == 'policy:night-1'


def test_unused_advisory_catalog_is_not_loaded_during_daemon_setup(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from QKD_PROJ.orchestrator import __main__ as entrypoint
    from QKD_PROJ.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
    def drift(*_, allow_partial=False):
        pytest.fail('current startup does not load the legacy reuse catalog')
    monkeypatch.setattr(CapabilityCatalog, 'load_default', drift)
    args = Namespace(ledger=str(tmp_path/'jobs.db'), runs=str(tmp_path/'runs'),
        contracts=str(Path(__file__).parents[1]/'contracts'), profile='offline', dry_run=True,
        workspace_root=str(tmp_path/'workspaces'),
        max_iterations=4, backoff=1.0, retries=2, interval=0.1, pipe=None)
    service, raw = entrypoint.build_service(args)
    assert service.control.capability_catalog is None
    assert 'reuse catalog' not in capsys.readouterr().err
