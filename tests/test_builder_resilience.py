import asyncio

import pytest

from helper.tmdb import tmdb_response_cache
from modules import builder


def build_config(tmp_path):
    return {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "tmdb": {"language": "en-US", "region": "US", "fallback": ["fr"]},
        "assets": {
            "run_poster": True,
            "run_season": True,
            "run_background": True,
        },
        "poster_set": {
            "prefer_vote": 7,
            "vote_relaxed": 3,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        },
        "season_set": {
            "prefer_vote": 5,
            "vote_relaxed": 2,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        },
        "background_set": {
            "prefer_vote": 7,
            "vote_relaxed": 3,
            "max_width": 1280,
            "max_height": 720,
            "min_width": 640,
            "min_height": 360,
        },
    }


def feature_flags(**overrides):
    values = {
        "metadata_basic": True,
        "metadata_enhanced": True,
        "poster": True,
        "season": True,
        "background": True,
        "dry_run": False,
    }
    values.update(overrides)
    return values


def movie_meta():
    return {
        "library_type": "movie",
        "title": "Example Movie",
        "year": 2020,
        "ratingKey": "m1",
        "tmdb_id": "100",
        "imdb_id": "tt100",
        "movie_path": "Example Movie (2020)",
        "movie_dir": "/media/Example Movie (2020)",
        "edition_title": None,
    }


def movie_details():
    return {
        "release_dates": {
            "results": [
                {
                    "iso_3166_1": "US",
                    "release_dates": [{"certification": "PG-13"}],
                }
            ]
        },
        "genres": [{"name": "Drama"}],
        "production_companies": [{"name": "Studio"}],
        "production_countries": [{"iso_3166_1": "US"}],
        "belongs_to_collection": {"id": 5, "name": "Example Collection"},
        "credits": {
            "crew": [
                {"job": "Director", "name": "Director"},
                {"job": "Writer", "name": "Writer"},
                {"job": "Producer", "name": "Producer"},
            ],
            "cast": [{"name": "Actor"}],
        },
        "images": {
            "posters": [
                {
                    "iso_639_1": "en",
                    "vote_average": 8,
                    "width": 1200,
                    "height": 1800,
                    "file_path": "/poster.jpg",
                }
            ],
            "backdrops": [
                {
                    "vote_average": 8,
                    "width": 1920,
                    "height": 1080,
                    "file_path": "/background.jpg",
                }
            ],
        },
        "original_title": "Original Movie",
        "release_date": "2020-01-02",
        "runtime": 121,
        "tagline": "Tagline",
        "overview": "Summary",
    }


def tv_meta():
    return {
        "library_type": "tv",
        "title": "Example Show",
        "year": 2021,
        "ratingKey": "s1",
        "tmdb_id": "200",
        "tvdb_id": "300",
        "imdb_id": "tt200",
        "show_path": "Example Show (2021)",
        "show_dir": "/media/Example Show (2021)",
        "seasons_episodes": {0: [1], 1: [1]},
    }


def tv_details():
    return {
        "content_ratings": {
            "results": [{"iso_3166_1": "US", "rating": "TV-14"}]
        },
        "genres": [{"name": "Drama"}],
        "networks": [{"name": "Network"}],
        "origin_country": ["US"],
        "credits": {"crew": [{"job": "Director", "name": "Show Director"}]},
        "images": {
            "posters": [
                {
                    "iso_639_1": "en",
                    "vote_average": 8,
                    "width": 1200,
                    "height": 1800,
                    "file_path": "/show-poster.jpg",
                }
            ],
            "backdrops": [
                {
                    "vote_average": 8,
                    "width": 1920,
                    "height": 1080,
                    "file_path": "/show-background.jpg",
                }
            ],
        },
        "seasons": [{"season_number": 0}, {"season_number": 1}],
        "original_name": "Original Show",
        "first_air_date": "2021-01-02",
        "tagline": "Show tagline",
        "overview": "Show summary",
    }


def season_details(number):
    return {
        "air_date": f"202{number}-01-01",
        "credits": {"crew": [{"job": "Writer", "name": "Season Writer"}]},
        "images": {
            "posters": [
                {
                    "iso_639_1": "en",
                    "vote_average": 7,
                    "width": 1200,
                    "height": 1800,
                    "file_path": f"/season-{number}.jpg",
                }
            ]
        },
        "episodes": [
            {
                "episode_number": 1,
                "name": f"Episode {number}",
                "air_date": f"202{number}-01-02",
                "overview": "Episode summary",
                "crew": [{"job": "Director", "name": "Episode Director"}],
            },
            {"episode_number": 99, "name": "Not in Plex", "crew": []},
        ],
    }


@pytest.fixture(autouse=True)
def isolated_tmdb_cache(tmp_path):
    tmdb_response_cache.configure(tmp_path / "tmdb-cache.json")
    tmdb_response_cache.clear()
    yield
    tmdb_response_cache.clear()


def install_successful_asset_mocks(monkeypatch):
    cache_calls = []

    async def cache_write(*args, **kwargs):
        cache_calls.append((args, kwargs))

    async def download(_config, image_path, save_path, **_kwargs):
        save_path.write_bytes(image_path.encode())
        return True, 200, None

    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(
        builder,
        "smart_asset_upgrade",
        lambda *_args, **_kwargs: (True, "NO_EXISTING_ASSET", {}),
    )
    monkeypatch.setattr(
        builder,
        "smart_season_asset_upgrade",
        lambda *_args, **_kwargs: (True, "NO_EXISTING_ASSET_SEASON", {}),
    )
    return cache_calls


def test_movie_builder_writes_enhanced_metadata_and_both_assets(monkeypatch, tmp_path):
    cache_calls = install_successful_asset_mocks(monkeypatch)
    tmdb_response_cache["movie/100"] = movie_details()
    consolidated = {"metadata": {}}
    existing_assets = set()

    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(),
            existing_assets=existing_assets,
            meta=movie_meta(),
            session=object(),
        )
    )

    entry = consolidated["metadata"]["Example Movie (2020)"]
    assert entry["content_rating"] == "PG-13"
    assert entry["cast.sync"] == ["Actor"]
    assert entry["director.sync"] == ["Director"]
    assert entry["writer.sync"] == ["Writer"]
    assert entry["producer.sync"] == ["Producer"]
    assert result["metadata_action"] == "downloaded"
    assert result["poster_action"] == "downloaded"
    assert result["background_action"] == "downloaded"
    assert len(existing_assets) == 2
    assert all(tmp_path in path.parents for path in map(type(tmp_path), existing_assets))
    assert cache_calls


def test_movie_builder_unchanged_metadata_preserves_cache_identity(
    monkeypatch, tmp_path
):
    calls = []

    async def cache_write(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    tmdb_response_cache["movie/100"] = movie_details()
    config = build_config(tmp_path)
    flags = feature_flags(poster=False, season=False, background=False)
    first = {"metadata": {}}
    asyncio.run(builder.build_movie(config, first, feature_flags=flags, meta=movie_meta()))
    calls.clear()

    result = asyncio.run(
        builder.build_movie(
            config,
            {"metadata": {}},
            feature_flags=flags,
            existing_yaml_data=first,
            meta=movie_meta(),
        )
    )

    assert result["metadata_action"] == "skipped"
    assert calls[-1]["update_timestamp"] is False


def test_movie_builder_dry_run_and_missing_identifiers_do_not_write_cache(
    monkeypatch, tmp_path
):
    calls = []

    async def cache_write(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    tmdb_response_cache["movie/100"] = movie_details()
    flags = feature_flags(dry_run=True)
    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=flags,
            meta=movie_meta(),
        )
    )
    missing = movie_meta()
    missing.update({"tmdb_id": None, "imdb_id": None})

    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"
    assert calls == []
    assert (
        asyncio.run(
            builder.build_movie(
                build_config(tmp_path),
                {"metadata": {}},
                feature_flags=feature_flags(),
                meta=missing,
            )
        )
        is None
    )


def test_movie_builder_handles_invalid_tmdb_and_asset_download_failure(
    monkeypatch, tmp_path
):
    async def no_details(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "tmdb_api_request", no_details)
    assert (
        asyncio.run(
            builder.build_movie(
                build_config(tmp_path),
                {"metadata": {}},
                feature_flags=feature_flags(),
                meta=movie_meta(),
            )
        )
        is None
    )

    tmdb_response_cache["movie/100"] = movie_details()

    async def no_cache(*_args, **_kwargs):
        return None

    async def failed_download(*_args, **_kwargs):
        return False, 503, "unavailable"

    monkeypatch.setattr(builder, "meta_cache_async", no_cache)
    monkeypatch.setattr(builder, "download_poster", failed_download)
    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "failed"
    assert result["background_action"] == "failed"


def test_tv_builder_writes_specials_episodes_and_all_assets(monkeypatch, tmp_path):
    install_successful_asset_mocks(monkeypatch)
    tmdb_response_cache["tv/200"] = tv_details()
    tmdb_response_cache["tv/200/season/0"] = season_details(0)
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    consolidated = {"metadata": {}}
    existing_assets = set()

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(),
            existing_assets=existing_assets,
            meta=tv_meta(),
            session=object(),
        )
    )

    entry = consolidated["metadata"]["Example Show (2021)"]
    assert set(entry["seasons"]) == {0, 1}
    assert entry["seasons"][0]["episodes"][1]["title"] == "Episode 0"
    assert 99 not in entry["seasons"][0]["episodes"]
    assert result["poster_action"] == "downloaded"
    assert result["background_action"] == "downloaded"
    assert result["season_poster_actions"] == {0: "downloaded", 1: "downloaded"}
    assert len(existing_assets) == 4


def test_tv_effective_metadata_only_flags_disable_every_asset(monkeypatch, tmp_path):
    calls = []

    async def no_cache(*_args, **_kwargs):
        return None

    async def unexpected_download(*_args, **_kwargs):
        calls.append(True)
        return False, 500, "must not run"

    monkeypatch.setattr(builder, "meta_cache_async", no_cache)
    monkeypatch.setattr(builder, "download_poster", unexpected_download)
    tmdb_response_cache["tv/200"] = tv_details()
    tmdb_response_cache["tv/200/season/0"] = season_details(0)
    tmdb_response_cache["tv/200/season/1"] = season_details(1)

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False,
                season=False,
                background=False,
            ),
            meta=tv_meta(),
        )
    )

    assert calls == []
    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"
    assert result["season_poster_actions"] == {}


def test_tv_builder_uses_external_ids_and_skips_missing_season_data(
    monkeypatch, tmp_path
):
    requests = []
    details = tv_details()

    async def request(_config, endpoint, **_kwargs):
        requests.append(endpoint)
        if endpoint.endswith("external_ids"):
            return {"tvdb_id": 456}
        if endpoint == "tv/200":
            return details
        return None

    async def no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "meta_cache_async", no_cache)
    meta = tv_meta()
    meta.update({"tvdb_id": None, "imdb_id": None})
    result_doc = {"metadata": {}}

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            result_doc,
            feature_flags=feature_flags(poster=False, season=False, background=False),
            meta=meta,
        )
    )

    assert result_doc["metadata"]["Example Show (2021)"]["match"]["mapping_id"] == 456
    assert result["seasons"] == {}
    assert "tv/200/external_ids" in requests
    assert "tv/200/season/0" in requests


def test_tv_builder_requires_a_mapping_identifier(monkeypatch, tmp_path):
    async def no_ids(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(builder, "tmdb_api_request", no_ids)
    meta = tv_meta()
    meta.update({"tvdb_id": None, "imdb_id": None})

    assert (
        asyncio.run(
            builder.build_tv(
                build_config(tmp_path),
                {"metadata": {}},
                feature_flags=feature_flags(),
                meta=meta,
            )
        )
        is None
    )
