import json
import os
from datetime import datetime, timedelta, timezone

from healthcheck import check_status
from helper.runtime import RuntimeStatus, validate_runtime_paths


def test_runtime_status_is_healthy_after_success(tmp_path):
    status_path = tmp_path / "status.json"
    status = RuntimeStatus(status_path, heartbeat_seconds=3600)
    status.start("scheduler")
    status.run_started()
    status.run_finished(True)
    try:
        healthy, message = check_status(status_path)
    finally:
        status.stop()

    assert healthy is True
    assert message == "idle"
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["history"][-1]["status"] == "success"


def test_runtime_status_bounds_recent_job_history(tmp_path):
    status_path = tmp_path / "status.json"
    status = RuntimeStatus(status_path, heartbeat_seconds=3600, history_limit=2)
    status.start("scheduler")
    try:
        for success in (True, False, True):
            status.run_started()
            status.run_finished(success, error=None if success else "failed")
    finally:
        status.stop()

    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert [entry["status"] for entry in saved["history"]] == ["failed", "success"]


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
