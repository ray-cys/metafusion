import json
import asyncio

from helper import cache as cache_module


def configure_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cache_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache_module, "CACHE_FILE", tmp_path / "meta_cache.json")


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
