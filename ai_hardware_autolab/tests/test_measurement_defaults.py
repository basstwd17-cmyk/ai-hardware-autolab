"""Offline regression: small observations, explicit qualification, compact evidence."""
from __future__ import annotations

import copy
import json
import struct
from pathlib import Path

import pytest

from ai_hardware_autolab.hwtools import RunRecord
from ai_hardware_autolab.hwtools.instruments import RTM3004
from ai_hardware_autolab.hwtools.recipes import probe_comp_check
from ai_hardware_autolab.hwtools.scope import measure_scope
from ai_hardware_autolab.hwtools.workspace_recipe import CaptureCommands
from ai_hardware_autolab.hwtools.safety import SafetyViolation
from ai_hardware_autolab.mcp_server.registry import ServerPolicy, _read_run
from ai_hardware_autolab.orchestrator.development import compact_task
from ai_hardware_autolab.tests.test_measure_scope import RESPONSES, PARAMS, selftest_gate


def capture(tmp_path, monkeypatch, **params):
    values = [0.0, 0.0, 2.0, 2.0] * 250
    original = RTM3004.__init__
    def init(scope, *a, **kw):
        original(scope, *a, **kw)
        scope.scpi.query_binary_block = lambda command: struct.pack(f'<{len(values)}f', *values)
    monkeypatch.setattr(RTM3004, '__init__', init)
    gate = selftest_gate()
    record = RunRecord('small-observation', gate, runs_dir=tmp_path)
    responses = {**RESPONSES, '*OPT?': 'K1', 'ACQ:POIN?': '100000',
                 'CHAN3:DATA:HEAD?': '0,0.00999,1000,1',
                 'MEAS1:RES:AVG?': '25000', 'MEAS1:RES:WFMC?': '1', 'MEAS2:RES:WFMC?': '1'}
    result = measure_scope(probe_comp_check, params, gate, record, mock=True,
                           screenshot=False, mock_responses=responses)
    return result, record


def test_default_observation_captures_once_without_old_qualification(tmp_path, monkeypatch):
    result, record = capture(tmp_path, monkeypatch, expected_probe_attenuation=10)
    assert result.ok, result.error
    assert result.commands.count('SING') == 1
    assert not any(command.startswith('ACQ:NSIN:COUN') for command in result.commands)  # no K15 needed
    assert 'ACQ:POIN 100000' in result.commands
    assert result.measurements['requested_acquisition_count'] == 1
    assert result.verdict is None  # 25 kHz is not a failure against the old 1 kHz spec
    assert result.measurements['performance_verdict'] == 'not_requested'
    assert result.measurements['frequency_hz'] == pytest.approx(25000)
    assert result.measurements['peak_to_peak_v'] == 2
    assert result.measurements['jitter_rms_ps'] is None
    assert result.measurements['stddev_voltage_v'] is None
    analysis = json.loads((record.dir/'waveform_analysis.json').read_text())
    assert analysis['statistics_level'] == 'basic'
    assert analysis['waveforms'][0]['period_s'] == pytest.approx(1/25000)
    assert analysis['artifacts'][0]['sha256']
    assert analysis['artifacts'][0]['path'].endswith('.csv')


def test_explicit_precision_is_not_capped(tmp_path, monkeypatch):
    result, _ = capture(tmp_path, monkeypatch, record_length=2_000_000, statistics_level='full')
    assert result.ok, result.error
    assert 'ACQ:POIN 2000000' in result.commands
    assert result.measurements['configured_record_length'] == 100000  # actual readback, not requested value
    assert result.measurements['jitter_rms_ps'] is not None
    assert result.measurements['stddev_voltage_v'] is not None


def test_intentional_existing_record_length_is_preserved(tmp_path, monkeypatch):
    result, _ = capture(tmp_path, monkeypatch, record_length=None)
    assert result.ok
    assert not any(command.startswith('ACQ:POIN') for command in result.commands)


def test_probe_mismatch_stops_before_acquisition(tmp_path, monkeypatch):
    result, _ = capture(tmp_path, monkeypatch, expected_probe_attenuation=1)
    assert not result.ok
    assert 'SING' not in result.commands


def test_explicit_contract_qualification_still_checks_thresholds():
    result = measure_scope(probe_comp_check, PARAMS, selftest_gate(), screenshot=False,
                           mock=True, mock_responses={**RESPONSES, 'MEAS1:RES:AVG?': '1050'})
    assert result.verdict == 'FAIL'
    assert 'ACQ:NSIN:COUN 50' in result.commands


def test_single_capture_resets_prior_nx_count_when_history_is_installed():
    scope = RTM3004('unused', mock=True, mock_responses=RESPONSES)
    scope.arm_single()
    assert scope.scpi.commands()[-3:] == ['ACQ:NSIN:COUN 1', 'SYST:ERR:ALL?', 'SING']


def test_capture_command_scope_allows_size_not_power_or_other_channels():
    guard = CaptureCommands(None, {3})
    guard.check('ACQ:POIN 100000', query=False)
    guard.check('ACQ:POIN?', query=True)
    for command in ('ACQ:POIN 100000;WGEN:OUTP ON', 'WGEN:OUTP ON', 'CHAN2:COUP DCL', '*RST'):
        with pytest.raises(SafetyViolation):
            guard.check(command, query=False)


def test_default_profile_capture_uses_same_small_path_through_command_boundary(tmp_path, monkeypatch):
    from ai_hardware_autolab.hwtools.recipes import profile_channel_capture
    from ai_hardware_autolab.hwtools.workspace_recipe import bounded_recipe
    from ai_hardware_autolab.hwtools.instruments import Waveform
    def read(scope, channel, binary=True):
        # Exercise the protected front-end/timebase/count path; avoid a real socket.
        return Waveform(channel, 0, .0019, 20, 1, [0., 0., 1., 1.]*5)
    monkeypatch.setattr(RTM3004, 'read_waveform', read)
    gate = selftest_gate()
    result = measure_scope(bounded_recipe(profile_channel_capture, {3}), {
        'channels': [{'channel': 3, 'signal': 'test', 'scale_v_per_div': .5}],
        'timebase_s_per_div': .001, 'trigger_channel': 3, 'trigger_level_v': .5,
        'timeout_s': 1,
    }, gate, RunRecord('profile-observation', gate, runs_dir=tmp_path),
        mock=True, screenshot=False, mock_responses={**RESPONSES, 'ACQ:POIN?': '100000'})
    assert result.ok, result.error
    assert result.commands.count('SING') == 1
    assert 'ACQ:NSIN:COUN 1' in result.commands
    assert 'ACQ:POIN 100000' in result.commands
    assert result.measurements['period_s'] is not None
    assert any(a.kind == 'waveform_analysis' for a in result.artifacts)


def test_large_valid_manifest_is_summarized_not_broken_json(tmp_path):
    directory = tmp_path/'sample_run'
    directory.mkdir()
    data = {'steps': [{'ok': False, 'error': 'failure after many captures',
                      'commands': ['SING'] * 2000,
                      'measurements': {'period_s': .001, 'sequential_rounds': [
                          {'round': i, 'detail': 'x'*3000} for i in range(100)]}}],
            'artifacts': [{'path': 'trace.csv', 'sha256': 'abc'}]}
    path = directory/'manifest.json'
    encoded = json.dumps(data)
    path.write_text(encoded)
    summary = _read_run(ServerPolicy(runs_dir=tmp_path), {'run': directory.name})
    assert summary['manifest']['steps'][0]['error'] == 'failure after many captures'
    assert summary['manifest']['steps'][0]['measurements']['period_s'] == .001
    assert summary['manifest']['artifacts'] == data['artifacts']
    assert len(summary['omitted']) == 2
    assert len(json.dumps(summary)) < len(encoded)/5
    assert _read_run(ServerPolicy(runs_dir=tmp_path), {'run': directory.name, 'detail': 'full'})['manifest'] == data
    assert path.read_text() == encoded


def test_context_compaction_retains_instructions_authority_and_real_results():
    row = {'runs': ['r1'], 'summary': 'needs repair', 'measurements': {'period_s': None},
           'execution_errors': ['SCPI -222'], 'hardware_state_unknown': True,
           'verification': {'passed': False}, 'artifacts': [{'path': 'trace.csv', 'sha256': 'abc'}],
           'workspace_changes': {'change_digest': 'hash', 'changes': ['large']*5000}}
    task = {'scenario': 'observe CH3', 'steering': [{'message': 'one trace'}],
            'plan': {'steps': [{'instructions': 'keep the resolution'}]},
            'resources': {'scenario_access': {'device': 'scope'}, 'capability_candidates': [1]},
            'previous_step_results': [row], 'step_results': [row], 'previous_iteration_results': [row]}
    before = copy.deepcopy(task)
    compact = compact_task(task, retain_catalog=True)
    assert task == before
    for key in ('scenario','steering','plan','resources'):
        assert compact[key] == task[key]
    for key in ('previous_step_results', 'step_results','previous_iteration_results'):
        reduced = compact[key][0]
        assert reduced['execution_errors'] == row['execution_errors']
        assert reduced['hardware_state_unknown'] is True
        assert reduced['measurements'] == row['measurements']
        assert reduced['artifacts'] == row['artifacts']
        assert 'workspace_changes' not in reduced
    assert len(json.dumps(compact)) < len(json.dumps(task))/10


def test_summary_bounds_status_and_manifest_output_without_hiding_errors(tmp_path):
    directory = tmp_path / 'noisy_run'
    directory.mkdir()
    data = {'verdict': 'FAIL', 'steps': [{'ok': False, 'error': 'SSH authentication failed',
            'stdout': 'head\n' + 'repeated lines\n' * 3000 + '\ntail',
            'measurements': {'period_s': None}}]}
    encoded = json.dumps(data)
    for name in ('run_status.json', 'manifest.json'):
        (directory / name).write_text(encoded)
    summary = _read_run(ServerPolicy(runs_dir=tmp_path), {'run': directory.name})
    for key in ('status', 'manifest'):
        assert summary[key]['steps'][0]['error'] == 'SSH authentication failed'
        assert summary[key]['steps'][0]['measurements']['period_s'] is None
        assert len(summary[key]['steps'][0]['stdout']) < 1600
    assert summary['omitted'] and summary['status_omitted']
    full = _read_run(ServerPolicy(runs_dir=tmp_path), {'run': directory.name, 'detail': 'full'})
    assert full['status'] == data and full['manifest'] == data
    assert (directory / 'manifest.json').read_text() == encoded


def test_prompt_compaction_removes_only_exact_scenario_duplicates():
    from ai_hardware_autolab.orchestrator.development import MODE
    scenario = 'current constraints and requested output ' * 100
    contract = {'scenario': scenario, 'sha256': 'pinned', 'mode': 'hardware'}
    task = {'scenario': scenario, 'resources': {'development_mode': MODE,
            'scenario_access': {'allow_flash': False}, 'mission_contract': contract,
            'preflight': {'scenario': scenario, 'mission_contract': contract, 'approval_required': False}}}
    before = copy.deepcopy(task)
    reduced = compact_task(task, retain_catalog=True)
    assert task == before
    assert reduced['scenario'] == scenario
    assert reduced['resources']['scenario_access'] == task['resources']['scenario_access']
    assert reduced['resources']['mission_contract']['sha256'] == 'pinned'
    assert reduced['resources']['preflight']['approval_required'] is False
    assert json.dumps(reduced).count(scenario) == 1
    different = copy.deepcopy(task)
    different['resources']['mission_contract']['scenario'] = 'different requirement'
    assert compact_task(different, retain_catalog=True)['resources']['mission_contract']['scenario'] == 'different requirement'
