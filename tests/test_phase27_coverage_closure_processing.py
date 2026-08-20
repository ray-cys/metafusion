import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_phase26_processing_matrix import _Section

from modules import processing


def _flags(**updates):
    values = {
        "dry_run": True,
        "metadata_basic": False,
        "metadata_enhanced": False,
        "plex_metadata": False,
        "poster": False,
        "season": False,
        "background": False,
        "cleanup": False,
    }
    values.update(updates)
    return values


def _config(tmp_path, mode="plex", **safety):
    return {
        "settings": {"mode": mode, "path": str(tmp_path), "dry_run": True},
        "runtime": {"max_concurrency": 2},
        "plex": {},
        "safety": safety,
    }


def _item(key="1", *, media_type="movie", title="Movie"):
    return SimpleNamespace(
        ratingKey=key,
        title=title,
        year=2020,
        type=media_type,
        updatedAt="now",
    )


def _meta(item, *, title=None, media_type=None, edition=None):
    normalized = media_type or item.type
    return {
        "ratingKey": item.ratingKey,
        "title": title or item.title,
        "year": item.year,
        "library_name": "Movies",
        "library_type": normalized,
        "movie_path": item.title if normalized == "movie" else None,
        "show_path": item.title if normalized in {"show", "tv"} else None,
        "seasons_episodes": {1: [1]} if normalized in {"show", "tv"} else None,
        "edition_title": edition,
    }


def _patch_library_defaults(monkeypatch):
    monkeypatch.setattr(processing, "load_item_exceptions", lambda *_args: [])
    monkeypatch.setattr(processing, "load_identity_overrides", lambda *_args: [])
    monkeypatch.setattr(processing, "load_cache", lambda: {})


def test_process_item_retry_and_storage_path_matrix(monkeypatch, tmp_path):
    item = _item()

    async def metadata(*_args, **_kwargs):
        return _meta(item)

    async def build(*_args, **_kwargs):
        return {
            "metadata_action": "failed",
            "poster_action": "failed",
            "background_action": "failed",
            "season_poster_actions": {None: "missing", 1: "failed", 2: "missing"},
        }

    valid = tmp_path / "Season01.jpg"
    valid.write_bytes(b"season")
    broken = tmp_path / "broken.jpg"

    def asset_path(_config, _meta, *, asset_type, season_number=None):
        if asset_type == "poster" or season_number == 2:
            return None
        if asset_type == "background":
            return broken
        return valid

    real_stat = Path.stat
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "build_movie", build)
    monkeypatch.setattr(processing, "get_asset_path", asset_path)
    monkeypatch.setattr(processing, "log_item_outcomes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("missing"))
            if candidate == broken
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    result = asyncio.run(
        processing.process_item(
            item,
            {},
            {"settings": {"mode": "plex", "dry_run": False}, "plex": {}},
            feature_flags=_flags(
                dry_run=False, poster=True, background=True, season=True
            ),
        )
    )
    assert result["_retry_error"].startswith("Builder or Plex")
    assert result["storage_files"] == [
        {"asset_type": "season", "path": str(valid.resolve()), "bytes": 6}
    ]


def test_failure_helpers_exception_records_and_show_inventory_failure(monkeypatch, tmp_path):
    assert processing.find_ambiguous_editions([_meta(_item())]) == []
    section = _Section([_item(None, media_type="show", title="Show")])
    section.title = "TV Shows"
    section.type = "show"
    section.uuid = "library"
    section._server = SimpleNamespace(machineIdentifier="server")

    async def failed_metadata(*_args, **_kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(processing, "get_plex_metadata", failed_metadata)
    monkeypatch.setattr(
        processing,
        "load_item_exceptions",
        lambda *_args: [{"rating_key": "1", "output_type": "poster"}],
    )
    monkeypatch.setattr(processing, "load_identity_overrides", lambda *_args: [])
    monkeypatch.setattr(processing, "load_due_item_retries", lambda *_args: {})
    monkeypatch.setattr(processing, "mark_items_started", lambda *_args: None)
    with pytest.raises(processing.LibraryProcessingError, match="failed"):
        asyncio.run(
            processing.process_library(
                section,
                _config(tmp_path),
                feature_flags=_flags(dry_run=False),
            )
        )
    assert processing._item_exception_scopes(
        {"_item_exceptions_by_rating_key": {"1": [{"output_type": "poster"}]}},
        {"ratingKey": "1"},
    ) == {"poster"}


def test_ambiguous_allowed_and_library_type_inference(monkeypatch, tmp_path):
    _patch_library_defaults(monkeypatch)
    items = [_item("1", title="Same"), _item("2", title="Same")]
    section = _Section(items)
    section.title = "Movies"
    section.type = "movies"

    async def metadata(item, **_kwargs):
        return _meta(item, title="Same")

    async def processed(**_kwargs):
        return {}

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", processed)
    assert asyncio.run(
        processing.process_library(
            section,
            _config(tmp_path / "allowed", mode="kometa", allow_ambiguous_editions=True),
            feature_flags=_flags(),
        )
    ) == []

    for title, expected in (
        ("Movies Archive", "movie"),
        ("TV Shows", "tv"),
        ("Misc", "unknown"),
    ):
        empty = _Section([])
        empty.title = title
        empty.type = None
        summaries = {}
        asyncio.run(
            processing.process_library(
                empty,
                _config(tmp_path / title),
                feature_flags=_flags(),
                metadata_summaries=summaries,
            )
        )
        assert summaries[title]["library_type"] == expected


def test_existing_yaml_parse_failure_is_wrapped(monkeypatch, tmp_path):
    _patch_library_defaults(monkeypatch)
    item = _item()
    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "movie_metadata.yml").write_text(
        "metadata: [broken\n", encoding="utf-8"
    )

    async def metadata(*_args, **_kwargs):
        return _meta(item)

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    with pytest.raises(processing.LibraryProcessingError, match="Unable to parse"):
        asyncio.run(
            processing.process_library(
                section,
                _config(tmp_path, mode="kometa"),
                feature_flags=_flags(),
            )
        )


def test_item_failure_tracking_retry_and_background_relaxation(monkeypatch, tmp_path):
    _patch_library_defaults(monkeypatch)
    items = [_item("1"), _item("2")]
    section = _Section(items)
    section.title = "Movies"
    section.type = "movie"
    section.uuid = "library"
    section._server = SimpleNamespace(machineIdentifier="server")

    async def metadata(item, **_kwargs):
        return _meta(item)

    async def processed(plex_item, **_kwargs):
        if plex_item.ratingKey == "1":
            raise RuntimeError("builder failed")
        return {
            "metadata_action": "skipped",
            "poster_action": "not_due",
            "background_action": "downloaded",
            "season_poster_actions": {},
            "artwork_providers": {"background": "tmdb"},
            "artwork_selection_stages": {"background": "missing_only_relaxed"},
            "_retry_error": "retry provider",
            "_retry_failure_class": "transient",
        }

    class Tracker:
        def __init__(self):
            self.calls = []

        def record_item(self, *args):
            self.calls.append(args)

    tracker = Tracker()
    failures = []
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", processed)
    monkeypatch.setattr(processing, "load_due_item_retries", lambda *_args: {})
    monkeypatch.setattr(processing, "mark_items_started", lambda *_args: None)
    monkeypatch.setattr(
        processing,
        "record_item_failure",
        lambda *args, **kwargs: failures.append((args, kwargs)),
    )
    monkeypatch.setattr(processing, "tracker_for", lambda _config: tracker)
    summaries = {}
    with pytest.raises(processing.LibraryProcessingError, match="builder failed"):
        asyncio.run(
            processing.process_library(
                section,
                _config(tmp_path),
                feature_flags=_flags(dry_run=False, background=True),
                metadata_summaries=summaries,
            )
        )
    assert len(failures) == 2 and len(tracker.calls) == 2
    assert summaries["Movies"]["library_summary"]["artwork_automatic_relaxed"] == 1


def test_plex_progress_heartbeat_runs_before_worker_finishes(monkeypatch, tmp_path):
    _patch_library_defaults(monkeypatch)
    item = _item()
    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    release = asyncio.Event()
    continue_heartbeat = asyncio.Event()
    finish = asyncio.Event()
    original_sleep = asyncio.sleep
    sleeps = [0]

    async def metadata(*_args, **_kwargs):
        return _meta(item)

    async def processed(**_kwargs):
        await release.wait()
        continue_heartbeat.set()
        await finish.wait()
        return {"metadata_action": "skipped"}

    async def controlled_sleep(_delay):
        sleeps[0] += 1
        if sleeps[0] == 1:
            release.set()
            await continue_heartbeat.wait()
        else:
            finish.set()
        await original_sleep(0)

    class Progress:
        minimum_seconds = 1
        heartbeat_seconds = 1

        def __init__(self, *_args, **_kwargs):
            self.updates = []

        def start(self):
            return True

        def update(self, *_args, **kwargs):
            self.updates.append(kwargs)
            return True

    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", processed)
    monkeypatch.setattr(processing, "PlexMetadataProgress", Progress)
    monkeypatch.setattr(processing.asyncio, "sleep", controlled_sleep)
    asyncio.run(
        processing.process_library(
            section,
            _config(tmp_path),
            feature_flags=_flags(plex_metadata=True),
        )
    )
    assert sleeps[0] >= 1


def test_kometa_dry_run_metadata_stat_failure_and_generic_wrapping(monkeypatch, tmp_path):
    _patch_library_defaults(monkeypatch)
    item = _item()
    section = _Section([item])
    section.title = "Movies"
    section.type = "movie"
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    output = metadata_dir / "movie_metadata.yml"
    output.write_text("metadata: {}\n", encoding="utf-8")
    processing_started = [False]

    async def metadata(*_args, **_kwargs):
        return _meta(item)

    async def processed(**_kwargs):
        processing_started[0] = True
        return {}

    real_stat = Path.stat
    real_exists = Path.exists
    monkeypatch.setattr(processing, "get_plex_metadata", metadata)
    monkeypatch.setattr(processing, "process_item", processed)
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: True if candidate == output else real_exists(candidate),
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda candidate, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("unreadable"))
            if candidate == output and processing_started[0]
            else real_stat(candidate, *args, **kwargs)
        ),
    )
    summaries = {}
    asyncio.run(
        processing.process_library(
            section,
            _config(tmp_path, mode="kometa"),
            feature_flags=_flags(metadata_basic=True),
            metadata_summaries=summaries,
        )
    )
    assert summaries["Movies"]["library_summary"]["metadata_bytes"] == 0

    monkeypatch.setattr(
        processing,
        "plan_items",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad plan")),
    )
    with pytest.raises(processing.LibraryProcessingError, match="Failed to process library"):
        asyncio.run(
            processing.process_library(
                section,
                _config(tmp_path / "bad"),
                feature_flags=_flags(),
            )
        )
