from __future__ import annotations

import pytest

from QKD_PROJ.hwtools.instruments.rtm3004 import Waveform
from QKD_PROJ.hwtools.measurement_stats import EdgePolicy, waveform_statistics


def test_waveform_statistics_extract_five_ns_rising_edges():
    # 1 ns samples, rising edges at 1, 6, 11 and 16 ns.
    values = [0.0, 3.3, 3.3, 0.0, 0.0] * 4
    waveform = Waveform(
        channel=1,
        x_start_s=0.0,
        x_stop_s=19e-9,
        record_length=20,
        values_per_sample=1,
        values_v=values,
    )

    stats = waveform_statistics(
        waveform,
        policy=EdgePolicy(threshold_v=1.65, hysteresis_v=0.2, direction="rising"),
    )

    assert stats["sample_interval_ns"] == pytest.approx(1.0)
    assert stats["edge_count"] == 4
    assert stats["edge_interval_avg_ns"] == pytest.approx(5.0)
    assert stats["edge_interval_min_ns"] == pytest.approx(5.0)
    assert stats["jitter_pp_ps"] == pytest.approx(0.0, abs=1e-6)
    assert stats["period_s"] == pytest.approx(5e-9)
    assert stats["frequency_hz"] == pytest.approx(200e6)
    assert stats["peak_to_peak_v"] == pytest.approx(3.3)
    assert stats["period_frequency_reason"] is None


def test_waveform_statistics_records_threshold_and_sample_rate():
    waveform = Waveform(2, 0.0, 3e-9, 4, 1, [0.1, 0.2, 3.0, 3.1])
    stats = waveform_statistics(waveform)
    assert stats["edge_threshold_v"] == pytest.approx(1.6)
    assert stats["sample_rate_hz"] == pytest.approx(1e9)


@pytest.mark.parametrize('direction', ['rising', 'falling'])
def test_period_uses_same_polarity_edges_not_absolute_time(direction):
    from types import SimpleNamespace
    waveform = SimpleNamespace(values_v=[0., 1., 1., 0., 0.] * 4,
                               x_start_s=1e12, x_increment_s=1e-9)
    stats = waveform_statistics(waveform, policy=EdgePolicy(direction=direction))
    assert stats['period_s'] == pytest.approx(5e-9)
    assert stats['frequency_hz'] == pytest.approx(200e6)


@pytest.mark.parametrize('values,direction', [
    ([1.] * 12, 'rising'), ([0., 0., 1., 1.], 'rising'),
    ([0., 1., 1., 0., 0.] * 4, 'both'),
])
def test_insufficient_or_mixed_edges_have_no_fabricated_period(values, direction):
    waveform = Waveform(1, 0., (len(values) - 1) * 1e-9, len(values), 1, values)
    stats = waveform_statistics(waveform, policy=EdgePolicy(direction=direction))
    assert stats['period_s'] is None
    assert stats['frequency_hz'] is None
    assert stats['period_frequency_reason']


def test_workspace_recipe_can_consume_the_host_statistics_api(tmp_path):
    # Reproduce the real recipe/host import seam, without importing a live job
    # or contacting an instrument. Transport and safety remain host-owned.
    import hashlib
    from QKD_PROJ.hwtools.workspace_recipe import load_recipe
    path = tmp_path / 'recipe.py'
    path.write_text('from ..measurement_stats import waveform_statistics\n'
                    'def analyse(wave):\n'
                    '    stats = waveform_statistics(wave)\n'
                    '    return {key: stats[key] for key in '
                    '("period_s", "frequency_hz", "peak_to_peak_v", "period_frequency_reason")}\n')
    recipe, _ = load_recipe(tmp_path, 'recipe.py:analyse', hashlib.sha256(path.read_bytes()).hexdigest())
    result = recipe(Waveform(3, 0., 19e-9, 20, 1, [0., 1., 1., 0., 0.] * 4))
    assert result['period_s'] == pytest.approx(5e-9)
    assert result['frequency_hz'] == pytest.approx(200e6)


def test_samples_on_threshold_do_not_add_edges():
    waveform = Waveform(1, 0., 8e-9, 9, 1, [0., 1., .5, .5, .5, 0., .5, .5, 1.])
    stats = waveform_statistics(waveform, policy=EdgePolicy(threshold_v=.5))
    assert stats['edge_count'] == 2
    assert stats['period_s'] == pytest.approx(7e-9)
