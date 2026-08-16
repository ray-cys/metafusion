import asyncio
from pathlib import Path

import pytest
import yaml

from modules import cleanup as cleanup_module


def flags(**overrides):
    values = {
        "dry_run": False,
        "metadata_basic": True,
        "metadata_enhanced": True,
        "poster": True,
        "season": True,
        "background": True,
    }
    values.update(overrides)
    return values


def kometa_config(tmp_path):
    return {"settings": {"mode": "kometa", "path": str(tmp_path)}}


def test_cleanup_reconciles_yaml_cache_and_specials_with_complete_inventory(
    monkeypatch, tmp_path
):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    movie_file = metadata_dir / "movie_metadata.yml"
    tv_file = metadata_dir / "tv_metadata.yml"
    movie_file.write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "Keep Movie (2020)": {"summary": "keep"},
                    "Old Movie (1999)": {"summary": "remove"},
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tv_file.write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "Keep Show (2021)": {
                        "seasons": {
                            0: {"episodes": {1: {"title": "Special"}}},
                            1: {"episodes": {1: {"title": "Removed"}}},
                        }
                    }
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cache = {
        "movie:Keep Movie:2020": {"media_type": "movie"},
        "movie:Old Movie:1999": {
            "media_type": "movie",
            "title": "Old Movie",
            "year": 1999,
        },
        "tv:Keep Show:2021": {
            "media_type": "tv",
            "seasons": {"0": {}, "1": {}},
        },
    }
    dirty = []
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: dirty.append(True))
    inventory = {
        "movie": {
            "library_type": "movie",
            "title": "Keep Movie",
            "year": 2020,
        },
        "show": {
            "library_type": "tv",
            "title": "Keep Show",
            "year": 2021,
            "seasons_episodes": {0: [1]},
        },
    }

    removed = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(poster=False, season=False, background=False),
            preloaded_plex_metadata=inventory,
            safe_library_types={"movies", "shows"},
        )
    )

    movie_doc = yaml.safe_load(movie_file.read_text(encoding="utf-8"))
    tv_doc = yaml.safe_load(tv_file.read_text(encoding="utf-8"))
    assert set(movie_doc["metadata"]) == {"Keep Movie (2020)"}
    assert set(tv_doc["metadata"]["Keep Show (2021)"]["seasons"]) == {0}
    assert set(cache) == {"movie:Keep Movie:2020", "tv:Keep Show:2021"}
    assert set(cache["tv:Keep Show:2021"]["seasons"]) == {"0"}
    assert dirty
    assert removed >= 1


def test_cleanup_removes_only_cache_managed_assets_and_empty_directory(
    monkeypatch, tmp_path
):
    asset_root = tmp_path / "assets"
    title_dir = asset_root / "movie" / "Old Movie (2000)"
    poster = title_dir / "poster.jpg"
    background = title_dir / "fanart.jpg"
    season = title_dir / "Season00.jpg"
    for asset in (poster, background, season):
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.write_bytes(asset.name.encode())

    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "title": "Old Movie",
            "year": 2000,
            "poster_path": str(poster),
            "background_path": str(background),
            "seasons": {"0": {"season_path": str(season)}},
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    removed = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(metadata_basic=False, metadata_enhanced=False),
            asset_path=asset_root,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert removed == 1
    assert not title_dir.exists()
    assert cache == {}


def test_cleanup_preserves_asset_confirmed_by_current_run(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"managed")
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "poster_path": str(poster),
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(metadata_basic=False, metadata_enhanced=False),
            asset_path=asset_root,
            existing_assets={str(poster.resolve())},
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert poster.read_bytes() == b"managed"


def test_cleanup_malformed_yaml_fails_without_overwriting(monkeypatch, tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    metadata_file = metadata_dir / "movie_metadata.yml"
    original = "metadata: [unterminated\n"
    metadata_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "load_cache", dict)

    with pytest.raises(cleanup_module.CleanupError, match="Failed to clean"):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                kometa_config(tmp_path),
                flags(poster=False, season=False, background=False),
                preloaded_plex_metadata={
                    "movie": {
                        "library_type": "movie",
                        "title": "Keep",
                        "year": 2020,
                    }
                },
                safe_library_types={"movie"},
            )
        )

    assert metadata_file.read_text(encoding="utf-8") == original


def test_cleanup_asset_permission_failure_is_recoverable(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"managed")
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "poster_path": str(poster),
        }
    }
    original_unlink = Path.unlink

    def guarded_unlink(path, *args, **kwargs):
        if path == poster:
            raise PermissionError("read only")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)
    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    with pytest.raises(cleanup_module.CleanupError, match="Failed to remove"):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                kometa_config(tmp_path),
                flags(
                    metadata_basic=False,
                    metadata_enhanced=False,
                    season=False,
                    background=False,
                ),
                asset_path=asset_root,
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )

    assert poster.exists()


def test_cleanup_unlinks_managed_symlink_without_touching_external_target(
    monkeypatch, tmp_path
):
    external = tmp_path / "manual-poster.jpg"
    external.write_bytes(b"manual")
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.symlink_to(external)
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "poster_path": str(poster),
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(
                metadata_basic=False,
                metadata_enhanced=False,
                season=False,
                background=False,
            ),
            asset_path=asset_root,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert not poster.exists()
    assert external.read_bytes() == b"manual"


def test_cleanup_plex_mode_removes_only_stale_cache_and_no_yaml(monkeypatch, tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    metadata_file = metadata_dir / "movie_metadata.yml"
    metadata_file.write_text("metadata:\n  Manual (2000): {}\n", encoding="utf-8")
    cache = {
        "movie:Old Movie:1999": {"media_type": "movie"},
        "tv:Keep Show:2020": {"media_type": "tv"},
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    asyncio.run(
        cleanup_module.cleanup_title_orphans(
            {"settings": {"mode": "plex", "path": str(tmp_path)}},
            flags(),
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )

    assert set(cache) == {"tv:Keep Show:2020"}
    assert "Manual (2000)" in metadata_file.read_text(encoding="utf-8")
