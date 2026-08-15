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


def test_healthcheck_rejects_failed_or_stale_runs(tmp_path):
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
    assert check_status(status_path)[0] is False


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
