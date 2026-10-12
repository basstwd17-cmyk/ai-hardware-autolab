"""Run-name retrieval without loading unrelated full manifests."""
from QKD_PROJ.mcp_server.registry import ServerPolicy, _list_runs, build_tools


def test_literal_filtered_run_list_keeps_total_and_does_not_change_records(tmp_path):
    for name in ('20260909_scope_capture', '20260908_GPIO_SPI', '20260907_spi_read', '20260906_RF'):
        (tmp_path / name).mkdir()
    policy = ServerPolicy(runs_dir=tmp_path)
    result = _list_runs(policy, {'query': 'spi', 'limit': 1})
    assert [r['run'] for r in result['runs']] == ['20260908_GPIO_SPI']
    assert result['total'] == 4 and result['matched_total'] == 2
    assert _list_runs(policy, {'query': '*'})['runs'] == []
    assert len(_list_runs(policy, {})['runs']) == 4
    tool = next(t for t in build_tools(policy) if t.name == 'list_runs')
    assert 'query' in tool.input_schema['properties']
