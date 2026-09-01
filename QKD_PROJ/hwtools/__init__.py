"""Layer 3 -- Hardware Tools.

No AI here. Every function in this package is deterministic: same inputs and
same board state produce the same ToolResult. Interpretation of those results
and the decision about what to try next belong to layer 1.

Fixed half (this package's core, changes rarely):
    result.py   ToolResult / Artifact -- the return contract
    safety.py   SafetyGate -- contract enforcement, sits in front of hardware
    scpi.py     ScpiTransport -- recording SCPI, mockable
    ssh.py      SshTransport -- recording SSH to the BBB, injectable client
    record.py   RunRecord -- run directory, manifest, hashes

Variable half (written fresh per experiment, on top of the above):
    instruments/  thin per-instrument fixed parts
    recipes/      measurement setups: timebase, trigger, how many samples
"""

from .record import RunRecord
from .result import FAIL, PASS, WARNING, Artifact, ToolResult
from .safety import Contracts, SafetyGate, SafetyViolation
from .scpi import ScpiError, ScpiTransport
from .jtag import JtagError, parse_pgrcmd_scan_log, scan_jtag, validate_scan_xcf
from .sram import (
    SramProgramError,
    parse_pgrcmd_program_log,
    program_cpld_sram,
    validate_program_xcf,
)
from .gpio import measure_gate1_gpio
from .ssh import (
    SshClient,
    SshError,
    SshExchange,
    SshTarget,
    SshTransport,
)
from .bbb import BbbError, deploy_pru, run_bbb

__all__ = [
    "Artifact",
    "BbbError",
    "Contracts",
    "FAIL",
    "JtagError",
    "measure_gate1_gpio",
    "PASS",
    "RunRecord",
    "SafetyGate",
    "SafetyViolation",
    "ScpiError",
    "ScpiTransport",
    "SshClient",
    "SshError",
    "SshExchange",
    "SshTarget",
    "SshTransport",
    "SramProgramError",
    "deploy_pru",
    "parse_pgrcmd_program_log",
    "parse_pgrcmd_scan_log",
    "scan_jtag",
    "program_cpld_sram",
    "run_bbb",
    "ToolResult",
    "validate_scan_xcf",
    "validate_program_xcf",
    "WARNING",
]
