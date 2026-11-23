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
    root = tmp_path / "movie"
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


def test_cleanup_rejects_malformed_yaml_and_reports_cache_commit_failure(
    monkeypatch, tmp_path
):
    _patch_state(monkeypatch)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "movie_metadata.yml").write_text(
        "metadata: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(cleanup, "load_cache", lambda: {})
    with pytest.raises(cleanup.CleanupError, match="Failed to clean metadata file"):
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path),
                _flags(),
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )

    class FailingCache(dict):
        def __delitem__(self, key):
            raise OSError(f"cannot delete {key}")

    monkeypatch.setattr(
        cleanup,
        "load_cache",
        lambda: FailingCache(
            {
                "movie:Old:2000": {
                    "media_type": "movie",
                    "title": "Old",
                    "year": 2000,
                    "rating_key": "1",
                }
            }
        ),
    )
    with pytest.raises(cleanup.CleanupError, match="update cleanup state cache") as caught:
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path, mode="plex"),
                _flags(),
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )
    assert caught.value.result.failed_reason == "state cache changes could not be committed"


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), ValueError("bad checksum")])
def test_cleanup_asset_task_propagates_cancel_and_normalizes_unexpected_failures(
    monkeypatch, tmp_path, failure
):
    _patch_state(monkeypatch)
    root = tmp_path / "assets"
    poster = root / "movie" / "Old (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster")
    cache = {
        "movie:Old:2000": {
            "media_type": "movie",
            "title": "Old",
            "year": 2000,
            "rating_key": "1",
            "poster_path": str(poster),
            "poster_checksum": cleanup.sha256_file(poster),
        }
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    monkeypatch.setattr(
        cleanup,
        "sha256_file",
        lambda _path: (_ for _ in ()).throw(failure),
    )

    expected = asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else cleanup.CleanupError
    with pytest.raises(expected):
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path),
                _flags(metadata_basic=False, metadata_enhanced=False),
                asset_path=root,
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )


def test_cleanup_empty_directory_failure_retains_completed_result(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    root = tmp_path / "assets"
    poster = root / "movie" / "Old (2000)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster")
    cache = {
        "movie:Old:2000": {
            "media_type": "movie",
            "title": "Old",
            "year": 2000,
            "rating_key": "1",
            "poster_path": str(poster),
            "poster_checksum": cleanup.sha256_file(poster),
        }
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    monkeypatch.setattr(
        cleanup,
        "_directory_is_empty",
        lambda _path: (_ for _ in ()).throw(OSError("directory busy")),
    )

    with pytest.raises(cleanup.CleanupError, match="empty asset directory") as caught:
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path),
                _flags(metadata_basic=False, metadata_enhanced=False),
                asset_path=root,
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )
    assert caught.value.result.assets == 1


def test_partial_scope_with_excluded_library(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from modules.cleanup_scope import cleanup_scopes

    _patch_state(monkeypatch)
    section = SimpleNamespace(type="show", uuid="f1", all=lambda: [
        SimpleNamespace(title="Shared", year=2020, ratingKey="7", locations=["/media/Shared", "/media/Shared folder"])
    ])
    plex = SimpleNamespace(library=SimpleNamespace(section=lambda name: section))
    libraries = [{"title": "TV", "type": "show", "uuid": "tv"}, {"title": "Formula 1", "type": "show", "uuid": "f1"}]
    scans = [{"server_id": "plex", "library_uuid": "tv", "library_name": "TV"}]
    partial, descriptions = asyncio.run(cleanup_scopes(plex, libraries, scans, set(), {}))
    assert "Formula 1" in descriptions["tv"]
    cache = {}
    root = tmp_path / "assets"
    for index, name in enumerate(("Gone", "Shared", "Other", "Shared path", "Folder collision")):
        directory = "Shared" if name == "Shared path" else ("Shared folder" if name == "Folder collision" else name)
        poster = root / "tv" / directory / "poster.jpg"
        poster.parent.mkdir(parents=True, exist_ok=True)
        poster.write_bytes(b"poster")
        cache[f"tv:plex:{index}"] = {
            "media_type": "tv", "title": name, "year": 2020, "server_id": "plex",
            "library_uuid": "f1" if name == "Other" else "tv",
            "poster_path": str(poster), "poster_checksum": cleanup.sha256_file(poster),
        }
    metadata = tmp_path / "metadata" / "tv_metadata.yml"
    metadata.parent.mkdir()
    metadata.write_text(yaml.safe_dump({"metadata": {
        f"{entry['title']} (2020)": {"summary": "entry"} for entry in cache.values()
    }}))
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    result = asyncio.run(cleanup.cleanup_title_orphans(
        _config(tmp_path), _flags(), root, preloaded_plex_metadata={}, partial_scopes=partial,
    ))
    assert (root / "tv/Shared folder/poster.jpg").exists()
    assert result.assets == 1
    assert not (root / "tv/Gone/poster.jpg").exists()
    assert (root / "tv/Shared/poster.jpg").exists()
    assert set(yaml.safe_load(metadata.read_text())["metadata"]) == {"Shared (2020)", "Other (2020)"}
    assert set(cache) == {"tv:plex:1", "tv:plex:2"}

    for mutation in ("uuid", "locations"):
        section.uuid = "wrong" if mutation == "uuid" else "f1"
        section.all = lambda: [SimpleNamespace(title="F1", year=2026, ratingKey="7", locations=[])]
        scopes, reasons = asyncio.run(cleanup_scopes(plex, libraries, scans, set(), {}))
        assert scopes == {} and "Deferred" in reasons["tv"]
    scopes, reasons = asyncio.run(cleanup_scopes(plex, libraries, [], set(), {}))
    assert not scopes and "No verified library owner" in reasons["tv"]


def test_disabled_poster_retains_proof_until_reenabled(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    root = tmp_path / "assets"
    poster = root / "movie/Gone/poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster")
    key = "movie:plex:1"
    cache = {key: {"media_type": "movie", "title": "Gone", "year": 2020,
                       "poster_path": str(poster), "poster_checksum": cleanup.sha256_file(poster)}}
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    def reconcile(enabled):
        return asyncio.run(cleanup.cleanup_title_orphans(
            _config(tmp_path), _flags(poster=enabled), root,
            preloaded_plex_metadata={}, safe_library_types={"movie"},
        ))
    assert reconcile(False).ownership_deferred == 1
    assert cache[key]["poster_checksum"] == cleanup.sha256_file(poster)
    assert reconcile(True).assets == 1
    assert not poster.exists() and key not in cache


def test_partial_scope_pending_yaml_and_protected_children(monkeypatch, tmp_path):
    from modules.cleanup_scope import PartialCleanupScope

    _patch_state(monkeypatch)
    key = "tv:plex:8"
    cache = {key: {"title": "Gone", "year": 2020, "media_type": "tv", "server_id": "s", "library_uuid": "u"}}
    meta = {"title": "Live", "year": 2020, "library_type": "show", "ratingKey": "9", "seasons_episodes": {}}
    doc = {"metadata": {"Gone (2020)": {}, "Live (2020)": {"seasons": {1: {}}}}}
    path = tmp_path / "metadata/tv_metadata.yml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup, "observe_cleanup_candidate", lambda key, *a, **kw: {"eligible": key.startswith("title:")})
    result = asyncio.run(cleanup.cleanup_title_orphans(
        _config(tmp_path), _flags(), preloaded_plex_metadata={"live": meta},
        partial_scopes={"tv": PartialCleanupScope(owners={("s", "u")})},
    ))
    assert result.pending_by_type == {"tv": 1}
    assert key in cache
    assert yaml.safe_load(path.read_text()) == doc


def test_plex_partial_scope_preserves_excluded_folder_and_disabled_season(monkeypatch, tmp_path):
    from modules.cleanup_scope import PartialCleanupScope

    _patch_state(monkeypatch)
    root = tmp_path / "plex"
    root.mkdir()
    poster = root / "poster.jpg"
    poster.write_bytes(b"poster")
    season = root / "Season02.jpg"
    season.write_bytes(b"season")
    owner = {"server_id": "s", "library_uuid": "u", "media_type": "tv", "year": 2020}
    cache = {
        "tv:plex:8": {**owner, "title": "Gone", "poster_path": str(poster), "poster_checksum": cleanup.sha256_file(poster)},
        "tv:plex:9": {**owner, "title": "Live", "seasons": {"2": {"season_path": str(season), "season_checksum": cleanup.sha256_file(season)}}},
        "tv:plex:7": {**owner, "title": "Old", "poster_path": str(root / "absent.jpg")},
    }
    meta = {"title": "Live", "year": 2020, "library_type": "show", "ratingKey": "9", "seasons_episodes": {}}
    config = _config(tmp_path, mode="plex", plex_remove_managed_artwork=True)
    config["plex"]["path_mappings"] = [f"/plex=>{root}"]
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    result = asyncio.run(cleanup.cleanup_title_orphans(
        config, _flags(season=False), preloaded_plex_metadata={"live": meta},
        partial_scopes={"tv": PartialCleanupScope(owners={("s", "u")}, protected_dirs={root})},
    ))
    assert poster.exists() and season.exists()
    assert result.ownership_deferred == 1
    assert cache["tv:plex:9"]["seasons"]["2"]["season_checksum"] == cleanup.sha256_file(season)


def test_cleanup_exception_matches_durable_plex_key(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    cache = {"movie:plex:141": {"media_type": "movie", "title": "Protected", "year": 2020}}
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup, "load_item_exceptions", lambda: [{"rating_key": "141", "output_type": "cleanup"}])
    path = tmp_path / "metadata/movie_metadata.yml"
    path.parent.mkdir()
    path.write_text("metadata:\n  Protected (2020): {}\n")
    result = asyncio.run(cleanup.cleanup_title_orphans(
        _config(tmp_path), _flags(), safe_library_types={"movie"}, preloaded_plex_metadata={},
    ))
    assert result.cache_entries == result.yaml_entries == 0
    assert cache and "Protected (2020)" in path.read_text()
