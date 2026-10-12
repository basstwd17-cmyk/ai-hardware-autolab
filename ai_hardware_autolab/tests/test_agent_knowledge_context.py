"""Offline memory selection and actual three-role dispatch, without model/device calls."""
import copy
import json

import pytest

from ai_hardware_autolab.knowledge.agent_context import AgentKnowledgeContext, MAX_CHARS, SCOPE_PREFIXES, _focus, _terms
from ai_hardware_autolab.knowledge.models import KnowledgeCandidate, KnowledgeScope
from ai_hardware_autolab.knowledge.reducer import FilesystemEvidenceResolver, KnowledgeReducer
from ai_hardware_autolab.knowledge.retriever import KnowledgeRetriever
from ai_hardware_autolab.knowledge.store import KnowledgeStore


SCOPE = KnowledgeScope('qkd-proj', 'bbb', 'v1')


@pytest.fixture
def memory(tmp_path):
    store = KnowledgeStore(tmp_path / 'memory.db')
    reducer = KnowledgeReducer(FilesystemEvidenceResolver(tmp_path))

    def add(name, *, statement=None, scope=SCOPE, kind='component', evidence_kind='file',
            proposed=False, refute=False, entity=None, value=1, tags=()):
        import hashlib
        path = tmp_path / ('runs' if evidence_kind == 'run' else 'sources') / f'{name}.json'
        path.parent.mkdir(exist_ok=True)
        data = b'{"ok": true}'
        path.write_bytes(data)
        evidence = [] if proposed else [{
            'kind': evidence_kind, 'role': 'refutes' if refute else 'supports',
            'locator': path.relative_to(tmp_path).as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
        }]
        raw = {'kind': 'KnowledgeCandidate', 'producer': 'builder', 'scope': scope.to_dict(),
               'subject': {'kind': kind, 'id': name, 'label': name}, 'relation': 'observed',
               'object': {'kind': 'entity', 'entity': {'kind': 'software', 'id': entity, 'label': entity}}
                         if entity else {'kind': 'literal', 'value': value},
               'statement': statement or f'BBB DMA {name}', 'disposition': 'refute' if refute else 'assert',
               'evidence': evidence, 'source_job_id': 'job_old', 'tags': list(tags)}
        claim = reducer.reduce(KnowledgeCandidate.parse(raw))
        store.record(claim)
        return claim

    return AgentKnowledgeContext(KnowledgeRetriever(store), SCOPE), add


def task(query='BBB DMA', **extra):
    return {'scenario': query, 'resources': {'knowledge_scope': SCOPE.to_dict()}, **extra}


def ids(packet):
    return {c['claim_id'] for c in packet['claims']}


@pytest.mark.parametrize('prefix', SCOPE_PREFIXES)
def test_generated_permission_prefix_never_consumes_search_terms(prefix):
    scenario = 'BBB와 RTASE V1 CPLD의 GPIO-SPI로 CODE0 CODE3 레지스터 읽기. 스코프 CH1 P9_17'
    current = task(prefix + '\r\n\r\n' + scenario)
    before = copy.deepcopy(current)
    query = _focus('lead-agent', current)
    assert query == _focus('lead-agent', task(scenario))
    assert {'bbb', 'cpld', 'gpio-spi', 'code0', 'code3', 'ch1', 'p9_17'} <= set(query.split())
    assert not {'submitting', 'delegates', 'authorized', 'devices'} & set(query.split())
    assert current == before  # Never change the signed scenario/authority.


def test_real_user_scope_constraints_and_feedback_remain_searchable():
    current = task('Execution scope: unusual ADC design\nSTM32G474 ADC',
                   steering=[{'message': 'DMA overflow 수정'}])
    query = _focus('lead-agent', current)
    assert {'unusual', 'adc', 'stm32g474', 'dma', 'overflow'} <= set(query.split())
    assert {'bbb', 'pru0', 'stm32g474', 'dma'} <= set(_terms('BBB에서 PRU0으로 STM32G474의 DMA를'))


def test_prefixed_request_retrieves_technical_fact_not_permission_boilerplate(memory):
    select, add = memory
    relevant = add('spi', statement='BBB GPIO-SPI CODE0 CODE3 register readback')
    noise = add('permission', proposed=True, statement='submitting delegates devices authorized below')
    packet = select('lead-agent', task(SCOPE_PREFIXES[1] + '\n\nBBB에서 GPIO-SPI CODE0 CODE3 읽어줘'))
    assert relevant.claim_id in ids(packet)
    assert noise.claim_id not in ids(packet)


def test_scope_revision_global_and_provenance(memory):
    select, add = memory
    good = add('code')
    common = add('common', scope=KnowledgeScope())
    wrong = add('stm32', scope=KnowledgeScope('qkd-proj', 'stm32', 'v1'))
    revision = add('v2', scope=KnowledgeScope('qkd-proj', 'bbb', 'v2'))
    packet = select('builder', task())
    assert {good.claim_id, common.claim_id} <= ids(packet)
    assert not {wrong.claim_id, revision.claim_id} & ids(packet)
    row = next(c for c in packet['claims'] if c['claim_id'] == good.claim_id)
    assert row['version_id'] == good.version_id and row['scope'] == SCOPE.to_dict()
    assert row['evidence'][0]['sha256'] == good.candidate.evidence[0].sha256
    assert row['source_job_id'] == 'job_old'


def test_failure_is_retained_and_unverified_is_limited(memory):
    select, add = memory
    for n in range(8):
        add(f'good-{n}')
        add(f'hint-{n}', proposed=True)
    failure = add('failure', kind='failure', refute=True)
    packet = select('builder', task())
    assert failure.claim_id in ids(packet)
    assert packet['claims'][0]['status'] == 'refuted'
    assert len(packet['claims']) <= 6
    assert sum(c['status'] == 'proposed' for c in packet['claims']) <= 2
    assert packet['omitted_count'] > 0


def test_exact_neighbor_expansion_does_not_leak_other_board(memory):
    select, add = memory
    add('seed', statement='uniquequery bridge', entity='shared')
    neighbor = add('related', statement='timing connection', entity='shared')
    other = add('wrong', statement='timing connection', entity='shared',
                scope=KnowledgeScope('qkd-proj', 'stm32', 'v1'))
    packet = select('lead-agent', task('uniquequery'))
    row = next(c for c in packet['claims'] if c['claim_id'] == neighbor.claim_id)
    assert row['selection'] == {'via': 'exact_entity_neighbor', 'entity': 'software:shared'}
    assert other.claim_id not in ids(packet)


def test_recorded_unmet_condition_is_a_failure_lesson_not_a_refuted_fact(memory):
    select, add = memory
    for n in range(8):
        add(f'positive-{n}')
    unmet = add('unmet-run', kind='run', evidence_kind='run', tags=['measurement', 'unmet'])
    packet = select('verifier', task())
    assert packet['claims'][0]['claim_id'] == unmet.claim_id
    assert packet['claims'][0]['status'] == 'verified'  # the observation, not a PASS


def test_role_ranking_and_builder_step_focus(memory):
    select, add = memory
    code = add('code', statement='BBB DMA', evidence_kind='file')
    run = add('run', statement='BBB DMA', evidence_kind='run')
    assert select('lead-agent', task())['claims'][0]['claim_id'] == code.claim_id
    assert select('verifier', task())['claims'][0]['claim_id'] == run.claim_id
    packet = select('builder', task(step={'intent': 'repair period_s', 'tool': 'measure_scope'}))
    assert packet['query'].startswith('repair period_s measure_scope')


def test_budget_skips_whole_oversized_claim_and_does_not_modify_store(memory):
    select, add = memory
    huge = add('huge', value='x' * 10000)
    small = add('small')
    before = select.store.latest_event_id()
    packet = select('builder', task())
    assert len(json.dumps(packet, ensure_ascii=False)) <= MAX_CHARS
    assert small.claim_id in ids(packet) and huge.claim_id not in ids(packet)
    assert packet['omitted_count'] == 1
    assert select.store.latest_event_id() == before


def test_new_facts_refreshed_and_superseded_facts_excluded(memory):
    select, add = memory
    old = add('old')
    assert old.claim_id in ids(select('lead-agent', task()))
    new = add('new')
    select.store.supersede(old.claim_id, new.claim_id, reason='new evidence')
    result = select('lead-agent', task())
    assert old.claim_id not in ids(result) and new.claim_id in ids(result)


def test_empty_or_unrelated_query_does_not_dump_recent_memory(memory):
    select, add = memory
    add('bbb')
    assert select('lead-agent', task(''))['claims'] == []
    assert select('lead-agent', task('unrelatedcamera'))['claims'] == []


def test_fts_unavailable_still_matches_terms_not_whole_sentence(memory):
    select, add = memory
    select.store._fts = False
    with select.store._connect() as connection:
        connection.execute('CREATE TABLE knowledge_fts_fallback (claim_id TEXT PRIMARY KEY, '
                           'statement TEXT, subject TEXT, object_text TEXT, tags TEXT)')
    found = add('dma', statement='BBB DMA buffering')
    assert found.claim_id in ids(select('builder', task('Please implement BBB DMA transfer')))
    # '_' must not match any arbitrary character in the fallback path.
    add('wildcard', statement='periodXs')
    assert select.store.search('period_s', scope=SCOPE) == []


def test_three_role_flow_selects_once_per_turn_and_preserves_authority(tmp_path, memory):
    from ai_hardware_autolab.tests.test_autonomous_development import plan, resources
    from ai_hardware_autolab.agents.schema import load_simple_team
    from ai_hardware_autolab.orchestrator.jobs import JobLedger
    from ai_hardware_autolab.orchestrator.loop import Loop, LoopPolicy
    from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission

    select, add = memory
    add('existing')
    ledger = JobLedger(tmp_path / 'jobs.db')
    runs = tmp_path / 'runs'
    (runs / 'run-1').mkdir(parents=True)
    (runs / 'run-1' / 'manifest.json').write_text(json.dumps({'verdict': 'PASS', 'steps': [
        {'tool': 'codex_workspace', 'action': 'implement', 'measurements': {'last_command_exit_code': 0}}]}))
    calls = []
    verifier_attempt = 0

    def runner(agent, current):
        nonlocal verifier_attempt
        calls.append((agent.name, copy.deepcopy(current)))
        if agent.name == 'lead-agent':
            return plan()
        if agent.name == 'builder':
            add('just-recorded', kind='failure', evidence_kind='run')
            return {'kind': 'StepResult', 'status': 'done', 'runs': ['run-1']}
        verifier_attempt += 1
        if verifier_attempt == 1:
            return {'requirements_met': 'true'}  # read-only repair, no repeated retrieval
        return {'kind': 'Verdict', 'agent': 'verifier', 'requirements_met': True, 'analysis': 'actual run checked'}

    authority = {'devices': ['BBB'], 'allow_flash': False}
    r = {**resources(), 'scenario_access': authority, 'knowledge_scope': SCOPE.to_dict(),
         'knowledge_context': {'claims': ['old broad snapshot']}}
    job, _ = start_simple_mission(ledger, 'DMA', 'BBB DMA', resources=r)
    loop = Loop(ledger, runner, runs_dir=runs, agents=load_simple_team(), planner_agent='lead-agent',
                reasoning_verifier=True, policy=LoopPolicy(report_on_finish=False), agent_knowledge=select)
    loop.run(job.job_id)
    assert ledger.job(job.job_id).outcome == 'satisfied'
    assert [name for name, _ in calls] == ['lead-agent', 'builder', 'verifier', 'verifier']
    for name, current in calls:
        assert 'knowledge_context' not in current
        packet = current['resources']['knowledge_context']
        assert packet['role'] == name and isinstance(packet['claims'][0], dict)
        assert current['resources']['scenario_access'] == authority
    assert any(c['subject']['id'] == 'just-recorded' for c in calls[2][1]['resources']['knowledge_context']['claims'])
    assert calls[2][1]['resources']['knowledge_context'] == calls[3][1]['resources']['knowledge_context']
    events = [e for e in ledger.events_since(0) if e.kind == 'knowledge.context.selected']
    assert len(events) == 3
    assert all(e.payload['claims'][0]['version_id'] for e in events)


def test_memory_failure_falls_back_without_stale_snapshot_or_job_block(tmp_path):
    from ai_hardware_autolab.agents.schema import load_simple_team
    from ai_hardware_autolab.orchestrator.jobs import JobLedger
    from ai_hardware_autolab.orchestrator.loop import Loop
    from ai_hardware_autolab.orchestrator.simple_mission import start_simple_mission
    from ai_hardware_autolab.orchestrator.development import MODE
    ledger = JobLedger(tmp_path / 'jobs.db')
    job, _ = start_simple_mission(ledger, 'test', 'BBB')
    def broken(*_):
        raise OSError('unavailable')
    loop = Loop(ledger, lambda *_: {}, agent_knowledge=broken)
    current = task(job_id=job.job_id)
    current['resources'].update(development_mode=MODE, knowledge_context={'claims': ['stale']})
    result = loop._with_agent_knowledge(load_simple_team()[0], current)
    assert result['resources']['knowledge_context']['unavailable'] is True
    assert result['resources']['knowledge_context']['claims'] == []
    assert ledger.job(job.job_id).state == 'pending'
    assert current['resources']['knowledge_context']['claims'] == ['stale']
