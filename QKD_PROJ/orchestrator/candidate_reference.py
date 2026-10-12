"""Resolve existing candidate identities without inheriting historical authority."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import yaml

from QKD_PROJ.hwtools.safety import SafetyViolation
from .boundary_normalization import _merge


def resolve_candidate_reference(proposal: dict, contracts_dir: Path) -> dict:
    result = copy.deepcopy(proposal)
    configuration = result.get("configuration") or {}
    if not isinstance(configuration, dict):
        raise SafetyViolation("candidate configuration must be an object")
    candidate = configuration.get("candidate") or {}
    if not isinstance(candidate, dict):
        raise SafetyViolation("candidate reference must be an object")
    references = [value for value in (result.get("candidate_contract"), candidate.get("source_contract"),
                  configuration.get("source_contract")) if value is not None]
    reference = references[0] if references else None
    if not reference:
        return result
    if not isinstance(reference, str) or Path(reference).name != reference or "/" in reference or "\\" in reference:
        raise SafetyViolation("candidate_contract must name one existing contract, not a path or inferred Gate")
    if any(value != reference for value in references):
        raise SafetyViolation("conflicting candidate source contract references")
    root = contracts_dir.resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        raise SafetyViolation("candidate contract escapes contracts directory")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected = [value for value in (result.get("candidate_contract_sha256"), candidate.get("source_contract_sha256"),
                configuration.get("source_contract_sha256")) if value is not None]
    if any(value != digest for value in expected):
        raise SafetyViolation("candidate source contract changed")
    source = yaml.safe_load(raw)
    identity = source.get("sram_program_candidate") if isinstance(source, dict) else None
    if not isinstance(identity, dict) or identity.get("immutable") is not True:
        raise SafetyViolation("referenced contract has no immutable SRAM candidate")
    build = source.get("build_result")
    design = source.get("test_design")
    if not isinstance(build, dict) or not isinstance(design, dict):
        raise SafetyViolation("referenced candidate needs build and design identity objects")
    # Allowlist identity fields: no approved_by, approved, counters or old PASS.
    keys = ("directory", "bitstream", "bitstream_sha256", "bitstream_design_sha256",
            "bitstream_bytes", "source_sha256", "companion_sha256", "immutable")
    pinned = {key: copy.deepcopy(identity[key]) for key in keys if key in identity}
    for key in keys:
        if key in candidate and candidate[key] != pinned.get(key):
            raise SafetyViolation(f"candidate reference conflicts with supplied {key}")
    inherited = {"sram_program_candidate": pinned,
                 "test_design": copy.deepcopy(design),
                 "build_result": {"bitstream_design_sha256": build.get("bitstream_design_sha256")}}
    # Recognize the older nested spelling, checking rather than overwriting it.
    nested = configuration.get("sram_program_candidate")
    if nested is not None:
        if not isinstance(nested, dict):
            raise SafetyViolation("nested sram_program_candidate must be an object")
        for key in keys:
            if key in nested and nested[key] != pinned.get(key):
                raise SafetyViolation(f"candidate reference conflicts with nested {key}")
    for key in ("test_design", "jtag"):
        if key in configuration:
            _merge(result, {key: configuration[key]}, "configuration")
    for key in ("device_map", "jtag"):
        if key in source:
            inherited[key] = copy.deepcopy(source[key])
    # Old files stored chain identity beside functional PASS criteria. Carry
    # only these device constraints into jtag, never the old success policy.
    criteria = source.get("pass_criteria") or {}
    if isinstance(criteria, dict):
        _merge(inherited, {"jtag": {key: criteria[key] for key in
               ("expected_idcode", "expected_device_count") if key in criteria}}, "candidate_reference")
    _merge(result, inherited, "candidate_reference")
    result["candidate_reference"] = {"name": reference, "sha256": digest,
                                     "authority_inherited": False}
    return result
