import asyncio
import logging
import os
import runpy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import docker_entrypoint
from helper import (
    cache,
    diagnostics,
    incremental,
    performance,
    plex_paths,
    provider_replay,
    reporting,
    runtime,
    state_reporting,
)


def test_incremental_naive_time_and_early_decision_paths(monkeypatch):
    aware_now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    naive_now = datetime(2026, 1, 10)  # noqa: DTZ001
    naive_old = datetime(2026, 1, 1).isoformat()  # noqa: DTZ001
    config = {
        "incremental": {
            "enabled": True,
            "full_scan_interval_hours": 24,
            "metadata_pending_recheck_hours": 1,
        },
        "assets": {"run_poster": True},
        "image_upgrades": {
            "default_days": 1,
            "movie_days": 1,
            "series_days": 1,
            "season_days": 1,
        },
    }
    assert incremental._timestamp_is_due(naive_old, timedelta(hours=1), aware_now)
    assert incremental.should_run_full_scan(
        config, now=naive_now, state={"last_full_scan": naive_old}
    )
    assert incremental.library_full_scan_decisions(config, scopes=[]) == {}
    scope = {"server_id": "server", "library_uuid": "library"}
    disabled = {"incremental": {"enabled": False}}
    assert incremental.library_full_scan_decisions(disabled, scopes=[scope]) == {
        ("server", "library"): True
    }
    assert incremental.library_full_scan_decisions(config, targeted=True, scopes=[scope]) == {
        ("server", "library"): False
    }
    monkeypatch.setattr(
        incremental,
        "load_state",
        lambda **_kwargs: {
            "libraries": {
                ("server", "library"): {
                    "last_full_scan_completed": naive_old,
                }
            }
        },
    )
    assert incremental.library_full_scan_decisions(config, now=naive_now, scopes=[scope]) == {
        ("server", "library"): True
    }
    assert incremental.timestamp_due(naive_old, 1, now=naive_now)
    causes = incremental.due_selection_causes(
        {
            "media_type": "movies",
            "metadata_pending_count": 1,
            "metadata_pending_at": naive_old,
        },
        "movies",
        config,
        feature_flags={"metadata_basic": True, "poster": False},
        now=naive_now,
    )
    assert causes == {"metadata_pending_recheck"}


def test_cache_identity_change_resets_all_artwork_observations(monkeypatch):
    document = {
        "movie": {
            "tmdb_id": "1",
            **{
                f"{asset}_{suffix}": "stale"
                for asset in ("poster", "background", "season")
                for suffix in (
                    "candidate_fingerprint",
                    "unchanged_checks",
                    "missing_checks",
                    "last_checked",
                )
            },
        }
    }
    monkeypatch.setattr(cache, "load_cache", lambda: document)
    asyncio.run(cache.meta_cache_async("movie", "2", "Movie", 2026, "movie"))
    assert document["movie"]["tmdb_id"] == "2"
    assert not any("candidate_fingerprint" in key for key in document["movie"])
    assert not any("unchanged_checks" in key for key in document["movie"])


def test_docker_status_symlink_and_main_guard_paths(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    status = config_dir / "status.json"
    status.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setenv("STATUS_FILE", str(status))
    monkeypatch.setattr(
        docker_entrypoint,
        "_set_owner",
        lambda path, *_args: calls.append(Path(path)),
    )
    docker_entrypoint.prepare_runtime_paths(config_dir, 99, 100)
    assert status in calls

    linked = tmp_path / "linked-config"
    linked.symlink_to(config_dir, target_is_directory=True)
    with pytest.raises(RuntimeError, match="Configuration directory"):
        docker_entrypoint.sync_config_template(linked, 99, 100)

    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    monkeypatch.setattr(os, "getegid", lambda: 10001)
    monkeypatch.setattr(os, "execvp", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["docker_entrypoint.py", "--healthcheck", "true"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(Path(docker_entrypoint.__file__)), run_name="__main__")
    assert caught.value.code == 0


def test_empty_diagnostics_and_support_error_report(tmp_path):
    plan = diagnostics.write_change_plan_report([], [], [], mode="kometa", base_dir=tmp_path)
    plan_text = plan.read_text(encoding="utf-8")
    assert plan_text.count("- none") >= 3
    audit = diagnostics.write_library_asset_audit_report([], [], mode="plex", base_dir=tmp_path)
    assert audit.read_text(encoding="utf-8").count("- none") >= 3
    assert diagnostics.write_artwork_gap_report(["invalid"], base_dir=tmp_path) is None
    history = diagnostics.write_destination_history_report(
        {"invalid": [], "empty": {}}, base_dir=tmp_path
    )
    assert history is None
    support = diagnostics.write_support_report(
        {"settings": {}, "plex_metadata": {}},
        validation_errors=["invalid"],
        base_dir=tmp_path,
        environ={},
    )
    assert "1 error(s)" in support.read_text(encoding="utf-8")


def test_reporting_unlink_error_keeps_processing(monkeypatch, tmp_path):
    first = tmp_path / "report-1.txt"
    second = tmp_path / "report-2.txt"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_one(path, *args, **kwargs):
        if path == first:
            raise OSError("busy")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)
    reporting.retain_diagnostic_reports(tmp_path, "report", 1)
    assert first.exists()


def test_performance_fanart_summary(caplog):
    tracker = performance.PerformanceTracker(clock=lambda: 1.0)
    tracker.increment("fanart_requests")
    with caplog.at_level(logging.INFO):
        performance.log_performance_summary(logging.getLogger("test"), tracker)
    assert "Fanart.tv requests" in caplog.text


def test_runtime_cache_relief_failure_and_kometa_preflight(monkeypatch, tmp_path):
    usage = SimpleNamespace(total=100, used=99, free=1)
    monkeypatch.setattr(runtime.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(runtime, "storage_pressure_threshold", lambda *_args: (1, 2))

    class BrokenCache:
        def relieve_space(self, *_args):
            raise AttributeError("unavailable")

    import helper.fanart
    import helper.tmdb_cache

    monkeypatch.setattr(helper.fanart, "fanart_response_cache", BrokenCache())
    monkeypatch.setattr(helper.tmdb_cache, "tmdb_response_cache", BrokenCache())
    with pytest.raises(runtime.DiskPressureError):
        runtime.ensure_storage_available({}, tmp_path)

    output = tmp_path / "kometa"
    output.mkdir()
    monkeypatch.setattr(runtime, "ensure_storage_available", lambda *args, **kwargs: args[1])
    assert runtime.validate_preflight_paths(
        {"settings": {"mode": "kometa", "path": str(output)}}, tmp_path
    )


def test_visible_mount_conventional_root(monkeypatch, tmp_path):
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("", encoding="utf-8")
    real_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: True if str(path) == "/movies" else real_is_dir(path),
    )
    assert Path("/movies") in plex_paths.visible_mount_roots(mountinfo)


def test_provider_replay_remaining_sanitizers_and_refusal(monkeypatch, tmp_path):
    assert provider_replay._redact_url("relative/path") == "relative/path"
    sanitized = provider_replay.sanitize_provider_payload(
        {
            "season_dirs": {"one": "/media/movie"},
            "values": ("one", "two"),
        }
    )
    assert sanitized["season_dirs"] == {"one": "<redacted-media-path>"}
    assert sanitized["values"] == ["one", "two"]
    monkeypatch.setattr(
        provider_replay,
        "sanitize_provider_payload",
        lambda _payload: {"token": "secret"},
    )
    with pytest.raises(ValueError, match="Unsafe replay capture"):
        provider_replay.write_sanitized_replay_capture([{"token": "secret"}], base_dir=tmp_path)


def test_state_report_empty_selection_and_terabyte_format(monkeypatch, tmp_path):
    assert state_reporting._format_bytes(2 * 1024**4) == "2.00 TiB"
    monkeypatch.setattr(
        state_reporting,
        "_database_counts",
        lambda _path: ({}, [], []),
    )
    monkeypatch.setattr(state_reporting, "find_media_state", lambda **_kwargs: [])
    for name in (
        "load_item_exceptions",
        "load_identity_overrides",
        "load_identity_reviews",
        "load_cleanup_candidates",
        "load_cleanup_history",
    ):
        monkeypatch.setattr(state_reporting, name, lambda **_kwargs: [])
    monkeypatch.setattr(state_reporting, "load_item_retries", lambda **_kwargs: [])
    monkeypatch.setattr(state_reporting, "load_library_rebinding_history", lambda **_kwargs: [])
    monkeypatch.setattr(state_reporting, "recent_job_runs", lambda **_kwargs: [])
    monkeypatch.setattr(
        state_reporting,
        "inspect_database",
        lambda *_args: {"status": "missing", "bytes": 0, "wal_bytes": 0},
    )
    report = state_reporting.write_state_report(
        rating_keys=["missing"],
        include_items=True,
        base_dir=tmp_path,
        path=tmp_path / "missing.sqlite3",
    )
    assert "Selected item records\n- none" in report.read_text(encoding="utf-8")
