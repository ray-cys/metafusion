import asyncio

import pytest

from helper import tmdb as tmdb_module
from modules import builder
from modules.kometa import (
    KometaSchemaError,
    merge_generated_metadata,
    validate_generated_metadata,
)


def test_generated_schema_rejects_cast_and_accepts_supported_crew():
    with pytest.raises(KometaSchemaError, match=r"cast\.sync"):
        validate_generated_metadata({"cast.sync": ["Actor"]}, "movie")

    assert validate_generated_metadata(
        {"director": ["Director"], "writer.sync": ["Writer"]}, "movie"
    )


def test_movie_merge_preserves_manual_and_nonempty_values_and_removes_deprecated():
    existing = {
        "match": {"mapping_id": 1},
        "summary": "Existing summary",
        "label": ["Manual"],
        "custom_user_field": "keep",
        "cast.sync": ["Old Actor"],
        "runtime": "120",
    }
    generated = {
        "match": {"title": "Example", "year": 2020, "mapping_id": 1},
        "summary": "",
        "genre": ["Drama"],
    }

    merged, diagnostics = merge_generated_metadata(existing, generated, "movie")

    assert merged["summary"] == "Existing summary"
    assert merged["label"] == ["Manual"]
    assert merged["custom_user_field"] == "keep"
    assert merged["genre"] == ["Drama"]
    assert "cast.sync" not in merged
    assert "runtime" not in merged
    assert diagnostics["existing_preserved"] == 1
    assert diagnostics["deprecated_removed"] == 2


def test_show_merge_preserves_failed_inventory_members_but_removes_absent_members():
    existing = {
        "match": {"mapping_id": 1},
        "seasons": {
            1: {
                "title": "Existing Season",
                "originally_available": "2020-01-01",
                "episodes": {
                    1: {"summary": "Keep after TMDb failure"},
                    2: {"summary": "Removed from Plex"},
                },
            },
            2: {"title": "Removed season", "episodes": {}},
        },
    }
    generated = {
        "match": {"title": "Example", "year": 2020, "mapping_id": 1},
        "summary": "Updated",
        "seasons": {},
    }

    merged, diagnostics = merge_generated_metadata(
        existing,
        generated,
        "show",
        authoritative_seasons={1},
        authoritative_episodes={1: [1]},
    )

    assert set(merged["seasons"]) == {1}
    assert merged["seasons"][1]["episodes"] == {
        1: {"summary": "Keep after TMDb failure"}
    }
    assert "originally_available" not in merged["seasons"][1]
    assert diagnostics["inventory_removed"] == 2


def test_episode_group_mapping_requires_one_exact_layout(monkeypatch):
    async def request(_config, endpoint, **_kwargs):
        if endpoint == "tv/1/episode_groups":
            return {"results": [{"id": "dvd", "type": 3}]}
        if endpoint == "tv/episode_group/dvd":
            return {
                "groups": [
                    {
                        "order": 0,
                        "name": "Disc One",
                        "episodes": [
                            {"id": 10, "order": 0, "name": "One"},
                            {"id": 11, "order": 1, "name": "Two"},
                        ],
                    }
                ]
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)
    result = asyncio.run(
        tmdb_module.resolve_episode_group_mapping(
            {"tmdb": {"episode_group_fallback": True}},
            "1",
            {1: [1, 2]},
            episode_ordering="tvdb_dvd",
            session=object(),
        )
    )

    assert result["group_id"] == "dvd"
    assert set(result["episodes"]) == {(1, 1), (1, 2)}


def test_movie_dry_run_selects_unfiltered_artwork_without_writing(monkeypatch, tmp_path):
    details = {
        "title": "Example",
        "original_title": "Example",
        "release_date": "2020-01-01",
        "release_dates": {"results": []},
        "credits": {"crew": [], "cast": []},
        "images": {"posters": [], "backdrops": []},
    }
    calls = []

    async def request(_config, endpoint, **_kwargs):
        assert endpoint == "movie/1"
        return details

    async def unfiltered(*_args, **_kwargs):
        calls.append(True)
        return {
            "posters": [
                {
                    "file_path": "/ko.jpg",
                    "iso_639_1": "ko",
                    "width": 1000,
                    "height": 1500,
                    "vote_average": 5,
                }
            ]
        }

    async def forbidden_cache(*_args, **_kwargs):
        raise AssertionError("dry-run must not write cache")

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "tmdb_unfiltered_images", unfiltered)
    monkeypatch.setattr(builder, "meta_cache_async", forbidden_cache)
    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "tmdb": {
            "language": "en-US",
            "region": "US",
            "fallback": [],
            "artwork_allow_any_language": True,
        },
        "poster_set": {
            "prefer_vote": 5,
            "vote_relaxed": 0,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        },
        "background_set": {},
    }
    result = asyncio.run(
        builder.build_movie(
            config,
            {"metadata": {}},
            feature_flags={
                "metadata_basic": False,
                "metadata_enhanced": False,
                "poster": True,
                "background": False,
                "dry_run": True,
            },
            meta={
                "library_type": "movie",
                "title": "Example",
                "year": 2020,
                "ratingKey": "1",
                "tmdb_id": "1",
                "movie_path": "Example (2020)",
            },
            session=object(),
        )
    )

    assert result["poster_action"] == "skipped"
    assert calls == [True]
