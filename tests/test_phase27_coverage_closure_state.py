import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from helper import state_db, tmdb_cache
from helper.state_db import MediaStateStore, StateDatabaseError
from helper.tmdb_cache import PersistentTTLCache


class QueryResult:
    def __init__(self, row=None, rows=None):
        self.row = row
        self.rows = list(rows or [])

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class SetupConnection:
    def __init__(self, integrity="ok"):
        self.integrity = integrity
        self.closed = False
        self.row_factory = None

    def execute(self, statement, _parameters=()):
        if "quick_check" in statement:
            return QueryResult((self.integrity,))
        return QueryResult((0,))

    def close(self):
        self.closed = True


class MigrationConnection:
    def __init__(self):
        self.table_info_calls = 0
        self.statements = []

    def execute(self, statement, _parameters=()):
        self.statements.append(statement)
        if statement == "PRAGMA user_version":
            return QueryResult((state_db.SCHEMA_VERSION,))
        if "table_info" in statement:
            self.table_info_calls += 1
            if self.table_info_calls == 1:
                return QueryResult(rows=[])
            if self.table_info_calls == 2:
                return QueryResult(rows=[])
            return QueryResult(
                rows=[
                    (0, name)
                    for name in (
                        "plex_rating_key",
                        "tmdb_id",
                        "imdb_id",
                        "tvdb_id",
                        "edition",
                        "season_number",
                        "identity_source",
                    )
                ]
            )
        return QueryResult()

    def executescript(self, statement):
        self.statements.append(statement)

    def commit(self):
        return None


class FailingFlushConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("locked")

    def executemany(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("locked")

    def rollback(self):
        raise sqlite3.OperationalError("rollback locked")

    def close(self):
        return None


def test_state_schema_backup_integrity_and_legacy_column_paths(monkeypatch, tmp_path):
    database = tmp_path / "state.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        for index in range(3):
            (tmp_path / f"state.sqlite3.pre-v1-old-{index}.bak").write_text("old", encoding="utf-8")
        state_db._backed_up_databases.clear()
        backup = state_db._backup_before_schema_upgrade(connection, database, 1)
    assert backup and backup.exists()
    assert len(list(tmp_path.glob("state.sqlite3.pre-v*.bak"))) == 2

    stat = database.stat()
    identity = (str(database.absolute()), stat.st_dev, stat.st_ino)
    state_db._initialized_databases.add(identity)
    state_db._integrity_checked_databases.discard(identity)
    broken = SetupConnection("bad")
    monkeypatch.setattr(state_db.sqlite3, "connect", lambda *_args, **_kwargs: broken)
    with pytest.raises(StateDatabaseError, match="integrity check failed"):
        state_db._connect(database)
    assert broken.closed
    state_db._initialized_databases.discard(identity)

    migration = MigrationConnection()
    state_db._initialize_schema(migration)
    statements = "\n".join(migration.statements)
    assert "identity_bindings ADD COLUMN source" in statements
    assert "identity_bindings ADD COLUMN match_reason" in statements
    assert "job_runs ADD COLUMN summary" in statements


def test_state_unresolved_filters_asset_backfill_and_flush_failure(tmp_path):
    database = tmp_path / "state.sqlite3"
    records = state_db.reconcile_unresolved_work(
        [
            "invalid",
            {
                "library": "Movies",
                "media_type": "movie",
                "title": "Movie",
                "asset_type": "poster",
                "category": "missing",
            },
        ],
        path=database,
    )
    assert len(records) == 1
    assert state_db.load_unresolved_work(statuses=["open"], path=database)
    assert state_db._asset_rows("show", {"media_type": "show"}) == []

    connection = state_db._connect(database)
    try:
        with connection:
            connection.execute("DELETE FROM asset_ownership")
            connection.execute(
                "INSERT OR REPLACE INTO media_state("
                "cache_key, media_type, title, payload) VALUES (?, ?, ?, ?)",
                (
                    "show",
                    "tv",
                    "Show",
                    json.dumps(
                        {
                            "media_type": "show",
                            "title": "Show",
                            "poster_path": str(tmp_path / "poster.jpg"),
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO season_state(cache_key, season_number, payload) "
                "VALUES (?, ?, ?)",
                ("show", "bad", "not-json"),
            )
            connection.execute(
                "INSERT OR REPLACE INTO season_state(cache_key, season_number, payload) "
                "VALUES (?, ?, ?)",
                (
                    "show",
                    "1",
                    json.dumps(
                        {
                            "season_path": str(tmp_path / "Season01.jpg"),
                            "season_checksum": "checksum",
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT OR REPLACE INTO media_state("
                "cache_key, media_type, title, payload) VALUES (?, ?, ?, ?)",
                ("bad", "movie", "Bad", "not-json"),
            )
            state_db._backfill_asset_ownership(connection)
            assert connection.execute("SELECT COUNT(*) FROM asset_ownership").fetchone()[0] == 2
    finally:
        connection.close()

    store = MediaStateStore(database)
    store["failure"] = {"media_type": "movie", "title": "Failure"}
    real_connection = store._connection
    store._connection = FailingFlushConnection()
    real_connection.close()
    with pytest.raises(StateDatabaseError, match="Unable to flush"):
        store.flush()
    store.close()


def test_state_retry_identity_cleanup_rebinding_and_maintenance(monkeypatch, tmp_path):
    database = tmp_path / "state.sqlite3"
    assert state_db.classify_item_failure(OSError("temporary")) == "transient"
    state_db.mark_item_started("server", "library", "1", library_name="Movies", path=database)
    state_db.record_item_failure(
        "server",
        "library",
        "1",
        ValueError("bad"),
        library_name="Movies",
        failure_class="permanent",
        path=database,
    )
    assert state_db.load_item_retries(statuses=["pending"], path=database) == []

    now = "2026-08-20T00:00:00+00:00"
    assert state_db.save_identity_binding(
        "server",
        "library",
        "1",
        "movie",
        "10",
        "fingerprint-1",
        confidence="high",
        path=database,
        now=now,
    )
    assert state_db.save_identity_binding(
        "server",
        "library",
        "1",
        "movie",
        "20",
        "fingerprint-1",
        confidence="high",
        path=database,
        now=now,
    )
    assert state_db.save_identity_binding(
        "server",
        "library",
        "1",
        "movie",
        "20",
        "fingerprint-2",
        confidence="high",
        path=database,
        now=now,
    )
    connection = state_db._connect(database)
    try:
        with connection:
            connection.execute(
                "UPDATE identity_bindings SET confidence='medium' "
                "WHERE server_id='server' AND library_uuid='library' "
                "AND rating_key='1'"
            )
    finally:
        connection.close()
    assert state_db.save_identity_binding(
        "server",
        "library",
        "1",
        "movie",
        "20",
        "fingerprint-2",
        confidence="high",
        path=database,
        now=now,
    )

    monkeypatch.setattr(
        state_db,
        "record_identity_binding_mismatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StateDatabaseError("history unavailable")),
    )
    assert (
        state_db.load_identity_binding(
            "server",
            "library",
            "1",
            "different",
            path=database,
            record_mismatch=True,
        )
        is None
    )

    assert (
        state_db._load_cleanup_rows(
            "cleanup_candidates",
            statuses=["pending"],
            libraries=["Movies"],
            rating_keys=["1"],
            path=database,
        )
        == []
    )
    assert state_db._load_cleanup_rows("missing_table", path=database) == []

    history_event = {"asset_type": "poster", "detected_at": now}
    merged = state_db._merge_rebound_payload(
        {"destination_history": [history_event]},
        {"destination_history": []},
    )
    assert merged["destination_history"] == [history_event]

    connection = state_db._connect(database)
    try:
        with connection:
            connection.execute(
                "INSERT INTO library_rebinding_history("
                "occurred_at, source_library_uuid, source_rating_key, "
                "destination_library_uuid, destination_rating_key, media_type, "
                "tmdb_id, status, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now, "old", "1", "new", "2", "movie", "20", "applied", "{}"),
            )
    finally:
        connection.close()
    assert state_db.load_library_rebinding_history(path=database)[0]["details"] == {}

    real_stat = Path.stat

    def large_wal(path, *args, **kwargs):
        if str(path).endswith("-wal"):
            return SimpleNamespace(st_size=2 * 1024 * 1024)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(state_db.Path, "stat", large_wal)
    result = state_db.maintain_state_database(database, wal_threshold_mb=1)
    assert result["optimized"] and result["checkpointed"]


class CacheFailingConnection:
    def __init__(self, connection=None, failures=()):
        self.connection = connection
        self.failures = tuple(failures)

    def execute(self, statement, parameters=()):
        if any(marker in statement for marker in self.failures):
            raise sqlite3.OperationalError("simulated failure")
        return self.connection.execute(statement, parameters)

    def executemany(self, statement, parameters):
        if any(marker in statement for marker in self.failures):
            raise sqlite3.OperationalError("simulated failure")
        return self.connection.executemany(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_tmdb_cache_remaining_error_and_maintenance_paths(monkeypatch, tmp_path):
    cache = PersistentTTLCache()
    cache.path = tmp_path / "cache.sqlite3"
    cache.path.write_text("broken", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("busy")),
    )
    cache._recover_database()
    assert cache._connection is None

    empty = PersistentTTLCache()
    assert empty._delete_expired(0) == 0
    assert empty._trim_database() is None
    empty.max_entries = 10
    empty._store_memory("present", {"value": 1}, tmdb_cache.time.time(), 60)
    assert "present" in empty

    path = tmp_path / "working.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["one"] = {"value": 1}
    cache.flush()

    real = cache._connection
    cache._connection = CacheFailingConnection(real, ("DELETE FROM tmdb_cache",))
    cache._delete_fetched_row("one", 1)
    assert cache._connection is None

    cache.configure(path)
    monkeypatch.setattr(
        cache,
        "_trim_database",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("trim")),
    )
    cache["two"] = {"value": 2}
    assert cache._connection is None and cache.get("two") == {"value": 2}

    monkeypatch.setattr(
        cache,
        "_trim_database",
        PersistentTTLCache._trim_database.__get__(cache),
    )
    cache.configure(path)
    cache._store_memory("shared", {"memory": True}, tmdb_cache.time.time(), 60)
    real = cache._connection
    cache._connection = CacheFailingConnection(real, ("SELECT 1 FROM tmdb_cache",))
    assert len(cache) == 1
    assert cache._connection is None

    cache.configure(path)
    cache._entry_count = 1
    real = cache._connection
    cache._connection = CacheFailingConnection(real, ("DELETE FROM tmdb_cache",))
    cache.clear()
    assert cache._connection is None

    cache.configure(path)
    cache["flush"] = {"value": 1}
    real = cache._connection
    cache._connection = CacheFailingConnection(
        real, ("UPDATE tmdb_cache_meta", "SELECT entry_count")
    )
    assert cache.flush() is False
    assert cache._connection is None

    cache.configure(path)
    destination = tmp_path / "destination"
    destination.mkdir()
    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("missing"))
            if candidate == destination
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    assert cache.relieve_space(destination, 1) == 0
    monkeypatch.setattr(Path, "stat", real_stat)

    cache["row"] = {"value": 1}
    cache.flush()
    monkeypatch.setattr(
        tmdb_cache.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    monkeypatch.setattr(cache, "flush", lambda: False)
    assert cache.relieve_space(destination, 1) >= 0

    cache.configure(path)
    cache["maintain"] = {"value": 1}
    cache.flush()
    wal = Path(f"{path}-wal")
    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            SimpleNamespace(st_size=2 * 1024 * 1024)
            if candidate == wal
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    result = cache.maintain(wal_threshold_mb=0)
    assert result["optimized"] and result["wal_bytes"] == 2 * 1024 * 1024
    assert isinstance(result["checkpointed"], bool)
    cache.close()


def test_state_and_tmdb_maintenance_oserror_paths(monkeypatch, tmp_path):
    assert state_db.classify_item_failure(OSError("unclassified")) == "transient"

    database = tmp_path / "state.sqlite3"
    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("missing"))
            if str(candidate).endswith("-wal")
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    result = state_db.maintain_state_database(database)
    assert result["optimized"] and result["wal_bytes"] == 0
    monkeypatch.setattr(Path, "stat", real_stat)

    cache = PersistentTTLCache()
    cache.configure(tmp_path / "cache.sqlite3")
    destination = tmp_path / "destination"
    destination.mkdir()
    cache._entry_count = 1
    cache._connection.execute("DELETE FROM tmdb_cache")
    monkeypatch.setattr(
        tmdb_cache.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    assert cache.relieve_space(destination, 1) == 0

    monkeypatch.setattr(
        tmdb_cache.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    assert cache.relieve_space(destination, 1) == 0

    wal = Path(f"{cache.path}-wal")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("missing"))
            if candidate == wal
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    maintained = cache.maintain()
    assert maintained["optimized"] and maintained["wal_bytes"] == 0
    cache.close()

    class MaintenanceConnection:
        def execute(self, statement):
            return QueryResult((0,)) if "wal_checkpoint" in statement else QueryResult()

        def close(self):
            return None

    cache = PersistentTTLCache()
    cache.path = tmp_path / "forced.sqlite3"
    cache.writable = True
    cache._connection = MaintenanceConnection()
    monkeypatch.setattr(cache, "flush", lambda: True)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda _candidate, *args, **kwargs: SimpleNamespace(st_size=2 * 1024 * 1024),
    )
    assert cache.maintain(wal_threshold_mb=1)["checkpointed"] is True
    cache.close()
