"""Keep physical hardware effects behind the daemon's Campaign boundary.

This module has two deliberately different jobs:

* classify commands proposed by a general coding runtime.  Source editing,
  builds, tests and simulations remain ordinary workspace work; commands that
  can program or communicate with a board/instrument must use a profiled MCP
  tool instead;
* quarantine historical one-shot scripts.  They remain useful as recipes and
  evidence of the previously verified procedure, but running them directly
  would skip the JobLedger/Campaign link.

The classifier is defence in depth, not an adversarial same-user security
boundary.  The enforceable path is still: profiled MCP tool -> durable
Campaign decision -> SafetyGate -> RunRecord.  A separate OS account/container
is required if arbitrary code from the coding runtime is considered hostile.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BROKER_ENV = "QKD_BROKERED_HARDWARE"


class HardwareExecutionDenied(RuntimeError):
    """A physical effect was requested outside the Campaign-aware broker."""


@dataclass(frozen=True)
class CommandAssessment:
    effect: str
    reasons: tuple[str, ...] = ()

    @property
    def touches_hardware(self) -> bool:
        return self.effect == "hardware"

    def describe(self) -> dict[str, object]:
        return {
            "effect": self.effect,
            "touches_hardware": self.touches_hardware,
            "reasons": list(self.reasons),
        }


# These are intentionally concrete effects, not generic words such as
# "program" or "measure" that occur in harmless compiler/test output.
_HARDWARE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "vendor programmer/debugger",
        re.compile(
            r"(?i)(?:^|[\\/\s\"';|&])(?:pgrcmd|quartus_pgm|openocd|"
            r"stm32programmer(?:_cli)?|st-flash|dfu-util|pyocd|jlink(?:exe|commander)?|"
            r"avrdude|bossac|flashrom)(?:\.exe)?(?:$|\s|[\"'])"
        ),
    ),
    (
        "remote board transport",
        re.compile(r"(?i)(?:^|[\\/\s\"';|&])(?:ssh|scp|sftp)(?:\.exe)?(?:[\s\"']|$)"),
    ),
    (
        "direct device/register access",
        re.compile(r"(?i)(?:^|[\\/\s\"';|&])(?:devmem|gpioset|gpioget|config-pin)(?:[\s\"']|$)"),
    ),
    (
        "instrument control",
        re.compile(
            r"(?i)(?:\bimport\s+pyvisa\b|\bfrom\s+pyvisa\b|\bpyvisa\.(?!py\b)|\bvisa\.ResourceManager|\bRTM3004\s*\(|socket[^\n]{0,80}\b5025\b|"
            r"CHAN\d*:|ACQ:|TRIG:|SYST:ERR\?)"
        ),
    ),
    (
        "legacy direct hardware entrypoint",
        re.compile(
            r"(?i)(?:program_once\.py|[/\\]program\.py|run_bbb_diagnostic\.py|"
            r"scope_capture\.py|timing_capture\.py)"
        ),
    ),
)


def _unwrap_powershell(text: str) -> str:
    """Read a literal -Command wrapper; never evaluate or decode shell code."""
    shell = re.match(
        r'''(?is)^\s*(?:"[^"]*[\\/]?(?:powershell|pwsh)(?:\.exe)?"|(?:\S*[\\/])?(?:powershell|pwsh)(?:\.exe)?)\s+(?:(?:-NoProfile|-NonInteractive|-NoLogo)\s+)*-Command\s+(.+)$''',
        text,
    )
    if shell:
        text = shell.group(1).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
    return text


def _inspection_only(text: str) -> bool:
    """Recognize literal local inspection, not arbitrary shell expressions.

    Inspecting a programmer's source is not running it. Deliberately decline
    this exemption for interpolation, redirection, invocation and remote/device
    paths. This is a convenience classifier, not a replacement for OS isolation.
    """
    if any(char in text for char in '$`&<>(){}'):
        return False
    if re.search(r"\\\\|://|/(?:dev|proc)/|(?i:\b(?:env|registry|hklm|hkcu):)|--pre(?:[=\s]|$)", text):
        return False
    try:
        lexer = shlex.shlex(text, posix=False, punctuation_chars=";|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    readers = {
        "get-content", "get-filehash", "get-childitem", "get-item",
        "test-path", "resolve-path", "select-string", "rg", "rg.exe",
        "cat", "head", "tail", "ls",
    }
    formatters = {"format-list", "format-table", "select-object"}
    need_command = True
    pipeline = False
    for token in tokens:
        if token in (";", "\n", "|"):
            if need_command:
                return False
            need_command, pipeline = True, token == "|"
            continue
        if set(token) <= set(";|\n"):
            return False
        if need_command:
            if token.lower() not in readers | (formatters if pipeline else set()):
                return False
            need_command = False
    return bool(tokens) and not need_command


def _offline_sequence(text: str) -> bool:
    """Exempt only a fully understood sequence, with quote-aware separators.

    One safe prefix must not hide a later invocation (e.g. Get-Content piped
    to Invoke-Expression). Unsupported syntax retains the legacy assessment.
    This does not claim to sandbox arbitrary project tests/build plugins.
    """
    if any(char in text for char in '$`&<>(){}'):
        return False
    try:
        lexer = shlex.shlex(text, posix=False, punctuation_chars=";|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    segments: list[tuple[list[str], bool]] = []
    current: list[str] = []
    piped = False
    for token in tokens:
        if token in (";", "|", "\n"):
            if not current:
                return False
            segments.append((current, piped))
            current, piped = [], token == "|"
        elif set(token) <= set(";|\n"):
            return False
        else:
            current.append(token)
    if not current:
        return False
    segments.append((current, piped))
    for words, piped in segments:
        command = " ".join(words)
        if _inspection_only(command) or _development_invocation(command):
            continue
        if piped and words[0].lower() in {"format-table", "format-list", "select-object"}:
            continue
        # No -c/--exec-path or external diff/textconv hook opt-in. This is a
        # local inspection exemption, not permission to run a Git alias.
        if (words[0].lower() in {"git", "git.exe"}
                and len(words) > 1 and words[1].lower() in {"diff", "status"}
                and not any(word.lower().split("=", 1)[0] in {
                    "--ext-diff", "--textconv", "--output",
                } for word in words[2:])):
            continue
        return False
    return True


def classify_workspace_command(command: str | Iterable[str]) -> CommandAssessment:
    """Classify one proposed coding-runtime command without executing it."""

    if isinstance(command, str):
        text = command
    else:
        text = " ".join(str(part) for part in command)
    text = _unwrap_powershell(text)
    if _inspection_only(text) or _development_invocation(text) or _offline_sequence(text):
        return CommandAssessment("offline")
    reasons = tuple(label for label, pattern in _HARDWARE_PATTERNS if pattern.search(text))
    return CommandAssessment("hardware" if reasons else "offline", reasons)


def _development_invocation(text: str) -> bool:
    """Recognize argument roles in a single development command.

    A pytest/build classification is NOT authority to open a device. Those
    programs execute project code; real device calls still need the broker.
    Do not exempt compound commands, shell expansions or Python -c programs.
    """
    if any(char in text for char in ';|&\n\r`$<>(){}'):
        return False
    try:
        tokens = shlex.split(text, posix=False)
    except ValueError:
        return False
    tokens = [token.strip("\"'") for token in tokens]
    if not tokens:
        return False
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if re.fullmatch(r"(?:py|python(?:\d+(?:\.\d+)*)?)(?:\.exe)?", executable):
        args = tokens[1:]
        while args and re.fullmatch(r"(?:-\d+(?:\.\d+)*|-B|-I|-E|-s|-S|-u)", args[0]):
            args.pop(0)
        return len(args) >= 2 and args[0] == "-m" and args[1] in {
            "py_compile", "compileall", "pytest", "unittest", "build",
        }
    if executable in {"pytest", "pytest.exe", "ruff", "ruff.exe", "mypy", "mypy.exe"}:
        return True
    # Arguments are compiler inputs/build targets, not executable entrypoints.
    return executable in {"cmake", "cmake.exe"} and tokens[1:2] == ["--build"]


def require_offline_workspace_command(command: str | Iterable[str]) -> None:
    """Refuse a physical command and point the caller at the governed path."""

    assessment = classify_workspace_command(command)
    if assessment.touches_hardware:
        raise HardwareExecutionDenied(
            "general workspace commands cannot touch hardware "
            f"({', '.join(assessment.reasons)}); use a profiled MCP tool so the "
            "active Campaign, SafetyGate and RunRecord are checked"
        )


def require_brokered_entrypoint(script: str | Path) -> None:
    """Quarantine a historical direct hardware script.

    The environment flag is reserved for a future short-lived broker worker.
    The daemon does not currently set it, so normal shell execution fails
    closed.  Keeping the narrow escape hatch here avoids editing every recipe
    again when a subprocess broker is introduced; it is not presented as a
    cryptographic boundary.
    """

    if os.environ.get(BROKER_ENV) == "1":
        return
    name = Path(script).name
    raise HardwareExecutionDenied(
        f"{name} is a historical direct hardware entrypoint and is disabled. "
        "Start the scenario through the web Campaign so the daemon can bind the "
        "job, approval and run evidence before touching hardware."
    )


_EFFECTFUL_CALLS = {
    "deploy_pru",
    "measure_scope",
    "program_cpld_sram",
    "run_bbb",
    "run_profile_stage",
    "scan_jtag",
    "RTM3004",
}
_ALLOWED_PARTS = {
    "hwtools",
    "mcp_server",
    "orchestrator",
    "references",
    "runs",
    "tests",
    # Per-job workspaces are immutable/audited snapshots of the checkout at
    # job creation time.  Scanning stale snapshots would keep reporting a path
    # after the source entrypoint had already been quarantined.
    "workspaces",
}


def direct_hardware_call_findings(root: Path) -> list[dict[str, object]]:
    """Find unquarantined Python entrypoints calling effectful primitives."""

    root = Path(root).resolve()
    findings: list[dict[str, object]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _ALLOWED_PARTS for part in relative.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        guarded = "require_brokered_entrypoint(" in source
        calls: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name in _EFFECTFUL_CALLS:
                calls.add(name)
        if calls and not guarded:
            findings.append(
                {
                    "path": relative.as_posix(),
                    "calls": sorted(calls),
                    "problem": "effectful primitive is callable without the broker guard",
                }
            )
    return findings


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Find direct hardware execution paths.")
    parser.add_argument(
        "root",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1]),
        help="QKD_PROJ root",
    )
    args = parser.parse_args(argv)
    findings = direct_hardware_call_findings(Path(args.root))
    print(json.dumps({"findings": findings}, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
