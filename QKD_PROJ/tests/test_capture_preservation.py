"""Acquired samples survive analysis errors, with no real device access."""
import json

import pytest

from QKD_PROJ.hwtools import RunRecord
from QKD_PROJ.hwtools.scope import RecipeOutcome
from QKD_PROJ.hwtools.workspace_recipe import bounded_recipe
from QKD_PROJ.tests.test_measure_scope import run, selftest_gate, RESPONSES


@pytest.mark.parametrize('mode', ['recipe_error', 'statistics_error', 'success', 'repeat'])
def test_acquisition_is_saved_before_analysis(tmp_path, mode):
    gate = selftest_gate()
    record = RunRecord('capture-preservation', gate, runs_dir=tmp_path)
    responses = {**RESPONSES, 'CHAN3:DATA:HEAD?': '0,0.003,4,1',
                 'CHAN3:DATA?': '0,1,0,1'}

    def recipe(scope, params, gate):
        waveform = scope.read_waveform(3, binary=False)
        if mode == 'recipe_error':
            raise KeyError('period_s')
        if mode == 'repeat':
            scope.read_waveform(3, binary=False)
        return RecipeOutcome(measurements={'sample_count': 4, 'retrieval_path': 'waveform'},
                             waveforms=(waveform,))

    result = run(bounded_recipe(recipe, {3}),
                 {'edge_direction': 'invalid'} if mode == 'statistics_error' else {},
                 gate, record=record, responses=responses)
    record.finish()
    paths = sorted(record.dir.glob('recipe_CH3*.csv'))
    assert len(paths) == (2 if mode == 'repeat' else 1)
    assert all('time_s,voltage_v' in path.read_text() for path in paths)
    assert all(len(path.read_text().splitlines()) == 5 for path in paths)
    manifest = json.loads((record.dir / 'manifest.json').read_text())
    assert 'waveform_csv' in json.dumps(manifest)
    if mode in {'recipe_error', 'statistics_error'}:
        assert not result.ok
        assert result.verdict == 'FAIL'
    assert 'WGEN:OUTP OFF' not in result.commands


def test_preservation_does_not_bypass_bounded_transport(tmp_path):
    gate = selftest_gate()
    record = RunRecord('bounded-capture', gate, runs_dir=tmp_path)

    def recipe(scope, params, gate):
        scope.read_waveform(4, binary=False)

    result = run(bounded_recipe(recipe, {3}), {}, gate, record=record)
    assert not result.ok
    assert 'CHAN4:DATA:HEAD?' not in result.commands
    assert not list(record.dir.glob('*.csv'))
