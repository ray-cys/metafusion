import sqlite3
from contextlib import closing

import pytest

from helper import state_db


def _empty_supported_database(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA user_version = 1")


def test_read_only_state_apis_do_not_create_a_missing_database(tmp_path):
    missing = tmp_path / "missing" / "state.sqlite3"
    assert state_db.load_unresolved_work(path=missing) == []
    assert state_db.load_global_full_scan(path=missing) is None
    assert state_db.load_due_item_retries("server", "library", path=missing) == {}
    assert state_db.retry_queue_summary(path=missing) == {}
    assert state_db.load_item_retries(path=missing) == []
    assert state_db.missing_library_inventory("server", [], path=missing) == []
    assert state_db.load_identity_binding(
        "server", "library", "1", "fingerprint", path=missing
    ) is None
    assert state_db.load_asset_ownership(path=missing) == []
    assert state_db.load_item_exceptions(path=missing) == []
    assert state_db.load_identity_overrides(path=missing) == []
    assert state_db.load_identity_reviews(path=missing) == []
    assert state_db.load_cleanup_candidates(path=missing) == []
    assert state_db.load_cleanup_history(path=missing) == []
    assert state_db.load_library_rebinding_history(path=missing) == []
    assert not missing.exists()


def test_read_only_state_apis_accept_supported_legacy_database_without_tables(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _empty_supported_database(database)
    state_db._integrity_checked_databases.clear()

    assert state_db.load_artwork_analysis("tmdb", "/poster", path=database) is None
    assert state_db.load_unresolved_work(path=database) == []
    assert state_db.load_plex_metadata_ownership("server", "library", "1", path=database) == {}
    assert state_db.load_due_item_retries("server", "library", path=database) == {}
    assert state_db.retry_queue_summary(path=database) == {}
    assert state_db.load_item_retries(path=database) == []
    assert state_db.missing_library_inventory("server", [], path=database) == []
    assert state_db.load_identity_binding(
        "server", "library", "1", "fingerprint", path=database
    ) is None
    assert state_db.load_item_exceptions(path=database) == []
    assert state_db.load_identity_overrides(path=database) == []
    assert state_db.load_identity_reviews(path=database) == []
    assert state_db.load_cleanup_history(path=database) == []
    assert state_db.load_library_rebinding_history(path=database) == []


def test_state_store_deleted_pending_and_iterable_replacement_paths(tmp_path):
    database = tmp_path / "state.sqlite3"
    store = state_db.MediaStateStore(path=database)
    store["movie:1"] = {
        "title": "One",
        "year": 2020,
        "media_type": "movie",
        "poster_path": "/poster.jpg",
    }
    store.flush()
    del store["movie:1"]
    with pytest.raises(KeyError):
        _ = store["movie:1"]
    assert store.flush() is True

    assert store.replace_all(
        [
            (
                "movie:2",
                {"title": "Two", "year": 2021, "media_type": "movie"},
            )
        ]
    ) is True
    assert set(store) == {"movie:2"}
    store.close()


def test_state_empty_batches_filters_and_season_asset_forget(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert state_db.mark_items_started("server", "library", [], path=database) is False
    assert state_db.clear_item_retries("server", "library", [], path=database) == 0

    state_db.save_item_exception(
        "server",
        "library",
        "1",
        "poster",
        library_name="Movies",
        path=database,
    )
    assert state_db.load_item_exceptions(libraries=["Other"], path=database) == []

    store = state_db.MediaStateStore(path=database)
    store["tv:1"] = {
        "title": "Show",
        "year": 2020,
        "media_type": "tv",
        "seasons": {
            "1": {
                "season_path": "/Season01.jpg",
                "season_checksum": "checksum",
                "season_last_checked": "now",
            }
        },
    }
    store.flush()
    store.close()
    assert state_db.remove_asset_ownership(
        "tv:1", "season", season_number=1, path=database
    )
    reloaded = state_db.MediaStateStore(path=database, writable=False)
    assert "season_path" not in reloaded["tv:1"]["seasons"]["1"]
    assert reloaded["tv:1"]["seasons"]["1"]["season_last_checked"] == "now"
    reloaded.close()


def test_schema_backup_is_deduplicated_for_one_database_identity(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    _empty_supported_database(database)
    state_db._backed_up_databases.clear()
    with closing(sqlite3.connect(database)) as connection:
        first = state_db._backup_before_schema_upgrade(connection, database, 1)
        second = state_db._backup_before_schema_upgrade(connection, database, 1)
    assert first is not None and first.exists()
    assert second is None
