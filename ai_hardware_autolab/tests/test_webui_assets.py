"""The vendored office assets: where they came from, and that they stay put.

A copy of somebody else's art with no record is a copy nobody can audit and a
licence nobody can honour. These tests are the record's teeth.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.webui.tools import export_assets as E  # noqa: E402

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ai_hardware_autolab.webui.backend.app import ASSETS_DIR, DEFAULT_PORT, create_app  # noqa: E402
from ai_hardware_autolab.webui.backend.security import Session  # noqa: E402

BASE = f"http://127.0.0.1:{DEFAULT_PORT}"


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not E.MANIFEST.is_file():
        pytest.skip("assets have not been exported; run tools/export_assets.py")
    return json.loads(E.MANIFEST.read_text(encoding="utf-8"))


# -- the record ---------------------------------------------------------------


def test_every_vendored_file_matches_its_recorded_digest():
    """`--check` re-hashes the tree. This is that, as a test."""
    problems = E.check(ASSETS_DIR)
    assert problems == [], "\n".join(problems)


def test_the_manifest_names_where_the_bytes_came_from(manifest):
    source = manifest["exported_from"]
    assert source["name"] == "Pixel Agents"
    assert source["license"] == "MIT"
    assert source["copyright"].startswith("Copyright (c)")
    assert source["repository"].startswith("https://github.com/")
    # A 40-character commit, not a branch name: a branch moves.
    assert len(source["commit"]) == 40 and int(source["commit"], 16) >= 0
    assert source["version"] == "1.4.1"


def test_the_manifest_says_whether_the_assets_drifted_from_the_pin(manifest):
    """The fork carries its own commits, so "from 1.4.1" is a claim.

    The export asks git rather than asserting it, and records the answer --
    including "unknown", which is a different thing from "yes".
    """
    answer = manifest["exported_from"]["assets_unchanged_since_pin"]
    assert answer.startswith(("yes:", "no:", "unknown:"))


def test_the_licence_travels_with_the_bytes(manifest):
    licence = ASSETS_DIR / manifest["exported_from"]["license_file"]
    assert licence.is_file(), "a licence in a directory nobody copied is not honoured"
    text = licence.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert manifest["exported_from"]["copyright"] in text


def test_the_character_pack_keeps_its_own_attribution(manifest):
    characters = [
        entry for entry in manifest["attribution"] if entry["applies_to"].startswith("assets/char")
    ]
    assert characters, "the characters are somebody else's work again, one level down"
    assert "JIK-A-4" in characters[0]["note"]
    assert characters[0]["source"].startswith("https://")


def test_the_tree_digest_covers_every_file(manifest):
    rolled = hashlib.sha256()
    for entry in manifest["files"]:
        rolled.update(entry["path"].encode("utf-8"))
        rolled.update(bytes.fromhex(entry["sha256"]))
    assert rolled.hexdigest() == manifest["tree_sha256"]
    assert manifest["file_count"] == len(manifest["files"])


def test_what_was_vendored_is_what_the_office_needs(manifest):
    paths = {entry["path"] for entry in manifest["files"]}
    for needed in ("assets/characters/char_0.png", "assets/floors/floor_0.png"):
        assert needed in paths
    assert any(p.startswith("assets/furniture/") for p in paths)
    assert any(p.startswith("fonts/") for p in paths)
    # The README's banner and screenshot are not office assets and are not here.
    assert not any("banner" in p or "Screenshot" in p for p in paths)


# -- drift --------------------------------------------------------------------


def test_an_edited_asset_is_caught(tmp_path):
    copy = tmp_path / "assets"
    copy.mkdir()
    (copy / "one.png").write_bytes(b"original")
    manifest = E.build_manifest(copy, "test")
    (E.MANIFEST.parent).mkdir(parents=True, exist_ok=True)

    # Write the manifest beside the copy rather than over the real one.
    (copy / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    original_manifest = E.MANIFEST
    try:
        E.MANIFEST = copy / "MANIFEST.json"
        assert E.check(copy) == []

        (copy / "one.png").write_bytes(b"edited")
        problems = E.check(copy)
        assert problems and "one.png" in problems[0]

        (copy / "one.png").write_bytes(b"original")
        (copy / "two.png").write_bytes(b"snuck in")
        assert any("not recorded" in p for p in E.check(copy))

        (copy / "two.png").unlink()
        (copy / "one.png").unlink()
        assert any("not on disk" in p for p in E.check(copy))
    finally:
        E.MANIFEST = original_manifest


# -- serving ------------------------------------------------------------------


def _app():
    session = Session(file=None)
    return create_app(address="tcp://127.0.0.1:1", session=session, port=DEFAULT_PORT), session


def test_the_assets_are_served_from_disk(manifest):
    app, _ = _app()
    with TestClient(app, base_url=BASE) as http:
        # Media, not data about this run, so no token -- the same rule as the
        # JS bundle. The Host check still applies.
        answer = http.get("/office-assets/assets/characters/char_0.png")
        assert answer.status_code == 200
        assert answer.headers["content-type"].startswith("image/")

        recorded = next(e for e in manifest["files"] if e["path"] == "assets/characters/char_0.png")
        assert hashlib.sha256(answer.content).hexdigest() == recorded["sha256"]


def test_serving_still_refuses_a_rebound_host():
    app, _ = _app()
    with TestClient(app, base_url="http://attacker.example:8787") as http:
        assert http.get("/office-assets/assets/characters/char_0.png").status_code == 403


def test_nothing_reaches_outside_the_vendored_directory():
    app, _ = _app()
    with TestClient(app, base_url=BASE) as http:
        for escape in (
            "/office-assets/../backend/app.py",
            "/office-assets/..%2fbackend%2fapp.py",
            "/office-assets/assets/../../MANIFEST.json",
        ):
            assert http.get(escape).status_code in (403, 404), escape


def test_html_revalidates_while_hashed_frontend_assets_are_immutable(tmp_path):
    static = tmp_path / "dist"
    assets = static / "assets"
    assets.mkdir(parents=True)
    (static / "index.html").write_text("<h1>office</h1>", encoding="utf-8")
    (assets / "app-abc123.js").write_text("export {}", encoding="utf-8")
    session = Session(file=None)
    app = create_app(
        address="tcp://127.0.0.1:1",
        static_dir=static,
        session=session,
        port=DEFAULT_PORT,
    )

    with TestClient(app, base_url=BASE) as http:
        assert http.get("/").headers["cache-control"] == "no-cache"
        assert (
            http.get("/assets/app-abc123.js").headers["cache-control"]
            == "public, max-age=31536000, immutable"
        )


def test_the_frontend_asks_for_no_host_but_this_one():
    """The rule stated plainly: no asset comes from a network at runtime.

    Anything that fetched a sprite from a CDN would render differently on the
    machine this project actually runs on, which usually has no route out.
    """
    frontend = Path(__file__).resolve().parents[1] / "webui" / "frontend" / "src"
    offenders: list[str] = []
    for source in frontend.rglob("*.ts*"):
        text = source.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "http://" in line or "https://" in line:
                # Links a person clicks are fine; a fetch is not.
                if any(word in line for word in ("fetch(", "import(", "src=", "url(")):
                    offenders.append(f"{source.name}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)
