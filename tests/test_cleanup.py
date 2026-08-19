import asyncio

from modules import cleanup as cleanup_module


def cleanup_config(mode, tmp_path):
    return {
        "settings": {"mode": mode, "path": str(tmp_path)},
        "cleanup": {"confirmation_scans": 1, "grace_hours": 0},
    }


def test_cleanup_preserves_disabled_and_unmanaged_assets(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    disabled_poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    manual_background = asset_root / "movie" / "Manual Movie (2001)" / "fanart.jpg"
    managed_background = asset_root / "movie" / "Generated Movie (2002)" / "fanart.jpg"
    for asset in (disabled_poster, manual_background, managed_background):
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"asset")

    cache = {
        "movie:Old Movie:2000": {"poster_path": str(disabled_poster)},
        "movie:Generated Movie:2002": {
            "background_path": str(managed_background),
            "background_checksum": cleanup_module.sha256_file(managed_background),
        },
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            cleanup_config("kometa", tmp_path),
            {
                "dry_run": False,
                "metadata_basic": False,
                "metadata_enhanced": False,
                "poster": False,
                "season": False,
                "background": True,
            },
            asset_path=asset_root,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert disabled_poster.exists()
    assert manual_background.exists()
    assert not managed_background.exists()
    assert result.assets == 1
    assert result.assets_preserved == 1
    assert result.assets_skipped == 0
    assert result.cache_entries == 2


def test_cleanup_dry_run_does_not_persist_cache(monkeypatch, tmp_path):
    dirty_calls = []
    monkeypatch.setattr(
        cleanup_module,
        "load_cache",
        lambda: {"movie:Old Movie:2000": {"title": "Old Movie", "year": 2000}},
    )
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: dirty_calls.append(True))

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            cleanup_config("kometa", tmp_path),
            {
                "dry_run": True,
                "metadata_basic": False,
                "metadata_enhanced": False,
                "poster": False,
                "season": False,
                "background": False,
            },
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert dirty_calls == []
    assert result.dry_run is True
    assert result.titles == 1
    assert result.cache_entries == 1


def test_cleanup_requires_an_explicit_complete_inventory(monkeypatch, tmp_path):
    cache = {"movie:Old Movie:2000": {"media_type": "movie"}}
    dirty_calls = []
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(
        cleanup_module, "mark_cache_dirty", lambda: dirty_calls.append(True)
    )

    removed = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            cleanup_config("plex", tmp_path),
            {"dry_run": False},
            preloaded_plex_metadata={},
        )
    )

    assert removed.titles == 0
    assert removed.skipped_reason
    assert "movie:Old Movie:2000" in cache
    assert dirty_calls == []


def test_cleanup_only_removes_cache_for_safe_library_types(monkeypatch, tmp_path):
    cache = {
        "movie:Old Movie:2000": {"media_type": "movie"},
        "tv:Old Show:2001": {"media_type": "tv"},
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            cleanup_config("plex", tmp_path),
            {"dry_run": False},
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert "movie:Old Movie:2000" not in cache
    assert "tv:Old Show:2001" in cache
    assert result.mode == "plex"
    assert result.cache_entries == 1
    assert result.yaml_entries == 0
    assert result.assets == 0


def test_cleanup_handles_shared_canonical_asset_owners(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    shared_poster = asset_root / "movie" / "Shared Movie (2000)" / "poster.jpg"
    shared_poster.parent.mkdir(parents=True)
    shared_poster.write_bytes(b"shared-managed-artwork")
    checksum = cleanup_module.sha256_file(shared_poster)
    cache = {
        "movie:plex:1": {
            "media_type": "movie",
            "title": "Shared Movie",
            "year": 2000,
            "poster_path": str(shared_poster),
            "poster_checksum": "not-the-current-checksum",
        },
        "movie:plex:2": {
            "media_type": "movie",
            "title": "Shared Movie",
            "year": 2000,
            "poster_path": str(shared_poster),
            "poster_checksum": checksum,
        },
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            cleanup_config("kometa", tmp_path),
            {
                "dry_run": False,
                "metadata_basic": False,
                "metadata_enhanced": False,
                "poster": True,
                "season": False,
                "background": False,
            },
            asset_path=asset_root,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert not shared_poster.exists()
    assert result.assets == 1
    assert result.titles == 1
    assert not cache
