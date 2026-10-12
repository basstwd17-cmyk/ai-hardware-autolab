"""Execute a job's selected recipe, keeping transport and records in the host.

This is an engineering extension point, not an OS sandbox for hostile Python.
The supplied instrument checks commands before sending them to the device.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from .safety import SafetyViolation


def execute_device_recipe(policy, args, dispatch, *, validate=None):
    """Run Builder orchestration above one authorized device operation.

    Unlike a raw shell/SSH connection, execute() cannot choose another tool or
    contract. The host dispatch validates arguments and consumes the attempt
    immediately before invoking the real driver. The driver remains responsible
    for protocol-specific safety. No new recipe registration/profile is needed.
    """
    from .record import RunRecord
    from .result import ToolResult, FAIL
    from .safety import Contracts, SafetyGate
    gate = SafetyGate(Contracts.load(policy.hardware_contract, policy.contracts_dir,
                                    project_root=policy.device_root or policy.project_root), allow_hardware=True)
    if hashlib.sha256(gate.contracts.test_spec_path.read_bytes()).hexdigest() != policy.hardware_contract_sha256:
        raise SafetyViolation("hardware boundary changed after authorization")
    if args.get("contract") != policy.hardware_contract:
        raise SafetyViolation("recipe call names another hardware boundary")
    record = RunRecord("device-recipe", gate, runs_dir=policy.runs_dir)
    attempted = False
    payload = None
    source = None
    content = None
    sources = RecipeSources(record)

    class DeviceAction:
        def execute(self, **parameters):
            nonlocal attempted, payload
            if attempted:
                raise SafetyViolation("recipe may execute only one device action per authorized step")
            if parameters.get("contract", policy.hardware_contract) != policy.hardware_contract:
                raise SafetyViolation("recipe cannot switch its approved device boundary")
            arguments = {**parameters, "contract": policy.hardware_contract}
            if validate is not None:
                validate(arguments)
            attempted = True  # conservative if dispatch/driver subsequently fails
            payload = dispatch(arguments)
            return copy.deepcopy(payload)

    try:
        recipe, content = load_recipe(policy.project_root, policy.workspace_recipe, policy.workspace_recipe_sha256,
                                      source_sink=sources)
        source = record.dir / "executed_recipe.py"
        source.write_bytes(content)
        recipe(DeviceAction(), copy.deepcopy(args.get("params") or args))
        if isinstance(payload, dict) and payload.get('execution_blocked') == 'platform_deployment':
            result = ToolResult(tool='device_recipe', action='prepare', ok=False, verdict=FAIL,
                error=payload['summary'], measurements={'hardware_attempted': False, 'hardware_state_unknown': False})
            sources.attach(result)
            record.add(result)
            record.finish(FAIL)
            return {**payload, 'preparation_run': record.dir.name}
        if not attempted or not isinstance(payload, dict) or not payload.get("run"):
            raise ValueError("recipe did not return a recorded device action; call device.execute(**arguments)")
        verdict = payload.get("run_verdict")
        if verdict not in {"PASS", "FAIL", "WARNING"}:
            raise ValueError("device action has no recorded verdict")
        result = ToolResult(tool="device_recipe", action="execute", ok=verdict != FAIL, verdict=verdict,
            measurements={**((payload.get("result") or {}).get("measurements") or {}),
                          "execution_completed": True, "hardware_attempted": attempted,
                          "device_run": payload["run"], "executed_source_sha256": hashlib.sha256(content).hexdigest()})
    except Exception as exc:
        uncertain = attempted and not (
            isinstance(payload, dict) and payload.get("run") and payload.get("run_dir")
            and payload.get("run_verdict") in {"PASS", "FAIL", "WARNING"})
        result = ToolResult(tool="device_recipe", action="execute", ok=False, verdict=FAIL, error=str(exc),
            measurements={"execution_completed": False, "hardware_attempted": attempted,
                          "hardware_state_unknown": uncertain,
                          "repair_target": "reconcile" if uncertain else "builder"})
    if source is not None and content is not None and source.is_file():
        result.measurements["executed_source_sha256"] = hashlib.sha256(content).hexdigest()
        result.add_artifact(source, "executed-source", relative_to=record.dir)
    if isinstance(payload, dict):
        result.measurements["device_run"] = payload.get("run")
    sources.attach(result)
    record.add(result)
    record.finish(result.verdict)
    return {"run": record.dir.name, "run_dir": str(record.dir), "run_verdict": result.verdict,
            "result": result.to_dict(), "device_result": payload}


def load_recipe(root: Path, entrypoint: str, expected_sha256: str, *, source_sink=None):
    reference, separator, symbol = entrypoint.partition(":")
    if not separator or not symbol.isidentifier() or Path(reference).suffix != ".py":
        raise ValueError("recipe must be a workspace-relative file.py:function")
    root = root.resolve()
    path = (root / reference).resolve()
    path.relative_to(root)
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("recipe changed after execution was prepared; prepare the current version again")
    from .recipe_imports import RecipeImports
    return RecipeImports(root, path, source_sink).recipe(symbol, content), content


class RecipeSources:
    """Copy bytes actually imported, before executing them, into the run."""
    def __init__(self, record):
        self.record = record
        self.sources = {}

    def __call__(self, reference, content):
        # Existing run consumers use a flat artifact directory. Preserve the
        # original import path in metadata and avoid same-basename collisions.
        key = hashlib.sha256(reference.encode()).hexdigest()[:16]
        digest = hashlib.sha256(content).hexdigest()
        path = self.record.dir / f'recipe_source_{key}_{digest[:16]}_{Path(reference).name}'
        if path.exists() and path.read_bytes() != content:
            raise ValueError('recipe source artifact name collision')
        path.write_bytes(content)
        self.sources[(reference, digest)] = path

    def attach(self, result):
        result.measurements['executed_sources'] = [
            {'path': reference, 'artifact': path.name, 'sha256': digest}
            for (reference, digest), path in self.sources.items()
        ]
        for path in self.sources.values():
            result.add_artifact(path, 'executed-source', relative_to=self.record.dir)


class CaptureCommands:
    """Capture/settings only; no source output, flash, reset or other channels."""
    def __init__(self, transport: Any, channels: set[int]):
        self._transport = transport
        self._channels = channels

    def check(self, command: str, *, query: bool) -> None:
        text = command.strip().upper().lstrip(":")
        if not text or any(c in text for c in (";", "\n", "\r")):
            raise SafetyViolation("send one scope command at a time")
        head, _, value = text.partition(" ")
        if ("?" in head) != query:
            raise SafetyViolation("scope query/write mismatch")
        probe = re.fullmatch(r"PROB([1-4]):SET:ATT:MAN\?", head)
        if query and probe:
            if int(probe[1]) not in self._channels:
                raise SafetyViolation("probe is outside approved channels")
            return
        channel = re.match(r"CHAN(?:NEL)?([1-4]):", head)
        if channel:
            if int(channel[1]) not in self._channels:
                raise SafetyViolation("scope channel is outside the approved connection")
            suffix = head.split(":", 1)[1]
            if query and re.fullmatch(r"(?:COUP|COUPLING|BAND|BANDWIDTH|SCAL|SCALE|OFFS|OFFSET|PROB:ATT|PROBE:ATTENUATION|DATA(?::HEAD)?|STAT)\?", suffix):
                return
            if not query:
                if suffix in ("COUP", "COUPLING") and value in ("DCL", "DCLIMIT"):
                    return
                if suffix in ("STAT", "STATE", "SCAL", "SCALE", "OFFS", "OFFSET", "BAND", "BANDWIDTH"):
                    if re.fullmatch(r"(?:ON|OFF|FULL|[+\-0-9.E]+)", value):
                        return
            raise SafetyViolation("scope setting is not a passive 1 Mohm capture setting")
        channel = re.search(r"(?:^TRIG:A:LEV([1-4])|\bCH([1-4])\b)", text)
        if channel and int(channel[1] or channel[2]) not in self._channels:
            raise SafetyViolation("trigger/measurement source is outside approved channels")
        if query and (head in {"*IDN?", "*OPT?", "*OPC?", "SYST:ERR?", "STAT:OPER:COND?", "ACQ:STAT?", "ACQ:POIN?"}
                      or re.fullmatch(r"(?:TIM:(?:SCAL|POS)|TRIG:A:(?:MODE|TYPE|SOUR|EDGE:SLOP|LEV[1-4])|MEAS[1-8]:(?:STAT:VAL:ALL|RES:(?:WFMC|ACT|AVG|STDD|NPE|PPE)))\?", head)):
            return
        if not query and text in {"STOP", "SING", "SINGLE", "FORM REAL,32", "FORM ASC", "FORM:BORD LSBF"}:
            return
        if not query and re.fullmatch(r"(?:TIM:(?:SCAL|POS)|TRIG:A:LEV[1-4]) [+-]?[0-9.]+(?:E[+-]?[0-9]+)?", text):
            return
        if not query and re.fullmatch(r"ACQ:(?:NSIN:COUN|POIN) [1-9][0-9]*", text):
            return
        if not query and re.fullmatch(r"TRIG:A:(?:MODE (?:AUTO|NORM)|TYPE EDGE|SOUR CH[1-4]|EDGE:SLOP (?:POS|NEG))", text):
            return
        if not query and re.fullmatch(r"MEAS[1-8]:(?:ENAB (?:ON|OFF)|SOUR CH[1-4]|MAIN [A-Z]+|STAT:ENAB (?:ON|OFF)|STAT:RES)", text):
            return
        raise SafetyViolation(f"not a permitted scope capture command: {command}")

    def write(self, command: str):
        self.check(command, query=False)
        return self._transport.write(command)

    def query(self, command: str):
        self.check(command, query=True)
        return self._transport.query(command)

    def query_float(self, command: str):
        return float(self.query(command))

    def query_binary_block(self, command: str):
        self.check(command, query=True)
        return self._transport.query_binary_block(command)

    def check_errors(self, context: str = ""):
        return self._transport.check_errors(context)


def bounded_recipe(recipe, channels: set[int]):
    def execute(scope, params, gate):
        instrument = copy.copy(scope)
        instrument.scpi = CaptureCommands(scope.scpi, channels)
        outcome = recipe(instrument, params, gate)
        observed = set(outcome.probed_channels) | {wave.channel for wave in outcome.waveforms}
        if not observed <= channels:
            raise SafetyViolation("recipe returned a channel outside its approved connection")
        return outcome
    execute.__name__ = recipe.__name__
    return execute
