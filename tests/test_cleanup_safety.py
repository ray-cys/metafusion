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
                            0: {
                                "episodes": {
                                    1: {"title": "Special"},
                                    2: {"title": "Removed special"},
                                }
                            },
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
    assert set(
        tv_doc["metadata"]["Keep Show (2021)"]["seasons"][0]["episodes"]
    ) == {1}
    assert set(cache) == {"movie:Keep Movie:2020", "tv:Keep Show:2021"}
    assert set(cache["tv:Keep Show:2021"]["seasons"]) == {"0"}
    assert dirty
    assert removed.titles == 1
    assert removed.seasons == 1
    assert removed.episodes == 1


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
            "poster_checksum": cleanup_module.sha256_file(poster),
            "background_path": str(background),
            "background_checksum": cleanup_module.sha256_file(background),
            "seasons": {
                "0": {
                    "season_path": str(season),
                    "season_checksum": cleanup_module.sha256_file(season),
                }
            },
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

    assert removed.titles == 1
    assert removed.assets == 3
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
            "poster_checksum": cleanup_module.sha256_file(poster),
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


def test_cleanup_validates_all_yaml_before_writing_any_file(monkeypatch, tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    movie_file = metadata_dir / "movie_metadata.yml"
    tv_file = metadata_dir / "tv_metadata.yml"
    original_movie = "metadata:\n  Old Movie (2000): {}\n"
    movie_file.write_text(original_movie, encoding="utf-8")
    tv_file.write_text("metadata: [unterminated\n", encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "load_cache", dict)

    with pytest.raises(cleanup_module.CleanupError, match="Failed to clean"):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                kometa_config(tmp_path),
                flags(poster=False, season=False, background=False),
                preloaded_plex_metadata={},
                safe_library_types={"movie", "tv"},
            )
        )

    assert movie_file.read_text(encoding="utf-8") == original_movie


def test_cleanup_refuses_to_overwrite_concurrent_metadata_edit(monkeypatch, tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    metadata_file = metadata_dir / "movie_metadata.yml"
    metadata_file.write_text(
        "metadata:\n  Old Movie (2000): {}\n", encoding="utf-8"
    )
    external = "metadata:\n  External Edit (2024): {}\n"
    real_writer = cleanup_module.write_kometa_metadata

    def concurrent_writer(path, document, **kwargs):
        path.write_text(external, encoding="utf-8")
        return real_writer(path, document, **kwargs)

    monkeypatch.setattr(cleanup_module, "load_cache", dict)
    monkeypatch.setattr(cleanup_module, "write_kometa_metadata", concurrent_writer)

    with pytest.raises(cleanup_module.CleanupError, match="Failed to clean"):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                kometa_config(tmp_path),
                flags(poster=False, season=False, background=False),
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )

    assert metadata_file.read_text(encoding="utf-8") == external


def test_cleanup_asset_permission_failure_is_recoverable(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"managed")
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "poster_path": str(poster),
            "poster_checksum": cleanup_module.sha256_file(poster),
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
    assert "movie:Old Movie:2000" in cache


def test_cleanup_preserves_managed_path_replaced_by_symlink(
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

    assert poster.is_symlink()
    assert external.read_bytes() == b"manual"


def test_cleanup_removes_orphaned_season_poster_from_valid_show(
    monkeypatch, tmp_path
):
    asset_root = tmp_path / "assets"
    show_dir = asset_root / "tv" / "Keep Show (2021)"
    special = show_dir / "Season00.jpg"
    removed_season = show_dir / "Season01.jpg"
    show_dir.mkdir(parents=True)
    special.write_bytes(b"special")
    removed_season.write_bytes(b"season-one")
    cache = {
        "tv:Keep Show:2021": {
            "media_type": "tv",
            "title": "Keep Show",
            "year": 2021,
            "seasons": {
                "0": {
                    "season_path": str(special),
                    "season_checksum": cleanup_module.sha256_file(special),
                },
                "1": {
                    "season_path": str(removed_season),
                    "season_checksum": cleanup_module.sha256_file(removed_season),
                },
            },
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(metadata_basic=False, metadata_enhanced=False),
            asset_path=asset_root,
            preloaded_plex_metadata={
                "show": {
                    "library_type": "tv",
                    "title": "Keep Show",
                    "year": 2021,
                    "show_path": "/media/Keep Show (2021)",
                    "seasons_episodes": {0: [1]},
                }
            },
            safe_library_types={"tv"},
        )
    )

    assert special.exists()
    assert not removed_season.exists()
    assert set(cache["tv:Keep Show:2021"]["seasons"]) == {"0"}
    assert result.seasons == 1
    assert result.assets == 1
    assert result.titles == 0


def test_cleanup_preserves_manually_replaced_artwork(monkeypatch, tmp_path):
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"generated")
    generated_checksum = cleanup_module.sha256_file(poster)
    poster.write_bytes(b"manual replacement")
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "title": "Old Movie",
            "year": 2000,
            "poster_path": str(poster),
            "poster_checksum": generated_checksum,
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
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

    assert poster.read_bytes() == b"manual replacement"
    assert result.assets == 0


def test_cleanup_preserves_legacy_managed_artwork_without_checksum(
    monkeypatch, tmp_path
):
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Old Movie (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"legacy generated file")
    cache = {
        "movie:Old Movie:2000": {
            "media_type": "movie",
            "title": "Old Movie",
            "year": 2000,
            "poster_path": str(poster),
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
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

    assert poster.read_bytes() == b"legacy generated file"
    assert result.assets == 0


def test_cleanup_scopes_same_named_asset_directories_by_media_type(
    monkeypatch, tmp_path
):
    asset_root = tmp_path / "assets"
    movie_poster = asset_root / "movie" / "Shared (2020)" / "poster.jpg"
    movie_poster.parent.mkdir(parents=True)
    movie_poster.write_bytes(b"stale movie")
    cache = {
        "movie:Shared:2020": {
            "media_type": "movie",
            "title": "Shared",
            "year": 2020,
            "poster_path": str(movie_poster),
            "poster_checksum": cleanup_module.sha256_file(movie_poster),
        },
        "tv:Shared:2020": {
            "media_type": "tv",
            "title": "Shared",
            "year": 2020,
        },
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(
                metadata_basic=False,
                metadata_enhanced=False,
                season=False,
                background=False,
            ),
            asset_path=asset_root,
            preloaded_plex_metadata={
                "show": {
                    "library_type": "tv",
                    "title": "Shared",
                    "year": 2020,
                    "show_path": "/media/Shared (2020)",
                }
            },
            safe_library_types={"movie", "tv"},
        )
    )

    assert not movie_poster.exists()
    assert result.assets == 1


def test_cleanup_scopes_same_named_yaml_titles_by_media_type(monkeypatch, tmp_path):
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    movie_file = metadata_dir / "movie_metadata.yml"
    tv_file = metadata_dir / "tv_metadata.yml"
    document = {"metadata": {"Shared (2020)": {"summary": "generated"}}}
    movie_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    tv_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(cleanup_module, "load_cache", dict)

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(poster=False, season=False, background=False),
            preloaded_plex_metadata={
                "show": {
                    "library_type": "tv",
                    "title": "Shared",
                    "year": 2020,
                    "seasons_episodes": {},
                }
            },
            safe_library_types={"movie", "tv"},
        )
    )

    assert yaml.safe_load(movie_file.read_text(encoding="utf-8"))["metadata"] == {}
    assert set(yaml.safe_load(tv_file.read_text(encoding="utf-8"))["metadata"]) == {
        "Shared (2020)"
    }
    assert result.titles == 1


def test_cleanup_aborts_before_writes_for_incomplete_episode_inventory(
    monkeypatch, tmp_path
):
    cache = {"tv:Old Show:2000": {"media_type": "tv"}}
    dirty = []
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: dirty.append(True))

    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            kometa_config(tmp_path),
            flags(poster=False, season=False, background=False),
            preloaded_plex_metadata={
                "show": {
                    "library_type": "tv",
                    "title": "Keep Show",
                    "year": 2021,
                    "seasons_episodes": None,
                }
            },
            safe_library_types={"tv"},
        )
    )

    assert result.skipped_reason == "Plex season/episode inventory was incomplete"
    assert cache == {"tv:Old Show:2000": {"media_type": "tv"}}
    assert dirty == []


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
