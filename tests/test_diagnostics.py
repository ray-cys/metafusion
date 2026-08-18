import sqlite3

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
    with sqlite3.connect(database) as connection:
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
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 7")
        connection.execute("CREATE TABLE example(value TEXT)")

    assert "schema 7, check ok" in _database_status(database)
    database.write_text("broken", encoding="utf-8")
    assert _database_status(database) == "unreadable (DatabaseError)"


def test_tmdb_cache_status_handles_missing_metadata_and_invalid_database(tmp_path):
    database = tmp_path / "tmdb_cache.sqlite3"
    with sqlite3.connect(database) as connection:
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
