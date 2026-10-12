"""A fail-closed catalog of manually verified implementation assets.

The platform already has BBB host code, PRU firmware, CPLD sources, contracts,
and successful run records.  A model should reuse those assets, but only while
the exact bytes and their recorded evidence still agree.  This module makes
that relationship explicit.  It does *not* discover files heuristically and
does not make a source file executable; the curated YAML names an asset, its
bounded use, and the PASS records that justify presenting it to a planner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MODES = {"offline", "hardware"}
_TOP = {"schema_version", "capabilities"}
_CAPABILITY = {
    "id", "title", "summary", "mode", "agents", "skills", "operations",
    "contracts", "runs", "assets",
}
_ASSET = {"path", "sha256"}


class CapabilityCatalogError(ValueError):
    """The catalog names a missing, drifted, or unsupported asset."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(root: Path, reference: str, *, what: str) -> Path:
    if not isinstance(reference, str) or not reference:
        raise CapabilityCatalogError(f"{what} must be a non-empty project-relative path")
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise CapabilityCatalogError(f"{what} escapes the project root: {reference!r}") from exc
    return candidate


def _text_list(value: Any, *, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise CapabilityCatalogError(f"{what} must be a non-empty list of text")
    return tuple(value)


@dataclass(frozen=True)
class Capability:
    """One reusable, evidence-backed implementation boundary."""

    capability_id: str
    title: str
    summary: str
    mode: str
    agents: tuple[str, ...]
    skills: tuple[str, ...]
    operations: tuple[str, ...]
    contracts: tuple[dict[str, str], ...]
    runs: tuple[str, ...]
    assets: tuple[dict[str, str], ...]
    digest: str

    def describe(self) -> dict[str, Any]:
        """A path-free planning input; source bytes stay on the local host."""
        return {
            "id": self.capability_id,
            "title": self.title,
            "summary": self.summary,
            "mode": self.mode,
            "agents": list(self.agents),
            "skills": list(self.skills),
            "operations": list(self.operations),
            "contracts": [dict(item) for item in self.contracts],
            "runs": list(self.runs),
            "asset_count": len(self.assets),
            "sha256": self.digest,
        }


@dataclass(frozen=True)
class CapabilityCatalog:
    """A deterministic projection of the curated verified-assets manifest."""

    root: Path
    source: Path
    capabilities: tuple[Capability, ...]
    digest: str
    withheld: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(
        cls, source: Path, *, project_root: Path, allow_partial: bool = False,
    ) -> "CapabilityCatalog":
        """Keep strict validation by default; advisory callers may quarantine rows.

        Withheld rows are never selectable. Catalog-wide schema and identity
        errors still reject the entire manifest, even in partial mode.
        """
        root = Path(project_root).resolve()
        source = Path(source).resolve()
        if not source.is_file():
            raise CapabilityCatalogError(f"capability catalog does not exist: {source}")
        try:
            data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise CapabilityCatalogError(f"capability catalog is unreadable: {exc}") from exc
        if not isinstance(data, dict) or set(data) - _TOP:
            raise CapabilityCatalogError("capability catalog has unknown top-level fields")
        if data.get("schema_version") != 1:
            raise CapabilityCatalogError("capability catalog schema_version must be 1")
        raw = data.get("capabilities")
        if not isinstance(raw, list) or not raw:
            raise CapabilityCatalogError("capability catalog needs at least one capability")
        identifiers = [item.get("id") if isinstance(item, dict) else None for item in raw]
        if any(not isinstance(identifier, str) or not _ID.fullmatch(identifier)
               for identifier in identifiers):
            raise CapabilityCatalogError("capability id must be a lowercase identifier")
        if len(identifiers) != len(set(identifiers)):
            raise CapabilityCatalogError("capability ids must be unique")
        admitted: list[Capability] = []
        withheld: list[tuple[str, str]] = []
        for identifier, item in zip(identifiers, raw):
            try:
                admitted.append(cls._capability(root, item))
            except (CapabilityCatalogError, OSError) as exc:
                if not allow_partial:
                    raise
                withheld.append((identifier, str(exc)))
        capabilities = tuple(admitted)
        payload = [item.describe() for item in capabilities]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(root, source, capabilities, digest, tuple(withheld))

    @classmethod
    def load_default(cls, project_root: Path, *, allow_partial: bool = False) -> "CapabilityCatalog":
        root = Path(project_root).resolve()
        return cls.load(root / "QKD_PROJ" / "capabilities" / "verified-assets.yaml",
                        project_root=root, allow_partial=allow_partial)

    @staticmethod
    def _capability(root: Path, raw: Any) -> Capability:
        if not isinstance(raw, dict) or set(raw) - _CAPABILITY:
            raise CapabilityCatalogError("capability has unknown fields")
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise CapabilityCatalogError("capability id must be a lowercase identifier")
        title = raw.get("title")
        summary = raw.get("summary")
        if not isinstance(title, str) or not title.strip() or not isinstance(summary, str) or not summary.strip():
            raise CapabilityCatalogError(f"capability {identifier} needs title and summary")
        mode = raw.get("mode")
        if mode not in _MODES:
            raise CapabilityCatalogError(f"capability {identifier} mode must be one of {sorted(_MODES)}")
        agents = _text_list(raw.get("agents"), what=f"capability {identifier}.agents")
        skills_raw = raw.get("skills", [])
        if not isinstance(skills_raw, list) or not all(isinstance(item, str) and item for item in skills_raw):
            raise CapabilityCatalogError(f"capability {identifier}.skills must be a text list")
        operations = _text_list(raw.get("operations"), what=f"capability {identifier}.operations")
        names = _text_list(raw.get("contracts"), what=f"capability {identifier}.contracts")
        contracts: list[dict[str, str]] = []
        for name in names:
            contract = _relative(root, f"QKD_PROJ/contracts/{name}", what=f"contract {name}")
            if not contract.is_file():
                raise CapabilityCatalogError(f"capability {identifier} contract is missing: {name}")
            contracts.append({"name": name, "sha256": _sha256(contract)})
        runs = _text_list(raw.get("runs"), what=f"capability {identifier}.runs")
        names_set = set(names)
        for run in runs:
            manifest_path = _relative(root, f"QKD_PROJ/runs/{run}/manifest.json", what=f"run {run}")
            if not manifest_path.is_file():
                raise CapabilityCatalogError(f"capability {identifier} run is missing: {run}")
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CapabilityCatalogError(f"capability {identifier} run manifest is unreadable: {run}") from exc
            contract = manifest.get("contract", {}) if isinstance(manifest, dict) else {}
            if not isinstance(manifest, dict) or manifest.get("verdict") != "PASS" or not isinstance(contract, dict) or contract.get("test_spec") not in names_set:
                raise CapabilityCatalogError(
                    f"capability {identifier} run {run} is not a PASS record for one of its contracts"
                )
        raw_assets = raw.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise CapabilityCatalogError(f"capability {identifier}.assets must be a non-empty list")
        assets: list[dict[str, str]] = []
        for asset in raw_assets:
            if not isinstance(asset, dict) or set(asset) != _ASSET:
                raise CapabilityCatalogError(f"capability {identifier} asset must contain path and sha256")
            reference, expected = asset.get("path"), asset.get("sha256")
            if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
                raise CapabilityCatalogError(f"capability {identifier} asset has an invalid SHA-256")
            path = _relative(root, reference, what=f"capability {identifier} asset")
            if not path.is_file():
                raise CapabilityCatalogError(f"capability {identifier} asset is missing: {reference}")
            actual = _sha256(path)
            if actual != expected:
                raise CapabilityCatalogError(
                    f"capability {identifier} asset drifted: {reference} hashes to {actual}, not {expected}"
                )
            assets.append({"path": reference, "sha256": actual})
        digest = hashlib.sha256(
            json.dumps(
                {
                    "id": identifier, "mode": mode, "agents": agents, "skills": skills_raw,
                    "operations": operations, "contracts": contracts, "runs": runs, "assets": assets,
                },
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return Capability(
            identifier, title.strip(), summary.strip(), mode, agents, tuple(skills_raw), operations,
            tuple(contracts), runs, tuple(assets), digest,
        )

    def for_mode(self, mode: str) -> list[dict[str, Any]]:
        if mode not in _MODES:
            raise CapabilityCatalogError(f"unknown execution mode {mode!r}")
        return [item.describe() for item in self.capabilities if item.mode == "offline" or mode == "hardware"]

    def for_planning(self, mode: str) -> list[dict[str, Any]]:
        """Describe every verified asset without widening the current profile.

        An offline mission needs to know that a later BBB or scope stage exists
        in order to produce one coherent design. Seeing an asset is not the
        same thing as being able to execute it: hardware rows are marked as
        deferred until a separately activated hardware campaign is in force.
        """
        if mode not in _MODES:
            raise CapabilityCatalogError(f"unknown execution mode {mode!r}")
        rows: list[dict[str, Any]] = []
        for item in self.capabilities:
            row = item.describe()
            available_now = item.mode == "offline" or mode == "hardware"
            row["available_now"] = available_now
            row["requires_hardware_campaign"] = item.mode == "hardware"
            rows.append(row)
        return rows

    def select(self, identifiers: list[str] | tuple[str, ...], *, mode: str) -> list[Capability]:
        available = {item.capability_id: item for item in self.capabilities if item.mode == "offline" or mode == "hardware"}
        selected: list[Capability] = []
        for identifier in identifiers:
            item = available.get(identifier)
            if item is None:
                raise CapabilityCatalogError(f"capability {identifier!r} is not available in {mode} mode")
            selected.append(item)
        return selected
