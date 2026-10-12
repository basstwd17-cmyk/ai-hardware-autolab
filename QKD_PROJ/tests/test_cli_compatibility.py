"""Old launch flags must not block the current contract-free daemon."""
from types import SimpleNamespace

import pytest

from QKD_PROJ.orchestrator import __main__ as cli


@pytest.mark.parametrize('option', [
    ['--contracts', r'QKD_PROJ\contracts'],
    [r'--contracts=QKD_PROJ\contracts'],
])
def test_legacy_contracts_flag_reaches_current_daemon_without_becoming_runtime_config(monkeypatch, capsys, option):
    calls = []
    def build(args):
        assert args.ledger == r'QKD_PROJ\webui\state\web-jobs.db'
        assert args.profile == 'hardware'
        assert args.pipe == r'\\.\pipe\qkd-agent-web'
        assert not hasattr(args, 'contracts') and not hasattr(args, 'legacy_contracts')
        calls.append('build')
        return SimpleNamespace(
            start=lambda: {'address':args.pipe,'cursor':0,'recovered':[]},
            run_forever=lambda max_ticks: calls.append(('run', max_ticks)),
            close=lambda: calls.append('close'),
        ), None
    monkeypatch.setattr(cli, 'build_service', build)
    assert cli.main([
        '--ledger', r'QKD_PROJ\webui\state\web-jobs.db',
        '--runs', r'QKD_PROJ\runs', *option,
        '--knowledge-db', r'QKD_PROJ\runs\knowledge.db',
        '--knowledge-project','qkd-proj','--knowledge-hardware','rtase-v1',
        '--knowledge-revision','v1.0','--profile','hardware',
        '--pipe',r'\\.\pipe\qkd-agent-web','--ticks','0',
    ]) == 0
    assert calls == ['build',('run',0),'close']
    assert '--contracts is obsolete and ignored' in capsys.readouterr().err
