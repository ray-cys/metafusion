import asyncio

import pytest
import yaml

from modules import cleanup


def _flags(**updates):
    values = {
        "dry_run": False,
        "metadata_basic": True,
        "metadata_enhanced": True,
        "poster": True,
        "season": True,
        "background": True,
    }
    values.update(updates)
    return values


def _config(tmp_path, mode="kometa", **cleanup_values):
    return {
        "settings": {"mode": mode, "path": str(tmp_path)},
        "cleanup": {
            "confirmation_scans": 1,
            "grace_hours": 0,
            **cleanup_values,
        },
        "plex": {"path_mappings": []},
    }


def _eligible(*_args, **_kwargs):
    return {"eligible": True, "status": "eligible"}


def _patch_state(monkeypatch):
    monkeypatch.setattr(cleanup, "observe_cleanup_candidate", _eligible)
    monkeypatch.setattr(cleanup, "load_item_exceptions", lambda: [])
    monkeypatch.setattr(cleanup, "load_cleanup_candidates", lambda: [])
    monkeypatch.setattr(cleanup, "record_cleanup_history", lambda *_a, **_k: True)
    monkeypatch.setattr(cleanup, "complete_cleanup_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(cleanup, "cancel_cleanup_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(cleanup, "mark_cache_dirty", lambda: None)


def test_cleanup_input_guards_and_cache_failure(monkeypatch, tmp_path):
    assert asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path), _flags(), preloaded_plex_metadata=None,
            safe_library_types={"movie"}
        )
    ).skipped_reason == "Plex metadata was unavailable"

    _patch_state(monkeypatch)
    monkeypatch.setattr(
        cleanup, "load_cache", lambda: (_ for _ in ()).throw(OSError("locked"))
    )
    with pytest.raises(cleanup.CleanupError, match="load cleanup state") as caught:
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path), _flags(), preloaded_plex_metadata={},
                safe_library_types={"movie"}
            )
        )
    assert caught.value.result.failures == 1


def test_cleanup_dry_run_covers_tv_title_season_episode_and_pending_candidates(
    monkeypatch, tmp_path
):
    _patch_state(monkeypatch)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "notes.yml").write_text("metadata: {}\n", encoding="utf-8")
    tv_file = metadata_dir / "tv_metadata.yml"
    tv_file.write_text(
        yaml.safe_dump(
            {
                "metadata": {
                    "Old Show (1990)": {},
                    "Keep Show (2020)": {
                        "seasons": {
                            1: {"episodes": {1: {}, 2: {}}},
                            2: {"episodes": {1: {}}},
                        }
                    },
                    "Malformed": [],
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cache = {
        "tv:Keep Show:2020": {
            "media_type": "tv",
            "title": "Keep Show",
            "year": 2020,
            "rating_key": "1",
            "seasons": {"1": {}, "2": {}},
        },
        "tv:Old Show:1990": {
            "media_type": "tv",
            "title": "Old Show",
            "year": 1990,
            "rating_key": "2",
        },
        "bad": [],
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    cancelled = []
    monkeypatch.setattr(
        cleanup,
        "load_cleanup_candidates",
        lambda: [
            {"candidate_key": "a", "cache_key": cleanup.cache_key_for_meta(inventory["show"]), "scope": "title"},
            {"candidate_key": "b", "cache_key": cleanup.cache_key_for_meta(inventory["show"]), "scope": "metadata_season", "season_number": 1},
            {"candidate_key": "c", "cache_key": cleanup.cache_key_for_meta(inventory["show"]), "scope": "metadata_episode", "season_number": 1, "episode_number": 1},
        ],
    )
    monkeypatch.setattr(
        cleanup,
        "cancel_cleanup_candidate",
        lambda key, **_kwargs: cancelled.append(key),
    )
    inventory = {
        "show": {
            "library_type": "tv",
            "title": "Keep Show",
            "year": 2020,
            "ratingKey": "1",
            "seasons_episodes": {1: [1]},
        }
    }
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(dry_run=True, poster=False, background=False, season=True),
            preloaded_plex_metadata=inventory,
            safe_library_types={"shows"},
        )
    )
    assert result.titles == 2
    assert result.seasons >= 1
    assert result.episodes == 1
    assert result.yaml_entries >= 3
    assert set(cancelled) == set()
    assert "Old Show" in tv_file.read_text(encoding="utf-8")

    # The same durable candidates are cancelled when a non-preview scan confirms
    # that the title, season and episode have returned to Plex.
    monkeypatch.setattr(
        cleanup,
        "load_cache",
        lambda: {
            cleanup.cache_key_for_meta(inventory["show"]): cache["tv:Keep Show:2020"]
        },
    )
    asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path, mode="plex"),
            _flags(metadata_basic=False, metadata_enhanced=False, poster=False, background=False),
            preloaded_plex_metadata=inventory,
            safe_library_types={"tv"},
        )
    )
    assert set(cancelled) == {"a", "b", "c"}


def test_plex_cleanup_checksum_and_scope_matrix(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    root = tmp_path / "media"
    outside = tmp_path / "outside" / "poster.jpg"
    no_checksum = root / "no-checksum" / "poster.jpg"
    checksum_error = root / "checksum-error" / "poster.jpg"
    modified = root / "modified" / "poster.jpg"
    removable = root / "removable" / "poster.jpg"
    disabled = root / "disabled" / "fanart.jpg"
    for path in (outside, no_checksum, checksum_error, modified, removable, disabled):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.parent.name.encode("utf-8"))
    real_checksum = cleanup.sha256_file
    removable_checksum = real_checksum(removable)
    cache = {
        "movie:outside:2000": {
            "media_type": "movie", "poster_path": str(outside),
            "poster_checksum": real_checksum(outside),
        },
        "movie:no checksum:2001": {
            "media_type": "movie", "poster_path": str(no_checksum),
        },
        "movie:error:2002": {
            "media_type": "movie", "poster_path": str(checksum_error),
            "poster_checksum": "value",
        },
        "movie:modified:2003": {
            "media_type": "movie", "poster_path": str(modified),
            "poster_checksum": "different",
        },
        "movie:remove:2004": {
            "media_type": "movie", "poster_path": str(removable),
            "poster_checksum": removable_checksum,
        },
        "movie:disabled:2005": {
            "media_type": "movie", "background_path": str(disabled),
            "background_checksum": real_checksum(disabled),
        },
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)

    def checked(path):
        if "checksum-error" in str(path):
            raise OSError("unreadable")
        return real_checksum(path)

    monkeypatch.setattr(cleanup, "sha256_file", checked)
    config = _config(tmp_path, mode="plex", plex_remove_managed_artwork=True)
    config["plex"]["path_mappings"] = [f"/plex=>{root}", "invalid"]
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            config,
            _flags(metadata_basic=False, metadata_enhanced=False, background=False),
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )
    assert not removable.exists()
    assert outside.exists() and no_checksum.exists() and checksum_error.exists()
    assert modified.exists() and disabled.exists()
    assert result.assets == 1
    assert result.assets_preserved == 4


def test_plex_cleanup_dry_run_and_state_only(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    root = tmp_path / "media"
    poster = root / "movie" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster")
    cache = {
        "movie:one:2000": {
            "media_type": "movie",
            "poster_path": str(poster),
            "poster_checksum": cleanup.sha256_file(poster),
        }
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    config = _config(tmp_path, mode="plex", plex_remove_managed_artwork=True)
    config["plex"]["path_mappings"] = [f"/plex=>{root}"]
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            config, _flags(dry_run=True), preloaded_plex_metadata={},
            safe_library_types={"movie"}
        )
    )
    assert poster.exists() and result.assets == 1

    monkeypatch.setattr(cleanup, "load_cache", lambda: {})
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path, mode="plex"), _flags(),
            preloaded_plex_metadata={}, safe_library_types={"movie"}
        )
    )
    assert result.assets == 0
