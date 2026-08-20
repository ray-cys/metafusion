import sqlite3
from types import SimpleNamespace

import pytest

from helper import tmdb_cache
from helper.tmdb_cache import PersistentTTLCache


class FailingConnection:
    def __init__(self, connection, *, fail_on="SELECT"):
        self.connection = connection
        self.fail_on = fail_on

    def execute(self, statement, parameters=()):
        if self.fail_on in statement:
            raise sqlite3.OperationalError("simulated database error")
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def test_automatic_limits_schema_guards_and_close_failures(monkeypatch, tmp_path):
    cache = PersistentTTLCache()
    cache.path = tmp_path / "missing" / "child" / "cache.sqlite3"
    monkeypatch.setattr(
        tmdb_cache.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    entries, size = cache._effective_limits(0, 0)
    assert entries >= 5000 and size >= 64 * 1024**2
    assert cache._database_files()
    cache.path = None
    assert cache._database_files() == []

    unsupported = tmp_path / "unsupported.sqlite3"
    with sqlite3.connect(unsupported) as connection:
        connection.execute("PRAGMA user_version=99")
    readonly = PersistentTTLCache()
    readonly.configure(unsupported, writable=False)
    assert readonly.stats()["health"] == "degraded"

    class BadClose:
        def close(self):
            raise sqlite3.OperationalError("close")

    cache._connection = BadClose()
    cache._close_database()
    assert cache._connection is None
    cache._refresh_totals()
    assert cache._entry_count == 0


def test_memory_expiry_delete_clear_and_health_states(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(tmdb_cache.time, "time", lambda: clock[0])
    cache = PersistentTTLCache()
    cache.configure(tmp_path / "missing.sqlite3", writable=False, max_entries=1)
    cache.set("one", {"v": 1}, ttl_seconds=1)
    cache.set("two", {"v": 2}, ttl_seconds=10)
    assert cache.stats()["evictions"] == 1
    assert cache["two"] == {"v": 2}
    del cache["two"]
    with pytest.raises(KeyError):
        del cache["two"]
    cache.set("expired", {"v": 3}, ttl_seconds=1)
    clock[0] += 2
    assert cache.get("expired", "default") == "default"
    cache.clear()
    assert len(cache) == 0
    assert cache.stats()["health"] == "memory_only"

    disabled = PersistentTTLCache()
    disabled.configure(tmp_path / "disabled.sqlite3", enabled=False)
    assert disabled.stats()["health"] == "disabled"


def test_database_update_delete_read_only_clear_and_duplicate_memory(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(tmdb_cache.time, "time", lambda: clock[0])
    path = tmp_path / "cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, max_entries=10)
    cache["one"] = {"value": 1}
    cache["one"] = {"value": 2}
    cache["two"] = {"value": 2}
    assert cache.flush()
    del cache["two"]
    with pytest.raises(KeyError):
        del cache["missing"]
    assert cache.flush()

    cache._store_memory("one", {"memory": True}, clock[0], 100)
    assert len(cache) == 1
    assert set(cache) == {"one"}
    cache.reset_memory()

    readonly = PersistentTTLCache()
    readonly.configure(path, writable=False)
    readonly.clear()
    assert len(readonly) == 0
    with pytest.raises(KeyError):
        del readonly["one"]
    readonly.reset_memory()

    writable = PersistentTTLCache()
    writable.configure(path)
    writable.clear()
    assert writable.flush()
    assert len(writable) == 0
    writable.reset_memory()


def test_fetch_iteration_length_and_flush_database_errors(monkeypatch, tmp_path):
    path = tmp_path / "cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["one"] = {"value": 1}
    cache.flush()

    real = cache._connection
    cache._connection = FailingConnection(real, fail_on="SELECT response")
    with pytest.raises(KeyError):
        _ = cache["one"]
    assert cache._connection is None

    cache.configure(path)
    real = cache._connection
    cache._connection = FailingConnection(real, fail_on="SELECT cache_key FROM tmdb_cache WHERE expires_at")
    assert set(cache) == set()
    assert cache._connection is None

    cache.configure(path)
    real = cache._connection
    cache._connection = FailingConnection(real, fail_on="SELECT COUNT")
    assert len(cache) == 0
    assert cache._connection is None

    cache.configure(path)
    cache["pending"] = {"value": 2}
    real = cache._connection
    cache._connection = FailingConnection(real, fail_on="UPDATE tmdb_cache_meta")
    assert cache.flush() is False
    cache.reset_memory()


def test_read_only_expired_and_corrupt_rows_are_safe_misses(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(tmdb_cache.time, "time", lambda: clock[0])
    path = tmp_path / "cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["expired"] = {"value": 1}
    cache["broken"] = {"value": 2}
    cache.flush()
    cache._connection.execute(
        "UPDATE tmdb_cache SET expires_at=0 WHERE cache_key='expired'"
    )
    cache._connection.execute(
        "UPDATE tmdb_cache SET response=? WHERE cache_key='broken'", (b"bad",)
    )
    cache._connection.commit()
    cache.reset_memory()

    readonly = PersistentTTLCache()
    readonly.configure(path, writable=False)
    assert readonly.get("expired") is None
    assert readonly.get("broken") is None
    readonly.reset_memory()


def test_recovery_retention_trim_empty_and_failed_reopen(monkeypatch, tmp_path):
    path = tmp_path / "cache.sqlite3"
    for index in range(6):
        candidate = tmp_path / f"cache.sqlite3.corrupt-old-{index}"
        candidate.write_text("old", encoding="utf-8")
    path.write_text("broken", encoding="utf-8")
    cache = PersistentTTLCache()
    cache.configure(path)
    assert len(list(tmp_path.glob("cache.sqlite3*.corrupt-*"))) <= 4
    cache.reset_memory()

    cache.path = path
    cache.writable = True
    monkeypatch.setattr(
        cache,
        "_open_database",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("still broken")),
    )
    cache._recover_database()
    assert cache._connection is None

    cache = PersistentTTLCache()
    cache.configure(tmp_path / "trim.sqlite3", max_entries=1)
    cache._entry_count = 2
    cache._connection.execute("DELETE FROM tmdb_cache")
    cache._trim_database()
    cache.reset_memory()


def test_relieve_space_and_maintenance_success_and_failure(monkeypatch, tmp_path):
    path = tmp_path / "cache.sqlite3"
    destination = tmp_path / "destination"
    destination.mkdir()
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["one"] = {"value": "x" * 100}
    cache["two"] = {"value": "y" * 100}
    cache.flush()

    monkeypatch.setattr(
        tmdb_cache.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0),
    )
    assert cache.relieve_space(destination, 1) == 2
    result = cache.maintain(wal_threshold_mb=9999)
    assert result["optimized"] is True

    real = cache._connection
    cache._connection = FailingConnection(real, fail_on="PRAGMA optimize")
    failed = cache.maintain()
    assert failed["optimized"] is False
    assert cache._connection is None

    empty = PersistentTTLCache()
    assert empty.relieve_space(destination, 1) == 0
    assert empty.maintain()["optimized"] is False
