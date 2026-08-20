import asyncio

import pytest
import yaml
from test_phase26_cleanup_edges import _config, _eligible, _flags, _patch_state

from modules import cleanup


def test_cleanup_dry_run_covers_identityless_title_season_and_asset(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    asset_root = tmp_path / "assets"
    poster = asset_root / "movie" / "Unknown" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"poster")
    show = {
        "library_type": "tv",
        "title": "Show",
        "year": 2020,
        "ratingKey": "show",
        "seasons_episodes": {1: [1]},
    }
    identityless_show = {
        "library_type": "tv",
        "title": None,
        "year": None,
        "ratingKey": "identityless-show",
        "seasons_episodes": {1: [1]},
    }
    show_key = cleanup.cache_key_for_meta(show)
    identityless_key = cleanup.cache_key_for_meta(identityless_show)
    cache = {
        "movie:invalid": {
            "media_type": "movie",
            "poster_path": str(poster),
            "poster_checksum": cleanup.sha256_file(poster),
        },
        show_key: {
            "media_type": "tv",
            "title": "Show",
            "year": 2020,
            "rating_key": "show",
            "seasons": {"1": {}, "2": {}},
        },
        identityless_key: {
            "media_type": "tv",
            "rating_key": "identityless-show",
            "seasons": {"1": {}, "2": {}},
        },
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(dry_run=True, metadata_basic=False, metadata_enhanced=False),
            asset_path=asset_root,
            preloaded_plex_metadata={
                "show": show,
                "identityless-show": identityless_show,
            },
            safe_library_types={"movie", "tv"},
        )
    )
    assert result.cache_entries == 4
    assert result.seasons == 1
    assert result.assets == 1 and poster.exists()


def test_cleanup_exceptions_pending_and_disappearing_season_record(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    show = {
        "library_type": "tv",
        "title": "Show",
        "year": 2020,
        "ratingKey": "1",
        "seasons_episodes": {1: [1]},
    }
    key = cleanup.cache_key_for_meta(show)
    cache = {
        key: {
            "media_type": "tv",
            "title": "Show",
            "year": 2020,
            "rating_key": "1",
            "seasons": {"1": {}, "2": {}},
        },
        "movie:Old:2000": {
            "media_type": "movie",
            "title": "Old",
            "year": 2000,
            "rating_key": "1",
        },
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    monkeypatch.setattr(
        cleanup,
        "load_item_exceptions",
        lambda: [{"rating_key": "1", "output_type": "cleanup"}],
    )
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(metadata_basic=False, metadata_enhanced=False),
            preloaded_plex_metadata={"show": show},
            safe_library_types={"movie", "tv"},
        )
    )
    assert result.cache_entries == 0

    monkeypatch.setattr(cleanup, "load_item_exceptions", lambda: [])
    monkeypatch.setattr(
        cleanup,
        "observe_cleanup_candidate",
        lambda _key, _record, scope, **_kwargs: (
            {"eligible": False, "status": "pending"}
            if scope == "season"
            else _eligible()
        ),
    )
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(metadata_basic=False, metadata_enhanced=False),
            preloaded_plex_metadata={"show": show},
            safe_library_types={"tv"},
        )
    )
    assert result.candidates_pending == 1

    def remove_before_apply(_key, _record, scope, **_kwargs):
        if scope == "season":
            cache[key]["seasons"].pop("2", None)
        return _eligible()

    cache[key]["seasons"]["2"] = {}
    monkeypatch.setattr(cleanup, "observe_cleanup_candidate", remove_before_apply)
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(metadata_basic=False, metadata_enhanced=False),
            preloaded_plex_metadata={"show": show},
            safe_library_types={"tv"},
        )
    )
    assert result.cache_entries == 0


def _show(title, year, rating_key, seasons):
    return {
        "library_type": "tv",
        "title": title,
        "year": year,
        "ratingKey": rating_key,
        "seasons_episodes": seasons,
    }


def test_cleanup_yaml_shape_exception_and_pending_matrix(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    entries = {
        "Valid List (2020)": [],
        "Bad Year (bad)": {"seasons": {}},
        "Seasons List (2021)": {"seasons": ["invalid"]},
        "Season Scalar (2022)": {"seasons": {1: "bad"}},
        "Episodes Scalar (2023)": {"seasons": {1: {"episodes": []}}},
        "Season Exception (2024)": {"seasons": {2: {}}},
        "Season Pending (2025)": {"seasons": {2: {}}},
        "Episode Exception (2026)": {"seasons": {1: {"episodes": {2: {}}}}},
        "Episode Pending (2027)": {"seasons": {1: {"episodes": {2: {}}}}},
        "Orphan Exception (1990)": {},
        "Orphan Pending (1991)": {},
    }
    (metadata_dir / "tv_metadata.yml").write_text(
        yaml.safe_dump({"metadata": entries}, sort_keys=False), encoding="utf-8"
    )
    shows = [
        _show("Valid List", 2020, "list", {}),
        _show("Bad Year", "bad", "bad-year", {}),
        _show("Seasons List", 2021, "seasons-list", {}),
        _show("Season Scalar", 2022, "season-scalar", {1: []}),
        _show("Episodes Scalar", 2023, "episodes-scalar", {1: []}),
        _show("Season Exception", 2024, "season-exception", {1: []}),
        _show("Season Pending", 2025, "season-pending", {1: []}),
        _show("Episode Exception", 2026, "episode-exception", {1: [1]}),
        _show("Episode Pending", 2027, "episode-pending", {1: [1]}),
    ]
    inventory = {str(index): value for index, value in enumerate(shows)}
    cache = {
        "tv:Orphan Exception:1990": {
            "media_type": "tv",
            "title": "Orphan Exception",
            "year": 1990,
            "rating_key": "orphan-exception",
        },
        "tv:Orphan Pending:1991": {
            "media_type": "tv",
            "title": "Orphan Pending",
            "year": 1991,
            "rating_key": "orphan-pending",
        },
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    exception_keys = {"orphan-exception", "season-exception", "episode-exception"}
    monkeypatch.setattr(
        cleanup,
        "load_item_exceptions",
        lambda: [
            {"rating_key": value, "output_type": "cleanup"}
            for value in exception_keys
        ],
    )

    def decisions(_key, record, scope, **_kwargs):
        if record.get("rating_key") in {
            "orphan-pending",
            "season-pending",
            "episode-pending",
        }:
            return {"eligible": False, "status": "pending"}
        return _eligible()

    monkeypatch.setattr(cleanup, "observe_cleanup_candidate", decisions)
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(dry_run=True, poster=False, background=False, season=True),
            preloaded_plex_metadata=inventory,
            safe_library_types={"tv"},
        )
    )
    assert result.candidates_pending >= 3


def test_cleanup_yaml_reraises_cleanup_error(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "movie_metadata.yml").write_text(
        "metadata:\n  Old (2000): {}\n", encoding="utf-8"
    )
    monkeypatch.setattr(cleanup, "load_cache", lambda: {})
    monkeypatch.setattr(
        cleanup,
        "write_kometa_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            cleanup.CleanupError("nested cleanup failure")
        ),
    )
    with pytest.raises(cleanup.CleanupError, match="nested cleanup failure"):
        asyncio.run(
            cleanup.cleanup_title_orphans(
                _config(tmp_path),
                _flags(poster=False, background=False, season=False),
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )


def test_cleanup_asset_valid_destination_checksum_error_and_dry_run(monkeypatch, tmp_path):
    _patch_state(monkeypatch)
    asset_root = tmp_path / "assets"
    shared = asset_root / "movie" / "Shared" / "poster.jpg"
    unreadable = asset_root / "movie" / "Old" / "poster.jpg"
    for path in (shared, unreadable):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"managed")
    cache = {
        "movie:Stale:2000": {
            "media_type": "movie",
            "title": "Stale",
            "year": 2000,
            "rating_key": "stale",
            "poster_path": str(shared),
            "poster_checksum": cleanup.sha256_file(shared),
        },
        "movie:Old:2001": {
            "media_type": "movie",
            "title": "Old",
            "year": 2001,
            "rating_key": "old",
            "poster_path": str(unreadable),
            "poster_checksum": cleanup.sha256_file(unreadable),
        },
    }
    monkeypatch.setattr(cleanup, "load_cache", lambda: cache)
    real_checksum = cleanup.sha256_file
    monkeypatch.setattr(
        cleanup,
        "sha256_file",
        lambda path: (_ for _ in ()).throw(OSError("unreadable"))
        if path == unreadable
        else real_checksum(path),
    )
    current = {
        "library_type": "movie",
        "title": "Current",
        "year": 2020,
        "ratingKey": "current",
        "movie_path": "Shared",
    }
    result = asyncio.run(
        cleanup.cleanup_title_orphans(
            _config(tmp_path),
            _flags(metadata_basic=False, metadata_enhanced=False, season=False, background=False),
            asset_path=asset_root,
            preloaded_plex_metadata={"current": current},
            safe_library_types={"movie"},
        )
    )
    assert result.assets_skipped == 1 and result.assets_preserved == 1
