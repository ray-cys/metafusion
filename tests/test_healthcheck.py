import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from healthcheck import check_status
from helper import runtime as runtime_module
from helper.runtime import (
    RuntimeStatus,
    ensure_storage_available,
    validate_runtime_paths,
)
from helper.state_db import recent_job_runs


def test_runtime_status_is_healthy_after_success(tmp_path):
    status_path = tmp_path / "status.json"
    database = tmp_path / "meta_db.sqlite3"
    status = RuntimeStatus(
        status_path, heartbeat_seconds=3600, state_database=database
    )
    status.start("scheduler")
    status.run_started()
    status.run_finished(
        True,
        library_results={
            "Movies": {
                "status": "success",
                "items_processed": 10,
                "items_succeeded": 10,
                "items_failed": 0,
            }
        },
    )
    try:
        healthy, message = check_status(status_path)
    finally:
        status.stop()

    assert healthy is True
    assert message == "idle"
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert "history" not in saved
    assert saved["version"]
    assert saved["commit"]
    recent = recent_job_runs(path=database)[-1]
    assert recent["status"] == "success"
    assert recent["library_results"]["Movies"]["items_succeeded"] == 10


def test_runtime_status_bounds_recent_job_history(tmp_path):
    status_path = tmp_path / "status.json"
    database = tmp_path / "meta_db.sqlite3"
    status = RuntimeStatus(
        status_path,
        heartbeat_seconds=3600,
        history_limit=2,
        state_database=database,
    )
    status.start("scheduler")
    try:
        for success in (True, False, True):
            status.run_started()
            status.run_finished(success, error=None if success else "failed")
    finally:
        status.stop()

    assert [entry["status"] for entry in recent_job_runs(path=database)] == [
        "failed",
        "success",
    ]


def test_healthcheck_separates_liveness_from_failed_jobs(tmp_path):
    status_path = tmp_path / "status.json"
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    status_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "state": "idle",
                "heartbeat_at": stale.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert check_status(status_path, max_heartbeat_age=60)[0] is False

    status_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "state": "failed",
                "last_run_status": "failed",
                "last_error": "scan failed",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    healthy, message = check_status(status_path)
    assert healthy is True
    assert "last run failed" in message
    assert check_status(status_path, fail_on_job_error=True)[0] is False


def test_runtime_path_preflight_creates_required_paths(tmp_path):
    config_dir = tmp_path / "config"
    kometa_dir = tmp_path / "kometa"
    validate_runtime_paths(
        {
            "settings": {
                "dry_run": False,
                "mode": "kometa",
                "path": str(kometa_dir),
            }
        },
        config_dir,
    )

    assert (config_dir / "logs").is_dir()
    assert (config_dir / "cache").is_dir()
    assert kometa_dir.is_dir()


def test_runtime_refuses_accidental_root_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module.os, "geteuid", lambda: 0)

    with pytest.raises(RuntimeError, match="refuses to run as root"):
        validate_runtime_paths(
            {"settings": {"dry_run": False, "mode": "plex"}},
            tmp_path / "config",
        )

    assert not (tmp_path / "config").exists()


def test_plex_mount_preflight_rejects_missing_mapping_destination(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(runtime_module.os, "geteuid", lambda: 99)
    with pytest.raises(RuntimeError, match="mapping destination is unavailable"):
        validate_runtime_paths(
            {
                "settings": {"dry_run": False, "mode": "plex"},
                "assets": {"run_poster": True},
                "plex": {
                    "path_mappings": [
                        f"/plex/movies=>{tmp_path / 'missing-media'}"
                    ]
                },
                "runtime": {
                    "validate_media_mounts": True,
                    "min_free_space_mb": 0,
                },
            },
            tmp_path / "config",
        )


def test_storage_preflight_rejects_low_free_space(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runtime_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1024, used=1023, free=1),
    )

    with pytest.raises(RuntimeError, match="MIN_FREE_SPACE_MB requires 10 MiB"):
        ensure_storage_available(
            {"runtime": {"min_free_space_mb": 10}},
            tmp_path,
            description="test destination",
        )
