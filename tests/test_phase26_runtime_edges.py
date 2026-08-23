import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from helper import concurrency, incremental

GIB = 1024 ** 3


def _resources(cpus=4, memory_gib=4, current=None):
    return concurrency.RuntimeResources(
        cpu_cores=float(cpus),
        memory_limit_bytes=int(memory_gib * GIB),
        memory_current_path=current,
    )


def test_runtime_resource_fallbacks_and_cgroup_v1(tmp_path, monkeypatch):
    class BrokenMemory:
        @property
        def total(self):
            raise OSError("unavailable")

    monkeypatch.setattr(concurrency.psutil, "virtual_memory", lambda: BrokenMemory())
    values = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 1024}
    monkeypatch.setattr(concurrency.os, "sysconf", lambda key: values[key])
    assert concurrency._host_memory_bytes() == 4096 * 1024

    monkeypatch.setattr(
        concurrency.os, "sysconf", lambda _key: (_ for _ in ()).throw(OSError())
    )
    assert concurrency._host_memory_bytes() == 4 * GIB

    root = tmp_path / "cgroup"
    (root / "cpu").mkdir(parents=True)
    (root / "memory").mkdir()
    (root / "cpu" / "cpu.cfs_quota_us").write_text("100000", encoding="utf-8")
    (root / "cpu" / "cpu.cfs_period_us").write_text("50000", encoding="utf-8")
    (root / "memory" / "memory.limit_in_bytes").write_text(
        str(2 * GIB), encoding="utf-8"
    )
    monkeypatch.setattr(concurrency, "_host_memory_bytes", lambda: 8 * GIB)
    monkeypatch.setattr(concurrency.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        concurrency.os,
        "sched_getaffinity",
        lambda _pid: (_ for _ in ()).throw(OSError()),
        raising=False,
    )
    detected = concurrency.detect_runtime_resources(root)
    assert detected.cpu_cores == 2
    assert detected.memory_limit_bytes == 2 * GIB
    assert detected.memory_current_path.name == "memory.usage_in_bytes"

    (root / "cpu.max").write_text("bad zero", encoding="utf-8")
    (root / "memory.max").write_text("bad", encoding="utf-8")
    detected = concurrency.detect_runtime_resources(root)
    assert detected.cpu_cores == 2
    assert detected.memory_limit_bytes == 8 * GIB
    assert concurrency._configured_ceiling({"runtime": {"max_concurrency": "bad"}}) == 0


def test_pressure_probe_handles_process_and_memory_failures(tmp_path, monkeypatch):
    class BrokenProcess:
        def cpu_percent(self, interval=None):
            raise OSError("no sample")

    monkeypatch.setattr(concurrency.psutil, "Process", BrokenProcess)
    monkeypatch.setattr(
        concurrency.psutil,
        "virtual_memory",
        lambda: (_ for _ in ()).throw(OSError()),
    )
    probe = concurrency.ResourcePressureProbe(_resources())
    assert probe() == {"cpu_percent": 0.0, "memory_percent": 0.0}

    class LaterBrokenProcess:
        calls = 0

        def cpu_percent(self, interval=None):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("no sample")
            return 0

    monkeypatch.setattr(concurrency.psutil, "Process", LaterBrokenProcess)

    current = tmp_path / "memory.current"
    current.write_text("invalid", encoding="utf-8")
    probe = concurrency.ResourcePressureProbe(_resources(current=current))
    assert probe()["memory_percent"] == 0.0


def test_adaptive_controller_lifecycle_slow_response_and_exception(caplog):
    clock = [100.0]

    async def run():
        controller, token = concurrency.begin_adaptive_concurrency(
            {"runtime": {"max_concurrency": "bad"}},
            resources=_resources(cpus=8, memory_gib=8),
            clock=lambda: clock[0],
            pressure_probe=lambda: (_ for _ in ()).throw(OSError()),
            pressure_interval=0,
        )
        assert concurrency.concurrency_ceiling({}, "item") == controller.ceiling("item")
        assert controller.lane("unknown") is controller.lane("nested")

        lease = await controller.lane("plex").acquire()
        await controller.release("plex", lease, 6.0)
        assert controller.snapshot()["lanes"]["plex"]["slow_responses"] == 1

        lease = await controller.lane("item").acquire()
        clock[0] += 1
        await controller.release("item", lease, 0.1)
        assert controller.snapshot()["pressure"] == {}

        with pytest.raises(TimeoutError):
            async with concurrency.runtime_slot({}, "nested"):
                raise TimeoutError("timeout")

        snapshot = concurrency.finish_adaptive_concurrency(controller, token)
        assert snapshot["lanes"]["nested"]["failures"] == 1

        first = concurrency.adaptive_controller({})
        second = concurrency.adaptive_controller({})
        assert first is second

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "Adaptive mode started" in caplog.text
    assert "Final limits" in caplog.text


def test_lane_half_open_collision_and_adjustment_messages(caplog):
    clock = [0.0]

    async def run():
        events = []
        lane = concurrency.AdaptiveLane(
            "tmdb",
            2,
            4,
            clock=lambda: clock[0],
            failure_threshold=1,
            adjustment_callback=lambda *args: events.append(args),
        )
        lease = await lane.acquire()
        lease.failure("outage", immediate_open=True, cooldown=2)
        await lane.release(lease, 0.1)
        clock[0] = 3
        probe = await lane.acquire()
        with pytest.raises(concurrency.CircuitOpenError):
            await lane.acquire()
        await lane.release(probe, 0.1)
        await lane.reduce_for_pressure("pressure")
        assert lane.snapshot()["average_seconds"] > 0
        assert any(event[-1] == "circuit_closed" for event in events)

        controller = concurrency.AdaptiveConcurrencyController(
            {}, resources=_resources(), clock=lambda: clock[0]
        )
        controller._record_adjustment("tmdb", 2, 2, "circuit_open:3.0s")
        controller._record_adjustment("tmdb", 2, 2, "circuit_closed")
        controller._record_adjustment("tmdb", 2, 3, "healthy_window")

    with caplog.at_level(logging.INFO):
        asyncio.run(run())
    assert "circuit opened" in caplog.text
    assert "circuit closed" in caplog.text
    assert "Lane: tmdb" in caplog.text
    assert "Previous limit: 2 | Current limit: 3 | Reason: healthy_window" in caplog.text


def _incremental_config():
    return {
        "incremental": {
            "enabled": True,
            "full_scan_interval_hours": 24,
            "metadata_pending_recheck_hours": 1,
        },
        "assets": {
            "run_poster": True,
            "run_season": True,
            "run_background": True,
        },
        "image_upgrades": {
            "default_days": 30,
            "movie_days": 30,
            "series_days": 15,
            "season_days": 10,
        },
        "plex_metadata": {"recheck_days": 7},
    }


def test_incremental_timestamp_and_adaptive_interval_edges(monkeypatch):
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert incremental.item_updated_at(
        SimpleNamespace(updatedAt=datetime(2026, 1, 1))  # noqa: DTZ001
    )
    assert incremental.child_inventory_fingerprint(
        {"library_type": "tv", "childCount": "bad", "leafCount": 10}
    )
    assert incremental.child_inventory_fingerprint({"type": "tv"}) is None
    assert incremental._timestamp_is_due("bad", timedelta(days=1), now)
    assert incremental.timestamp_due("bad", 1, now)
    assert incremental.timestamp_due(now.isoformat(), "bad", now)
    assert not incremental.timestamp_due(now.isoformat(), 0, now)
    assert incremental.timestamp_due(None, 1, now)
    assert not incremental.timestamp_due(
        datetime(2026, 1, 9).isoformat(), 2, now  # noqa: DTZ001
    )
    assert incremental.adaptive_artwork_days({}, "poster", "bad") == "bad"
    assert incremental.adaptive_artwork_days({}, "poster", 0) == 0
    assert incremental.adaptive_artwork_days(
        {"poster_missing_checks": 20}, "poster", 90
    ) == 60
    assert incremental.adaptive_artwork_days(
        {"poster_unchanged_checks": 3}, "poster", 30
    ) == 180


def test_incremental_due_matrix_and_state_wrappers(tmp_path, monkeypatch):
    now = datetime(2026, 1, 10)  # noqa: DTZ001
    config = _incremental_config()
    cached = {
        "media_type": "show",
        "metadata_pending_count": "bad",
        "plex_metadata_last_checked": "bad",
        "poster_last_checked": "bad",
        "background_last_checked": "bad",
        "season_last_checked": "bad",
    }
    flags = {
        "metadata_basic": True,
        "plex_metadata": True,
        "poster": True,
        "background": True,
        "season": True,
    }
    causes = incremental.due_selection_causes(
        cached, "shows", config, feature_flags=flags, now=now
    )
    assert causes == {
        "plex_metadata_recheck",
        "poster_refresh_due",
        "background_refresh_due",
        "season_refresh_due",
    }
    assert incremental.due_selection_causes(cached, "clip", config) == set()
    assert incremental.image_upgrade_due(
        cached, "tv", config, feature_flags=flags, now=now
    )
    assert incremental.enabled_work_reasons(
        "movies", {"metadata_basic": False}
    ) == {"identity"}
    assert incremental.enabled_work_reasons(
        "shows", {"metadata_basic": False, "season": True}
    ) == {"season"}

    calls = []
    monkeypatch.setattr(
        incremental,
        "persist_scan_started",
        lambda scopes, **kwargs: calls.append(("start", scopes, kwargs)) or True,
    )
    monkeypatch.setattr(
        incremental,
        "persist_scan_complete",
        lambda scopes, **kwargs: calls.append(("complete", scopes, kwargs)) or True,
    )
    scopes = [{"library_name": "Movies"}]
    assert not incremental.mark_library_scan_started(scopes, True, dry_run=True)
    assert not incremental.mark_library_scan_complete(scopes, True, dry_run=True)
    assert incremental.mark_library_scan_started(scopes, False, path=tmp_path / "db", now="x")
    assert incremental.mark_library_scan_complete(scopes, True, path=tmp_path / "db", now="y")
    assert [call[0] for call in calls] == ["start", "complete"]


def test_incremental_selection_causes_cover_new_changed_retry_and_scoped_cache():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    items = [
        SimpleNamespace(ratingKey="1", type="movie", updatedAt=None),
        SimpleNamespace(ratingKey="2", type="movie", updatedAt=now),
    ]

    class ScopedCache:
        def entries_for_scope(self, *_args, **_kwargs):
            return {
                "movie:1": {
                    "rating_key": "1",
                    "plex_updated_at": "old",
                    "config_fingerprint": "old",
                }
            }

        def values(self):
            raise AssertionError("scoped path expected")

    planned = incremental.plan_items(
        items,
        ScopedCache(),
        "new",
        config=_incremental_config(),
        feature_flags={"metadata_basic": True},
        server_id="server",
        library_uuid="library",
        retry_rating_keys=["1"],
        change_rating_keys=["1"],
    )
    by_key = {item.item.ratingKey: item for item in planned}
    assert by_key["1"].selection_causes == {
        "tmdb_change_detected",
        "deferred_retry_due",
        "missing_plex_update_marker",
        "configuration_changed",
    }
    assert by_key["2"].selection_causes == {"new_rating_key"}
