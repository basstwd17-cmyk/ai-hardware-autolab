"""Workspace helper imports with host-owned transport; no device/model calls."""
import hashlib
import sys

import pytest

from ai_hardware_autolab.hwtools.workspace_recipe import load_recipe
from ai_hardware_autolab.hwtools import measurement_stats as host_stats


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def load(root, reference='ai_hardware_autolab/hwtools/recipes/capture.py', sink=None):
    path = root / reference
    return load_recipe(root, reference + ':run', hashlib.sha256(path.read_bytes()).hexdigest(), source_sink=sink)[0]


@pytest.mark.parametrize('syntax', [
    'from ..measurement_stats import value',
    'from ai_hardware_autolab.hwtools.measurement_stats import value',
    'from ai_hardware_autolab.hwtools import measurement_stats\nvalue = measurement_stats.value',
    'import ai_hardware_autolab.hwtools.measurement_stats as stats\nvalue = stats.value',
])
def test_workspace_helper_wins_without_changing_host(tmp_path, syntax):
    source = 'value = 123\n'
    write(tmp_path, 'ai_hardware_autolab/hwtools/measurement_stats.py', source)
    write(tmp_path, 'ai_hardware_autolab/hwtools/recipes/capture.py', syntax + '\ndef run(): return value\n')
    seen = {}
    before = set(sys.modules)
    recipe = load(tmp_path, sink=lambda path, content: seen.update({path: content}))
    assert recipe() == 123
    assert sys.modules['ai_hardware_autolab.hwtools.measurement_stats'] is host_stats
    assert not hasattr(host_stats, 'value')
    assert not [name for name in set(sys.modules) - before if name.startswith('_qkd_recipe_')]
    assert seen['ai_hardware_autolab/hwtools/measurement_stats.py'] == (tmp_path / 'ai_hardware_autolab/hwtools/measurement_stats.py').read_bytes()


def test_root_recipe_sibling_nested_import_dataclass_and_fresh_load(tmp_path):
    write(tmp_path, 'capture.py', 'from .helper import calculate\ndef run(): return calculate()\n')
    write(tmp_path, 'helper.py', 'from __future__ import annotations\nfrom dataclasses import dataclass\n'
          'from .coeff import OFFSET\n@dataclass\nclass Result:\n    value: int\n'
          'def calculate(): return Result(OFFSET).value\n')
    write(tmp_path, 'coeff.py', 'OFFSET = 7\n')
    first = load(tmp_path, 'capture.py')
    write(tmp_path, 'coeff.py', 'OFFSET = 8\n')
    second = load(tmp_path, 'capture.py')
    assert first() == 7  # an already loaded invocation keeps its own snapshot
    assert second() == 8


def test_local_package_and_function_time_import(tmp_path):
    write(tmp_path, 'capture.py', 'def run():\n    from analysis import calculate\n    return calculate()\n')
    write(tmp_path, 'analysis/__init__.py', 'from .helpers import calculate\n')
    write(tmp_path, 'analysis/helpers.py', 'def calculate(): return 42\n')
    seen = {}
    recipe = load(tmp_path, 'capture.py', lambda path, content: seen.update({path: content}))
    assert recipe() == 42
    assert 'analysis/helpers.py' in seen
    assert not any(name.startswith('_qkd_recipe_') for name in sys.modules)


def test_recipe_package_exports_remain_functions(tmp_path):
    write(tmp_path, 'ai_hardware_autolab/hwtools/recipes/__init__.py', 'from .analysis import calculate\n')
    write(tmp_path, 'ai_hardware_autolab/hwtools/recipes/analysis.py', 'def calculate(): return 9\n')
    write(tmp_path, 'ai_hardware_autolab/hwtools/recipes/capture.py',
          'from ai_hardware_autolab.hwtools.recipes import calculate\ndef run(): return calculate()\n')
    assert load(tmp_path)() == 9


def test_two_job_helpers_do_not_leak_into_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    for number in (1, 2):
        root = tmp_path / str(number)
        write(root, 'capture.py', 'def run():\n    from .helper import value\n    return value\n')
        write(root, 'helper.py', f'value = {number}\n')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda n: load(tmp_path / str(n), 'capture.py')(), (1, 2)))
    assert results == [1, 2]
    assert not any(name.startswith('_qkd_recipe_') for name in sys.modules)


def test_importlib_helpers_use_the_same_workspace_snapshot(tmp_path):
    write(tmp_path, 'capture.py', 'import importlib\ndef run():\n'
          '    return importlib.import_module("analysis").value\n')
    write(tmp_path, 'analysis.py', 'value = 19\n')
    assert load(tmp_path, 'capture.py')() == 19


def test_helper_edits_change_actual_device_recipe_result_and_run_sources(tmp_path):
    from ai_hardware_autolab.tests.test_device_workflow_revision import multiple_devices
    from ai_hardware_autolab.mcp_server.registry import ServerPolicy
    from ai_hardware_autolab.hwtools.workspace_recipe import execute_device_recipe
    from ai_hardware_autolab.hwtools.result import sha256_file
    from ai_hardware_autolab.orchestrator.evidence import verify_artifacts
    _, _, controller, _, _, _, _ = multiple_devices(tmp_path)
    path = write(tmp_path, 'action.py', 'def execute(device, params):\n'
                 '    from .helper import value\n    device.execute(setting=value)\n')
    policy = ServerPolicy(role='builder', allow_hardware=True, project_root=tmp_path,
        contracts_dir=controller.contracts_dir, runs_dir=tmp_path/'runs', hardware_tool='scan_jtag',
        hardware_contract='jtag.yaml', hardware_contract_sha256=sha256_file(controller.contracts_dir/'jtag.yaml'),
        workspace_recipe='action.py:execute', workspace_recipe_sha256=sha256_file(path))
    observed, results = [], []
    def dispatch(args):
        observed.append(args['setting'])
        return {'run': 'fake-device-run', 'run_dir': 'fake-device-dir', 'run_verdict': 'PASS',
                'result': {'measurements': {'value': args['setting']}}}
    for value in (11, 12):
        write(tmp_path, 'helper.py', f'value = {value}\n')
        result = execute_device_recipe(policy, {'contract': 'jtag.yaml'}, dispatch)
        assert result['run_verdict'] == 'PASS'
        assert verify_artifacts(result['run'], tmp_path/'runs') == []
        sources = result['result']['measurements']['executed_sources']
        results.append({row['path']: row for row in sources})
    assert observed == [11, 12]
    assert results[0]['action.py']['sha256'] == results[1]['action.py']['sha256']
    assert results[0]['helper.py']['sha256'] != results[1]['helper.py']['sha256']


def test_workspace_safety_and_package_initializers_do_not_replace_host(tmp_path):
    for path in ['ai_hardware_autolab/__init__.py', 'ai_hardware_autolab/hwtools/__init__.py',
                 'ai_hardware_autolab/hwtools/safety.py', 'ai_hardware_autolab/hwtools/instruments/__init__.py']:
        write(tmp_path, path, 'raise RuntimeError("workspace safety must not execute")\n')
    write(tmp_path, 'ai_hardware_autolab/hwtools/recipes/capture.py',
          'from ..safety import SafetyGate\nfrom ai_hardware_autolab.hwtools import SafetyGate as Export\n'
          'from ..instruments import RTM3004\ndef run(): return SafetyGate is Export\n')
    assert load(tmp_path)()


def test_missing_helper_does_not_silently_succeed_and_aliases_are_removed(tmp_path):
    write(tmp_path, 'capture.py', 'from .nonexistent_calculator import value\ndef run(): return value\n')
    with pytest.raises(ImportError):
        load(tmp_path, 'capture.py')
    assert not any(name.startswith('_qkd_recipe_') for name in sys.modules)


def test_changed_main_source_is_still_rejected(tmp_path):
    path = write(tmp_path, 'capture.py', 'def run(): return 1\n')
    with pytest.raises(ValueError, match='changed'):
        load_recipe(tmp_path, 'capture.py:run', '0' * 64)


@pytest.mark.parametrize('reference', ['ai_hardware_autolab/hwtools/safety.py', 'ai_hardware_autolab/__init__.py'])
def test_entrypoint_never_substitutes_host_code_under_workspace_hash(tmp_path, reference):
    path = write(tmp_path, reference, 'def SafetyGate(): return "workspace"\n')
    with pytest.raises(ValueError, match='host-owned'):
        load_recipe(tmp_path, reference + ':SafetyGate', hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('package', ['ai_hardware_autolab/analysis', 'analysis'])
def test_package_initializer_entrypoint_resolves_relative_helpers(tmp_path, package):
    write(tmp_path, package + '/__init__.py', 'from .helpers import value\ndef run(): return value\n')
    write(tmp_path, package + '/helpers.py', 'value = 29\n')
    assert load(tmp_path, package + '/__init__.py')() == 29


def test_source_record_keeps_each_version_instead_of_overwriting(tmp_path):
    from types import SimpleNamespace
    from ai_hardware_autolab.hwtools.workspace_recipe import RecipeSources
    from ai_hardware_autolab.hwtools.result import ToolResult
    sources = RecipeSources(SimpleNamespace(dir=tmp_path))
    sources('helper.py', b'value = 1\n')
    sources('helper.py', b'value = 2\n')
    result = ToolResult(tool='scope', action='analysis')
    sources.attach(result)
    rows = result.measurements['executed_sources']
    assert len(rows) == 2
    assert [row['path'] for row in rows] == ['helper.py', 'helper.py']
    assert rows[0]['sha256'] != rows[1]['sha256']
    assert [(tmp_path / row['artifact']).read_bytes() for row in rows] == [b'value = 1\n', b'value = 2\n']


def test_helper_symlink_cannot_escape_workspace(tmp_path):
    root = tmp_path / 'workspace'
    write(root, 'capture.py', 'from .helper import value\ndef run(): return value\n')
    outside = write(tmp_path, 'outside.py', 'value = 9\n')
    try:
        (root / 'helper.py').symlink_to(outside)
    except OSError:
        pytest.skip('host does not permit symlinks')
    with pytest.raises(ValueError, match='escapes'):
        load(root, 'capture.py')
