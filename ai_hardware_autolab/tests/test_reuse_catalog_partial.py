"""Advisory reuse degrades per entry; it never certifies changed source bytes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml

from ai_hardware_autolab.capabilities.catalog import CapabilityCatalog, CapabilityCatalogError
from ai_hardware_autolab.tests.test_capabilities import _root


def _mixed_catalog(tmp_path):
    _root(tmp_path)
    path = tmp_path / "ai_hardware_autolab/capabilities/verified-assets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    bad = deepcopy(data["capabilities"][0])
    bad["id"] = "drifted-scope"
    bad["assets"][0]["sha256"] = "0" * 64
    data["capabilities"].append(bad)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_partial_keeps_healthy_rows_and_never_exposes_withheld_rows(tmp_path):
    path = _mixed_catalog(tmp_path)
    before = path.read_bytes()
    catalog = CapabilityCatalog.load_default(tmp_path, allow_partial=True)
    assert [row["id"] for row in catalog.for_planning("hardware")] == ["verified-build"]
    assert [row["id"] for row in catalog.for_mode("offline")] == ["verified-build"]
    assert catalog.select(["verified-build"], mode="offline")
    assert catalog.withheld[0][0] == "drifted-scope"
    assert "asset drifted" in catalog.withheld[0][1]
    with pytest.raises(CapabilityCatalogError, match="not available"):
        catalog.select(["drifted-scope"], mode="hardware")
    with pytest.raises(CapabilityCatalogError, match="asset drifted"):
        CapabilityCatalog.load_default(tmp_path)
    assert CapabilityCatalog.load_default(tmp_path, allow_partial=True) == catalog
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing_source", "missing_run", "bad_manifest"])
def test_entry_evidence_failures_are_withheld(tmp_path, failure):
    source = _root(tmp_path)
    manifest = tmp_path / "ai_hardware_autolab/runs/run-pass/manifest.json"
    if failure == "missing_source":
        source.unlink()
    elif failure == "missing_run":
        manifest.unlink()
    else:
        manifest.write_text("[]", encoding="utf-8")
    catalog = CapabilityCatalog.load_default(tmp_path, allow_partial=True)
    assert catalog.for_planning("hardware") == []
    assert len(catalog.withheld) == 1


@pytest.mark.parametrize("failure", ["duplicate", "schema", "bad_id"])
def test_catalog_wide_errors_still_fail_closed(tmp_path, failure):
    path = _mixed_catalog(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if failure == "duplicate":
        data["capabilities"][1]["id"] = "verified-build"
    elif failure == "schema":
        data["schema_version"] = 99
    else:
        data["capabilities"][1]["id"] = []
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(CapabilityCatalogError):
        CapabilityCatalog.load_default(tmp_path, allow_partial=True)


def test_startup_loader_reports_partial_catalog_without_discarding_it(tmp_path, capsys):
    from ai_hardware_autolab.orchestrator.__main__ import _load_reuse_catalog

    _mixed_catalog(tmp_path)
    catalog = _load_reuse_catalog(tmp_path)
    assert len(catalog.capabilities) == 1
    output = capsys.readouterr().err
    assert "[reuse item withheld] drifted-scope" in output
    assert "1 verified entries retained; 1 withheld" in output
    assert "catalog unavailable" not in output


def test_unreadable_catalog_keeps_the_explicit_unavailable_warning(tmp_path, capsys):
    from ai_hardware_autolab.orchestrator.__main__ import _load_reuse_catalog

    assert _load_reuse_catalog(tmp_path) is None
    assert "reuse catalog unavailable" in capsys.readouterr().err


def test_lead_refresh_preserves_partial_catalog_and_clears_resolved_warning(tmp_path, monkeypatch):
    from ai_hardware_autolab.orchestrator import simple_mission as module

    path = _mixed_catalog(tmp_path)
    fake_boards = SimpleNamespace(digest="board-digest", planning_context=lambda _: {})
    monkeypatch.setattr(module.BoardCatalog, "load_default", lambda _: fake_boards)
    refresher = module.SimpleMissionResourceRefresher(tmp_path)
    resources = {
        "development_mode": "autonomous-v1", "execution_mode": "hardware",
        "capability_catalog_source": str(path), "available_tools": ["codex_workspace"],
    }
    refreshed = refresher("verified-build", resources)
    assert [row["id"] for row in refreshed["available_capabilities"]] == ["verified-build"]
    assert [row["id"] for row in refreshed["capability_candidates"]] == ["verified-build"]
    assert "drifted-scope withheld" in refreshed["capability_catalog_warning"]
    assert "capability_catalog_warning" not in resources
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["capabilities"] = data["capabilities"][:1]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert "capability_catalog_warning" not in refresher("verified-build", refreshed)


@pytest.mark.parametrize("profile", ["hardware", "offline"])
def test_startup_banner_describes_current_device_execution(monkeypatch, capsys, profile):
    from ai_hardware_autolab.orchestrator import __main__ as module

    fake_service = SimpleNamespace(
        start=lambda: {"address": "fake-no-transport", "cursor": 0, "recovered": []},
        run_forever=lambda **_: None, close=lambda: None,
    )
    monkeypatch.setattr(module, "build_service", lambda _: (fake_service, None))
    assert module.main(["--profile", profile, "--ticks", "0"]) == 0
    output = capsys.readouterr().err
    assert "manual approvals only" not in output
    if profile == "hardware":
        assert "automatic device execution within submitted hardware scenarios" in output
    else:
        assert "hardware access disabled (offline)" in output
        assert "scenario delegation enabled" not in output


def test_hardware_is_the_default_operator_profile(monkeypatch, capsys):
    from ai_hardware_autolab.orchestrator import __main__ as module

    seen = []
    fake_service = SimpleNamespace(
        start=lambda: {"address": "fake-no-transport", "cursor": 0, "recovered": []},
        run_forever=lambda **_: None, close=lambda: None,
    )
    monkeypatch.setattr(
        module, "build_service", lambda args: seen.append(args.profile) or (fake_service, None)
    )

    assert module.main(["--ticks", "0"]) == 0
    assert seen == ["hardware"]
    assert "automatic device execution within submitted hardware scenarios" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--campaign", "--campaign-sha256", "--contracts"])
def test_retired_cli_options_cannot_reactivate_old_workflows(flag):
    from ai_hardware_autolab.orchestrator import __main__ as module

    with pytest.raises(SystemExit) as exc:
        module.main([flag + '=retired'])
    assert exc.value.code == 2
