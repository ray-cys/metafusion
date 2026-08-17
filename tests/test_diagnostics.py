import sqlite3

from helper.diagnostics import (
    _tmdb_cache_status,
    write_artwork_gap_report,
    write_destination_history_report,
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
