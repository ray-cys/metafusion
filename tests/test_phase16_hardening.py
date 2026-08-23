import asyncio
import copy
import logging
from datetime import datetime, timezone

import pytest
from PIL import Image

import metafusion
from helper import tmdb as tmdb_module
from helper.asset_registry import AssetDestinationRegistry
from helper.concurrency import bounded_callables, bounded_map, runtime_slot
from helper.config import DEFAULT_CONFIG
from helper.diagnostics import write_asset_audit_report
from helper.performance import (
    PerformanceTracker,
    begin_performance_tracking,
    log_performance_summary,
    reset_performance_tracking,
    tracker_for,
)
from helper.runtime import validate_preflight_paths
from modules import builder
from modules.processing import cleanup_inventory_errors
from modules.utils import get_asset_path


def test_bounded_map_preserves_order_and_caps_worker_count():
    state = {"active": 0, "maximum": 0}

    async def operation(value):
        state["active"] += 1
        state["maximum"] = max(state["maximum"], state["active"])
        await asyncio.sleep(0)
        state["active"] -= 1
        return value * 2

    result = asyncio.run(bounded_map(operation, range(20), 3))

    assert result == [value * 2 for value in range(20)]
    assert state["maximum"] == 3
    assert asyncio.run(bounded_map(operation, [], 3)) == []


def test_bounded_callables_cancels_siblings_after_failure():
    cancelled = []

    async def fail():
        raise RuntimeError("injected failure")

    async def wait():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def run():
        try:
            await bounded_callables([wait, fail], 2)
        except RuntimeError:
            return
        raise AssertionError("failure was not propagated")

    asyncio.run(run())
    assert cancelled == [True]


def test_runtime_slots_are_shared_across_nested_work_queues():
    config = {"runtime": {"max_concurrency": 2}}
    state = {"active": 0, "maximum": 0}

    async def request():
        async with runtime_slot(config, "tmdb"):
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
            await asyncio.sleep(0.01)
            state["active"] -= 1

    async def run():
        await asyncio.gather(
            bounded_callables([request] * 6, 6),
            bounded_callables([request] * 6, 6),
        )

    asyncio.run(run())
    assert state["maximum"] == 2


def test_performance_summary_is_value_safe(caplog):
    clock = iter([100.0, 102.0]).__next__
    tracker = PerformanceTracker(clock=clock)
    token = begin_performance_tracking(tracker)
    try:
        tracker.increment("tmdb_cache_hits", 3)
        tracker.increment("tmdb_cache_misses")
        tracker.increment("tmdb_requests", 2)
        tracker.increment("tmdb_retries")
        tracker.increment("tmdb_rate_limits")
        tracker.increment("tmdb_rate_limit_wait_seconds", 4)
        tracker.add_duration("plex_inventory", 0.5)
        tracker.add_duration("library_processing", 1.5)
        tracker.record_item("Movies", "42", 1.25)
    finally:
        reset_performance_tracking(token)

    with caplog.at_level(logging.DEBUG):
        log_performance_summary(logging.getLogger("performance-test"), tracker)

    assert "Items/minute: 30.0" in caplog.text
    assert "Cache hits: 3" in caplog.text
    assert "Cache hit rate: 75.0%" in caplog.text
    assert "Slow item | Library: Movies | Plex rating key: 42" in caplog.text
    slow_record = next(record for record in caplog.records if "Slow item" in record.message)
    assert slow_record.levelno == logging.DEBUG
    assert "/media" not in caplog.text
    assert tracker_for({"_performance_tracker": tracker}) is tracker
    assert log_performance_summary(logging.getLogger("none"), None) is None


def test_preflight_path_validation_never_creates_destinations(monkeypatch, tmp_path):
    calls = []

    def inspect(_config, path, **kwargs):
        calls.append((str(path), kwargs))
        return path

    monkeypatch.setattr("helper.runtime.ensure_storage_available", inspect)
    config = {
        "settings": {"mode": "plex"},
        "runtime": {"validate_media_mounts": True},
        "assets": {"run_poster": True},
        "plex": {"path_mappings": ["/plex/movies=>/media/movies"]},
    }

    assert validate_preflight_paths(config, tmp_path) is True
    assert [path for path, _kwargs in calls] == [
        str(tmp_path),
        "/media/movies",
    ]
    assert all(not kwargs.get("create", False) for _path, kwargs in calls)


def test_preflight_cli_reports_connectors_without_starting_runtime(monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"].update(url="http://plex:32400", token="secret")
    config["tmdb"]["api_key"] = "secret"
    config["plex_libraries"] = ["Movies"]

    async def preflight(_config):
        return {
            "plex_version": "1.42.0",
            "libraries": ["Movies"],
            "available_count": 1,
        }

    monkeypatch.setattr(metafusion, "load_config_file", lambda **_kwargs: (config, {}))
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: True)
    monkeypatch.setattr(metafusion, "connector_preflight", preflight)

    assert metafusion.main(["--preflight"]) == 0


def test_connector_preflight_reuses_connection_and_rejects_missing_library(
    monkeypatch,
):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    plex = type("SimplePlex", (), {"version": "1.42"})()

    async def connectors(_config, _session):
        return plex

    available = [{"title": "Movies", "type": "movie"}]
    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(metafusion, "preflight_connectors", connectors)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: (
            [type("Section", (), {"title": "Movies"})()],
            ["Movies"],
            available,
        ),
    )

    result = asyncio.run(metafusion.connector_preflight({"runtime": {}}))
    assert result == {
        "plex_version": "1.42",
        "libraries": ["Movies"],
        "available_count": 1,
        "library_discovery": "explicit",
        "path_advice": {"records": [], "suggestions": []},
    }

    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: ([], ["Missing"], available),
    )
    with pytest.raises(RuntimeError, match="Missing"):
        asyncio.run(metafusion.connector_preflight({"runtime": {}}))


def test_preflight_cli_redacts_connector_secrets(monkeypatch, capsys):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"].update(url="http://plex:32400", token="plex-secret")
    config["tmdb"]["api_key"] = "tmdb-secret"
    config["plex_libraries"] = ["Movies"]

    async def fail(_config):
        raise RuntimeError("plex-secret and tmdb-secret rejected")

    monkeypatch.setattr(metafusion, "load_config_file", lambda **_kwargs: (config, {}))
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: True)
    monkeypatch.setattr(metafusion, "connector_preflight", fail)

    assert metafusion.main(["--preflight"]) == 1
    error = capsys.readouterr().err
    assert "plex-secret" not in error
    assert "tmdb-secret" not in error
    assert "***" in error


def test_asset_audit_report_is_bounded_and_omits_paths(tmp_path):
    records = [
        {
            "library": "Movies",
            "media_type": "Movie",
            "title": "Example (2020)",
            "asset_type": "poster",
            "action": "would_consider_upgrade",
            "ownership": "managed",
            "existing_width": 1000,
            "existing_height": 1500,
            "candidate": {
                "width": 2000,
                "height": 3000,
                "language": "en",
                "vote": 6.5,
            },
        }
    ]
    gaps = [
        {
            "library": "TV",
            "media_type": "TV Show",
            "title": "Missing (2024)",
            "asset_type": "poster",
            "category": "identity_rejected",
            "detail": "year mismatch",
        }
    ]

    first = write_asset_audit_report(records, gaps, base_dir=tmp_path, retention=1)
    second = write_asset_audit_report(records, gaps, base_dir=tmp_path, retention=1)
    contents = second.read_text(encoding="utf-8")

    assert "would_consider_upgrade" in contents
    assert "candidate 2000x3000" in contents
    assert "identity_rejected" in contents
    assert "/media" not in contents
    assert not first.exists()

    empty = write_asset_audit_report([], base_dir=tmp_path, retention=2)
    assert "Candidates: 0" in empty.read_text(encoding="utf-8")
    assert "- none" in empty.read_text(encoding="utf-8")


def test_asset_audit_assesses_missing_managed_unmanaged_and_collision(
    monkeypatch, tmp_path
):
    config = {
        "settings": {
            "mode": "kometa",
            "path": str(tmp_path),
            "dry_run": True,
        },
        "assets": {"update_policy": "managed"},
        "_execution": {"asset_audit": True},
        "_library_name": "Movies",
        "_asset_audit_records": [],
        "_asset_destination_registry": AssetDestinationRegistry(),
    }
    meta = {
        "library_type": "movie",
        "movie_path": "Example (2020)",
        "tmdb_id": "10",
    }
    candidate = {
        "file_path": "/candidate.jpg",
        "iso_639_1": "en",
        "width": 2000,
        "height": 3000,
        "vote_average": 7.0,
    }

    asyncio.run(
        builder._audit_asset_candidate(
            config,
            meta,
            "movie:one",
            candidate,
            media_type="Movie",
            full_title="Example (2020)",
            asset_type="poster",
        )
    )
    assert config["_asset_audit_records"][-1]["action"] == "would_download"

    asset = tmp_path / "assets" / "movie" / "Example (2020)" / "poster.jpg"
    asset.parent.mkdir(parents=True)
    Image.new("RGB", (1000, 1500)).save(asset)
    monkeypatch.setattr(builder, "asset_write_allowed", lambda *_args, **_kwargs: (True, "managed"))
    config["_asset_destination_registry"] = AssetDestinationRegistry()
    asyncio.run(
        builder._audit_asset_candidate(
            config,
            meta,
            "movie:one",
            candidate,
            media_type="Movie",
            full_title="Example (2020)",
            asset_type="poster",
        )
    )
    assert config["_asset_audit_records"][-1]["action"] == "would_consider_upgrade"

    monkeypatch.setattr(
        builder,
        "asset_write_allowed",
        lambda *_args, **_kwargs: (False, "modified"),
    )
    config["_asset_destination_registry"] = AssetDestinationRegistry()
    asyncio.run(
        builder._audit_asset_candidate(
            config,
            meta,
            "movie:one",
            candidate,
            media_type="Movie",
            full_title="Example (2020)",
            asset_type="poster",
        )
    )
    assert config["_asset_audit_records"][-1]["action"] == "preserve_unmanaged"

    config["_asset_destination_registry"] = AssetDestinationRegistry()
    first_meta = {**meta, "tmdb_id": "10"}
    second_meta = {**meta, "tmdb_id": "11"}
    for key, current in (("movie:one", first_meta), ("movie:two", second_meta)):
        asyncio.run(
            builder._audit_asset_candidate(
                config,
                current,
                key,
                candidate,
                media_type="Movie",
                full_title="Example (2020)",
                asset_type="poster",
            )
        )
    assert config["_asset_audit_records"][-1]["action"] == "collision"


def test_resolving_a_kometa_asset_path_does_not_create_directories(tmp_path):
    root = tmp_path / "kometa"
    result = get_asset_path(
        {"settings": {"mode": "kometa", "path": str(root), "dry_run": True}},
        {"library_type": "movie", "movie_path": "Example (2020)"},
        "poster",
    )

    assert result == root / "assets" / "movie" / "Example (2020)" / "poster.jpg"
    assert not root.exists()


def test_tmdb_mixed_transient_failures_are_retried_with_metrics(monkeypatch):
    class Response:
        content = None

        def __init__(self, status, payload=None, retry_after=None):
            self.status = status
            self.payload = payload
            self.headers = (
                {"Retry-After": str(retry_after)} if retry_after is not None else {}
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return "temporary"

    class Session:
        def __init__(self):
            self.responses = [
                Response(500),
                Response(429, retry_after=0),
                Response(200, {"ok": True}),
            ]

        def get(self, *_args, **_kwargs):
            return self.responses.pop(0)

    async def no_sleep(_seconds):
        return None

    tracker = PerformanceTracker()
    token = begin_performance_tracking(tracker)
    monkeypatch.setattr(tmdb_module.asyncio, "sleep", no_sleep)
    try:
        result = asyncio.run(
            tmdb_module.tmdb_api_request(
                {
                    "tmdb": {"api_key": "key"},
                    "runtime": {"max_concurrency": 2},
                },
                "configuration",
                retries=3,
                session=Session(),
                cache=False,
            )
        )
    finally:
        reset_performance_tracking(token)

    snapshot = tracker.snapshot()["counters"]
    assert result == {"ok": True}
    assert snapshot["tmdb_requests"] == 3
    assert snapshot["tmdb_retries"] == 2
    assert snapshot["tmdb_rate_limits"] == 1


def test_synthetic_production_inventory_is_complete_and_linear():
    metadata = []
    for index in range(2000):
        metadata.append(
            {
                "library_type": "movie",
                "title": f"Movie {index}",
                "year": 2000 + index % 25,
                "ratingKey": f"m{index}",
                "movie_path": f"Movie {index}",
            }
        )

    seasons_remaining = 1000
    episodes_remaining = 8000
    for index in range(300):
        show_seasons = min(4, seasons_remaining)
        seasons_remaining -= show_seasons
        inventory = {}
        for season in range(show_seasons):
            count = min(8, episodes_remaining)
            episodes_remaining -= count
            inventory[season] = list(range(1, count + 1))
        metadata.append(
            {
                "library_type": "tv",
                "title": f"Show {index}",
                "year": 2000 + index % 25,
                "ratingKey": f"t{index}",
                "show_path": f"Show {index}",
                "seasons_episodes": inventory,
            }
        )

    assert len(metadata) == 2300
    assert sum(len(item.get("seasons_episodes", {})) for item in metadata) == 1000
    assert sum(
        len(episodes)
        for item in metadata
        for episodes in item.get("seasons_episodes", {}).values()
    ) == 8000
    assert cleanup_inventory_errors(
        metadata,
        {
            "poster": True,
            "background": True,
            "season": True,
            "metadata_basic": True,
        },
    ) == []


def test_catch_up_uses_local_timezone_across_dst_offset_change():
    now = datetime.fromisoformat("2026-11-01T07:30:00-05:00")
    missed = metafusion.missed_schedule_due(
        ["06:00"], [], max_hours=3, now=now
    )

    assert missed == datetime.fromisoformat("2026-11-01T06:00:00-05:00")
    assert missed.tzinfo == timezone(now.utcoffset())
