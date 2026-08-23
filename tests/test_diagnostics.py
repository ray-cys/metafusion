import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from helper import diagnostics
from helper.diagnostics import (
    _database_status,
    _tmdb_cache_status,
    record_kometa_metadata_audit,
    write_artwork_gap_report,
    write_destination_history_report,
    write_metadata_audit_report,
    write_release_qualification_report,
    write_support_report,
)


def test_support_report_omits_configuration_values(tmp_path):
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "plex": {
            "url": "http://private-plex:32400",
            "token": "secret-plex-token",
            "path_mappings": ["/private/source=>/media"],
        },
        "plex_libraries": ["Private Movies"],
        "tmdb": {"api_key": "secret-tmdb-key"},
        "plex_metadata": {"enabled": True, "policy": "fill_missing"},
    }

    report = write_support_report(
        config,
        base_dir=tmp_path,
        environ={
            "PLEX_TOKEN": "secret-plex-token",
            "TMDB_API_KEY": "secret-tmdb-key",
            "METAFUSION_VERSION": "develop",
            "METAFUSION_COMMIT": "abc123",
        },
    )
    contents = report.read_text(encoding="utf-8")

    assert "secret-plex-token" not in contents
    assert "secret-tmdb-key" not in contents
    assert "private-plex" not in contents
    assert "Private Movies" not in contents
    assert "/private/source" not in contents
    assert "PLEX_TOKEN" in contents
    assert "TMDB_API_KEY" in contents
    assert "Version: develop" in contents
    assert "Commit: abc123" in contents


def test_support_reports_do_not_overwrite_within_one_second(tmp_path):
    config = {
        "settings": {"mode": "kometa", "dry_run": True},
        "plex": {"path_mappings": []},
        "plex_libraries": [],
        "plex_metadata": {},
    }

    first = write_support_report(config, base_dir=tmp_path, environ={})
    second = write_support_report(config, base_dir=tmp_path, environ={})

    assert first != second
    assert first.exists()
    assert second.exists()


def test_global_retention_bounds_support_and_release_reports(monkeypatch, tmp_path):
    monkeypatch.setattr("helper.diagnostics.STATE_DATABASE", tmp_path / "state.sqlite3")
    monkeypatch.setattr("helper.diagnostics.CACHE_DIR", tmp_path / "cache")
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "output": {"report_retention": 1},
        "plex": {"path_mappings": []},
        "plex_libraries": [],
        "plex_metadata": {},
    }

    old_support = write_support_report(config, base_dir=tmp_path, environ={})
    new_support = write_support_report(config, base_dir=tmp_path, environ={})
    old_release, _ = write_release_qualification_report(
        config,
        {"available_count": 1, "path_advice": {"records": []}},
        base_dir=tmp_path,
        environ={},
    )
    new_release, _ = write_release_qualification_report(
        config,
        {"available_count": 1, "path_advice": {"records": []}},
        base_dir=tmp_path,
        environ={},
    )

    assert not old_support.exists()
    assert new_support.exists()
    assert not old_release.exists()
    assert new_release.exists()


def test_tmdb_cache_status_reports_entries_and_compressed_size(tmp_path):
    database = tmp_path / "tmdb_cache.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE tmdb_cache_meta("
            "singleton INTEGER PRIMARY KEY, entry_count INTEGER, stored_bytes INTEGER)"
        )
        connection.execute("INSERT INTO tmdb_cache_meta VALUES (1, 42, 12345)")
        connection.execute("PRAGMA user_version = 1")

    status = _tmdb_cache_status(database)

    assert "entries 42" in status
    assert "compressed 12345 bytes" in status
    assert "health ok" in status


def test_release_qualification_report_passes_without_existing_databases(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("helper.diagnostics.STATE_DATABASE", tmp_path / "state.sqlite3")
    monkeypatch.setattr("helper.diagnostics.CACHE_DIR", tmp_path / "cache")
    config = {"settings": {"mode": "kometa"}}

    report, passed = write_release_qualification_report(
        config,
        {
            "available_count": 2,
            "path_advice": {"records": [{"status": "visible"}]},
        },
        base_dir=tmp_path,
        environ={"METAFUSION_VERSION": "1.2.3", "METAFUSION_COMMIT": "abc123"},
    )
    contents = report.read_text(encoding="utf-8")

    assert passed is True
    assert "Result: PASS" in contents
    assert "Version: 1.2.3" in contents
    assert "Commit: abc123" in contents
    assert "[PASS] Supported libraries: 2 available" in contents
    assert "[PASS] Plex media path samples: resolved" in contents
    assert "Manual release gates still required" in contents


def test_release_qualification_report_fails_for_unresolved_paths_and_bad_databases(
    monkeypatch, tmp_path
):
    state_database = tmp_path / "state.sqlite3"
    tmdb_database = tmp_path / "cache" / "tmdb_cache.sqlite3"
    state_database.write_text("not sqlite", encoding="utf-8")
    tmdb_database.parent.mkdir()
    tmdb_database.write_text("not sqlite", encoding="utf-8")
    monkeypatch.setattr("helper.diagnostics.STATE_DATABASE", state_database)
    monkeypatch.setattr("helper.diagnostics.CACHE_DIR", tmdb_database.parent)

    report, passed = write_release_qualification_report(
        {"settings": {"mode": "plex"}},
        {
            "available_count": 0,
            "path_advice": {
                "records": [
                    {"status": "unresolved"},
                    "ignored non-mapping record",
                ]
            },
        },
        base_dir=tmp_path,
        environ={},
    )
    contents = report.read_text(encoding="utf-8")

    assert passed is False
    assert "Result: FAIL" in contents
    assert "[FAIL] Supported libraries: 0 available" in contents
    assert "[FAIL] Plex media path samples: 1 unresolved" in contents
    assert "unreadable (DatabaseError)" in contents


def test_database_status_reports_healthy_and_unreadable_files(tmp_path):
    database = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA user_version = 7")
        connection.execute("CREATE TABLE example(value TEXT)")

    assert "schema 7, check ok" in _database_status(database)
    database.write_text("broken", encoding="utf-8")
    assert _database_status(database) == "unreadable (DatabaseError)"


def test_tmdb_cache_status_handles_missing_metadata_and_invalid_database(tmp_path):
    database = tmp_path / "tmdb_cache.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE tmdb_cache_meta("
            "singleton INTEGER PRIMARY KEY, entry_count INTEGER, stored_bytes INTEGER)"
        )

    assert _tmdb_cache_status(database) == "unreadable (metadata row missing)"
    database.write_text("broken", encoding="utf-8")
    assert _tmdb_cache_status(database) == "unreadable (DatabaseError)"


def test_artwork_gap_reports_are_deduplicated_and_retained(tmp_path):
    gap = {
        "library": "Movies",
        "media_type": "Movie",
        "title": "Example (2020)",
        "asset_type": "poster",
        "category": "artwork_missing",
    }
    first = write_artwork_gap_report([gap, gap], base_dir=tmp_path, retention=2)
    second = write_artwork_gap_report([gap], base_dir=tmp_path, retention=2)
    third = write_artwork_gap_report([gap], base_dir=tmp_path, retention=2)

    assert "Entries: 1" in third.read_text(encoding="utf-8")
    assert len(list((tmp_path / "reports").glob("artwork-gaps-*.txt"))) == 2
    assert not first.exists()
    assert second.exists()


def test_recorded_missing_artwork_gaps_recover_movie_and_season_state():
    cache = {
        "movie:plex:1": {
            "library_name": "Movies",
            "media_type": "movie",
            "rating_key": 1,
            "tmdb_id": 11,
            "title": "Missing Movie",
            "year": 2024,
            "edition": "Extended",
            "poster_missing_checks": 2,
            "poster_candidate_fingerprint": "",
            "poster_last_checked": "2026-08-22T00:00:00+00:00",
            "background_missing_checks": "invalid",
        },
        "tv:plex:2": {
            "library_name": "TV Shows",
            "media_type": "show",
            "rating_key": 2,
            "tmdb_id": 22,
            "tvdb_id": 33,
            "title": "Missing Season",
            "year": 2025,
            "season_missing_checks": 3,
            "season_candidate_fingerprint": "0:|10:|bad|x:",
            "season_last_checked": "2026-08-22T00:00:00+00:00",
            "seasons": {
                "0": {
                    "season_path": "/assets/season00.jpg",
                    "season_checksum": "abc",
                }
            },
        },
        "tv:plex:3": {
            "library_name": "TV Shows",
            "media_type": "tv",
            "title": "No recorded gaps",
            "season_missing_checks": 0,
            "season_candidate_fingerprint": "",
        },
        "invalid": [],
    }
    config = {
        "assets": {
            "run_poster": True,
            "run_background": True,
            "run_season": True,
        }
    }

    recovered = diagnostics.recorded_missing_artwork_gaps(cache, config)

    assert [(entry["asset_type"], entry["title"]) for entry in recovered] == [
        ("poster", "Missing Movie (2024) [Extended]"),
        ("season 10 poster", "Missing Season (2025)"),
    ]
    assert recovered[0]["plex_rating_key"] == "1"
    assert recovered[1]["season_number"] == 10
    assert recovered[1]["tvdb_id"] == "33"
    disabled = diagnostics.recorded_missing_artwork_gaps(
        cache,
        {
            "assets": {
                "run_poster": False,
                "run_background": False,
                "run_season": False,
            }
        },
    )
    assert disabled == []


def test_artwork_gap_snapshot_combines_current_carried_and_resolved(tmp_path):
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    cache = {
        "movie:plex:1": {
            "library_name": "Movies",
            "media_type": "movie",
            "rating_key": "1",
            "poster_path": "/assets/poster.jpg",
            "poster_checksum": "abc",
            "poster_missing_checks": 1,
            "poster_last_checked": (now - timedelta(hours=12)).isoformat(),
        },
        "tv:plex:2": {
            "library_name": "TV Shows",
            "media_type": "tv",
            "tmdb_id": "22",
            "season_missing_checks": 2,
            "season_last_checked": (now - timedelta(days=1)).isoformat(),
            "seasons": {},
        },
    }
    current = [
        {
            "library": "Movies",
            "media_type": "Movie",
            "title": "Current (2024)",
            "asset_type": "poster",
            "category": "artwork_missing",
            "plex_rating_key": "1",
            "detail": "No candidate",
        },
        {
            "library": "Movies",
            "media_type": "Movie",
            "title": "Identity (2024)",
            "asset_type": "metadata",
            "category": "identity_rejected",
        },
    ]
    persistent = [
        {
            "library_name": "TV Shows",
            "media_type": "TV Show",
            "title": "Carried (2025)",
            "asset_type": "season 10 poster",
            "category": "artwork_missing",
            "tmdb_id": "22",
            "season_number": 10,
            "status": "open",
        },
        {
            "library_name": "Movies",
            "media_type": "Movie",
            "title": "Disabled (2024)",
            "asset_type": "background",
            "category": "policy_preserved_missing",
            "status": "open",
        },
        {
            "library_name": "Movies",
            "media_type": "Movie",
            "title": "Manual (2024)",
            "asset_type": "poster",
            "category": "artwork_preserved",
            "status": "open",
        },
        {
            "library_name": "Movies",
            "media_type": "Movie",
            "title": "Resolved (2024)",
            "asset_type": "poster",
            "category": "artwork_missing",
            "status": "resolved",
        },
    ]
    config = {
        "assets": {
            "run_poster": True,
            "run_background": False,
            "run_season": True,
        },
        "image_upgrades": {
            "movie_days": 30,
            "series_days": 15,
            "season_days": 15,
            "default_days": 30,
        },
        "library_overrides": {
            "TV Shows": {"image_upgrades": {"season_days": 20}}
        },
    }

    snapshot = diagnostics.artwork_gap_snapshot(
        current,
        persistent_records=persistent,
        recorded_gaps=[persistent[0]],
        cache=cache,
        config=config,
        now=now,
    )

    assert snapshot["summary"] == {
        "current_run": 2,
        "open": 5,
        "carried_forward": 3,
        "resolved": 1,
        "artwork_current_run": 1,
        "artwork_open": 4,
        "artwork_carried_forward": 3,
        "artwork_resolved": 1,
        "artwork_not_due": 2,
    }
    entries = {entry["title"]: entry for entry in snapshot["entries"]}
    assert entries["Current (2024)"]["destination_state"] == "recorded_present"
    assert entries["Current (2024)"]["recheck_status"] == "not_due"
    assert entries["Carried (2025)"]["recheck_status"] == "not_due"
    assert entries["Disabled (2024)"]["recheck_status"] == "disabled"
    assert entries["Manual (2024)"]["destination_state"] == "recorded_present"
    assert snapshot["libraries"]["TV Shows"]["carried_forward"] == 1

    report = write_artwork_gap_report(
        current,
        base_dir=tmp_path,
        snapshot=snapshot,
    )
    text = report.read_text(encoding="utf-8")
    assert "Current run\n- [artwork_missing]" in text
    assert "Carried-forward open gaps" in text
    assert "Recently resolved" in text
    assert "next recheck=" in text


def test_artwork_gap_edge_states_and_snapshot_deduplication():
    indexes = diagnostics._cache_index(
        {
            "invalid": [],
            "show": {
                "library_name": "TV Shows",
                "media_type": "show",
                "tmdb_id": 22,
            },
        }
    )
    assert indexes[1][("tv shows", "tv", "22")]["tmdb_id"] == 22
    assert diagnostics._gap_destination_state(
        {"status": "resolved"}, None
    ) == "resolved"
    assert diagnostics._gap_destination_state(
        {"category": "path_invalid"}, None
    ) == "unavailable"
    assert diagnostics._gap_destination_state(
        {
            "category": "artwork_missing",
            "asset_type": "season 1 poster",
            "season_number": 1,
        },
        {
            "seasons": {
                "1": {"season_path": "/asset.jpg", "season_checksum": "abc"}
            }
        },
    ) == "recorded_present"
    assert diagnostics._gap_destination_state(
        {"category": "artwork_missing", "asset_type": "background"},
        {},
    ) == "recorded_missing"

    record = {
        "library": "Movies",
        "media_type": "Movie",
        "title": "Example (2024)",
        "asset_type": "poster",
        "category": "artwork_missing",
    }
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    disabled = diagnostics._gap_recheck(
        record,
        {"poster_last_checked": now.isoformat()},
        {
            "assets": {"run_poster": True},
            "image_upgrades": {"movie_days": 0},
        },
        now,
    )
    assert disabled[2] == "disabled"
    invalid = diagnostics._gap_recheck(
        record,
        {"poster_last_checked": "invalid"},
        {
            "assets": {"run_poster": True},
            "image_upgrades": {"movie_days": 30},
        },
        now,
    )
    assert invalid[2] == "due"
    naive = diagnostics._gap_recheck(
        record,
        {"poster_last_checked": "2026-08-01T00:00:00"},
        {
            "assets": {"run_poster": True},
            "image_upgrades": {"movie_days": 30},
        },
        now,
    )
    assert naive[2] == "not_due"

    snapshot = diagnostics.artwork_gap_snapshot(
        [record],
        persistent_records=[{**record, "status": "open"}],
        recorded_gaps=[
            {
                **record,
                "title": "Recorded only (2024)",
                "plex_rating_key": "2",
            }
        ],
        now=datetime(2026, 8, 23, tzinfo=timezone.utc).replace(tzinfo=None),
    )
    assert snapshot["summary"]["open"] == 2
    assert {entry["observation"] for entry in snapshot["entries"]} == {
        "current_run",
        "recorded_state",
    }


def test_compatibility_report_without_warnings(tmp_path):
    report = diagnostics.write_compatibility_report(
        {
            "profile": "default",
            "mode": "kometa",
            "contract": "supported",
            "checks": [],
            "capabilities": [],
            "warnings": [],
        },
        base_dir=tmp_path,
    )
    assert "Warnings" not in report.read_text(encoding="utf-8")


def test_destination_history_report_marks_events_without_deleting_paths(tmp_path):
    old_path = tmp_path / "old" / "poster.jpg"
    new_path = tmp_path / "new" / "poster.jpg"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    old_path.write_text("old", encoding="utf-8")
    new_path.write_text("new", encoding="utf-8")
    cache = {
        "movie:1": {
            "title": "Renamed Movie",
            "year": 2020,
            "destination_history": [
                {
                    "asset_type": "poster",
                    "season_number": None,
                    "previous_destination": str(old_path),
                    "new_destination": str(new_path),
                    "detected_at": "2026-01-01T00:00:00+00:00",
                    "reported_at": None,
                }
            ],
        }
    }

    report = write_destination_history_report(cache, base_dir=tmp_path)

    assert str(old_path) in report.read_text(encoding="utf-8")
    assert cache["movie:1"]["destination_history"][0]["reported_at"]
    assert old_path.exists()
    assert new_path.exists()
    assert write_destination_history_report(cache, base_dir=tmp_path) is None


def test_metadata_audit_reports_state_and_actions_without_values(tmp_path):
    config = {
        "_execution": {"metadata_audit": True},
        "_metadata_audit_records": [],
    }
    record_kometa_metadata_audit(
        config,
        library="Movies",
        media_type="Movie",
        title="Private Title (2020)",
        existing={"summary": "old secret", "studio": "same"},
        generated={
            "summary": "new secret",
            "studio": "same",
            "tagline": "added secret",
            "content_rating": "",
        },
        diagnostics={"deprecated_removed": 1},
    )
    report = write_metadata_audit_report(
        config["_metadata_audit_records"],
        [
            {
                "library": "Movies",
                "media_type": "Movie",
                "title": "Rejected (2022)",
                "category": "identity_rejected",
                "detail": "year mismatch",
            }
        ],
        mode="kometa",
        base_dir=tmp_path,
    )
    contents = report.read_text(encoding="utf-8")

    assert "[different]" in contents
    assert "proposed=update" in contents
    assert "[missing]" in contents
    assert "[source_missing]" in contents
    assert "[unsupported]" in contents
    assert "identity_rejected" in contents
    assert "old secret" not in contents
    assert "new secret" not in contents
    assert "added secret" not in contents

    second = write_metadata_audit_report(
        config["_metadata_audit_records"],
        mode="kometa",
        base_dir=tmp_path,
        retention=1,
    )
    assert second.exists()
    assert not report.exists()
    assert record_kometa_metadata_audit(
        {"_execution": {}},
        library="Movies",
        media_type="Movie",
        title="Ignored",
        existing={},
        generated={},
    ) == 0
