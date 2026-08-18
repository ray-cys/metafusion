import asyncio
import json
import sqlite3
from stat import S_IMODE

import pytest

from helper import cache as cache_module
from helper.state_db import StateDatabaseError


def test_cache_lock_is_recreated_for_each_scheduled_event_loop():
    async def current_lock():
        return cache_module.get_cache_lock()

    first = asyncio.run(current_lock())
    second = asyncio.run(current_lock())

    assert first is not second


def configure_cache(tmp_path, writable=True):
    return cache_module.begin_cache_session(
        writable=writable,
        database_path=tmp_path / "meta_db.sqlite3",
    )


def database_rows(path, table):
    with sqlite3.connect(path) as connection:
        return connection.execute(f"SELECT * FROM {table}").fetchall()


def test_cache_is_persisted_as_normalized_sqlite_rows(tmp_path):
    configure_cache(tmp_path)
    original = {
        "tv:Example:2020": {
            "media_type": "tv",
            "season_average": 4.2,
            "season_number": 1,
            "seasons": {"1": {"season_average": 4.2}},
        }
    }

    cache_module.save_cache(original)
    database = tmp_path / "meta_db.sqlite3"
    media_payload = json.loads(database_rows(database, "media_state")[0][-1])
    season_payload = json.loads(database_rows(database, "season_state")[0][-1])

    assert "season_average" not in media_payload
    assert "season_number" not in media_payload
    assert "seasons" not in media_payload
    assert season_payload["season_average"] == 4.2
    assert S_IMODE(database.stat().st_mode) == 0o664
    assert original["tv:Example:2020"]["season_average"] == 4.2


def test_corrupt_durable_state_fails_without_deleting_it(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    database.write_text("not sqlite", encoding="utf-8")

    with pytest.raises(StateDatabaseError, match="Unable to open"):
        configure_cache(tmp_path)

    assert database.read_text(encoding="utf-8") == "not sqlite"


def test_read_only_missing_state_does_not_create_directory(tmp_path):
    cache_dir = tmp_path / "missing" / "cache"

    store = cache_module.begin_cache_session(
        writable=False,
        database_path=cache_dir / "meta_db.sqlite3",
    )

    assert dict(store) == {}
    assert not cache_dir.exists()


def test_cache_updates_preserve_identity_and_use_exact_season_number(tmp_path):
    configure_cache(tmp_path)

    async def update_with_boolean():
        await cache_module.meta_cache_async(
            "tv:Example:2020",
            123,
            "Example",
            2020,
            "tv",
            season_number=2,
            season_average=7.0,
        )
        await cache_module.meta_cache_async(
            "tv:Example:2020",
            None,
            None,
            None,
            None,
            update_timestamp=False,
            plex_child_fingerprint="child-fingerprint",
        )
        await cache_module.meta_cache_async(
            "tv:Example:2020",
            None,
            None,
            None,
            None,
            update_timestamp=False,
            season_number=2,
            season_upgraded=True,
        )

    asyncio.run(update_with_boolean())
    entry = cache_module.load_cache()["tv:Example:2020"]
    assert "season_last_upgraded" not in entry["seasons"]["2"]

    async def update_with_number():
        await cache_module.meta_cache_async(
            "tv:Example:2020",
            None,
            None,
            None,
            None,
            update_timestamp=False,
            season_number=2,
            season_upgraded=2,
        )

    asyncio.run(update_with_number())
    cache_module.flush_cache()
    configure_cache(tmp_path)
    entry = cache_module.load_cache()["tv:Example:2020"]

    assert entry["tmdb_id"] == 123
    assert entry["plex_child_fingerprint"] == "child-fingerprint"
    assert entry["title"] == "Example"
    assert entry["year"] == 2020
    assert entry["media_type"] == "tv"
    assert "season_last_upgraded" in entry["seasons"]["2"]


def test_obsolete_title_year_cache_key_is_not_promoted(tmp_path):
    configure_cache(tmp_path)
    cache_module.save_cache(
        {
            "movie:Example:2020": {
                "tmdb_id": "123",
                "title": "Example",
                "year": 2020,
                "media_type": "movie",
                "poster_average": 7.5,
            }
        }
    )

    asyncio.run(
        cache_module.meta_cache_async(
            "movie:plex:10",
            "123",
            "Example",
            2020,
            "movie",
            update_timestamp=False,
        )
    )
    cache_module.flush_cache()
    configure_cache(tmp_path)
    cache = cache_module.load_cache()

    assert cache["movie:Example:2020"]["poster_average"] == 7.5
    assert "poster_average" not in cache["movie:plex:10"]


def test_cache_records_independent_artwork_check_timestamps(tmp_path):
    configure_cache(tmp_path)

    asyncio.run(
        cache_module.meta_cache_async(
            "tv:Example:2020",
            123,
            "Example",
            2020,
            "tv",
            update_timestamp=False,
            poster_checked=True,
            background_checked=True,
            season_checked=True,
        )
    )
    entry = cache_module.load_cache()["tv:Example:2020"]

    assert "poster_last_checked" in entry
    assert "background_last_checked" in entry
    assert "season_last_checked" in entry
    assert "last_updated" not in entry


def test_cache_tracks_pending_metadata_and_artwork_destination_changes(tmp_path):
    configure_cache(tmp_path)

    async def update_paths():
        await cache_module.meta_cache_async(
            "tv:1",
            1,
            "Old Title",
            2020,
            "tv",
            update_timestamp=False,
            poster_path=str(tmp_path / "old" / "poster.jpg"),
            poster_checksum="old-checksum",
        )
        await cache_module.meta_cache_async(
            "tv:1",
            1,
            "Old Title",
            2020,
            "tv",
            update_timestamp=False,
            season_number=1,
            season_path=str(tmp_path / "old" / "Season01.jpg"),
            season_checksum="old-season-checksum",
        )
        await cache_module.meta_cache_async(
            "tv:1",
            1,
            "New Title",
            2020,
            "tv",
            update_timestamp=False,
            poster_path=str(tmp_path / "new" / "poster.jpg"),
            poster_checksum="new-checksum",
            metadata_pending_count=2,
        )
        await cache_module.meta_cache_async(
            "tv:1",
            1,
            "New Title",
            2020,
            "tv",
            update_timestamp=False,
            season_number=1,
            season_path=str(tmp_path / "new" / "Season01.jpg"),
            season_checksum="new-season-checksum",
        )

    asyncio.run(update_paths())
    entry = cache_module.load_cache()["tv:1"]

    assert entry["metadata_pending_count"] == 2
    assert entry["metadata_pending_at"]
    assert {event["asset_type"] for event in entry["destination_history"]} == {
        "poster",
        "season",
    }
    assert all(event["reported_at"] is None for event in entry["destination_history"])

    asyncio.run(
        cache_module.meta_cache_async(
            "tv:1",
            1,
            "New Title",
            2020,
            "tv",
            update_timestamp=False,
            metadata_pending_count=0,
        )
    )
    entry = cache_module.load_cache()["tv:1"]
    assert entry["metadata_pending_count"] == 0
    assert entry["metadata_pending_at"] == ""


def test_cache_updates_are_batched_until_flush(tmp_path):
    configure_cache(tmp_path)
    database = tmp_path / "meta_db.sqlite3"

    async def update_many():
        await asyncio.gather(
            *(
                cache_module.meta_cache_async(
                    f"movie:Example {index}:2020",
                    index,
                    f"Example {index}",
                    2020,
                    "movie",
                )
                for index in range(25)
            )
        )

    asyncio.run(update_many())

    assert database_rows(database, "media_state") == []
    assert cache_module.flush_cache() is True
    assert len(database_rows(database, "media_state")) == 25
    assert cache_module.flush_cache() is False


def test_scoped_cache_read_returns_only_requested_library_rows(tmp_path):
    store = configure_cache(tmp_path)
    cache_module.save_cache(
        {
            "movie:1": {
                "server_id": "server",
                "library_uuid": "movies",
                "rating_key": "1",
                "media_type": "movie",
                "seasons": {"1": {"season_average": 1}},
            },
            "tv:2": {
                "server_id": "server",
                "library_uuid": "tv",
                "rating_key": "2",
                "media_type": "tv",
                "poster_path": str(tmp_path / "show" / "poster.jpg"),
                "seasons": {
                    "0": {
                        "season_average": 2,
                        "season_path": str(tmp_path / "show" / "Season00.jpg"),
                    }
                },
            },
        }
    )

    scoped = store.entries_for_scope("server", "tv", rating_keys=["2"])

    assert set(scoped) == {"tv:2"}
    assert scoped["tv:2"]["seasons"]["0"]["season_average"] == 2
    owners = store.asset_destination_records(
        [{"server_id": "server", "library_uuid": "tv"}]
    )
    assert {
        (record["cache_key"], record["asset_type"], record["season_number"], record["destination"])
        for record in owners
    } == {
        ("tv:2", "poster", "", str((tmp_path / "show" / "poster.jpg").resolve())),
        ("tv:2", "season", "0", str((tmp_path / "show" / "Season00.jpg").resolve())),
    }


def test_unchanged_entry_does_not_create_pending_write(tmp_path):
    configure_cache(tmp_path)
    value = {"title": "One", "media_type": "movie"}
    cache_module.save_cache({"movie:one": value})
    configure_cache(tmp_path)

    cache_module.load_cache()["movie:one"] = value

    assert cache_module.flush_cache() is False


def test_legacy_json_is_ignored_and_left_untouched(tmp_path):
    legacy = tmp_path / "meta_cache.json"
    legacy.write_text(
        json.dumps({"movie:one": {"title": "One", "media_type": "movie"}}),
        encoding="utf-8",
    )
    original = legacy.read_bytes()

    store = configure_cache(tmp_path)

    assert dict(store) == {}
    assert legacy.read_bytes() == original


def test_read_only_session_never_persists_updates(tmp_path):
    configure_cache(tmp_path, writable=False)

    asyncio.run(
        cache_module.meta_cache_async(
            "movie:one", 1, "One", 2020, "movie"
        )
    )

    assert cache_module.flush_cache() is False
    assert not (tmp_path / "meta_db.sqlite3").exists()


def test_cache_scope_is_persisted_with_media_identity(tmp_path):
    configure_cache(tmp_path)
    token = cache_module.set_cache_scope("server-1", "library-1", "Movies")
    try:
        asyncio.run(
            cache_module.meta_cache_async(
                "movie:plex:10",
                123,
                "Example",
                2020,
                "movie",
                rating_key="10",
            )
        )
    finally:
        cache_module.reset_cache_scope(token)
    cache_module.flush_cache()

    row = database_rows(tmp_path / "meta_db.sqlite3", "media_state")[0]
    assert row[1:5] == ("server-1", "library-1", "Movies", "10")
