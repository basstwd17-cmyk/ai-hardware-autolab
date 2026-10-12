from __future__ import annotations

from pathlib import Path

import pytest

from ai_hardware_autolab.orchestrator.execution_guard import (
    HardwareExecutionDenied,
    classify_workspace_command,
    direct_hardware_call_findings,
    require_brokered_entrypoint,
    require_offline_workspace_command,
)


@pytest.mark.parametrize(
    "command",
    [
        "py -3 -m pytest ai_hardware_autolab/tests -q",
        "cmake --build build",
        "python ai_hardware_autolab/gate12_pru_dac1/build.py",
        "iverilog -o sim.out top.v tb.v",
        r"Get-Content -Raw ai_hardware_autolab\tests\test_rtm3004.py",
        r"Get-FileHash -Algorithm SHA256 ai_hardware_autolab\hwtools\instruments\rtm3004.py",
        r"Get-Content ai_hardware_autolab\program.py; Get-FileHash ai_hardware_autolab\program.py | Format-List",
        r'''"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Command "Get-Content ai_hardware_autolab\program.py; Get-FileHash ai_hardware_autolab\scope_capture.py | Format-List"''',
        'rg "pyvisa|RTM3004" ai_hardware_autolab/hwtools',
        "py -3 -m pytest ai_hardware_autolab/tests/test_rtm3004.py -q",
        "py -3 -m pytest ai_hardware_autolab/tests/test_pyvisa.py -q",
        "python -m py_compile tools/program_once.py",
        "python -m pytest tests/test_scope_capture.py -q",
        "py -3 -B -m compileall tools/program_once.py",
        '"C:\\Python313\\python.exe" -m py_compile tools/program_once.py',
    ],
)
def test_offline_workspace_commands_remain_available(command: str):
    assert classify_workspace_command(command).effect == "offline"
    require_offline_workspace_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "pgrcmd.exe -infile program.xcf",
        "openocd -f interface/stlink.cfg",
        "ssh debian@192.168.7.2 sudo remoteproc start",
        "python ai_hardware_autolab/spi_gpio_code_regs/program.py",
        "python ai_hardware_autolab/spi_timing_capture.py --arm",
        'python -c "from pyvisa import ResourceManager; ResourceManager()"',
        'python -c "RTM3004(resource).write(\'RUN\')"',
        r'Get-Content ai_hardware_autolab\program.py; python ai_hardware_autolab\program.py',
        r'Get-Content ai_hardware_autolab\program.py | Invoke-Expression',
        r'Get-Content "$(python ai_hardware_autolab\program.py)"',
        r'Get-Content ai_hardware_autolab\program.py && openocd -f board.cfg',
        r'Get-Content ai_hardware_autolab\program.py > output.txt',
        r'rg --pre "python ai_hardware_autolab\program.py" pattern source.txt',
        "python -m py_compile tools/program_once.py; openocd -f board.cfg",
        "python -m pytest tests/test_scope_capture.py && ssh board reboot",
    ],
)
def test_workspace_hardware_commands_are_sent_to_profiled_tools(command: str):
    with pytest.raises(HardwareExecutionDenied, match="profiled MCP tool"):
        require_offline_workspace_command(command)


# Regressions from job_af81239f2769; classification only, never run devices.
_JOB_COMMANDS = [
    "python -m pytest ai_hardware_autolab/tests/test_workspace_gpio_spi_scope_capture.py -q",
    "git diff --check; git diff --stat; Get-FileHash -Algorithm SHA256 "
    "ai_hardware_autolab/workspace_recipes/gpio_spi_scope_capture.py, "
    "ai_hardware_autolab/workspace_recipes/gpio_spi_bbb_preflight.py, "
    "BeagleBone-Black/host/rtase_spi.py | Format-Table -AutoSize",
    'python -m py_compile "tools with spaces/program_once.py"; '
    'Get-FileHash "tools with spaces/scope_capture.py" | Format-List',
    'Get-Content "notes; scope_capture.py"; git diff --stat',
    "git diff --check\npython -m pytest tests/test_scope_capture.py -q",
]


@pytest.mark.parametrize("command", _JOB_COMMANDS)
@pytest.mark.parametrize("wrapper", [
    "{}",
    '''"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command '{}' ''',
    "pwsh -NoLogo -NoProfile -NonInteractive -Command '{}'",
])
def test_runtime_wrapped_development_sequences(command, wrapper):
    require_offline_workspace_command(wrapper.format(command))


@pytest.mark.parametrize("command", [
    "python -m pytest tests/test_scope_capture.py; ssh board reboot",
    "git diff --check; pgrcmd.exe -infile program.xcf",
    "Get-FileHash scope_capture.py; python scope_capture.py",
    "Get-Content program_once.py | Invoke-Expression",
    'Get-Content "$(python program_once.py)"',
    "Get-Content scope_capture.py > output.txt",
    "git diff --check; python -c 'import pyvisa; pyvisa.ResourceManager()'",
    "python -m pytest tests/test_scope_capture.py && ssh board reboot",
    "rg --pre 'python program_once.py' pattern source.txt; git diff --stat",
    "git diff --ext-diff -- scope_capture.py",
    "Get-FileHash scope_capture.py; unknown-command",
    "git diff --check;ssh board reboot",
    "git diff --check;openocd -f board.cfg",
    "git diff --check;devmem 0x1234",
    'git diff --check; "C:\\Windows\\System32\\OpenSSH\\ssh.exe" board reboot',
])
def test_wrapper_or_safe_prefix_does_not_hide_device_execution(command):
    wrapped = '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -Command "' + command + '"'
    assert classify_workspace_command(wrapped).touches_hardware


def test_legacy_entrypoint_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("QKD_BROKERED_HARDWARE", raising=False)
    with pytest.raises(HardwareExecutionDenied, match="historical direct hardware entrypoint"):
        require_brokered_entrypoint("program.py")


def test_repository_has_no_unquarantined_direct_hardware_entrypoint():
    root = Path(__file__).resolve().parents[1]
    assert direct_hardware_call_findings(root) == []
