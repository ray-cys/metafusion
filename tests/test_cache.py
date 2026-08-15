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
