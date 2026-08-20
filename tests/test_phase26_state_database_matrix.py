import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from helper import state_db


def _entry(key="1", *, library="Movies", tmdb_id="100", poster=None):
    result = {
        "server_id": "server",
        "library_uuid": f"uuid-{library}",
        "library_name": library,
        "rating_key": str(key),
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "title": f"Example {key}",
        "year": 2020,
        "edition": "",
    }
    if poster is not None:
        result.update(
            poster_path=str(poster),
            poster_checksum="checksum",
            poster_source_path="/provider/poster.jpg",
        )
    return result


def _ownership(child_key="", rating_key="1", field="summary"):
    return {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": rating_key,
        "media_type": "movie",
        "child_key": child_key,
        "field_name": field,
        "field_kind": "scalar",
        "original_value": {"value": "old"},
        "applied_value": {"value": "new"},
        "owned_values": {},
        "original_locked": False,
        "metafusion_locked": True,
    }


def test_state_primitives_reject_invalid_values_and_schema(tmp_path):
    naive = state_db._as_utc(datetime(2026, 1, 2, 3, 4, 5))  # noqa: DTZ001
    assert naive.tzinfo == timezone.utc
    assert state_db._as_utc("2026-01-02T03:04:05Z").tzinfo == timezone.utc
    assert state_db._unresolved_work_class("season_poster") == "season"
    assert state_db._unresolved_work_class("other") == "metadata"

    with pytest.raises(state_db.StateDatabaseError, match="Invalid JSON"):
        state_db._json_load("{broken", "test")
    with pytest.raises(state_db.StateDatabaseError, match="Invalid object"):
        state_db._json_load("[]", "test")

    connection = sqlite3.connect(":memory:")
    connection.execute(f"PRAGMA user_version={state_db.SCHEMA_VERSION + 1}")
    with pytest.raises(state_db.StateDatabaseError, match="unsupported"):
        state_db._initialize_schema(connection)
    connection.close()

    unsupported = tmp_path / "unsupported.sqlite3"
    with closing(sqlite3.connect(unsupported)) as connection, connection:
        connection.execute(f"PRAGMA user_version={state_db.SCHEMA_VERSION + 1}")
    state_db._integrity_checked_databases.clear()
    with pytest.raises(state_db.StateDatabaseError, match="unsupported"):
        state_db._connect(unsupported, writable=False)


def test_artwork_analysis_and_application_records_cover_empty_and_update_paths(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert state_db.save_artwork_analysis("tmdb", "", {}, path=database) is False
    assert state_db.save_artwork_analysis("tmdb", "/a", [], path=database) is False
    assert state_db.load_artwork_analysis("tmdb", "", path=database) is None
    assert state_db.load_artwork_analysis("tmdb", "/missing", path=database) is None

    assert state_db.save_artwork_analysis(
        None,
        "/a",
        {"width": -1, "height": 20, "blank": True, "format": "JPEG"},
        path=database,
    )
    assert state_db.save_artwork_analysis(
        None,
        "/a",
        {"width": 100, "height": 200, "sharpness": 1.5},
        path=database,
    )
    analysis = state_db.load_artwork_analysis(None, "/a", path=database)
    assert analysis["width"] == 100
    assert analysis["format"] == "unknown"
    assert analysis["blank"] is False

    assert state_db.load_application_record("missing", path=database) is None
    with pytest.raises(TypeError):
        state_db.save_application_record("bad", [], path=database)
    assert state_db.save_application_record("settings", {"a": 1}, path=database)
    assert state_db.load_application_record("settings", path=database) == {"a": 1}


def test_media_store_mapping_scope_assets_replace_and_read_only_memory(tmp_path):
    database = tmp_path / "state.sqlite3"
    poster = tmp_path / "poster.jpg"
    store = state_db.MediaStateStore(database)
    with pytest.raises(TypeError):
        store["bad"] = []
    with pytest.raises(KeyError):
        del store["missing"]

    first = _entry("1", poster=poster)
    first.update(
        media_type="tv",
        season_average=99,
        season_number=8,
        seasons={
            "0": {"season_path": str(tmp_path / "season0.jpg"), "season_checksum": "s0"},
            "1": "ignored",
        },
    )
    store["tv:1"] = first
    assert store["tv:1"]["title"] == "Example 1"
    assert len(store) == 1
    assert store.flush() is True
    assert store.flush() is False
    assert "season_average" not in store["tv:1"]
    assert store.entries_for_scope("server", "uuid-Movies", ["1"])["tv:1"][
        "seasons"
    ]["0"]["season_checksum"] == "s0"
    assert store.entries_for_scope("server", "uuid-Movies")["tv:1"]["rating_key"] == "1"
    assert len(store.asset_destination_records()) == 2
    assert len(
        store.asset_destination_records(
            [{"server_id": "server", "library_uuid": "uuid-Movies"}]
        )
    ) == 2
    assert list(store.items()) and list(store.values())

    same = store["tv:1"]
    store["tv:1"] = same
    assert store.flush() is False
    store.replace_all({"movie:2": _entry("2"), "bad": []})
    assert set(store) == {"movie:2"}
    store.close()
    store.close()

    readonly = state_db.MediaStateStore(database, writable=False)
    readonly["memory"] = _entry("3")
    assert readonly["memory"]["rating_key"] == "3"
    del readonly["memory"]
    with pytest.raises(KeyError):
        _ = readonly["memory"]
    assert readonly.flush() is False
    readonly.close()

    missing = state_db.MediaStateStore(tmp_path / "missing.sqlite3", writable=False)
    with pytest.raises(KeyError):
        _ = missing["absent"]
    missing["temporary"] = _entry("4")
    assert set(missing) == {"temporary"}
    missing.close()


def test_plex_ownership_pruning_and_scan_state_round_trip(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert state_db.save_plex_metadata_ownership([], path=database) is False
    records = [
        _ownership("season:1", "1", "summary"),
        _ownership("season:2", "1", "title"),
        _ownership("", "2", "summary"),
    ]
    assert state_db.save_plex_metadata_ownership(records, path=database)
    loaded = state_db.load_plex_metadata_ownership("server", "uuid", "1", path=database)
    assert loaded[("season:1", "summary")]["original_value"] == {"value": "old"}
    assert state_db.prune_plex_metadata_children(
        "server", "uuid", "1", ["season:1"], path=database
    ) == 1
    assert state_db.prune_plex_metadata_children(
        "server", "uuid", "1", ["season:1"], path=database
    ) == 0
    assert state_db.prune_plex_metadata_library(
        "server", "uuid", ["1"], path=database
    ) == 1
    assert state_db.prune_plex_metadata_library(
        "server", "uuid", ["1"], path=database
    ) == 0

    scope = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "config_fingerprint": "abc",
        "item_count": 2,
    }
    assert state_db.load_scan_states([scope], path=tmp_path / "absent.sqlite3") == {}
    assert state_db.mark_scan_started([scope], False, path=database, now="start")
    assert state_db.mark_scan_started([scope], True, path=database, now="full-start")
    assert state_db.mark_scan_complete([scope], False, path=database, now="incremental")
    assert state_db.mark_scan_complete([scope], True, path=database, now="full-end")
    scan = state_db.load_scan_states([scope], path=database)[("server", "uuid")]
    assert scan["last_full_scan_completed"] == "full-end"
    assert state_db.mark_global_full_scan("global", path=database)
    assert state_db.load_global_full_scan(path=database) == "global"


def test_identity_binding_history_mismatch_touch_and_replacement(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert not state_db.save_identity_binding("s", "l", "1", "movie", None, "fp", path=database)
    assert not state_db.save_identity_binding("s", "l", "1", "movie", "10", "fp", confidence="low", path=database)
    assert state_db.load_identity_binding("s", "l", "1", "fp", path=database) is None
    assert state_db.save_identity_binding(
        "s", "l", "1", "movie", "10", "fp", source=None, match_reason=None,
        path=database, now="2026-01-01T00:00:00Z"
    )
    assert state_db.save_identity_binding(
        "s", "l", "1", "movie", "10", "fp", source="plex", match_reason="trusted",
        path=database, now="2026-01-01T00:00:01Z"
    )
    current = state_db.load_identity_binding(
        "s", "l", "1", "fp", path=database, touch=True,
        now="2026-01-01T00:00:02Z"
    )
    assert current["tmdb_id"] == "10"
    assert state_db.load_identity_binding(
        "s", "l", "1", "different", path=database, record_mismatch=True
    ) is None
    assert state_db.save_identity_binding(
        "s", "l", "1", "movie", "11", "fp2", path=database,
        now="2026-01-01T00:00:03Z"
    )
    inspected = state_db.inspect_identity_binding(
        "s", "l", "1", current_fingerprint="fp2", path=database
    )
    assert inspected["status"] == "current"
    assert len(inspected["history"]) >= 3
    assert state_db.inspect_identity_binding("s", "l", "1", path=database)[
        "status"
    ] == "unverifiable"
    assert state_db.inspect_identity_binding("s", "l", "1", current_fingerprint="bad", path=database)[
        "status"
    ] == "stale"
    assert state_db.inspect_identity_binding("s", "l", "missing", path=database)[
        "status"
    ] == "missing"


def test_asset_forget_exceptions_overrides_reviews_and_cleanup_filters(tmp_path):
    database = tmp_path / "state.sqlite3"
    store = state_db.MediaStateStore(database)
    entry = _entry("1", poster=tmp_path / "poster.jpg")
    entry["background_path"] = str(tmp_path / "background.jpg")
    entry["background_checksum"] = "bg"
    entry["seasons"] = {"0": {"season_path": str(tmp_path / "s0.jpg"), "season_checksum": "s"}}
    store["movie:1"] = entry
    store.flush()
    store.close()
    assert not state_db.remove_asset_ownership("missing", "poster", path=database)
    assert state_db.remove_asset_ownership("movie:1", "poster", path=database)
    assert state_db.remove_asset_ownership("movie:1", "season", 0, path=database)
    with pytest.raises(ValueError, match="Unsupported"):
        state_db.remove_asset_ownership("movie:1", "logo", path=database)

    with pytest.raises(ValueError, match="Unsupported exception"):
        state_db.save_item_exception("s", "l", "1", "logo", path=database)
    state_db.save_item_exception("s", "l", "1", "poster", library_name="Movies", path=database)
    state_db.save_item_exception("s", "l", "1", "season", library_name="Movies", season_number=2, path=database)
    assert len(state_db.load_item_exceptions(libraries=["Movies"], rating_keys=["1"], path=database)) == 2
    assert state_db.remove_item_exception("s", "l", "1", path=database) == 2

    with pytest.raises(ValueError, match="numeric"):
        state_db.save_identity_override("s", "l", "1", "movie", "bad", path=database)
    state_db.save_identity_override("s", "l", "1", "show", "20", library_name="Shows", path=database)
    assert state_db.load_identity_overrides(
        "s", "l", libraries=["Shows"], rating_keys=["1"], include_inactive=True, path=database
    )[0]["media_type"] == "tv"
    assert state_db.remove_identity_override("s", "l", "1", path=database) == 1

    assert state_db.reconcile_identity_reviews([None, {"category": "other"}], path=database) == []
    gap = {
        "category": "tmdb_missing",
        "server_id": "s",
        "library_uuid": "l",
        "library": "Movies",
        "plex_rating_key": "1",
        "title": "Example",
    }
    state_db.reconcile_identity_reviews([gap], path=database)
    assert state_db.load_identity_reviews(
        statuses=["open"], libraries=["Movies"], rating_keys=["1"], path=database
    )
    assert state_db.resolve_identity_reviews("s", "l", "1", path=database) == 1

    preview = state_db.observe_cleanup_candidate(
        "preview", {}, "title", writable=False, path=tmp_path / "none.sqlite3",
        now="2026-01-01T00:00:00Z", confirmations_required=1, grace_hours=0
    )
    assert preview["status"] == "preview" and preview["eligible"] is True
    state_db.record_cleanup_history(
        "manual", "remove", "completed", {"library_name": "Movies", "rating_key": "1"},
        details={"why": "test"}, path=database
    )
    history = state_db.load_cleanup_history(
        sources=["manual"], libraries=["Movies"], rating_keys=["1"], limit=0, path=database
    )
    assert history[0]["details"] == {"why": "test"}


def test_rebinding_plans_all_outcomes_and_handles_stale_or_conflicting_plans(tmp_path):
    database = tmp_path / "state.sqlite3"
    store = state_db.MediaStateStore(database)
    store["source:none"] = _entry("1", library="Old", tmdb_id=None)
    store["source:missing"] = _entry("2", library="Old", tmdb_id="200")
    store["source:ambiguous"] = _entry("3", library="Old", tmdb_id="300")
    store["dest:a"] = _entry("30", library="New", tmdb_id="300")
    store["dest:b"] = _entry("31", library="New", tmdb_id="300")
    store.flush()
    store.close()
    plan = state_db.plan_library_rebinding("Old", "New", path=database)
    assert {row["status"] for row in plan} == {"unmatched", "ambiguous"}
    assert all(not row["destination"] for row in plan)
    assert all(not row["applied"] for row in state_db.apply_library_rebinding(plan, path=database))

    stale = {
        "status": "ready",
        "source": {"cache_key": "missing"},
        "destination": {"cache_key": "dest:a"},
    }
    assert state_db.apply_library_rebinding([stale], path=database)[0]["status"] == "stale_plan"

    source_poster = tmp_path / "source.jpg"
    destination_poster = tmp_path / "destination.jpg"
    store = state_db.MediaStateStore(database)
    store["source:conflict"] = _entry("4", library="Old", tmdb_id="400", poster=source_poster)
    store["dest:conflict"] = _entry("40", library="New", tmdb_id="400", poster=destination_poster)
    store.flush()
    store.close()
    conflict_plan = state_db.plan_library_rebinding("Old", "New", path=database)
    conflict_plan = [row for row in conflict_plan if row.get("tmdb_id") == "400"]
    assert state_db.apply_library_rebinding(conflict_plan, path=database)[0]["status"] == "conflict"

    merged = state_db._merge_rebound_payload(
        {
            "poster_path": "/source",
            "destination_history": [{"event": 1}],
            "seasons": {"0": "bad", "1": {"season_path": "/s1"}},
        },
        {
            "poster_path": "/destination",
            "destination_history": [{"event": 1}],
            "seasons": {"1": "bad"},
        },
    )
    assert merged["poster_path"] == "/destination"
    assert merged["seasons"]["1"]["season_path"] == "/s1"


def test_job_history_and_maintenance_empty_paths(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert state_db.recent_job_runs(path=tmp_path / "missing.sqlite3") == []
    assert state_db.record_job_run(
        "full", "start", "end", "success", summary={"Movies": {"ok": 1}},
        history_limit=0, path=database
    )
    runs = state_db.recent_job_runs(limit=0, path=database)
    assert runs[0]["library_results"] == {"Movies": {"ok": 1}}
    maintenance = state_db.maintain_state_database(database, wal_threshold_mb=9999)
    assert maintenance["optimized"] is True
    assert maintenance["checkpointed"] is False
