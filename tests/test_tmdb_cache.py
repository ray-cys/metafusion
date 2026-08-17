import json
import sqlite3
from stat import S_IMODE

from helper import tmdb_cache as cache_module
from helper.tmdb_cache import PersistentTTLCache


def test_sqlite_tmdb_cache_round_trips_compresses_and_expires(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    path = tmp_path / "cache" / "tmdb.sqlite3"
    value = {"title": "Example", "images": [{"path": "/poster.jpg"}] * 50}
    cache = PersistentTTLCache()
    cache.configure(path, ttl_hours=1, max_entries=10)
    cache["movie/1"] = value

    assert cache.flush() is True
    assert path.read_bytes().startswith(b"SQLite format 3\x00")
    assert S_IMODE(path.stat().st_mode) == 0o664
    assert not path.with_name(f"{path.name}.bak").exists()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        stored_bytes = connection.execute(
            "SELECT stored_bytes FROM tmdb_cache WHERE cache_key = 'movie/1'"
        ).fetchone()[0]
    compact_bytes = len(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    assert stored_bytes < compact_bytes

    reloaded = PersistentTTLCache()
    clock[0] += 30
    reloaded.configure(path, ttl_hours=1, max_entries=10)
    assert reloaded["movie/1"] == value

    reloaded.reset_memory()
    clock[0] += 3601
    expired = PersistentTTLCache()
    expired.configure(path, ttl_hours=1, max_entries=10)
    assert "movie/1" not in expired
    assert expired.flush() is True


def test_tmdb_cache_is_entry_bounded_and_ignores_non_json_values(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    cache = PersistentTTLCache()
    cache.configure(tmp_path / "tmdb.sqlite3", ttl_hours=1, max_entries=2)
    for key in ("one", "two", "three"):
        clock[0] += 1
        cache[key] = {"key": key}
    cache["bytes"] = b"not-json"

    assert set(cache) == {"two", "three"}
    assert "bytes" not in cache
    assert cache.stats()["evictions"] == 1


def test_tmdb_cache_optional_byte_limit_uses_lru_eviction(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    values = [
        {"value": f"entry-{index}-" + "abcdefghijklmnopqrstuvwxyz" * 10}
        for index in range(3)
    ]
    encoded_size = max(len(PersistentTTLCache._encode(value)) for value in values)
    max_bytes = encoded_size * 2 + 10
    cache = PersistentTTLCache()
    cache.configure(
        tmp_path / "tmdb.sqlite3",
        ttl_hours=1,
        max_entries=10,
        max_mb=max_bytes / (1024 * 1024),
    )
    for index, value in enumerate(values):
        clock[0] += 1
        cache[str(index)] = value

    assert "0" not in cache
    assert set(cache) == {"1", "2"}
    assert cache.stats()["stored_bytes"] <= max_bytes


def test_tmdb_cache_persists_approximate_lru_touches(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, ttl_hours=1, max_entries=2)
    cache["one"] = {"value": 1}
    clock[0] += 1
    cache["two"] = {"value": 2}
    cache.flush()

    clock[0] += 901
    assert cache["one"] == {"value": 1}
    cache.flush()
    clock[0] += 1
    cache["three"] = {"value": 3}

    assert set(cache) == {"one", "three"}


def test_tmdb_cache_dry_run_reads_without_creating_or_writing(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    writable = PersistentTTLCache()
    writable.configure(path)
    writable["persisted"] = {"id": 1}
    writable.flush()
    writable.reset_memory()
    before = path.read_bytes()

    dry_run = PersistentTTLCache()
    dry_run.configure(path, enabled=True, writable=False)
    assert dry_run["persisted"] == {"id": 1}
    dry_run["transient"] = {"id": 2}
    assert dry_run["transient"] == {"id": 2}
    assert dry_run.flush() is False
    dry_run.reset_memory()
    assert path.read_bytes() == before

    missing = tmp_path / "missing.sqlite3"
    no_database = PersistentTTLCache()
    no_database.configure(missing, writable=False)
    no_database["memory-only"] = {"id": 3}
    assert no_database["memory-only"] == {"id": 3}
    assert not missing.exists()


def test_disabled_tmdb_cache_does_not_create_or_store_values(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, enabled=False)
    cache["movie/1"] = {"id": 1}

    assert cache == {}
    assert cache.flush() is False
    assert not path.exists()


def test_corrupt_tmdb_database_is_rebuilt_without_blocking_jobs(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    path.write_text("{broken", encoding="utf-8")
    cache = PersistentTTLCache()
    cache.configure(path)

    assert cache.stats()["recoveries"] == 1
    assert cache == {}
    cache["movie/1"] = {"id": 1}
    assert cache.flush() is True
    assert cache["movie/1"] == {"id": 1}
    assert path.read_bytes().startswith(b"SQLite format 3\x00")


def test_corrupt_read_only_tmdb_database_is_left_untouched(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    path.write_text("{broken", encoding="utf-8")
    cache = PersistentTTLCache()
    cache.configure(path, writable=False)
    cache["memory-only"] = {"id": 1}

    assert cache["memory-only"] == {"id": 1}
    assert cache.flush() is False
    assert path.read_text(encoding="utf-8") == "{broken"


def test_corrupt_cached_response_becomes_a_miss_instead_of_failing_the_job(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["broken"] = {"id": 1}
    cache.flush()
    cache._connection.execute(
        "UPDATE tmdb_cache SET response = ? WHERE cache_key = ?",
        (b"not-zlib", "broken"),
    )
    cache._connection.commit()

    assert cache.get("broken") is None
    assert "broken" not in cache
    assert cache.flush() is True


def test_legacy_json_cache_is_left_for_manual_removal(tmp_path):
    legacy = tmp_path / "tmdb_response_cache.json"
    legacy_backup = tmp_path / "tmdb_response_cache.json.bak"
    legacy.write_text("{}", encoding="utf-8")
    legacy_backup.write_text("{}", encoding="utf-8")
    path = tmp_path / "tmdb_cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)

    assert path.exists()
    assert legacy.exists()
    assert legacy_backup.exists()


def test_single_cache_update_does_not_create_a_full_file_copy(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, max_entries=1000)
    response = {
        "credits": [{"id": index, "name": f"Person {index}"} for index in range(500)]
    }
    for index in range(200):
        cache[f"movie/{index}"] = response
    cache.flush()
    page_count_before = cache._connection.execute("PRAGMA page_count").fetchone()[0]

    cache["movie/100"] = {
        "credits": [{"id": index, "name": f"Updated {index}"} for index in range(500)]
    }
    cache.flush()
    page_count_after = cache._connection.execute("PRAGMA page_count").fetchone()[0]

    assert page_count_after <= page_count_before + 2
    assert sorted(item.name for item in tmp_path.iterdir()) == ["tmdb.sqlite3"]


def test_flush_failure_rolls_back_cache_changes(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache["committed"] = {"id": 1}
    cache.flush()
    cache["uncommitted"] = {"id": 2}

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError("simulated write failure")

    cache._connection = FailingConnection(cache._connection)
    assert cache.flush() is False
    cache.reset_memory()

    reloaded = PersistentTTLCache()
    reloaded.configure(path)
    assert reloaded["committed"] == {"id": 1}
    assert "uncommitted" not in reloaded


def test_database_write_error_falls_back_to_job_memory(tmp_path):
    path = tmp_path / "tmdb.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)

    class FailingConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, parameters=()):
            if statement.lstrip().startswith("INSERT INTO tmdb_cache"):
                raise sqlite3.OperationalError("simulated disk failure")
            return self.connection.execute(statement, parameters)

    cache._connection = FailingConnection(cache._connection)
    cache["available"] = {"id": 1}

    assert cache["available"] == {"id": 1}
    assert cache._connection is None
    assert cache.flush() is False
