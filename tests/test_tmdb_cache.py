import json

from helper import tmdb_cache as cache_module
from helper.tmdb_cache import PersistentTTLCache


def test_persistent_tmdb_cache_round_trips_and_expires(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    path = tmp_path / "cache" / "tmdb.json"
    cache = PersistentTTLCache()
    cache.configure(path, ttl_hours=1, max_entries=10)
    cache["movie/1"] = {"title": "Example"}

    assert cache.flush() is True
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2

    reloaded = PersistentTTLCache()
    clock[0] += 30
    reloaded.configure(path, ttl_hours=1, max_entries=10)
    assert reloaded["movie/1"] == {"title": "Example"}

    clock[0] += 3601
    expired = PersistentTTLCache()
    expired.configure(path, ttl_hours=1, max_entries=10)
    assert "movie/1" not in expired
    assert expired.flush() is True


def test_tmdb_cache_is_bounded_and_ignores_non_json_values(monkeypatch, tmp_path):
    clock = [100.0]
    monkeypatch.setattr(cache_module.time, "time", lambda: clock[0])
    cache = PersistentTTLCache()
    cache.configure(tmp_path / "tmdb.json", ttl_hours=1, max_entries=2)
    for key in ("one", "two", "three"):
        clock[0] += 1
        cache[key] = {"key": key}
    cache["bytes"] = b"not-json"

    assert set(cache) == {"two", "three"}
    assert "bytes" not in cache


def test_tmdb_cache_dry_run_never_writes_and_corruption_is_safe(tmp_path):
    path = tmp_path / "tmdb.json"
    path.write_text("{broken", encoding="utf-8")
    cache = PersistentTTLCache()
    cache.configure(path, enabled=True, writable=False)
    cache["movie/1"] = {"id": 1}

    assert cache.flush() is False
    assert path.read_text(encoding="utf-8") == "{broken"
    assert cache["movie/1"] == {"id": 1}


def test_disabled_tmdb_cache_does_not_store_values(tmp_path):
    cache = PersistentTTLCache()
    cache.configure(tmp_path / "tmdb.json", enabled=False)
    cache["movie/1"] = {"id": 1}

    assert cache == {}
    assert cache.flush() is False


def test_tmdb_cache_recovers_backup_and_reports_usage(tmp_path):
    path = tmp_path / "tmdb.json"
    cache = PersistentTTLCache()
    cache.configure(path, ttl_hours=1, max_entries=10)
    cache["first"] = {"value": 1}
    cache.flush()
    cache["second"] = {"value": 2}
    cache.flush()
    path.write_text("{broken", encoding="utf-8")

    recovered = PersistentTTLCache()
    recovered.configure(path, ttl_hours=1, max_entries=10)

    assert recovered["first"] == {"value": 1}
    assert recovered.stats()["hits"] == 1
