import asyncio
import shutil
from pathlib import Path

from test_phase26_builder_matrix import (
    _config,
    _flags,
    _movie_meta,
    _patch_identity,
    _show_details,
    _show_meta,
)

from modules import builder


def _candidate(asset_type="poster"):
    return {
        "file_path": f"/{asset_type}.jpg",
        "provider": "tmdb",
        "vote_average": 8,
        "width": 1200,
        "height": 1800 if asset_type != "background" else 675,
        "selection_stage": "strict",
        "candidate_pool": [],
    }


def test_selection_fanart_reserve_relaxed_and_empty_attempts(monkeypatch, tmp_path):
    config = _config(tmp_path)
    config["tmdb"]["artwork_allow_any_language"] = False

    async def fanart(*_args, **_kwargs):
        return [_candidate()]

    async def no_unfiltered(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(builder, "fanart_artwork_candidates", fanart)
    monkeypatch.setattr(builder, "tmdb_unfiltered_images", no_unfiltered)
    attempts = []
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id="1",
            attempts_out=attempts,
        )
    )
    assert selected["provider"] == "tmdb" or selected["provider"] == "fanart"
    assert attempts[-1]["provider"] == "Fanart.tv"

    async def no_fanart(*_args, **_kwargs):
        return []

    monkeypatch.setattr(builder, "fanart_artwork_candidates", no_fanart)
    reserve_attempts = []
    reserve = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [{"file_path": "/tiny.jpg", "width": 10, "height": 15}],
            asset_type="poster",
            media_type="movie",
            tmdb_id="1",
            attempts_out=reserve_attempts,
        )
    )
    assert reserve["selection_reason"].startswith("best available")
    assert reserve_attempts[-1]["status"] == "selected_best_available"

    missing_destination = tmp_path / "missing.jpg"
    monkeypatch.setattr(builder, "get_asset_path", lambda *_args, **_kwargs: missing_destination)

    async def relaxed_images(*_args, **_kwargs):
        return {"posters": [{"file_path": "/relaxed.jpg", "width": 10, "height": 15}]}

    monkeypatch.setattr(builder, "tmdb_unfiltered_images", relaxed_images)
    relaxed_attempts = []
    relaxed = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id="1",
            attempts_out=relaxed_attempts,
        )
    )
    assert relaxed["selection_stage"] == "missing_only_relaxed"
    assert relaxed_attempts[-1]["status"] == "selected_missing_only_relaxed"

    missing_destination.write_bytes(b"existing")
    empty_attempts = []
    assert asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id="1",
            attempts_out=empty_attempts,
            excluded_providers={"tmdb", "fanart", "plex"},
        )
    ) is None
    assert len(empty_attempts) == 3


def test_verification_and_recovery_rejection_paths(monkeypatch, tmp_path):
    class Registry:
        def mark_verified(self, *_args, **_kwargs):
            return "checksum"

    monkeypatch.setattr(builder, "_asset_registry", lambda _config: Registry())
    asset = tmp_path / "poster.jpg"
    asset.write_bytes(b"asset")
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda candidate: (_ for _ in ()).throw(OSError("unreadable"))
        if candidate == asset
        else False,
    )
    config = {}
    builder._mark_asset_verified(
        config,
        "movie:1",
        asset,
        media_type="movie",
        tmdb_id="1",
        asset_type="poster",
        source_path="/poster.jpg",
    )
    assert config["_adoption_audit_records"][0]["status"] == "verification_failed"

    async def request(_config, endpoint, **_kwargs):
        return {"id": endpoint}

    async def replacement(*_args, **_kwargs):
        return "replacement"

    monkeypatch.setattr(builder, "tmdb_api_request", request)
    monkeypatch.setattr(builder, "resolve_tmdb_id", replacement)
    monkeypatch.setattr(builder, "resolve_split_series_mapping", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (False, False, "mismatch"),
    )
    original, details, recovered = asyncio.run(
        builder.tmdb_details_with_recovery({}, "movie", "old")
    )
    assert original == "old" and details and recovered is None


def test_movie_manual_alias_upgrade_plex_and_identity_rejection(monkeypatch, tmp_path):
    _patch_identity(monkeypatch, identity_reason="trusted external ID alias")
    all_fields = {
        "sort_title",
        "original_title",
        "originally_available",
        "content_rating",
        "studio",
        "tagline",
        "summary",
        "country",
        "genre",
        "director",
        "writer",
        "producer",
    }
    config = _config(tmp_path / "plex", mode="plex")
    result = asyncio.run(
        builder._build_movie(
            config,
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=True, poster=False, background=False),
            ignored_fields=all_fields,
            meta=_movie_meta(manual_identity_override=True),
            session=object(),
        )
    )
    assert result["percent"] == 100 and result["plex_candidate"]

    config = _config(tmp_path / "kometa")
    current = {"metadata": {"Example Movie (2020)": {"summary": "old"}}}
    result = asyncio.run(
        builder._build_movie(
            config,
            {"metadata": {}},
            existing_yaml_data=current,
            feature_flags=_flags(metadata_basic=True, poster=False, background=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["metadata_action"] == "upgraded"

    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (False, False, "external mismatch"),
    )
    rejected = asyncio.run(
        builder._build_movie(
            config,
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=True, poster=False, background=False),
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert rejected["metadata_action"] == "failed"


def _patch_artwork_pipeline(monkeypatch, *, should_upgrade):
    async def select(*_args, asset_type, **_kwargs):
        return _candidate(asset_type)

    async def audit(*_args, **_kwargs):
        return None

    async def allowed(*_args, **_kwargs):
        return True, "missing"

    async def download(_config, _meta, best, temp_path, *_args, **_kwargs):
        Path(temp_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_path).write_bytes(b"download")
        return best, True, 200, None

    def copy_only(source, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", select)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", allowed)
    monkeypatch.setattr(builder, "managed_source_matches", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(builder, "_download_with_missing_failover", download)
    monkeypatch.setattr(builder, "atomic_replace_file", copy_only)
    monkeypatch.setattr(builder, "_mark_asset_verified", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        builder,
        "smart_asset_upgrade",
        lambda *_args, **_kwargs: (
            should_upgrade,
            "NO_EXISTING_ASSET" if should_upgrade else "NO_UPGRADE_NEEDED",
            {},
        ),
    )
    monkeypatch.setattr(
        builder,
        "smart_season_asset_upgrade",
        lambda *_args, **_kwargs: (
            should_upgrade,
            "NO_EXISTING_ASSET_SEASON" if should_upgrade else "NO_UPGRADE_NEEDED_SEASON",
            {},
        ),
    )


def test_movie_artwork_temp_cleanup_preservation_and_no_upgrade(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    _patch_artwork_pipeline(monkeypatch, should_upgrade=True)
    assets = set()
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path / "upgrade"),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False, season=False),
            existing_assets=assets,
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == result["background_action"] == "downloaded"

    _patch_artwork_pipeline(monkeypatch, should_upgrade=False)
    root = tmp_path / "skip" / "assets" / "movie" / "Example Movie (2020)"
    root.mkdir(parents=True)
    (root / "poster.jpg").write_bytes(b"old")
    (root / "fanart.jpg").write_bytes(b"old")
    assets = set()
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path / "skip"),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False, season=False),
            existing_assets=assets,
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == result["background_action"] == "skipped"
    assert len(assets) == 2

    async def no_candidate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", no_candidate)
    preserved = set()
    result = asyncio.run(
        builder._build_movie(
            _config(tmp_path / "skip"),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False, poster=False, season=False),
            existing_assets=preserved,
            meta=_movie_meta(),
            session=object(),
        )
    )
    assert result["background_action"] == "preserved" and len(preserved) == 1


def test_tv_identity_mapping_and_metadata_edge_matrix(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    config = _config(tmp_path)

    async def no_identity(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "resolve_tmdb_id", no_identity)
    missing = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True, poster=False, background=False, season=False
            ),
            meta=_show_meta(),
        )
    )
    assert missing["metadata_action"] == "failed"

    _patch_identity(monkeypatch, recovered="old")
    recovered_meta = _show_meta(plex_tmdb_id="legacy")
    recovered = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=False, poster=False, background=False, season=True
            ),
            meta=recovered_meta,
            session=object(),
        )
    )
    assert recovered_meta["identity_source"] == "stale_identity_recovery"
    assert recovered["metadata_action"] == "not_due"

    _patch_identity(monkeypatch)
    manual = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=False, poster=False, background=False, season=True
            ),
            meta=_show_meta(manual_identity_override=True),
            session=object(),
        )
    )
    assert manual["metadata_action"] == "not_due"

    monkeypatch.setattr(
        builder,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (False, False, "mismatch"),
    )
    rejected = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True, poster=False, background=False, season=False
            ),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert rejected["metadata_action"] == "failed"


def test_tv_imdb_external_missing_group_fallback_and_dry_run(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    config = _config(tmp_path / "imdb", mode="plex")
    imdb = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True,
                poster=False,
                background=False,
                season=False,
                dry_run=True,
            ),
            meta=_show_meta(tvdb_id=None, imdb_id="tt300"),
            session=object(),
        )
    )
    assert imdb["plex_candidate"]

    details = _show_details()
    details["external_ids"] = {"imdb_id": "tt-from-tmdb"}

    async def recovered_details(*_args, **_kwargs):
        return "200", details, None

    monkeypatch.setattr(builder, "tmdb_details_with_recovery", recovered_details)
    external = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "external"),
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True, poster=False, background=False, season=False
            ),
            meta=_show_meta(tvdb_id=None, imdb_id=None),
            session=object(),
        )
    )
    assert external["metadata_action"] in {"downloaded", "failed"}

    details["external_ids"] = {}

    async def empty_external_ids(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(builder, "tmdb_api_request", empty_external_ids)
    missing = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "missing"),
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True, poster=False, background=False, season=False
            ),
            meta=_show_meta(tvdb_id=None, imdb_id=None),
            session=object(),
        )
    )
    assert missing["metadata_action"] == "failed"

    details["external_ids"] = {"tvdb_id": 300}
    details["seasons"] = [{"season_number": 0}, {"season_number": 1}]

    async def empty_season(_config, endpoint, **_kwargs):
        if "/season/" in endpoint:
            return {"name": "Season", "overview": "", "episodes": [], "images": {"posters": []}}
        return {}

    async def group(*_args, **_kwargs):
        return {
            "group_id": "group",
            "episodes": {
                (1, 1): {
                    "episode_number": 1,
                    "name": "Mapped",
                    "overview": "",
                    "crew": [],
                }
            },
            "seasons": {1: {"title": "Season 1", "summary": "Mapped"}},
        }

    monkeypatch.setattr(builder, "tmdb_api_request", empty_season)
    monkeypatch.setattr(builder, "resolve_episode_group_mapping", group)
    monkeypatch.setattr(builder, "recursive_season_diff", lambda *_args, **_kwargs: [])
    grouped = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "group", mode="plex"),
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True,
                poster=False,
                background=False,
                season=False,
                dry_run=True,
            ),
            meta=_show_meta(seasons_episodes={1: [1]}, plex_seasons=[1]),
            session=object(),
        )
    )
    assert grouped["metadata_action"] == "skipped"
    assert grouped["seasons"][1]["episodes"][1]["title"] == "Mapped"

    skipped = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "group-kometa"),
            {"metadata": {}},
            feature_flags=_flags(
                metadata_basic=True,
                poster=False,
                background=False,
                season=False,
                dry_run=True,
            ),
            meta=_show_meta(seasons_episodes={1: [1]}, plex_seasons=[1]),
            session=object(),
        )
    )
    assert skipped["metadata_action"] == "skipped"


def test_tv_asset_existing_upgrade_skip_exclusion_and_preservation(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)
    _patch_artwork_pipeline(monkeypatch, should_upgrade=True)
    config = _config(tmp_path / "upgrade")
    config["_excluded_seasons"] = {1}
    result = asyncio.run(
        builder._build_tv(
            config,
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False),
            existing_assets=set(),
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["season_poster_actions"][1] == "not_due"

    _patch_artwork_pipeline(monkeypatch, should_upgrade=False)
    root = tmp_path / "skip" / "assets" / "tv" / "Example Show (2020)"
    root.mkdir(parents=True)
    for name in ("poster.jpg", "fanart.jpg", "Season00.jpg", "Season01.jpg"):
        (root / name).write_bytes(b"old")
    assets = set()
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "skip"),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False),
            existing_assets=assets,
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == result["background_action"] == "skipped"
    assert set(result["season_poster_actions"].values()) == {"skipped"}
    assert len(assets) == 4

    async def no_candidate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", no_candidate)
    preserved = set()
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path / "skip"),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False, poster=False, season=False),
            existing_assets=preserved,
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["background_action"] == "preserved" and len(preserved) == 1


def test_tv_protected_existing_assets_are_adopted_into_current_inventory(monkeypatch, tmp_path):
    _patch_identity(monkeypatch)

    async def select(*_args, asset_type, **_kwargs):
        return _candidate(asset_type)

    async def audit(*_args, **_kwargs):
        return None

    async def protected(*_args, **_kwargs):
        return False, "manual"

    async def preserve(*_args, **_kwargs):
        return False

    monkeypatch.setattr(builder, "_select_artwork_with_fallback", select)
    monkeypatch.setattr(builder, "_audit_asset_candidate", audit)
    monkeypatch.setattr(builder, "protected_asset_destination_async", protected)
    monkeypatch.setattr(builder, "adopt_exact_tmdb_asset", preserve)
    root = tmp_path / "assets" / "tv" / "Example Show (2020)"
    root.mkdir(parents=True)
    for name in ("poster.jpg", "fanart.jpg", "Season00.jpg", "Season01.jpg"):
        (root / name).write_bytes(b"manual")
    assets = set()
    result = asyncio.run(
        builder._build_tv(
            _config(tmp_path),
            {"metadata": {}},
            feature_flags=_flags(metadata_basic=False),
            existing_assets=assets,
            meta=_show_meta(),
            session=object(),
        )
    )
    assert result["poster_action"] == result["background_action"] == "skipped"
    assert len(assets) == 4
