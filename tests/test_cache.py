import json
import asyncio

from helper import cache as cache_module


def configure_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_module, "CACHE_FILE", tmp_path / "meta_cache.json")
    cache_module.begin_cache_session()


def test_cache_cleanup_is_persisted_atomically(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)
    original = {
        "tv:Example:2020": {
            "media_type": "tv",
            "season_average": 4.2,
            "season_number": 1,
            "seasons": {"1": {"season_average": 4.2}},
        }
    }

    cache_module.save_cache(original)
    persisted = json.loads(cache_module.CACHE_FILE.read_text(encoding="utf-8"))

    assert "season_average" not in persisted["tv:Example:2020"]
    assert "season_number" not in persisted["tv:Example:2020"]
    assert persisted["tv:Example:2020"]["seasons"]["1"]["season_average"] == 4.2
    assert original["tv:Example:2020"]["season_average"] == 4.2
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_cache_falls_back_to_empty(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)
    cache_module.CACHE_FILE.write_text("{not-json", encoding="utf-8")

    assert cache_module.load_cache() == {}


def test_loading_missing_cache_does_not_create_directory(monkeypatch, tmp_path):
    cache_dir = tmp_path / "missing" / "cache"
    monkeypatch.setattr(cache_module, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(cache_module, "CACHE_FILE", cache_dir / "meta_cache.json")

    assert cache_module.load_cache() == {}
    assert not cache_dir.exists()


def test_cache_updates_preserve_identity_and_use_exact_season_number(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)

    async def update_with_boolean():
        await cache_module.meta_cache_async(
            "tv:Example:2020", 123, "Example", 2020, "tv",
            season_number=2, season_average=7.0,
        )
        await cache_module.meta_cache_async(
            "tv:Example:2020", None, None, None, None,
            update_timestamp=False, season_number=2, season_upgraded=True,
        )

    asyncio.run(update_with_boolean())
    entry = cache_module.load_cache()["tv:Example:2020"]

    assert "season_last_upgraded" not in entry["seasons"]["2"]

    async def update_with_number():
        await cache_module.meta_cache_async(
            "tv:Example:2020", None, None, None, None,
            update_timestamp=False, season_number=2, season_upgraded=2,
        )

    asyncio.run(update_with_number())
    entry = cache_module.load_cache()["tv:Example:2020"]

    assert entry["tmdb_id"] == 123
    assert entry["title"] == "Example"
    assert entry["year"] == 2020
    assert entry["media_type"] == "tv"
    assert "season_last_upgraded" in entry["seasons"]["2"]


def test_cache_key_migration_preserves_existing_entry(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)
    cache_module.save_cache({
        "movie:Example:2020": {
            "tmdb_id": "123",
            "title": "Example",
            "year": 2020,
            "media_type": "movie",
            "poster_average": 7.5,
        }
    })

    asyncio.run(
        cache_module.meta_cache_async(
            "movie:plex:10",
            "123",
            "Example",
            2020,
            "movie",
            legacy_cache_key="movie:Example:2020",
            update_timestamp=False,
        )
    )
    cache = cache_module.load_cache()

    assert "movie:Example:2020" not in cache
    assert cache["movie:plex:10"]["poster_average"] == 7.5


def test_cache_records_independent_artwork_check_timestamps(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)

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


def test_cache_updates_are_batched_until_flush(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)
    write_calls = []
    real_write = cache_module._write_cache

    def counted_write(cache):
        write_calls.append(len(cache))
        real_write(cache)

    monkeypatch.setattr(cache_module, "_write_cache", counted_write)

    async def update_many():
        await asyncio.gather(*(
            cache_module.meta_cache_async(
                f"movie:Example {index}:2020",
                index,
                f"Example {index}",
                2020,
                "movie",
            )
            for index in range(25)
        ))

    asyncio.run(update_many())

    assert not cache_module.CACHE_FILE.exists()
    assert write_calls == []
    assert cache_module.flush_cache() is True
    assert write_calls == [25]
    assert len(json.loads(cache_module.CACHE_FILE.read_text(encoding="utf-8"))) == 25
    assert cache_module.flush_cache() is False


def test_cache_file_is_loaded_once_per_session(monkeypatch, tmp_path):
    configure_cache(monkeypatch, tmp_path)
    cache_module.CACHE_FILE.write_text('{"movie:Example:2020": {}}', encoding="utf-8")
    load_calls = []
    real_json_load = cache_module.json.load

    def counted_load(handle):
        load_calls.append(handle.name)
        return real_json_load(handle)

    monkeypatch.setattr(cache_module.json, "load", counted_load)

    first = cache_module.load_cache()
    second = cache_module.load_cache()

    assert first is second
    assert len(load_calls) == 1
