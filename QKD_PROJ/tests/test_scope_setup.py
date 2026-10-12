from pathlib import Path
import hashlib

import pytest

from QKD_PROJ.boards.hardware import HardwareProfile
from QKD_PROJ.boards.scope_setup import validate_connections, is_connection_confirmation, scenario_connections
from QKD_PROJ.hwtools.safety import SafetyViolation
from QKD_PROJ.orchestrator.conversation_flow import ConversationFlow


@pytest.mark.parametrize("message", ["아직 연결 안 했어", "연결 완료?", "네", "CH3로 바꿔줘"])
def test_ambiguous_answers_do_not_assert_physical_connection(message):
    assert not is_connection_confirmation(message)


@pytest.mark.parametrize("rows", [[{"channel": True, "signal": "RF1"}],
    [{"channel": 5, "signal": "RF1"}], [{"channel": 3, "signal": "RF1", "max_abs_input_v": 30}],
    [{"channel": 3, "signal": "RF1"}, {"channel": 3, "signal": "MOSI"}]])
def test_wiring_cannot_supply_electrical_limits_or_duplicate_channels(rows):
    with pytest.raises(ValueError):
        validate_connections(rows)


def test_connection_overlay_preserves_installed_limits_and_profile_bytes():
    profile = HardwareProfile.load(Path(__file__).resolve().parents[2], 'rtase-v1', 'v1.0')
    original = profile.path.read_bytes()
    gate = profile.compatibility_gate(scope_connections=[{'channel': 3, 'signal': 'RF1'}])
    gate.require_scope_capture({'channels': [{'channel': 3, 'signal': 'RF1'}]})
    with pytest.raises(SafetyViolation):
        gate.require_scope_capture({'channels': [{'channel': 3, 'signal': 'MOSI'}]})
    with pytest.raises(SafetyViolation):
        gate.require_measured_voltage({'CH3_min_v': 0, 'CH3_max_v': 3.31})
    row = gate.contracts.test_spec['hardware_safety']['scope_channels'][2]
    assert row['termination'] == '1_Mohm' and row['probe_attenuation'] == 10
    assert profile.path.read_bytes() == original
    assert profile.data['scope']['channels'][2]['signal'] == 'MOSI'


def test_model_cannot_claim_a_completed_connection(tmp_path):
    with pytest.raises(ValueError, match='connection question'):
        ConversationFlow.validate_reply('lead', {'status': 'ready', 'summary': 'Connected',
            'scope_connections': [{'channel': 3, 'signal': 'RF1'}]}, tmp_path, device_tools=['measure_scope'])


@pytest.mark.parametrize('text', ['CH3=RF1', '스코프 CH3: RF1', 'RF1을 CH3로 측정해줘',
                                  'CH3에서 RF1 측정', 'RF1은 CH3에 연결되어 있어',
                                  'RF1을 스코프 CH3로 측정해줘', 'CH3에는 RF1',
                                  'CH3=RF1. 프로브 변경하지 않고 진행해줘'])
def test_scenario_channel_assignment_is_accepted_without_confirmation(text):
    assert scenario_connections(text) == [{'channel': 3, 'signal': 'RF1'}]


def test_multiple_scenario_channels_do_not_become_overlapping_assignments():
    assert scenario_connections('CH1=CS CH2=CLK CH3=RF1') == [
        {'channel': 1, 'signal': 'CS'}, {'channel': 2, 'signal': 'CLK'}, {'channel': 3, 'signal': 'RF1'}]


@pytest.mark.parametrize('text', ['RF1 파형을 확인해줘', 'CH3=RF1로 연결하면 측정해줘',
                                  'CH3는 RF1이 아니야', 'CH3=RF1?', '예시: CH3=RF1'])
def test_goal_or_conditional_is_not_an_actual_channel_assignment(text):
    assert scenario_connections(text) == []


@pytest.mark.parametrize('declared', [True, False])
def test_web_connection_confirmation_updates_capture_and_next_job(tmp_path, monkeypatch, declared):
    from QKD_PROJ.tests.test_conversation_service import setup_flow
    from QKD_PROJ.orchestrator.control import ControlPlane
    from QKD_PROJ.orchestrator.scenario_preflight import ScenarioPreflight
    from QKD_PROJ.orchestrator.conversation_hardware import ConversationHardwareExecutor, ConversationHardwareConfig
    from QKD_PROJ.orchestrator.jobs import JobLedger
    from QKD_PROJ.hwtools import scope
    from QKD_PROJ.hwtools.result import ToolResult

    connection = [{'channel': 3, 'signal': 'RF1'}]
    params = {'channels': [{'channel': 3, 'signal': 'RF1', 'scale_v_per_div': 0.2}],
              'timebase_s_per_div': 0.0002, 'trigger_channel': 3, 'trigger_level_v': 0.5,
              'timeout_s': 3}
    def runtime(role, context):
        if not context['scope_connections']:
            return {'status': 'needs_user', 'summary': 'Move the probe to RF1.', 'scope_connections': connection}
        assert context['scope_connections'] == connection
        if role == 'lead':
            return {'status': 'ready', 'summary': 'Use the confirmed RF1 channel.'}
        if role == 'builder' and not context['device_results']:
            return {'status': 'device', 'summary': 'Measure RF1.',
                    'device_call': {'tool': 'measure_scope', 'arguments': {'params': params}}}
        return {'status': 'satisfied' if role == 'verifier' else 'ready',
                'summary': 'RF1 fixture measured.', 'runs': [context['device_results'][-1]['run']]}

    source, ledger, flow, daemon, _ = setup_flow(tmp_path, runtime)
    project = Path(__file__).resolve().parents[2]
    profile = HardwareProfile.load(project, 'rtase-v1', 'v1.0')
    before = profile.path.read_bytes()
    calls = []
    def capture(_recipe, settings, gate, record):
        gate.require_scope_capture(settings)
        calls.append(settings)
        result = ToolResult('scope', 'capture', verdict='PASS')
        result.measurements.update(CH3_min_v=0.1, CH3_max_v=1.0)
        record.add(result)
        return result
    monkeypatch.setattr(scope, 'measure_scope', capture)
    flow.runtime.device_executor = ConversationHardwareExecutor(
        ledger, profile, ConversationHardwareConfig(project, tmp_path / 'runs'))
    control = ControlPlane(ledger, daemon, preflight_engine=ScenarioPreflight(tmp_path / 'no-contracts'),
                           hardware_profile=profile, hardware_enabled=True)
    def submit(mapping=False):
        scenario = 'Execution scope: hardware. Measure RF1.'
        if mapping:
            scenario += '\n스코프 CH3=RF1. 이 설정으로 계속 진행해줘.'
        return control.mission('Measure RF1', scenario, hashlib.sha256(scenario.encode()).hexdigest())['job']['job_id']
    job_id = submit(declared)
    daemon.tick()
    if declared:
        assert ledger.job(job_id).state == 'running'
        assert not [e for e in ledger.events_since(0, job_id) if e.kind == 'conversation.feedback']
    else:
        assert ledger.job(job_id).state == 'blocked' and calls == []
        assert ledger.scope_connection_events(profile.sha256) == []
        control.steer(job_id, '아직 연결 안 했어', 'operator')
        daemon.tick()
        assert ledger.job(job_id).state == 'blocked' and calls == []
        assert ledger.scope_connection_events(profile.sha256) == []
        assert control.steer(job_id, '연결 완료', 'operator')['will_be_delivered']
    for _ in range(8):
        daemon.tick()
        if ledger.job(job_id).state == 'done':
            break
    assert ledger.job(job_id).outcome == 'satisfied' and calls == [params]
    assert not ledger.approvals(job_id)
    events = JobLedger(ledger.path).scope_connection_events(profile.sha256)
    assert len(events) == 1 and events[0].payload['connections'] == connection
    assert JobLedger(ledger.path).scope_connection_events('other-profile') == []
    next_job = submit()
    assert flow._context(next_job, 'lead')['scope_connections'] == connection
    assert flow._context(next_job, 'lead')['inputs']['scope_channels'][2]['max_abs_input_v'] == 3.3
    assert profile.path.read_bytes() == before
