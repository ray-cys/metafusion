import asyncio
from pathlib import Path

import pytest

from helper.runtime import DiskPressureError
from modules import builder


def _config(tmp_path, *, mode="kometa"):
    return {
        "settings": {"mode": mode, "path": str(tmp_path)},
        "runtime": {"max_concurrency": 3},
        "tmdb": {"language": "en-US", "region": "US", "fallback": []},
        "kometa": {"tag_policy": "append"},
        "assets": {"update_policy": "managed"},
        "poster_set": {
            "prefer_vote": 5,
            "vote_relaxed": 1,
            "vote_threshold": 5,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        },
        "background_set": {
            "prefer_vote": 5,
            "vote_relaxed": 1,
            "vote_threshold": 5,
            "max_width": 1920,
            "max_height": 1080,
            "min_width": 640,
            "min_height": 360,
        },
        "season_set": {
            "prefer_vote": 5,
            "vote_relaxed": 1,
            "vote_threshold": 5,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        },
    }


def _movie_meta(**updates):
    value = {
        "library_type": "movie",
        "title": "Example Movie",
        "year": 2020,
        "ratingKey": "10",
        "tmdb_id": "100",
        "imdb_id": "tt100",
        "movie_path": "Example Movie (2020)",
    }
    value.update(updates)
    return value


def _show_meta(**updates):
    value = {
        "library_type": "tv",
        "title": "Example Show",
        "year": 2020,
        "ratingKey": "20",
        "tmdb_id": "200",
        "tvdb_id": "300",
        "show_path": "Example Show (2020)",
        "plex_seasons": [0, 1],
        "seasons_episodes": {0: [1], 1: [1]},
    }
    value.update(updates)
    return value


def _movie_details():
    return {
        "id": 100,
        "title": "Example Movie",
        "original_title": "Example Movie",
        "release_date": "2020-01-01",
        "release_dates": {"results": []},
        "credits": {"crew": []},
        "external_ids": {"imdb_id": "tt100"},
        "genres": [],
        "production_companies": [],
        "production_countries": [],
        "images": {"posters": [], "backdrops": []},
    }


def _show_details():
    return {
        "id": 200,
        "name": "Example Show",
        "original_name": "Example Show",
        "first_air_date": "2020-01-01",
        "content_ratings": {"results": []},
        "credits": {"crew": []},
        "external_ids": {"tvdb_id": 300},
        "genres": [],
        "networks": [],
        "production_companies": [],
        "origin_country": [],
        "seasons": [{"season_number": 0}, {"season_number": 1}],
        "images": {"posters": [], "backdrops": []},
    }


def _patch_identity(monkeypatch, *, recovered=None, identity_reason="matched"):
    async def resolve(_config, media_type, **_kwargs):
        return "100" if media_type == "movie" else "200"

    async def details(_config, media_type, tmdb_id, **_kwargs):
        document = _movie_details() if media_type == "movie" else _show_details()
        return tmdb_id, document, recovered

    async def saved(*_args, **_kwargs):
        return None

    async def cache(*_args, **_kwargs):
        return None

    async def season(_config, endpoint, **_kwargs):
        number = int(endpoint.rsplit("/", 1)[-1])
        return {
            "name": "Specials" if number == 0 else f"Season {number}",
            "overview": "",
            "episodes": [{"episode_number": 1, "name": "Episode", "crew": []}],
            "images": {"posters": []},
        }

    monkeypatch.setattr(builder, "resolve_tmdb_id", resolve)
    monkeypatch.setattr(builder, "tmdb_details_with_recovery", details)
    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (True, True, "trusted external ID matched"),
    )
    monkeypatch.setattr(
        builder,
        "tmdb_identity_consistent",
        lambda *_args, **_kwargs: (True, identity_reason),
    )
    monkeypatch.setattr(builder, "_save_high_confidence_identity", saved)
    monkeypatch.setattr(builder, "meta_cache_async", cache)
    monkeypatch.setattr(builder, "tmdb_api_request", season)
    monkeypatch.setattr(
        builder, "resolve_split_series_mapping", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        builder, "resolve_episode_overrides", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(builder, "load_cache", lambda: {})


def _flags(**updates):
    value = {
        "metadata_basic": False,
        "metadata_enhanced": False,
        "poster": True,
        "background": True,
        "season": True,
        "dry_run": False,
    }
    value.update(updates)
    return value


def test_movie_missing_provider_preserves_only_existing_artwork(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def no_candidate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", no_candidate)
    poster = tmp_path / "assets" / "movie" / "Example Movie (2020)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"manual-poster")
    assets = set()
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(season=False),
            existing_assets=assets,
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "preserved"
    assert result["poster"]["size"] == len(b"manual-poster")
    assert result["background_action"] == "missing"
    assert str(poster.resolve()) in assets


def test_tv_missing_provider_tracks_show_and_season_outcomes(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def no_candidate(*_args, attempts_out=None, **_kwargs):
        if attempts_out is not None:
            attempts_out.append({"provider": "tmdb", "status": "missing"})
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", no_candidate)
    root = tmp_path / "assets" / "tv" / "Example Show (2020)"
    root.mkdir(parents=True)
    (root / "poster.jpg").write_bytes(b"show-poster")
    (root / "Season00.jpg").write_bytes(b"specials")
    assets = set()
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(),
            existing_assets=assets,
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "preserved"
    assert result["background_action"] == "missing"
    assert result["season_poster_actions"] == {0: "preserved", 1: "missing"}
    assert result["season_artwork_attempts"][0][0]["status"] == "missing"


def test_split_series_preserve_policy_reconciles_top_level_files(
    monkeypatch, tmp_path
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        builder,
        "resolve_split_series_mapping",
        lambda *_args, **_kwargs: {
            "show_policy": "preserve",
            "seasons": {1: {"tmdb_id": "200", "season_number": 1}},
        },
    )
    config = _config(tmp_path)
    config["_artwork_gaps"] = []
    root = tmp_path / "assets" / "tv" / "Example Show (2020)"
    root.mkdir(parents=True)
    (root / "poster.jpg").write_bytes(b"show-poster")
    (root / "fanart.jpg").write_bytes(b"show-background")

    result = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(season=False),
            existing_assets=set(),
            meta=_show_meta(),
            session=object(),
        )
    )

    assert result["poster_action"] == "policy_preserved"
    assert result["poster"]["size"] == len(b"show-poster")
    assert result["background_action"] == "policy_preserved"
    assert result["background"]["size"] == len(b"show-background")
    assert config["_artwork_gaps"] == []


def test_split_series_preserve_policy_handles_disappearing_top_level_files(
    monkeypatch, tmp_path
):
    _patch_identity(monkeypatch)
    monkeypatch.setattr(
        builder,
        "resolve_split_series_mapping",
        lambda *_args, **_kwargs: {
            "show_policy": "preserve",
            "seasons": {1: {"tmdb_id": "200", "season_number": 1}},
        },
    )

    class DisappearingAsset:
        def is_file(self):
            return True

        def stat(self):
            raise OSError("asset disappeared during reconciliation")

        def resolve(self):
            raise AssertionError("a failed stat must not register the asset")

    monkeypatch.setattr(
        builder,
        "get_asset_path",
        lambda *_args, **_kwargs: DisappearingAsset(),
    )
    config = _config(tmp_path)
    config["_artwork_gaps"] = []

    result = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(season=False),
            existing_assets=set(),
            meta=_show_meta(),
            session=object(),
        )
    )

    assert result["poster_action"] == "policy_missing"
    assert result["background_action"] == "policy_missing"
    assert result["poster"]["size"] == 0
    assert result["background"]["size"] == 0
    assert {gap["asset_type"] for gap in config["_artwork_gaps"]} == {
        "poster",
        "background",
    }


def test_candidate_observation_does_not_replace_installed_provider(monkeypatch, tmp_path):
    calls = []

    async def cache(*args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(builder, "meta_cache_async", cache)
    candidate = {
        "provider": "fanart",
        "file_path": "https://assets.fanart.tv/poster.jpg",
        "vote_average": 7,
    }
    asyncio.run(
        builder._record_asset_observation(
            "movie:1", "1", "Movie", 2020, "movie", "poster", candidate
        )
    )
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"poster")
    asyncio.run(
        builder._record_asset_observation(
            "movie:1",
            "1",
            "Movie",
            2020,
            "movie",
            "poster",
            candidate,
            asset_path=destination,
            checksum="checksum",
        )
    )

    assert calls[0]["poster_candidate_provider"] == "fanart"
    assert calls[0]["poster_candidate_average"] == 7
    assert "poster_provider" not in calls[0]
    assert "poster_average" not in calls[0]
    assert "poster_source_path" not in calls[0]
    assert calls[1]["poster_provider"] == "fanart"
    assert calls[1]["poster_average"] == 7
    assert calls[1]["poster_source_path"].endswith("poster.jpg")


def test_tv_dry_run_selects_every_artwork_type_without_writes(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "width": 1000,
            "height": 1500,
            "selection_stage": "strict",
            "candidate_pool": [],
        }

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(dry_run=True),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"
    assert result["season_poster_actions"] == {0: "skipped", 1: "skipped"}


@pytest.mark.parametrize("missing_kind", ["show_path", "destination"])
def test_tv_artwork_rejects_unresolved_destinations(
    monkeypatch, tmp_path, missing_kind
):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    meta = _show_meta(show_path=None) if missing_kind == "show_path" else _show_meta()
    if missing_kind == "destination":
        monkeypatch.setattr(builder, "get_asset_path", lambda *_args, **_kwargs: None)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=meta,
            session=object(),
        )
    )
    assert result["poster_action"] == "failed"
    assert result["background_action"] == "failed"
    assert set(result["season_poster_actions"].values()) == {"failed"}


@pytest.mark.parametrize(
    ("adoption", "expected"),
    [(True, "adopted"), (False, "skipped"), (None, "deferred")],
)
def test_tv_protected_artwork_adoption_outcomes(
    monkeypatch, tmp_path, adoption, expected
):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return False, "shared"

    async def adopt(*_args, **_kwargs):
        return adoption

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "adopt_exact_tmdb_asset", adopt)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == expected
    assert result["background_action"] == expected
    assert set(result["season_poster_actions"].values()) == {expected}


def test_tv_managed_source_and_disk_pressure_paths(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "managed"

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: True)
    root = tmp_path / "assets" / "tv" / "Example Show (2020)"
    root.mkdir(parents=True)
    for name in ("poster.jpg", "fanart.jpg", "Season00.jpg", "Season01.jpg"):
        (root / name).write_bytes(name.encode())
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"
    assert set(result["season_poster_actions"].values()) == {"skipped"}

    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_asset_temp_path_or_defer", lambda *_args: None)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "deferred"
    assert result["background_action"] == "deferred"
    assert set(result["season_poster_actions"].values()) == {"deferred"}


@pytest.mark.parametrize(
    ("movie_status", "season_status", "expected"),
    [
        ("NO_EXISTING_ASSET", "NO_EXISTING_ASSET_SEASON", "downloaded"),
        ("FORCE_UPGRADE_STALE", "FORCE_UPGRADE_STALE_SEASON", "upgraded"),
        ("UPGRADE_DIMENSIONS", "UPGRADE_DIMENSIONS_SEASON", "upgraded"),
    ],
)
def test_tv_download_and_upgrade_status_matrix(
    monkeypatch, tmp_path, movie_status, season_status, expected
):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "width": 1000,
            "height": 1500,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "missing"

    async def download(
        _config,
        _meta,
        best,
        temp_path,
        _asset_path,
        _images,
        **_kwargs,
    ):
        Path(temp_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).write_bytes(b"downloaded-image")
        return best, True, 200, None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_download_with_missing_failover", download)
    monkeypatch.setattr(
        builder,
        "smart_asset_upgrade",
        lambda *_args, **_kwargs: (
            True,
            movie_status,
            {"last_upgraded": "old"},
        ),
    )
    monkeypatch.setattr(
        builder,
        "smart_season_asset_upgrade",
        lambda *_args, **_kwargs: (
            True,
            season_status,
            {"last_upgraded": "old"},
        ),
    )
    monkeypatch.setattr(builder, "_mark_asset_verified", lambda *_args, **_kwargs: None)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path / movie_status),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == expected
    assert result["background_action"] == expected
    assert set(result["season_poster_actions"].values()) == {expected}


def test_tv_download_failure_and_upgrade_rejection(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "fanart",
            "vote_average": 8,
            "selection_stage": "fallback",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "missing"

    async def failed(
        _config,
        _meta,
        best,
        _temp_path,
        _asset_path,
        _images,
        **_kwargs,
    ):
        return best, False, 503, "provider unavailable"

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_download_with_missing_failover", failed)
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "failed"),
            {"metadata": {}},
            feature_flags=_flags(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "failed"
    assert result["background_action"] == "failed"
    assert set(result["season_poster_actions"].values()) == {"failed"}


def test_movie_managed_source_and_disk_pressure_paths(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "managed"

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: True)
    root = tmp_path / "assets" / "movie" / "Example Movie (2020)"
    root.mkdir(parents=True)
    (root / "poster.jpg").write_bytes(b"poster")
    (root / "fanart.jpg").write_bytes(b"background")
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(season=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "skipped"
    assert result["background_action"] == "skipped"

    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_asset_temp_path_or_defer", lambda *_args: None)
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(season=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "deferred"
    assert result["background_action"] == "deferred"


@pytest.mark.parametrize(
    ("status", "should_upgrade", "expected"),
    [
        ("NO_EXISTING_ASSET", True, "downloaded"),
        ("FORCE_UPGRADE_STALE", True, "upgraded"),
        ("UPGRADE_DIMENSIONS", True, "upgraded"),
        ("NO_UPGRADE_NEEDED", False, "skipped"),
    ],
)
def test_movie_download_upgrade_status_matrix(
    monkeypatch, tmp_path, status, should_upgrade, expected
):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "tmdb",
            "vote_average": 8,
            "width": 1000,
            "height": 1500,
            "selection_stage": "strict",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "missing"

    async def download(
        _config,
        _meta,
        best,
        temp_path,
        _asset_path,
        _images,
        **_kwargs,
    ):
        Path(temp_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).write_bytes(b"downloaded-image")
        return best, True, 200, None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_download_with_missing_failover", download)
    monkeypatch.setattr(
        builder,
        "smart_asset_upgrade",
        lambda *_args, **_kwargs: (
            should_upgrade,
            status,
            {"last_upgraded": "old", "error": "none"},
        ),
    )
    monkeypatch.setattr(builder, "_mark_asset_verified", lambda *_args, **_kwargs: None)
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path / status),
            {"metadata": {}},
            feature_flags=_flags(season=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == expected
    assert result["background_action"] == expected


def test_movie_download_failure_records_both_asset_failures(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def candidate(*_args, asset_type, **_kwargs):
        return {
            "file_path": f"/{asset_type}.jpg",
            "provider": "fanart",
            "vote_average": 8,
            "selection_stage": "fallback",
        }

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return True, "missing"

    async def failed(
        _config,
        _meta,
        best,
        _temp_path,
        _asset_path,
        _images,
        **_kwargs,
    ):
        return best, False, 503, "provider unavailable"

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", candidate)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_download_with_missing_failover", failed)
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(season=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == "failed"
    assert result["background_action"] == "failed"


def test_builder_provider_language_identity_and_reporting_helpers(monkeypatch, tmp_path):
    assert builder._candidate_provider(None) == "tmdb"
    assert builder._provider_label({"provider": "other"}) == "Best available"
    assert builder._with_provider(None) is None
    assert builder._with_provider({"file_path": "/a"}, "custom")["provider_label"] == "Custom"
    config = _config(tmp_path)
    config["tmdb"].update(fallback="ja", artwork_allow_any_language=False)
    assert builder._artwork_language_allowed(config, {"iso_639_1": None})
    assert builder._artwork_language_allowed(config, {"iso_639_1": "ja"})
    assert not builder._artwork_language_allowed(config, {"iso_639_1": "fr"})

    config["plex"] = {"url": "http://plex"}
    meta = {
        "plex_artwork": {
            "poster": "/library/poster",
            "seasons": {"1": "/library/season"},
        }
    }
    assert builder._plex_artwork_candidate(config, meta, "poster")["file_path"] == "http://plex/library/poster"
    assert builder._plex_artwork_candidate(config, meta, "season", 1)["provider"] == "plex"
    meta["plex_artwork"]["poster"] = "https://foreign/poster"
    assert builder._plex_artwork_candidate(config, meta, "poster") is None

    assert builder._identity_binding_source({"manual_identity_override": True}, "1") == "manual_override"
    assert builder._identity_binding_source({}, "1", recovered=True) == "stale_tmdb_recovery"
    assert builder._identity_binding_source({"plex_provider_tmdb_id": "1"}, "1") == "plex_tmdb_guid"
    assert builder._identity_binding_source({}, "1", split_mapping=True) == "split_series_mapping"
    assert builder._identity_binding_source({}, "1", consensus_reason="IMDb matched") == "imdb_external_id"
    assert builder._identity_binding_source({}, "1", consensus_reason="TVDB matched") == "tvdb_external_id"
    assert builder._identity_source_hint({}, None, "tt1", "2") == "external_id_resolution"
    assert builder._identity_source_hint({}, None, "tt1") == "imdb_external_id"
    assert builder._identity_source_hint({}, None, tvdb_id="2") == "tvdb_external_id"
    assert builder._identity_source_hint({}, None) == "title_year_search"
    assert builder._identity_source_hint({"identity_source": "manual"}, "1") == "manual"

    gaps_config = {"_artwork_gaps": [], "_library_name": "Movies"}
    builder._record_artwork_gap(
        gaps_config, "missing", "TV Show", "Example", "season 0 poster",
        identity={"tmdb_id": "1", "year": 2020}
    )
    assert gaps_config["_artwork_gaps"][0]["season_number"] == 0
    builder._record_artwork_gap({}, "missing", "Movie", "Example")
    builder._remember_item_report_identity(gaps_config, "Movie", "Example", {"tmdb_id": "1"})
    assert gaps_config["_item_report_identities"][("Movie", "Example")]["tmdb_id"] == "1"
    assert builder._episode_pair_labels({(1, 2), (0, 1)}, limit=1).startswith("S00E01")


def test_identity_save_disk_pressure_certification_and_cache_helpers(monkeypatch, tmp_path):
    assert asyncio.run(builder._save_high_confidence_identity({}, "1", trusted=True, dry_run=False)) is False
    assert asyncio.run(
        builder._save_high_confidence_identity(
            {"server_id": "server", "ratingKey": "1", "manual_identity_override": True},
            "1", trusted=True, dry_run=False
        )
    ) is False
    resolved = []
    saved = []
    monkeypatch.setattr(builder, "resolve_identity_reviews", lambda *_a: resolved.append(True))
    monkeypatch.setattr(builder, "plex_identity_fingerprint", lambda _meta: "fingerprint")
    monkeypatch.setattr(builder, "save_identity_binding", lambda *_a, **_k: saved.append(True) or True)
    meta = {
        "server_id": "server",
        "library_uuid": "uuid",
        "ratingKey": "1",
        "library_type": "show",
        "title": "Example",
        "year": 2020,
    }
    assert asyncio.run(
        builder._save_high_confidence_identity(meta, "10", trusted=True, dry_run=False)
    ) is True
    assert resolved and saved
    monkeypatch.setattr(builder, "plex_identity_fingerprint", lambda _meta: None)
    assert asyncio.run(
        builder._save_high_confidence_identity(meta, "10", trusted=True, dry_run=False)
    ) is False

    pressure = DiskPressureError(tmp_path, 1, 2, "pressure")
    monkeypatch.setattr(builder, "asset_temp_path", lambda *_a: (_ for _ in ()).throw(pressure))
    config = {}
    assert builder._asset_temp_path_or_defer(config, {}) is None
    assert config["_deferred_artwork"] == 1
    assert builder._asset_temp_path_or_defer(config, {}) is None

    movie_ratings = [
        {"iso_3166_1": "US", "release_dates": [{"type": 4, "certification": "PG"}, {"type": 3, "certification": "R"}]}
    ]
    assert builder.regional_movie_certification(movie_ratings, "GB") == "R"
    assert builder.regional_movie_certification([], "US") == ""
    assert builder.regional_tv_certification([{"iso_3166_1": "US", "rating": "TV-14"}], "GB") == "TV-14"
    assert builder.regional_tv_certification([], "US") == ""

    asset = tmp_path / "poster.jpg"
    assert not builder.cached_source_matches("key", "/source", asset, "poster")
    asset.write_bytes(b"x")
    monkeypatch.setattr(builder, "load_cache", lambda: {"key": []})
    assert not builder.cached_source_matches("key", "/source", asset, "poster")
    monkeypatch.setattr(
        builder,
        "load_cache",
        lambda: {
            "key": {
                "poster_source_path": "/source",
                "seasons": {"1": {"season_source_path": "/season"}},
            }
        },
    )
    assert builder.cached_source_matches("key", "/source", asset, "poster")
    assert builder.cached_source_matches("key", "/season", asset, "season", 1)
    assert builder.managed_source_matches("managed", "key", "/source", asset, "poster")


def test_download_failover_and_tmdb_details_recovery_matrix(monkeypatch, tmp_path):
    destination = tmp_path / "poster.jpg"
    attempts = []

    async def download(_config, source, *_args, **_kwargs):
        attempts.append(source)
        return (len(attempts) == 2, 200 if len(attempts) == 2 else 503, "failed")

    async def select(*_args, **_kwargs):
        return {"file_path": "/fanart", "provider": "fanart"}

    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(builder, "_select_artwork_with_fallback", select)
    selected, success, status, _error = asyncio.run(
        builder._download_with_missing_failover(
            {}, {}, {"file_path": "/tmdb", "provider": "tmdb"}, tmp_path / "tmp",
            destination, [], asset_type="poster", media_type="movie", tmdb_id="1"
        )
    )
    assert success and status == 200
    assert selected["selection_stage"] == "missing_only_download_failover"

    destination.write_bytes(b"manual")
    attempts.clear()
    _selected, success, status, detail = asyncio.run(
        builder._download_with_missing_failover(
            {}, {}, {"file_path": "/tmdb", "provider": "tmdb"}, tmp_path / "tmp",
            destination, [], asset_type="poster", media_type="movie", tmdb_id="1"
        )
    )
    assert not success and status == 503 and "TMDb" in detail

    responses = {"movie/old": None, "movie/new": {"id": "new"}}

    async def request(_config, endpoint, **_kwargs):
        return responses.get(endpoint)

    async def resolve(*_args, **_kwargs):
        return "new"

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "resolve_tmdb_id", resolve)
    monkeypatch.setattr(builder, "resolve_split_series_mapping", lambda *_a, **_k: None)
    monkeypatch.setattr(builder, "tmdb_external_id_consensus", lambda *_a, **_k: (True, True, "matched"))
    assert asyncio.run(
        builder.tmdb_details_with_recovery({}, "movie", "old", title="Example")
    ) == ("new", {"id": "new"}, "old")
    assert asyncio.run(
        builder.tmdb_details_with_recovery({}, "movie", "old", authoritative=True)
    ) == ("old", None, None)
