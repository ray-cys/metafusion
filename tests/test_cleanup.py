import asyncio

from modules import cleanup as cleanup_module


def test_cleanup_preserves_disabled_and_unmanaged_assets(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    disabled_poster = asset_root / "Old Movie (2000)" / "poster.jpg"
    manual_background = asset_root / "Manual Movie (2001)" / "fanart.jpg"
    managed_background = asset_root / "Generated Movie (2002)" / "fanart.jpg"
    for asset in (disabled_poster, manual_background, managed_background):
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(b"asset")

    cache = {
        "movie:Old Movie:2000": {"poster_path": str(disabled_poster)},
        "movie:Generated Movie:2002": {"background_path": str(managed_background)},
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    asyncio.run(
        cleanup_module.cleanup_title_orphans(
            {"settings": {"mode": "kometa", "path": str(tmp_path)}},
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
        )
    )

    assert disabled_poster.exists()
    assert manual_background.exists()
    assert not managed_background.exists()


def test_cleanup_dry_run_does_not_persist_cache(monkeypatch, tmp_path):
    dirty_calls = []
    monkeypatch.setattr(
        cleanup_module,
        "load_cache",
        lambda: {"movie:Old Movie:2000": {"title": "Old Movie", "year": 2000}},
    )
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: dirty_calls.append(True))

    asyncio.run(
        cleanup_module.cleanup_title_orphans(
            {"settings": {"mode": "kometa", "path": str(tmp_path)}},
            {
                "dry_run": True,
                "metadata_basic": False,
                "metadata_enhanced": False,
                "poster": False,
                "season": False,
                "background": False,
            },
            preloaded_plex_metadata={},
        )
    )

    assert dirty_calls == []
