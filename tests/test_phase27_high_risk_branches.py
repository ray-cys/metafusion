import asyncio
import copy
from types import SimpleNamespace

import pytest

from helper import asset_registry, concurrency, config_impact, provider_mappings, runtime
from helper import config as config_module
from helper.config import DEFAULT_CONFIG


def test_configuration_policy_boundaries_and_equivalent_impact(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(
        config_module,
        "log_config_event",
        lambda event, **values: events.append((event, values)),
    )
    enhanced = copy.deepcopy(DEFAULT_CONFIG)
    enhanced["settings"]["mode"] = "kometa"
    enhanced["metadata"].update(run_basic=True, run_enhanced=True)
    config_module.get_disabled_features(enhanced, None)
    assert events[-1][1]["metadata"] == "Enhanced"

    disabled = copy.deepcopy(DEFAULT_CONFIG)
    disabled["settings"]["mode"] = "plex"
    disabled["plex_metadata"]["enabled"] = False
    disabled["metadata"].update(run_basic=True, run_enhanced=True)
    config_module.get_disabled_features(disabled, None)
    assert events[-1][1]["metadata"] == "Disabled"

    invalid = copy.deepcopy(DEFAULT_CONFIG)
    invalid["settings"].update(mode="kometa", path=str(tmp_path))
    invalid["compatibility"]["profile"] = "plex-api-v1"
    invalid["cleanup"].update(confirmation_scans=101, grace_hours=8761)
    errors = config_module.validate_config(invalid)
    assert any("requires Plex mode" in error for error in errors)
    assert any("confirmation_scans" in error for error in errors)
    assert any("grace_hours" in error for error in errors)

    invalid["cleanup"].update(confirmation_scans="bad", grace_hours="bad")
    errors = config_module.validate_config(invalid)
    assert any("must be an integer" in error for error in errors)
    assert any("must be numeric" in error for error in errors)
    assert config_module.report_retention({"output": None}) == 10

    _result, report = config_impact.write_config_impact_report(
        enhanced, copy.deepcopy(enhanced), base_dir=tmp_path
    )
    assert "effective configurations are equivalent" in report.read_text(encoding="utf-8")


def test_provider_mapping_invalid_and_empty_boundaries():
    assert provider_mappings._normalized_split_mapping(
        {"seasons": {1: "bad", 2: {"tmdb_id": None}}}
    ) is None
    assert provider_mappings._split_mapping_catalog(
        {"tmdb": {"split_series_mappings": []}}
    )
    assert provider_mappings._episode_pair({"season": "bad", "episode": 1}) is None
    assert provider_mappings._episode_pair(3) is None

    errors = provider_mappings.validate_provider_mapping_config(
        {
            "split_series_mappings": {
                "tmdb:1": {"seasons": {}},
                "tvdb:2": {
                    "seasons": {
                        1: {"tmdb_id": 0, "season_number": -1},
                    }
                },
            }
        }
    )
    assert any("non-empty mapping" in error for error in errors)
    assert any("positive tmdb_id" in error for error in errors)


def test_asset_registry_records_and_unverifiable_checksum(tmp_path, monkeypatch):
    destination = tmp_path / "poster.jpg"
    destination.write_bytes(b"poster")
    registry = asset_registry.AssetDestinationRegistry(
        [
            {
                "cache_key": "one",
                "destination": str(destination),
                "media_type": "movie",
                "tmdb_id": "1",
                "asset_type": "poster",
                "source_path": "/poster",
            }
        ]
    )
    assert registry.records_for(destination)[0]["cache_key"] == "one"
    monkeypatch.setattr(registry, "_current_checksum", lambda _path: None)
    assert registry.shared_checksum(
        "two",
        destination,
        media_type="movie",
        tmdb_id="1",
        asset_type="poster",
        source_path="/poster",
    ) is None


def test_runtime_resource_parse_pressure_and_waiting_circuit(monkeypatch, tmp_path):
    values = {
        "cpu.max": None,
        "cpu/cpu.cfs_quota_us": "broken",
        "cpu/cpu.cfs_period_us": "0",
        "memory.max": None,
        "memory/memory.limit_in_bytes": "broken",
    }
    monkeypatch.setattr(
        concurrency,
        "_read_text",
        lambda path: values.get(str(path).replace(str(tmp_path) + "/", "")),
    )
    resources = concurrency.detect_runtime_resources(tmp_path)
    assert resources.cpu_cores > 0
    assert resources.memory_limit_bytes > 0

    zero_memory = concurrency.RuntimeResources(1, 0, tmp_path / "current")
    (tmp_path / "current").write_text("1", encoding="utf-8")
    assert concurrency.ResourcePressureProbe(zero_memory)()["memory_percent"] == 0

    async def waiting_rejection():
        clock = [10.0]
        lane = concurrency.AdaptiveLane("tmdb", 1, 1, clock=lambda: clock[0])
        lane.active = 1
        waiter = asyncio.create_task(lane.acquire())
        await asyncio.sleep(0)
        async with lane.condition:
            lane.open_until = 20.0
            lane.condition.notify_all()
        with pytest.raises(concurrency.CircuitOpenError):
            await waiter
        return lane.rejections

    assert asyncio.run(waiting_rejection()) == 1


def test_runtime_path_errors_cache_relief_and_empty_release(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
    )
    with pytest.raises(RuntimeError, match="Unable to inspect"):
        runtime.ensure_storage_available({}, tmp_path)

    usage = SimpleNamespace(total=100, used=99, free=1)
    calls = []
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(runtime, "storage_pressure_threshold", lambda *_args: (1, 1))
    assert runtime.ensure_storage_available({}, tmp_path) == tmp_path

    class Cache:
        def relieve_space(self, *_args):
            calls.append(True)
            usage.free = 2

    import helper.fanart
    import helper.tmdb_cache

    monkeypatch.setattr(helper.fanart, "fanart_response_cache", Cache())
    monkeypatch.setattr(helper.tmdb_cache, "tmdb_response_cache", Cache())
    usage.free = 0
    assert runtime.ensure_storage_available({}, tmp_path) == tmp_path
    assert calls == [True]

    monkeypatch.setattr(runtime.os, "geteuid", lambda: 99, raising=False)
    real_write = runtime.Path.write_text
    monkeypatch.setattr(
        runtime.Path,
        "write_text",
        lambda self, *args, **kwargs: (
            (_ for _ in ()).throw(OSError("read only"))
            if self.name == ".metafusion-write-test"
            else real_write(self, *args, **kwargs)
        ),
    )
    with pytest.raises(RuntimeError, match="Required path is not writable"):
        runtime.validate_runtime_paths(
            {"settings": {"mode": "plex"}}, tmp_path
        )

    lock = runtime.JobRunLock(tmp_path / "run.lock")
    assert lock.release() is None
    invalid_status = tmp_path / "invalid-status.json"
    invalid_status.write_text("[]", encoding="utf-8")
    assert runtime.RuntimeStatus(invalid_status)._data == {}
