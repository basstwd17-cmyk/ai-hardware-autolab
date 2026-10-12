"""Graph layout persistence: the one piece of state the user authors.

Everything else this app holds is derived and can be rebuilt from the ledger.
A position cannot, so these tests are mostly about the ways it could be lost:
a stale write clobbering another tab, a truncated file after a kill, and a
file this version cannot read being overwritten by one that can.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_hardware_autolab.webui.backend.layout import (  # noqa: E402
    MAX_ID_LENGTH,
    MAX_POSITIONS,
    VERSION,
    LayoutConflict,
    LayoutError,
    LayoutStore,
)

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ai_hardware_autolab.webui.backend.app import DEFAULT_PORT, create_app  # noqa: E402
from ai_hardware_autolab.webui.backend.security import Session  # noqa: E402

BASE = f"http://127.0.0.1:{DEFAULT_PORT}"


@pytest.fixture()
def store(tmp_path) -> LayoutStore:
    return LayoutStore(tmp_path / "state" / "graph-layout.json")


def at(x: float, y: float) -> dict[str, float]:
    return {"x": x, "y": y}


# -- reading ------------------------------------------------------------------


def test_a_missing_file_is_an_empty_layout_not_an_error(store):
    assert store.read() == {"version": VERSION, "revision": 0, "positions": {}, "unreadable": None}


def test_what_was_written_is_what_comes_back(store):
    store.write({"job:1": at(10, 20.5)}, expected_revision=0)
    layout = store.read()

    assert layout["revision"] == 1
    assert layout["positions"] == {"job:1": {"x": 10.0, "y": 20.5}}
    assert store.path.is_file()


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '["a list, not a layout"]',
        '{"version": 99, "revision": 3, "positions": {"job:1": {"x": 1, "y": 2}}}',
    ],
)
def test_a_file_this_version_cannot_read_is_reported_and_left_alone(store, content):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(content, encoding="utf-8")

    layout = store.read()
    assert layout["positions"] == {}
    assert layout["unreadable"], "an unreadable file is said out loud, not treated as absent"
    # And it is still there: a future version's file is somebody's work.
    assert store.path.read_text(encoding="utf-8") == content


def test_one_bad_row_does_not_lose_the_rest_of_the_file(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps(
            {
                "version": VERSION,
                "revision": 4,
                "positions": {
                    "job:1": {"x": 1, "y": 2},
                    "job:2": {"x": "over there", "y": 2},
                    "job:3": {"x": 5, "y": 6},
                },
            }
        ),
        encoding="utf-8",
    )
    layout = store.read()
    assert sorted(layout["positions"]) == ["job:1", "job:3"]
    assert layout["revision"] == 4


# -- writing ------------------------------------------------------------------


def test_a_stale_write_is_refused_and_carries_the_current_layout_back(store):
    store.write({"job:1": at(1, 1)}, expected_revision=0)
    store.write({"job:1": at(2, 2)}, expected_revision=1)  # another tab saved

    with pytest.raises(LayoutConflict) as refused:
        store.write({"job:9": at(9, 9)}, expected_revision=1)

    assert refused.value.current["revision"] == 2
    assert refused.value.current["positions"] == {"job:1": {"x": 2.0, "y": 2.0}}
    # Nothing was written, so the other tab's arrangement survives intact.
    assert store.read()["positions"] == {"job:1": {"x": 2.0, "y": 2.0}}


def test_merging_onto_the_returned_copy_is_what_makes_the_refusal_recoverable(store):
    store.write({"a": at(1, 1)}, expected_revision=0)
    store.write({"a": at(1, 1), "b": at(2, 2)}, expected_revision=1)  # the other tab

    mine = {"c": at(3, 3)}
    try:
        store.write(mine, expected_revision=1)
        raise AssertionError("expected a conflict")
    except LayoutConflict as exc:
        merged = {**exc.current["positions"], **mine}
        saved = store.write(merged, expected_revision=exc.current["revision"])

    assert sorted(saved["positions"]) == ["a", "b", "c"], "neither tab lost a node"
    assert saved["revision"] == 3


def test_the_revision_climbs_by_one_per_write(store):
    for expected in range(4):
        assert store.write({"a": at(expected, 0)}, expected_revision=expected)["revision"] == (
            expected + 1
        )


def test_nothing_partial_is_left_behind(store):
    store.write({"a": at(1, 1)}, expected_revision=0)
    store.write({"a": at(2, 2)}, expected_revision=1)
    siblings = [p.name for p in store.path.parent.iterdir()]
    assert siblings == [store.path.name], f"a temporary file survived: {siblings}"


# -- refusals -----------------------------------------------------------------


@pytest.mark.parametrize(
    "positions, because",
    [
        ({"a": {"x": float("nan"), "y": 0}}, "finite"),
        ({"a": {"x": float("inf"), "y": 0}}, "finite"),
        ({"a": {"x": 2_000_000, "y": 0}}, "outside"),
        ({"a": {"x": "3", "y": 0}}, "must be numbers"),
        ({"a": {"x": True, "y": 0}}, "must be numbers"),
        ({"a": {"y": 0}}, "must be numbers"),
        ({"a": [1, 2]}, "must be an object"),
        ({"": {"x": 0, "y": 0}}, "non-empty node id"),
        ({"x" * (MAX_ID_LENGTH + 1): {"x": 0, "y": 0}}, "longer than"),
    ],
)
def test_a_position_that_is_not_one_is_refused_with_a_reason(store, positions, because):
    with pytest.raises(LayoutError) as refused:
        store.write(positions, expected_revision=0)
    assert because in str(refused.value)
    assert not store.path.exists(), "a refused save writes nothing"


def test_more_positions_than_a_person_could_have_moved_is_refused(store):
    too_many = {f"n:{i}": at(i, i) for i in range(MAX_POSITIONS + 1)}
    with pytest.raises(LayoutError) as refused:
        store.write(too_many, expected_revision=0)
    assert str(MAX_POSITIONS) in str(refused.value)


def test_a_refused_save_leaves_an_existing_layout_untouched(store):
    store.write({"a": at(1, 1)}, expected_revision=0)
    with pytest.raises(LayoutError):
        store.write({"a": {"x": float("inf"), "y": 0}}, expected_revision=1)
    assert store.read()["positions"] == {"a": {"x": 1.0, "y": 1.0}}


# -- over HTTP ----------------------------------------------------------------


def _app(tmp_path):
    session = Session(file=None)
    app = create_app(
        address="tcp://127.0.0.1:1",
        session=session,
        port=DEFAULT_PORT,
        layout_file=tmp_path / "graph-layout.json",
    )
    return app, session


def _client(app, session):
    return TestClient(app, base_url=BASE, headers={"Authorization": f"Bearer {session.token}"})


def test_the_layout_round_trips_over_http(tmp_path):
    app, session = _app(tmp_path)
    with _client(app, session) as http:
        empty = http.get("/api/graph/layout").json()
        assert empty == {"version": 1, "revision": 0, "positions": {}, "unreadable": None}

        saved = http.put(
            "/api/graph/layout",
            json={"revision": 0, "positions": {"job:1": {"x": 12, "y": 34}}},
        )
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1

        assert http.get("/api/graph/layout").json()["positions"] == {"job:1": {"x": 12, "y": 34}}


def test_a_second_tab_gets_a_409_it_can_act_on(tmp_path):
    app, session = _app(tmp_path)
    with _client(app, session) as http:
        http.put("/api/graph/layout", json={"revision": 0, "positions": {"a": {"x": 1, "y": 1}}})

        stale = http.put(
            "/api/graph/layout", json={"revision": 0, "positions": {"b": {"x": 2, "y": 2}}}
        )
        assert stale.status_code == 409
        body = stale.json()
        assert body["ok"] is False
        # The whole point: the answer contains what to merge onto.
        assert body["current"]["revision"] == 1
        assert body["current"]["positions"] == {"a": {"x": 1.0, "y": 1.0}}

        merged = http.put(
            "/api/graph/layout",
            json={
                "revision": body["current"]["revision"],
                "positions": {**body["current"]["positions"], "b": {"x": 2, "y": 2}},
            },
        )
        assert merged.status_code == 200
        assert sorted(merged.json()["positions"]) == ["a", "b"]


def test_a_bad_position_is_a_422_not_a_500(tmp_path):
    app, session = _app(tmp_path)
    with _client(app, session) as http:
        # Pydantic refuses a non-number before the store sees it...
        assert (
            http.put(
                "/api/graph/layout",
                json={"revision": 0, "positions": {"a": {"x": "over there", "y": 1}}},
            ).status_code
            == 422
        )
        # ...and the store's own bounds are a 422 too, with its reason.
        huge = http.put(
            "/api/graph/layout",
            json={"revision": 0, "positions": {"a": {"x": 9e9, "y": 1}}},
        )
        assert huge.status_code == 422
        assert "outside" in huge.json()["detail"]


def test_the_layout_routes_need_the_token_like_everything_else(tmp_path):
    app, _ = _app(tmp_path)
    with TestClient(app, base_url=BASE) as http:
        assert http.get("/api/graph/layout").status_code == 401
        assert (
            http.put("/api/graph/layout", json={"revision": 0, "positions": {}}).status_code == 401
        )
