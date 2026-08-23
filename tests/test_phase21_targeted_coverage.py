import asyncio
from types import SimpleNamespace

import pytest

from helper.asset_registry import AssetDestinationRegistry
from modules import builder, processing


def _asset_kwargs(asset_type="poster"):
    return {
        "protection_status": "no_ownership_record",
        "media_type": "movie",
        "log_media_type": "Movie",
        "full_title": "Example (2020)",
        "tmdb_id": "123",
        "title": "Example",
        "year": 2020,
        "asset_type": asset_type,
    }


def test_builder_identity_binding_and_small_helpers(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(builder, "plex_identity_fingerprint", lambda _meta: "fingerprint")
    monkeypatch.setattr(
        builder,
        "save_identity_binding",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "ratingKey": "10",
        "library_type": "show",
        "title": "Example",
        "year": 2020,
    }

    assert asyncio.run(
        builder._save_high_confidence_identity(meta, "123", trusted=True, dry_run=False)
    )
    assert calls[0][0][3:6] == ("tv", "123", "fingerprint")
    monkeypatch.setattr(builder, "plex_identity_fingerprint", lambda _meta: "")
    assert not asyncio.run(
        builder._save_high_confidence_identity(meta, "123", trusted=True, dry_run=False)
    )

    missing = tmp_path / "missing.jpg"
    invalid = tmp_path / "invalid.jpg"
    invalid.write_text("not an image", encoding="utf-8")
    assert builder._existing_image_dimensions(missing) is None
    assert builder._existing_image_dimensions(invalid) is None
    assert builder._episode_pair_labels([(1, number) for number in range(1, 15)]).endswith(
        "+2 more"
    )


@pytest.mark.parametrize(
    "claim,exists,permission,dimensions,expected",
    [
        (("collision", "movie:other"), False, (False, "manual"), None, "collision"),
        (("shared", "movie:other"), False, (False, "manual"), None, "shared"),
        (("new", None), False, (False, "missing"), None, "would_download"),
        (
            ("new", None),
            True,
            (False, "no_ownership_record"),
            (500, 750),
            "would_verify_for_adoption",
        ),
        (("new", None), True, (False, "manual"), (500, 750), "preserve_unmanaged"),
        (
            ("new", None),
            True,
            (True, "managed"),
            (500, 750),
            "would_consider_upgrade",
        ),
    ],
)
def test_asset_audit_classifies_candidate_safely(
    monkeypatch, tmp_path, claim, exists, permission, dimensions, expected
):
    destination = tmp_path / "poster.jpg"
    if exists:
        destination.write_bytes(b"image")

    class Registry:
        def claim(self, *_args, **_kwargs):
            return claim

    config = {"_execution": {"asset_audit": True}, "_asset_audit_records": []}
    monkeypatch.setattr(builder, "_asset_registry", lambda _config: Registry())
    monkeypatch.setattr(builder, "get_asset_path", lambda *_args, **_kwargs: destination)
    monkeypatch.setattr(builder, "load_cache", lambda: {})
    monkeypatch.setattr(builder, "asset_write_allowed", lambda *_args, **_kwargs: permission)
    monkeypatch.setattr(builder, "_existing_image_dimensions", lambda _path: dimensions)

    asyncio.run(
        builder._audit_asset_candidate(
            config,
            {"tmdb_id": "123"},
            "movie:1",
            {"file_path": "/poster.jpg", "width": 1000, "height": 1500},
            media_type="Movie",
            full_title="Example (2020)",
            asset_type="poster",
        )
    )

    assert config["_asset_audit_records"][0]["action"] == expected


def test_asset_audit_records_invalid_destination(monkeypatch):
    config = {"_execution": {"asset_audit": True}, "_asset_audit_records": []}
    monkeypatch.setattr(builder, "get_asset_path", lambda *_args, **_kwargs: None)

    asyncio.run(
        builder._audit_asset_candidate(
            config,
            {},
            "movie:1",
            {},
            media_type="Movie",
            full_title="Example",
            asset_type="poster",
        )
    )

    assert config["_asset_audit_records"][0]["ownership"] == "path_invalid"


def test_media_asset_lock_covers_background_and_season(monkeypatch, tmp_path):
    destinations = []
    monkeypatch.setattr(
        builder,
        "get_asset_path",
        lambda *_args, **kwargs: destinations.append(kwargs) or tmp_path / "asset.jpg",
    )
    config = {"_asset_destination_registry": AssetDestinationRegistry()}

    async def get_locks():
        background = builder._media_asset_lock(
            config, {"seasons_episodes": {1: [1]}}, {"background": True}
        )
        season = builder._media_asset_lock(
            config, {"seasons_episodes": {2: [1]}}, {"season": True}
        )
        return background, season

    background, season = asyncio.run(get_locks())
    assert background is not None
    assert destinations[0]["asset_type"] == "background"
    assert season is not None
    assert destinations[-1] == {"asset_type": "season", "season_number": 2}
    assert builder._media_asset_lock(config, {}, {"season": True}) is None


def test_cached_season_source_and_protected_shared_destination(monkeypatch, tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"image")
    monkeypatch.setattr(
        builder,
        "load_cache",
        lambda: {
            "tv:1": {"seasons": {"2": {"season_source_path": "/season.jpg"}}},
            "bad": [],
        },
    )
    assert builder.cached_source_matches(
        "tv:1", "/season.jpg", destination, "season", season_number=2
    )
    assert not builder.cached_source_matches("bad", "/poster.jpg", destination, "poster")
    assert not builder.cached_source_matches("tv:1", None, destination, "poster")

    config = {"assets": {"update_policy": "managed"}}
    first = builder.protected_asset_destination(
        config,
        "movie:1",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example",
        tmdb_id="1",
        source_path="/poster.jpg",
        permission=(True, "managed"),
    )
    second = builder.protected_asset_destination(
        config,
        "movie:2",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example edition",
        tmdb_id="1",
        source_path="/poster.jpg",
        permission=(False, "manual"),
    )
    third = builder.protected_asset_destination(
        config,
        "movie:2",
        destination,
        "poster",
        media_type="Movie",
        full_title="Example edition",
        tmdb_id="1",
        source_path="/poster.jpg",
        shared_managed=True,
        permission=(False, "manual"),
    )
    assert first == (True, "managed")
    assert second == (False, "shared_unverified")
    assert third == (False, "shared")


def test_asset_observation_records_each_asset_shape(monkeypatch, tmp_path):
    calls = []

    async def capture(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(builder, "meta_cache_async", capture)
    path = tmp_path / "asset.jpg"
    path.write_bytes(b"image")
    for asset_type in ("poster", "background", "season"):
        asyncio.run(
            builder._record_asset_observation(
                "key",
                "1",
                "Title",
                2020,
                "movie",
                asset_type,
                {
                    "file_path": f"/{asset_type}.jpg",
                    "vote_average": 8,
                    "vote_count": 42,
                    "iso_639_1": "en",
                },
                asset_path=path,
                checksum="checksum",
                season_number=2,
            )
        )

    assert calls[0][1]["poster_checksum"] == "checksum"
    assert calls[0][1]["poster_vote_count"] == 42
    assert calls[0][1]["poster_language"] == "en"
    assert calls[1][1]["background_checksum"] == "checksum"
    assert calls[1][1]["background_vote_count"] == 42
    assert calls[2][1]["season_checksum"] == "checksum"
    assert calls[2][1]["season_vote_count"] == 42


def test_exact_asset_adoption_preserves_symlink_and_failed_download(
    monkeypatch, tmp_path
):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"existing")
    symlink = tmp_path / "poster.jpg"
    symlink.symlink_to(outside)
    observations = []

    async def observe(*args, **kwargs):
        observations.append((args, kwargs))

    monkeypatch.setattr(builder, "_record_asset_observation", observe)
    result = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            {"assets": {"update_policy": "managed"}},
            {},
            "movie:1",
            symlink,
            {"file_path": "/poster.jpg"},
            None,
            **_asset_kwargs(),
        )
    )
    assert result is False
    assert observations

    destination = tmp_path / "background.jpg"
    destination.write_bytes(b"existing")
    temp = tmp_path / "download.tmp"

    async def failed_download(*_args, **_kwargs):
        return False, 503, "unavailable"

    monkeypatch.setattr(builder, "_asset_temp_path_or_defer", lambda *_args: temp)
    monkeypatch.setattr(builder, "download_poster", failed_download)
    result = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            {"assets": {"update_policy": "managed"}},
            {},
            "movie:1",
            destination,
            {"file_path": "/background.jpg"},
            None,
            **_asset_kwargs("background"),
        )
    )
    assert result is False
    assert not temp.exists()


def test_exact_asset_adoption_handles_checksum_read_failure(monkeypatch, tmp_path):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"existing")
    temp = tmp_path / "download.tmp"
    observations = []

    async def download(*_args, **_kwargs):
        temp.write_bytes(b"download")
        return True, 200, None

    async def observe(*args, **kwargs):
        observations.append((args, kwargs))

    monkeypatch.setattr(builder, "_asset_temp_path_or_defer", lambda *_args: temp)
    monkeypatch.setattr(builder, "download_poster", download)
    monkeypatch.setattr(builder, "_record_asset_observation", observe)
    monkeypatch.setattr(
        builder,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(OSError("unreadable")),
    )

    result = asyncio.run(
        builder.adopt_exact_tmdb_asset(
            {"assets": {"update_policy": "managed"}},
            {},
            "movie:1",
            destination,
            {"file_path": "/poster.jpg"},
            None,
            **_asset_kwargs(),
        )
    )

    assert result is False
    assert observations
    assert not temp.exists()


def test_processing_item_success_updates_plex_identity_and_cache(monkeypatch):
    item = SimpleNamespace(
        title="Example",
        year=2020,
        ratingKey="10",
        updatedAt="before",
    )
    meta = {
        "title": "Example",
        "year": 2020,
        "ratingKey": "10",
        "tmdb_id": "1",
        "library_type": "movie",
    }
    cache_calls = []

    async def get_meta(*_args, **_kwargs):
        return meta

    async def build(*_args, **_kwargs):
        return {"metadata_action": "skipped", "plex_candidate": {"root": {}}}

    async def apply(*_args, **_kwargs):
        return {"writes": 1, "failures": 0}

    async def operation(call, *_args, **_kwargs):
        call()

    async def cache(*args, **kwargs):
        cache_calls.append((args, kwargs))

    def reload():
        item.updatedAt = SimpleNamespace(isoformat=lambda: "after")

    item.reload = reload
    monkeypatch.setattr(processing, "get_plex_metadata", get_meta)
    monkeypatch.setattr(processing, "build_movie", build)
    monkeypatch.setattr(processing, "apply_plex_metadata", apply)
    monkeypatch.setattr(processing, "plex_operation", operation)
    monkeypatch.setattr(processing, "meta_cache_async", cache)

    stats = asyncio.run(
        processing.process_item(
            item,
            {"metadata": {}},
            {"plex": {}, "runtime": {}, "settings": {}},
            feature_flags={"plex_metadata": True, "dry_run": False},
        )
    )

    assert stats["plex_metadata_writes"] == 1
    assert stats["metadata_action"] == "upgraded"
    assert stats["_incremental_success"] is True
    assert meta["updatedAt"] == "after"
    assert cache_calls[0][1]["plex_metadata_checked"] is True


def test_processing_item_failure_and_deferred_classification(monkeypatch):
    item = SimpleNamespace(title="Show", year=2020, ratingKey="20")
    config = {"plex": {}, "runtime": {}, "settings": {}, "_artwork_gaps": []}

    async def get_meta(*_args, **_kwargs):
        return {
            "title": "Show",
            "year": 2020,
            "ratingKey": "20",
            "library_type": "show",
        }

    async def build(_config, *_args, **_kwargs):
        _config["_artwork_gaps"].append(
            {"category": "identity_rejected", "detail": "year mismatch"}
        )
        return {
            "metadata_action": "failed",
            "poster_action": "deferred",
            "season_poster_actions": {1: "failed", 2: "deferred"},
            "plex_candidate": {},
        }

    async def apply(*_args, **_kwargs):
        return {"writes": 0, "failures": 1, "deferred": 1}

    monkeypatch.setattr(processing, "get_plex_metadata", get_meta)
    monkeypatch.setattr(processing, "build_tv", build)
    monkeypatch.setattr(processing, "apply_plex_metadata", apply)

    stats = asyncio.run(
        processing.process_item(
            item,
            {"metadata": {}},
            config,
            feature_flags={"plex_metadata": True, "dry_run": True},
            work_reasons={"metadata", "poster", "season"},
        )
    )

    assert stats["metadata_action"] == "failed"
    assert stats["_incremental_success"] is False
    assert stats["_retry_failure_class"] == "permanent"
    assert stats["_retry_error"] == "year mismatch"


def test_processing_rejects_missing_item_unsupported_type_and_empty_builder(
    monkeypatch
):
    assert asyncio.run(
        processing.process_item(
            None, {}, {}, feature_flags={"plex_metadata": False}
        )
    ) is None

    async def unsupported(*_args, **_kwargs):
        return {"title": "Music", "year": 2020, "library_type": "artist"}

    monkeypatch.setattr(processing, "get_plex_metadata", unsupported)
    with pytest.raises(processing.ItemProcessingError, match="Failed to process"):
        asyncio.run(
            processing.process_item(
                SimpleNamespace(title="Music", year=2020),
                {},
                {"plex": {}},
                feature_flags={"plex_metadata": False},
            )
        )

    async def movie(*_args, **_kwargs):
        return {"title": "Movie", "year": 2020, "library_type": "movie"}

    async def empty(*_args, **_kwargs):
        return None

    monkeypatch.setattr(processing, "get_plex_metadata", movie)
    monkeypatch.setattr(processing, "build_movie", empty)
    with pytest.raises(processing.ItemProcessingError, match="Builder returned no result"):
        asyncio.run(
            processing.process_item(
                SimpleNamespace(title="Movie", year=2020),
                {},
                {"plex": {}},
                feature_flags={"plex_metadata": False},
            )
        )


def test_processing_failure_formatting_and_inventory_edges(monkeypatch):
    class RootError(RuntimeError):
        pass

    failures = []
    for number in range(12):
        try:
            try:
                raise RootError(f"root-{number}")
            except RootError as error:
                raise processing.ItemProcessingError("wrapper") from error
        except processing.ItemProcessingError as error:
            failures.append((f"Title {number}", error))
    formatted = processing.format_item_failures(failures)
    assert "root-0" in formatted
    assert "... and 2 more item(s)" in formatted

    errors = processing.cleanup_inventory_errors(
        [
            {
                "title": "Show",
                "year": 2020,
                "ratingKey": "1",
                "library_type": "show",
                "show_path": "/media/show",
                "seasons_episodes": None,
            },
            {
                "title": "Movie",
                "year": 2020,
                "ratingKey": "2",
                "library_type": "movie",
            },
        ],
        {"poster": True, "season": True, "metadata_basic": True},
    )
    assert "seasons_episodes" in errors[0]
    assert "movie_path" in errors[1]
