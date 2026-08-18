import asyncio
from pathlib import Path

import yaml

from helper.tmdb import tmdb_response_cache
from modules import builder as builder_module
from modules.kometa import build_episode_metadata, validate_metadata_document
from modules.utils import smart_meta_update

GOLDEN_DIR = Path(__file__).parent / "golden"


def test_episode_metadata_matches_kometa_schema_golden_file():
    episode = build_episode_metadata(
        {
            "name": "Pilot Special",
            "air_date": "2020-01-01",
            "overview": "A schema-compatible special.",
            "runtime": 45,
        },
        directors=["Director One"],
        writers=["Writer One"],
        enhanced=True,
    )
    rendered = yaml.safe_dump(episode, allow_unicode=True, sort_keys=False)

    assert rendered == (GOLDEN_DIR / "kometa_episode.yml").read_text(encoding="utf-8")
    assert set(episode) == {
        "title",
        "sort_title",
        "originally_available",
        "summary",
        "director",
        "writer",
    }


def test_representative_generated_metadata_matches_pinned_kometa_contract():
    document = {
        "metadata": {
            "MetaFusion Contract Movie (2020)": {
                "match": {
                    "title": "MetaFusion Contract Movie",
                    "year": 2020,
                    "mapping_id": 123,
                },
                "sort_title": "MetaFusion Contract Movie",
                "original_title": "Original Contract Movie",
                "originally_available": "2020-01-02",
                "content_rating": "PG-13",
                "studio": "Contract Studio",
                "tagline": "Contract tagline",
                "summary": "Contract movie summary.",
                "country": ["United States of America"],
                "genre": ["Science Fiction"],
                "director": ["Example Director"],
                "writer": ["Example Writer"],
                "producer": ["Example Producer"],
            },
            "MetaFusion Contract Show (2021)": {
                "match": {
                    "title": "MetaFusion Contract Show",
                    "year": 2021,
                    "mapping_id": 456,
                },
                "sort_title": "MetaFusion Contract Show",
                "original_title": "Original Contract Show",
                "originally_available": "2021-02-03",
                "content_rating": "TV-14",
                "studio": "Contract Network",
                "tagline": "Contract show tagline",
                "summary": "Contract show summary.",
                "genre": ["Drama"],
                "seasons": {
                    0: {
                        "title": "Specials",
                        "summary": "Contract specials.",
                        "episodes": {
                            1: build_episode_metadata(
                                {
                                    "name": "Contract Special",
                                    "air_date": "2021-02-03",
                                    "overview": "Contract episode summary.",
                                },
                                directors=["Example Director"],
                                writers=["Example Writer"],
                                enhanced=True,
                            )
                        },
                    }
                },
            },
        }
    }

    assert validate_metadata_document(document) is True
    assert yaml.safe_dump(document, allow_unicode=True, sort_keys=False) == (
        GOLDEN_DIR / "kometa_contract.yml"
    ).read_text(encoding="utf-8")


def test_smart_meta_update_applies_exclusions_recursively():
    existing = {
        "cache_key": "old",
        "nested": {"last_updated": "yesterday", "title": "Same"},
    }
    new = {
        "cache_key": "new",
        "nested": {"last_updated": "today", "title": "Same"},
    }

    assert smart_meta_update(existing, new) == []
    assert smart_meta_update(existing, new, exclude_fields=set()) == ["cache_key", "nested"]


def test_specials_are_emitted_as_season_zero(monkeypatch):
    async def no_cache_write(*args, **kwargs):
        return None

    monkeypatch.setattr(builder_module, "meta_cache_async", no_cache_write)
    tmdb_response_cache.clear()
    tmdb_response_cache["tv/123"] = {
        "content_ratings": {"results": []},
        "genres": [],
        "networks": [],
        "origin_country": [],
        "credits": {"crew": []},
        "images": {"posters": [], "backdrops": []},
        "seasons": [{"season_number": 0}],
        "first_air_date": "2020-01-01",
        "overview": "A show with specials.",
    }
    tmdb_response_cache["tv/123/season/0"] = {
        "air_date": "2019-12-01",
        "credits": {"crew": []},
        "images": {"posters": []},
        "episodes": [
            {
                "episode_number": 1,
                "name": "Pilot Special",
                "air_date": "2019-12-01",
                "overview": "A special episode.",
                "runtime": 50,
                "crew": [
                    {"job": "Director", "name": "Director One"},
                    {"job": "Writer", "name": "Writer One"},
                ],
            }
        ],
    }

    async def cached_request(_config, endpoint, **_kwargs):
        return tmdb_response_cache.get(endpoint)

    monkeypatch.setattr(builder_module, "tmdb_api_request", cached_request)
    config = {
        "settings": {"mode": "kometa"},
        "tmdb": {"language": "en", "region": "US"},
        "assets": {"run_background": False, "run_season": False},
    }
    flags = {
        "metadata_basic": True,
        "metadata_enhanced": True,
        "poster": False,
        "background": False,
        "season": False,
        "dry_run": False,
    }
    consolidated = {"metadata": {}}

    result = asyncio.run(
        builder_module.build_tv(
            config,
            consolidated,
            feature_flags=flags,
            meta={
                "library_type": "tv",
                "title": "Example",
                "year": 2020,
                "show_path": "Example (2020)",
                "tmdb_id": "123",
                "tvdb_id": "456",
                "imdb_id": None,
                "seasons_episodes": {0: [1]},
            },
        )
    )

    assert 0 in result["seasons"]
    episode = result["seasons"][0]["episodes"][1]
    assert episode["title"] == "Pilot Special"
    assert "runtime" not in episode
    assert "original_title" not in episode
    assert "cast.sync" not in episode
    assert "guest" not in episode
    tmdb_response_cache.clear()
