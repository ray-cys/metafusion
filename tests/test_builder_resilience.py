import asyncio
import copy

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


def test_builder_rejects_destination_claimed_by_another_item(tmp_path):
    destination = tmp_path / "poster.jpg"
    config = {
        "assets": {"update_policy": "managed"},
        "_asset_destination_registry": {str(destination.absolute()): "movie:1"},
    }

    with pytest.raises(builder.AssetDestinationCollisionError):
        builder.protected_asset_destination(
            config,
            "movie:2",
            destination,
            "poster",
            media_type="Movie",
            full_title="Example (2020)",
        )


def test_builder_shares_only_an_identical_canonical_tmdb_asset(tmp_path):
    destination = tmp_path / "poster.jpg"
    config = {"assets": {"update_policy": "managed"}}
    assert builder.protected_asset_destination(
        config,
        "movie:plex:1",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example (2020) [Theatrical]",
        tmdb_id="100",
        source_path="/poster.jpg",
    )[0]
    assert builder.protected_asset_destination(
        config,
        "movie:plex:2",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example (2020) [Director's Cut]",
        tmdb_id="100",
        source_path="/poster.jpg",
    )[0]

    with pytest.raises(builder.AssetDestinationCollisionError):
        builder.protected_asset_destination(
            config,
            "movie:plex:3",
            destination,
            "poster",
            media_type="Movie",
            full_title="Different Mapping (2020)",
            tmdb_id="999",
            source_path="/different.jpg",
        )


def test_secondary_shared_claim_never_rewrites_under_overwrite_policy(tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"managed")
    checksum = builder.sha256_file(destination)
    config = {
        "assets": {"update_policy": "overwrite"},
        "_asset_destination_registry": builder.AssetDestinationRegistry(
            [
                {
                    "cache_key": "movie:plex:1",
                    "media_type": "movie",
                    "tmdb_id": "100",
                    "asset_type": "poster",
                    "source_path": "/poster.jpg",
                    "destination": str(destination),
                    "checksum": checksum,
                }
            ]
        ),
    }

    assert builder.protected_asset_destination(
        config,
        "movie:plex:2",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example (2020) [Director's Cut]",
        tmdb_id="100",
        source_path="/poster.jpg",
        shared_managed=True,
    ) == (False, "shared")


def test_exact_tmdb_asset_adoption_preserves_existing_file(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    config["assets"]["update_policy"] = "managed"
    destination = tmp_path / "existing" / "poster.jpg"
    destination.parent.mkdir()
    destination.write_bytes(b"same-selected-tmdb-image")
    destination.chmod(0o664)
    original = destination.stat()
    cache_calls = []

    async def download(_config, _source, save_path, **_kwargs):
        save_path.write_bytes(destination.read_bytes())
        return True, 200, None

    async def cache_write(*args, **kwargs):
        cache_calls.append((args, kwargs))

    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)

    adopted = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            config,
            movie_meta(),
            "movie:plex:m1",
            destination,
            {"file_path": "/poster.jpg", "vote_average": 8},
            object(),
            protection_status="no_ownership_record",
            media_type="movie",
            log_media_type="Movie",
            full_title="Example Movie (2020)",
            tmdb_id="100",
            title="Example Movie",
            year=2020,
            asset_type="poster",
        )
    )

    current = destination.stat()
    assert adopted is True
    assert destination.read_bytes() == b"same-selected-tmdb-image"
    assert current.st_ino == original.st_ino
    assert current.st_mtime_ns == original.st_mtime_ns
    assert current.st_mode == original.st_mode
    assert cache_calls[-1][1]["poster_path"] == str(destination.resolve())
    assert cache_calls[-1][1]["poster_checksum"] == builder.sha256_file(destination)
    assert cache_calls[-1][1]["poster_source_path"] == "/poster.jpg"
    assert not list((tmp_path / "assets" / "movie").glob("temp_*.jpg"))


def test_different_tmdb_asset_is_observed_but_not_adopted(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    config["assets"]["update_policy"] = "managed"
    destination = tmp_path / "existing" / "poster.jpg"
    destination.parent.mkdir()
    destination.write_bytes(b"manual-artwork")
    original = destination.stat()
    cache_calls = []

    async def download(_config, _source, save_path, **_kwargs):
        save_path.write_bytes(b"different-tmdb-artwork")
        return True, 200, None

    async def cache_write(*args, **kwargs):
        cache_calls.append((args, kwargs))

    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)

    adopted = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            config,
            movie_meta(),
            "movie:plex:m1",
            destination,
            {"file_path": "/poster.jpg", "vote_average": 8},
            object(),
            protection_status="missing_checksum",
            media_type="movie",
            log_media_type="Movie",
            full_title="Example Movie (2020)",
            tmdb_id="100",
            title="Example Movie",
            year=2020,
            asset_type="poster",
        )
    )

    current = destination.stat()
    assert adopted is False
    assert destination.read_bytes() == b"manual-artwork"
    assert current.st_ino == original.st_ino
    assert current.st_mtime_ns == original.st_mtime_ns
    assert cache_calls[-1][1]["poster_checked"] is True
    assert "poster_path" not in cache_calls[-1][1]
    assert "poster_checksum" not in cache_calls[-1][1]


def test_asset_adoption_reports_disk_pressure_as_deferred(monkeypatch, tmp_path):
    config = build_config(tmp_path)
    config["assets"]["update_policy"] = "managed"
    destination = tmp_path / "existing" / "poster.jpg"
    destination.parent.mkdir()
    destination.write_bytes(b"existing")
    cache_calls = []

    async def cache_write(*args, **kwargs):
        cache_calls.append((args, kwargs))

    monkeypatch.setattr(builder, "_asset_temp_path_or_defer", lambda *_args: None)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)

    adopted = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            config,
            movie_meta(),
            "movie:plex:m1",
            destination,
            {"file_path": "/poster.jpg", "vote_average": 8},
            object(),
            protection_status="no_ownership_record",
            media_type="movie",
            log_media_type="Movie",
            full_title="Example Movie (2020)",
            tmdb_id="100",
            title="Example Movie",
            year=2020,
            asset_type="poster",
        )
    )

    assert adopted is None
    assert destination.read_bytes() == b"existing"
    assert cache_calls == []


def test_cached_source_skip_requires_verified_managed_status(
    monkeypatch, tmp_path
):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"artwork")
    monkeypatch.setattr(
        builder,
        "load_cache",
        lambda: {"movie": {"poster_source_path": "/poster.jpg"}},
    )

    assert builder.managed_source_matches(
        "managed", "movie", "/poster.jpg", destination, "poster"
    )
    assert not builder.managed_source_matches(
        "overwrite", "movie", "/poster.jpg", destination, "poster"
    )
    assert not builder.managed_source_matches(
        "no_ownership_record", "movie", "/poster.jpg", destination, "poster"
    )


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


def test_movie_editions_share_managed_artwork_without_duplicate_writes(
    monkeypatch, tmp_path
):
    cache = {}
    downloads = []

    async def cache_write(
        cache_key, tmdb_id, title, year, media_type, **kwargs
    ):
        entry = cache.setdefault(cache_key, {})
        entry.update(
            {
                "tmdb_id": tmdb_id,
                "title": title,
                "year": year,
                "media_type": media_type,
            }
        )
        entry.update(
            {
                key: value
                for key, value in kwargs.items()
                if key
                not in {
                    "update_timestamp",
                    "poster_checked",
                    "background_checked",
                    "poster_upgraded",
                    "background_upgraded",
                }
            }
        )

    async def download(_config, image_path, save_path, **_kwargs):
        downloads.append(image_path)
        save_path.write_bytes(image_path.encode())
        return True, 200, None

    def write_allowed(_config, cache_key, asset_path, asset_type, **_kwargs):
        if not asset_path.exists():
            return True, "missing"
        entry = cache.get(cache_key, {})
        expected_path = entry.get(f"{asset_type}_path")
        expected_checksum = entry.get(f"{asset_type}_checksum")
        if (
            expected_path
            and expected_checksum
            and builder._normalized_destination(expected_path)
            == builder._normalized_destination(asset_path)
            and expected_checksum == builder.sha256_file(asset_path)
        ):
            return True, "managed"
        return False, "unmanaged"

    monkeypatch.setattr(builder, "load_cache", lambda: cache)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(builder, "asset_write_allowed", write_allowed)
    monkeypatch.setattr(
        builder,
        "smart_asset_upgrade",
        lambda *_args, **_kwargs: (True, "NO_EXISTING_ASSET", {}),
    )
    tmdb_response_cache["movie/100"] = movie_details()

    theatrical = movie_meta()
    theatrical.update(
        {
            "ratingKey": "1",
            "edition_title": "Theatrical",
            "requires_unique_key": True,
        }
    )
    directors_cut = movie_meta()
    directors_cut.update(
        {
            "ratingKey": "2",
            "edition_title": "Director's Cut",
            "requires_unique_key": True,
        }
    )
    config = build_config(tmp_path)

    async def build_editions():
        return await asyncio.gather(
            builder.build_movie(
                config,
                {"metadata": {}},
                feature_flags=feature_flags(season=False),
                meta=theatrical,
                session=object(),
            ),
            builder.build_movie(
                config,
                {"metadata": {}},
                feature_flags=feature_flags(season=False),
                meta=directors_cut,
                session=object(),
            ),
        )

    first, second = asyncio.run(build_editions())

    assert first["poster_action"] == "downloaded"
    assert first["background_action"] == "downloaded"
    assert second["poster_action"] == "skipped"
    assert second["background_action"] == "skipped"
    assert downloads.count("/poster.jpg") == 1
    assert downloads.count("/background.jpg") == 1
    for cache_key in ("movie:plex:1", "movie:plex:2"):
        assert cache[cache_key]["poster_path"].endswith("/poster.jpg")
        assert cache[cache_key]["background_path"].endswith("/fanart.jpg")
        assert len(cache[cache_key]["poster_checksum"]) == 64
        assert len(cache[cache_key]["background_checksum"]) == 64


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
def isolated_tmdb_cache(tmp_path, monkeypatch):
    tmdb_response_cache.configure(tmp_path / "tmdb-cache.json")
    tmdb_response_cache.clear()

    async def cached_request(_config, endpoint, **_kwargs):
        return tmdb_response_cache.get(endpoint)

    monkeypatch.setattr(builder, "tmdb_api_request", cached_request)
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
    assert "cast" not in entry
    assert "cast.sync" not in entry
    assert "runtime" not in entry
    assert entry["director"] == ["Director"]
    assert entry["writer"] == ["Writer"]
    assert entry["producer"] == ["Producer"]
    assert result["metadata_action"] == "downloaded"
    assert result["poster_action"] == "downloaded"
    assert result["background_action"] == "downloaded"
    assert len(existing_assets) == 2
    assert all(tmp_path in path.parents for path in map(type(tmp_path), existing_assets))
    assert cache_calls
    poster_cache = next(
        kwargs for _, kwargs in cache_calls if kwargs.get("poster_path")
    )
    background_cache = next(
        kwargs for _, kwargs in cache_calls if kwargs.get("background_path")
    )
    assert len(poster_cache["poster_checksum"]) == 64
    assert len(background_cache["background_checksum"]) == 64


def test_movie_builder_accepts_tmdb_year_from_terminal_plex_title(
    monkeypatch, tmp_path
):
    cache_calls = install_successful_asset_mocks(monkeypatch)
    details = movie_details()
    details.update(
        {
            "title": "Monster",
            "original_title": "Monster",
            "release_date": "2022-05-01",
        }
    )
    tmdb_response_cache["movie/100"] = details
    meta = movie_meta()
    meta.update(
        {
            "title": "Monster (2022)",
            "year": 2024,
            "movie_path": "Monster (2022)",
        }
    )
    consolidated = {"metadata": {}}

    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(
                poster=False, background=False, season=False
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "downloaded"
    assert next(iter(consolidated["metadata"].values()))["match"]["mapping_id"] == 100
    assert cache_calls


def test_builders_apply_per_type_upgrade_intervals_and_record_checks(
    monkeypatch, tmp_path
):
    cache_calls = install_successful_asset_mocks(monkeypatch)
    asset_decisions = []
    season_decisions = []

    def asset_decision(*_args, **kwargs):
        asset_decisions.append(kwargs)
        return True, "NO_EXISTING_ASSET", {}

    def season_decision(*_args, **kwargs):
        season_decisions.append(kwargs)
        return True, "NO_EXISTING_ASSET_SEASON", {}

    monkeypatch.setattr(builder, "smart_asset_upgrade", asset_decision)
    monkeypatch.setattr(builder, "smart_season_asset_upgrade", season_decision)
    tmdb_response_cache["movie/100"] = movie_details()
    tmdb_response_cache["tv/200"] = tv_details()
    tmdb_response_cache["tv/200/season/0"] = season_details(0)
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    config = build_config(tmp_path)
    config["image_upgrades"] = {
        "default_days": 30,
        "movie_days": 30,
        "series_days": 15,
        "season_days": 7,
    }

    asyncio.run(
        builder.build_movie(
            config,
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=movie_meta(),
            session=object(),
        )
    )
    movie_decisions = list(asset_decisions)
    asset_decisions.clear()
    asyncio.run(
        builder.build_tv(
            config,
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=tv_meta(),
            session=object(),
        )
    )

    assert {call["stale_days"] for call in movie_decisions} == {30}
    assert {call["stale_days"] for call in asset_decisions} == {15}
    assert {call["stale_days"] for call in season_decisions} == {7}
    assert any(call.get("poster_checked") for _, call in cache_calls)
    assert any(call.get("background_checked") for _, call in cache_calls)
    assert any(call.get("season_checked") for _, call in cache_calls)
    assert any(call.get("poster_upgraded") for _, call in cache_calls)
    assert any(call.get("background_upgraded") for _, call in cache_calls)
    assert sum(bool(call.get("season_upgraded")) for _, call in cache_calls) == 1
    assert any(call.get("season_upgraded") == 0 for _, call in cache_calls)
    assert all(
        len(call[checksum_field]) == 64
        for _, call in cache_calls
        for path_field, checksum_field in (
            ("poster_path", "poster_checksum"),
            ("background_path", "background_checksum"),
            ("season_path", "season_checksum"),
        )
        if call.get(path_field)
    )


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


def test_tv_partial_tmdb_failure_preserves_existing_season_and_manual_fields(
    monkeypatch, tmp_path
):
    install_successful_asset_mocks(monkeypatch)
    tmdb_response_cache["tv/200"] = tv_details()
    tmdb_response_cache["tv/200/season/0"] = season_details(0)

    async def no_episode_group(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "resolve_episode_group_mapping", no_episode_group)
    existing = {
        "metadata": {
            "Example Show (2021)": {
                "match": {"mapping_id": 300},
                "manual_field": "keep",
                "country.sync": ["United States of America"],
                "seasons": {
                    0: {"episodes": {1: {"summary": "Old special"}}},
                    1: {
                        "title": "Keep failed season",
                        "episodes": {1: {"summary": "Keep failed episode"}},
                    },
                    2: {"title": "No longer in Plex", "episodes": {}},
                },
            }
        }
    }
    consolidated = copy.deepcopy(existing)

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            existing_yaml_data=existing,
            feature_flags=feature_flags(
                poster=False, season=False, background=False
            ),
            meta=tv_meta(),
            session=object(),
        )
    )

    entry = consolidated["metadata"]["Example Show (2021)"]
    assert result["metadata_action"] == "failed"
    assert entry["manual_field"] == "keep"
    assert "country.sync" not in entry
    assert set(entry["seasons"]) == {0, 1}
    assert entry["seasons"][1]["title"] == "Keep failed season"
    assert entry["seasons"][1]["episodes"][1]["summary"] == "Keep failed episode"


def test_tv_builder_maps_provider_split_anthology_seasons(tmp_path):
    meta = tv_meta()
    meta.update(
        {
            "title": "The Haunting",
            "year": 2018,
            "tmdb_id": "72844",
            "tvdb_id": "345246",
            "seasons_episodes": {1: [1], 2: [1]},
        }
    )
    details = tv_details()
    details.update(
        {
            "name": "The Haunting of Hill House",
            "original_name": "The Haunting of Hill House",
            "first_air_date": "2018-10-12",
            "seasons": [{"season_number": 1}],
        }
    )
    tmdb_response_cache["tv/72844"] = details
    tmdb_response_cache["tv/72844/season/1"] = season_details(1)
    bly_manor = season_details(1)
    bly_manor["name"] = "The Haunting of Bly Manor"
    bly_manor["episodes"][0]["name"] = "The Great Good Place"
    tmdb_response_cache["tv/109958/season/1"] = bly_manor

    consolidated = {"metadata": {}}
    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(
                poster=False, season=False, background=False
            ),
            meta=meta,
            session=object(),
        )
    )

    seasons = consolidated["metadata"]["The Haunting (2018)"]["seasons"]
    assert result["metadata_action"] == "downloaded"
    assert seasons[1]["episodes"][1]["title"] == "Episode 1"
    assert seasons[2]["title"] == "The Haunting of Bly Manor"
    assert seasons[2]["episodes"][1]["title"] == "The Great Good Place"
    assert "summary" not in consolidated["metadata"]["The Haunting (2018)"]


def test_tv_builder_treats_future_tmdb_episode_as_pending(tmp_path):
    meta = tv_meta()
    meta["seasons_episodes"] = {1: [1, 2]}
    details = tv_details()
    details["seasons"] = [{"season_number": 1}]
    tmdb_response_cache["tv/200"] = details
    pending = season_details(1)
    pending["episodes"] = [pending["episodes"][0]]
    tmdb_response_cache["tv/200/season/1"] = pending

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False, season=False, background=False
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "downloaded"
    assert result["is_complete"] is True
    assert result["metadata_pending_count"] == 1


def test_tv_builder_treats_announced_empty_season_as_pending(tmp_path):
    meta = tv_meta()
    meta["seasons_episodes"] = {2: [1]}
    details = tv_details()
    details["seasons"] = [{"season_number": 2}]
    tmdb_response_cache["tv/200"] = details
    announced = season_details(2)
    announced["episodes"] = []
    tmdb_response_cache["tv/200/season/2"] = announced

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False, season=False, background=False
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "downloaded"
    assert result["is_complete"] is True
    assert result["metadata_pending_count"] == 1


def test_tv_builder_applies_explicit_episode_number_override(tmp_path):
    meta = tv_meta()
    meta["seasons_episodes"] = {1: [1]}
    details = tv_details()
    details["seasons"] = [{"season_number": 1}]
    tmdb_response_cache["tv/200"] = details
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    target = season_details(2)
    target["episodes"] = [
        {
            "episode_number": 3,
            "name": "Mapped Episode",
            "air_date": "2022-03-04",
            "overview": "Mapped summary",
            "crew": [],
        }
    ]
    tmdb_response_cache["tv/200/season/2"] = target
    config = build_config(tmp_path)
    config["tmdb"]["episode_overrides"] = {
        "tvdb:300": {"S01E01": "S02E03"}
    }
    consolidated = {"metadata": {}}

    result = asyncio.run(
        builder.build_tv(
            config,
            consolidated,
            feature_flags=feature_flags(
                poster=False, season=False, background=False
            ),
            meta=meta,
            session=object(),
        )
    )

    generated = consolidated["metadata"]["Example Show (2021)"]
    assert generated["seasons"][1]["episodes"][1]["title"] == "Mapped Episode"
    assert result["metadata_pending_count"] == 0


def test_split_series_preserve_policy_skips_top_level_artwork(tmp_path):
    meta = tv_meta()
    meta.update(
        {
            "title": "The Haunting",
            "year": 2018,
            "tmdb_id": "72844",
            "tvdb_id": "345246",
            "seasons_episodes": {1: [1]},
        }
    )
    details = tv_details()
    details.update(
        {
            "name": "The Haunting of Hill House",
            "first_air_date": "2018-10-12",
            "seasons": [{"season_number": 1}],
        }
    )
    tmdb_response_cache["tv/72844"] = details

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                metadata_basic=False,
                metadata_enhanced=False,
                poster=True,
                season=False,
                background=True,
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"


def test_tmdb_details_recovery_replaces_external_id_conflict(monkeypatch):
    async def request(_config, endpoint, **_kwargs):
        if endpoint == "movie/100":
            return {"external_ids": {"imdb_id": "tt-wrong"}}
        if endpoint == "movie/101":
            return {"external_ids": {"imdb_id": "tt-right"}}
        raise AssertionError(endpoint)

    async def resolve(*_args, **kwargs):
        assert kwargs["excluded_ids"] == {"100"}
        return "101"

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "resolve_tmdb_id", resolve)

    resolved, details, recovered = asyncio.run(
        builder.tmdb_details_with_recovery(
            {}, "movie", "100", imdb_id="tt-right", session=object()
        )
    )

    assert resolved == "101"
    assert recovered == "100"
    assert details["external_ids"]["imdb_id"] == "tt-right"


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
    missing_result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=missing,
        )
    )
    assert missing_result["metadata_action"] == "failed"


def test_movie_builder_handles_invalid_tmdb_and_asset_download_failure(
    monkeypatch, tmp_path
):
    async def no_details(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "tmdb_api_request", no_details)
    invalid_result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=movie_meta(),
        )
    )
    assert invalid_result["metadata_action"] == "failed"

    tmdb_response_cache["movie/100"] = movie_details()

    async def cached_details(_config, endpoint, **_kwargs):
        return tmdb_response_cache.get(endpoint)

    monkeypatch.setattr(builder, "tmdb_api_request", cached_details)

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


@pytest.mark.parametrize("missing_output_path", [True, False])
def test_movie_builder_preserves_metadata_when_asset_destination_is_unavailable(
    monkeypatch, tmp_path, missing_output_path
):
    async def no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "meta_cache_async", no_cache)
    tmdb_response_cache["movie/100"] = movie_details()
    meta = movie_meta()
    if missing_output_path:
        meta["movie_path"] = None
    else:
        monkeypatch.setattr(builder, "get_asset_path", lambda *_args, **_kwargs: None)

    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                metadata_basic=False,
                metadata_enhanced=False,
                season=False,
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "not_due"
    assert result["poster_action"] == "failed"
    assert result["background_action"] == "failed"


def test_movie_builder_recovers_stale_tmdb_id_from_imdb(monkeypatch, tmp_path):
    cache_calls = install_successful_asset_mocks(monkeypatch)
    tmdb_response_cache["movie/101"] = movie_details()

    async def resolve(_config, _media_type, tmdb_id=None, **_kwargs):
        return str(tmdb_id) if tmdb_id is not None else "101"

    monkeypatch.setattr(builder, "resolve_tmdb_id", resolve)
    meta = movie_meta()
    consolidated = {"metadata": {}}

    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(poster=False, background=False),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "downloaded"
    assert meta["tmdb_id"] == "101"
    assert meta["plex_tmdb_id"] == "100"
    assert consolidated["metadata"]["Example Movie (2020)"]["match"][
        "mapping_id"
    ] == 101
    assert any(
        kwargs.get("tmdb_recovery_source_id") == "100"
        for _, kwargs in cache_calls
    )


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


def test_episode_crew_uses_only_episode_credits_and_preserves_missing_values(
    monkeypatch, tmp_path
):
    async def no_cache(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "meta_cache_async", no_cache)
    tmdb_response_cache["tv/200"] = tv_details()
    special = season_details(0)
    special["credits"] = {
        "crew": [
            {"job": "Director", "name": "Season Director"},
            {"job": "Writer", "name": "Season Writer"},
        ]
    }
    special["episodes"][0]["crew"] = []
    tmdb_response_cache["tv/200/season/0"] = special
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    existing = {
        "metadata": {
            "Example Show (2021)": {
                "seasons": {
                    0: {
                        "episodes": {
                            1: {
                                "director": ["Existing Director"],
                                "writer": ["Existing Writer"],
                            }
                        }
                    }
                }
            }
        }
    }
    consolidated = {"metadata": {}}

    asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(
                poster=False, background=False, season=False
            ),
            existing_yaml_data=existing,
            meta=tv_meta(),
            session=object(),
        )
    )

    episode = consolidated["metadata"]["Example Show (2021)"]["seasons"][0][
        "episodes"
    ][1]
    assert episode["director"] == ["Existing Director"]
    assert episode["writer"] == ["Existing Writer"]
    assert "Season Director" not in repr(episode)
    assert "Season Writer" not in repr(episode)


def test_crew_names_excludes_assistants_and_deduplicates_in_order():
    crew = [
        {"job": "Director", "name": "Jane Doe"},
        {"job": "Assistant Director", "name": "Assistant"},
        {"job": "Co-Director", "name": "John Doe"},
        {"job": "Director", "name": "jane doe"},
    ]

    assert builder._crew_names(crew, {"Director", "Co-Director"}) == [
        "Jane Doe",
        "John Doe",
    ]


def test_tv_builder_uses_production_company_when_network_is_missing(
    monkeypatch, tmp_path
):
    install_successful_asset_mocks(monkeypatch)
    details = tv_details()
    details["networks"] = []
    details["production_companies"] = [{"name": "Fallback Studio"}]
    tmdb_response_cache["tv/200"] = details
    tmdb_response_cache["tv/200/season/0"] = season_details(0)
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    consolidated = {"metadata": {}}

    asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(
                poster=False, background=False, season=False
            ),
            meta=tv_meta(),
            session=object(),
        )
    )

    assert consolidated["metadata"]["Example Show (2021)"]["studio"] == (
        "Fallback Studio"
    )


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
    assert result["poster_action"] == "not_due"
    assert result["background_action"] == "not_due"
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

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(),
            meta=meta,
        )
    )
    assert result["metadata_action"] == "failed"


def test_season_only_work_does_not_generate_metadata_or_other_artwork(
    monkeypatch, tmp_path
):
    downloads = []

    async def cache_write(*_args, **_kwargs):
        return None

    async def download(_config, image_path, save_path, **_kwargs):
        downloads.append(image_path)
        save_path.write_bytes(image_path.encode())
        return True, 200, None

    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(
        builder,
        "smart_season_asset_upgrade",
        lambda *_args, **_kwargs: (True, "NO_EXISTING_ASSET_SEASON", {}),
    )
    tmdb_response_cache["tv/200"] = tv_details()
    tmdb_response_cache["tv/200/season/0"] = season_details(0)
    tmdb_response_cache["tv/200/season/1"] = season_details(1)
    consolidated = {"metadata": {}}

    result = asyncio.run(
        builder.build_tv(
            build_config(tmp_path),
            consolidated,
            feature_flags=feature_flags(
                metadata_basic=False,
                metadata_enhanced=False,
                poster=False,
                background=False,
                season=True,
            ),
            meta=tv_meta(),
            session=object(),
        )
    )

    assert consolidated == {"metadata": {}}
    assert result["metadata_action"] == "not_due"
    assert result["poster_action"] == "not_due"
    assert result["background_action"] == "not_due"
    assert set(result["season_poster_actions"]) == {0, 1}
    assert set(downloads) == {"/season-0.jpg", "/season-1.jpg"}


def test_metadata_certifications_follow_configured_region_with_us_fallback():
    movie_ratings = [
        {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
        {"iso_3166_1": "SG", "release_dates": [{"certification": "PG"}]},
    ]
    tv_ratings = [
        {"iso_3166_1": "US", "rating": "TV-14"},
        {"iso_3166_1": "SG", "rating": "PG13"},
    ]

    assert builder.regional_movie_certification(movie_ratings, "SG") == "PG"
    assert builder.regional_movie_certification(movie_ratings, "AU") == "PG-13"
    assert builder.regional_tv_certification(tv_ratings, "SG") == "PG13"
    assert builder.regional_tv_certification(tv_ratings, "AU") == "TV-14"


def test_movie_certification_prefers_theatrical_release_over_premiere():
    release_dates = [
        {
            "iso_3166_1": "US",
            "release_dates": [
                {"type": 1, "certification": "R"},
                {"type": 3, "certification": "PG-13"},
            ],
        }
    ]

    assert builder.regional_movie_certification(release_dates, "US") == "PG-13"


def test_artwork_fallback_languages_do_not_replace_metadata_language():
    config = {
        "tmdb": {
            "language": "en-US",
            "fallback": ["zh-CN", "ja", "en"],
        }
    }

    assert builder.artwork_language_codes(config) == "en,zh,ja,null"
    assert config["tmdb"]["language"] == "en-US"


def test_movie_request_keeps_metadata_language_while_including_artwork_fallbacks(
    monkeypatch, tmp_path
):
    requests = []

    async def request(_config, endpoint, params=None, **_kwargs):
        requests.append((endpoint, params))
        return movie_details()

    async def no_cache_write(*_args, **_kwargs):
        return None

    config = build_config(tmp_path)
    config["tmdb"]["fallback"] = ["zh-CN", "ja"]
    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "meta_cache_async", no_cache_write)

    asyncio.run(
        builder.build_movie(
            config,
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False,
                season=False,
                background=False,
            ),
            meta=movie_meta(),
            session=object(),
        )
    )

    endpoint, params = requests[0]
    assert endpoint == "movie/100"
    assert params["language"] == "en-US"
    assert params["region"] == "US"
    assert params["include_image_language"] == "en,zh,ja,null"


def test_movie_builder_rejects_a_resolved_but_inconsistent_tmdb_identity(
    monkeypatch, tmp_path
):
    tmdb_response_cache["movie/100"] = movie_details()
    gaps = []
    config = build_config(tmp_path)
    config["_artwork_gaps"] = gaps
    monkeypatch.setattr(
        builder,
        "tmdb_identity_consistent",
        lambda *_args, **_kwargs: (False, "forced title and year mismatch"),
    )

    result = asyncio.run(
        builder.build_movie(
            config,
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False,
                season=False,
                background=False,
            ),
            meta=movie_meta(),
            session=object(),
        )
    )

    assert result["is_complete"] is False
    assert result["metadata_action"] == "failed"
    assert gaps == [
        {
            "library": None,
            "category": "identity_rejected",
            "media_type": "Movie",
            "title": "Example Movie (2020)",
            "asset_type": "metadata",
            "detail": "forced title and year mismatch",
        }
    ]


def test_movie_builder_persists_a_recovered_tmdb_identity(monkeypatch, tmp_path):
    async def recovered_details(*_args, **_kwargs):
        return "101", movie_details(), "100"

    async def cache_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "tmdb_details_with_recovery", recovered_details)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (True, False, "not available"),
    )
    monkeypatch.setattr(
        builder,
        "tmdb_identity_consistent",
        lambda *_args, **_kwargs: (True, "matched"),
    )
    meta = movie_meta()

    result = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=feature_flags(
                poster=False,
                season=False,
                background=False,
            ),
            meta=meta,
            session=object(),
        )
    )

    assert result["metadata_action"] == "downloaded"
    assert meta["plex_tmdb_id"] == "100"
    assert meta["tmdb_id"] == "101"


def test_movie_builder_handles_a_zero_tmdb_id_with_and_without_imdb_fallback(
    monkeypatch, tmp_path
):
    async def zero_identity(*_args, **_kwargs):
        return "0"

    async def recovered_details(*_args, **_kwargs):
        return "100", movie_details(), "0"

    async def cache_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "resolve_tmdb_id", zero_identity)
    monkeypatch.setattr(builder, "tmdb_details_with_recovery", recovered_details)
    monkeypatch.setattr(builder, "meta_cache_async", cache_write)
    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (True, False, "not available"),
    )
    monkeypatch.setattr(
        builder,
        "tmdb_identity_consistent",
        lambda *_args, **_kwargs: (True, "matched"),
    )
    flags = feature_flags(poster=False, season=False, background=False)

    without_imdb = movie_meta()
    without_imdb["imdb_id"] = None
    failed = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=flags,
            meta=without_imdb,
            session=object(),
        )
    )
    recovered = asyncio.run(
        builder.build_movie(
            build_config(tmp_path),
            {"metadata": {}},
            feature_flags=flags,
            meta=movie_meta(),
            session=object(),
        )
    )

    assert failed["metadata_action"] == "failed"
    assert recovered["metadata_action"] == "downloaded"


def test_cached_tmdb_source_can_skip_artwork_download(monkeypatch, tmp_path):
    asset = tmp_path / "poster.jpg"
    asset.write_bytes(b"managed")
    monkeypatch.setattr(
        builder,
        "load_cache",
        lambda: {"movie:plex:1": {"poster_source_path": "/poster.jpg"}},
    )

    assert builder.cached_source_matches(
        "movie:plex:1", "/poster.jpg", asset, "poster"
    ) is True
    assert builder.cached_source_matches(
        "movie:plex:1", "/different.jpg", asset, "poster"
    ) is False
