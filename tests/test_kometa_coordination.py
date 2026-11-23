import argparse
import fcntl
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helper.kometa_coordination import acquire_kometa_output_lock
from helper.runtime import JobAlreadyRunningError

spec = importlib.util.spec_from_file_location(
    "coordinator", Path(__file__).parents[1] / "tools/kometa_coordinator.py"
)
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)


def test_producer_and_consumer_share_exact_lock(tmp_path):
    config = {"settings": {"mode": "kometa", "path": str(tmp_path)}}
    producer = acquire_kometa_output_lock(config)
    with pytest.raises(JobAlreadyRunningError):
        acquire_kometa_output_lock(config)
    consumer = (tmp_path / ".metafusion/output.lock").open("a+")
    with pytest.raises(BlockingIOError):
        fcntl.flock(consumer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    producer.release()
    fcntl.flock(consumer, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(JobAlreadyRunningError):
        acquire_kometa_output_lock(config)
    consumer.close()
    acquire_kometa_output_lock(config).release()
    assert acquire_kometa_output_lock({"settings": {"dry_run": True}}) is None
    assert acquire_kometa_output_lock({"settings": {"mode": "plex"}}) is None


def test_native_launcher_one_shot_no_kometa_credentials(tmp_path):
    child = tmp_path / "child.py"
    child.write_text('import sys; assert sys.argv[1:] == ["--run"]; print("fixture completed")')
    assert coordinator.main(["--root", str(tmp_path), "--script", str(child), "--once"]) == 0
    result = json.loads((tmp_path / ".metafusion/kometa-coordinator.json").read_text())
    assert result["state"] == "idle" and result["last_exit"] == 0
    assert coordinator.next_boundary(datetime(2026, 11, 23, 23, 55, tzinfo=timezone.utc)).day == 24


def test_consumer_wait_is_bounded_and_does_not_start_job(tmp_path):
    lock = acquire_kometa_output_lock({"settings": {"mode": "kometa", "path": str(tmp_path)}})
    args = argparse.Namespace(
        root=str(tmp_path),
        wait_seconds=0,
        python=sys.executable,
        script="not-executed",
        kometa_args=[],
    )
    assert coordinator.run_once(args) == 75
    lock.release()


def test_missing_output_and_bounded_wait_retry(tmp_path, monkeypatch):
    from helper import kometa_coordination as module

    assert module.acquire_kometa_output_lock({"settings": {}}) is None
    acquired = acquire_kometa_output_lock({"settings": {"mode": "kometa", "path": str(tmp_path)}})
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: acquired.release())
    retry = module.acquire_kometa_output_lock(
        {"settings": {"mode": "kometa", "path": str(tmp_path)}}, wait_seconds=10
    )
    retry.release()


def test_runtime_status_exposes_native_reconciliation_metrics(tmp_path):
    from helper.runtime import RuntimeStatus

    status = RuntimeStatus(tmp_path / "status.json")
    status.run_finished(True, metrics={"kometa_reconciliation": {"deferred": 1, "restored": 0}})
    assert json.loads(status.path.read_text())["metrics"]["kometa_reconciliation"]["deferred"] == 1


def test_ephemeral_status_recovers_completed_history_not_liveness(tmp_path):
    from helper.runtime import RuntimeStatus
    from helper.state_db import record_job_run

    database = tmp_path / "state.sqlite3"
    record_job_run(
        mode="scheduler",
        started_at="2026-11-23T00:00:00+00:00",
        finished_at="2026-11-23T00:10:00+00:00",
        status="success",
        path=database,
    )
    record_job_run(
        mode="scheduler",
        started_at="2026-11-23T04:00:00+00:00",
        finished_at="2026-11-23T04:10:00+00:00",
        status="failed",
        error="provider failed",
        metrics={"kometa_reconciliation": {"status": "deferred"}},
        path=database,
    )
    restored = RuntimeStatus(tmp_path / "missing-status.json", state_database=database)
    assert restored._data["last_run_status"] == "failed"
    assert restored._data["last_error"] == "provider failed"
    assert restored._data["last_success"] == "2026-11-23T00:10:00+00:00"
    assert "pid" not in restored._data and "heartbeat_at" not in restored._data
    assert restored._data["metrics"]["kometa_reconciliation"]["status"] == "deferred"


def test_ephemeral_status_missing_failed_only_and_broken_history(tmp_path, monkeypatch):
    from helper import runtime
    from helper.state_db import StateDatabaseError

    database = tmp_path / "state.sqlite3"
    assert runtime.RuntimeStatus(tmp_path / "missing", state_database=database)._data == {}
    monkeypatch.setattr(
        runtime, "recent_job_runs", lambda **kwargs: [{"status": "failed", "error": "failed"}]
    )
    assert (
        runtime.RuntimeStatus(tmp_path / "missing", state_database=database)._data["last_success"]
        is None
    )
    monkeypatch.setattr(
        runtime,
        "recent_job_runs",
        lambda **kwargs: (_ for _ in ()).throw(StateDatabaseError("fixture")),
    )
    assert runtime.RuntimeStatus(tmp_path / "missing", state_database=database)._data == {}


def test_existing_runtime_status_is_preserved_before_history_fallback(tmp_path):
    from helper.runtime import RuntimeStatus

    path = tmp_path / "status.json"
    original = {"last_run_status": "success", "last_success": "2026-11-23T04:00:00+00:00"}
    path.write_text(json.dumps(original))
    assert RuntimeStatus(path, state_database=tmp_path / "missing.sqlite3")._data == original


def test_existing_lock_mode_and_inode_survive_both_writers(tmp_path, monkeypatch):
    config = {"settings": {"path": str(tmp_path)}}
    path = tmp_path / ".metafusion/output.lock"
    path.parent.mkdir()
    path.touch()
    path.chmod(0o660)
    before = path.stat()
    acquire_kometa_output_lock(config).release()
    child = tmp_path / "child.py"
    child.write_text("pass")
    monkeypatch.setattr(coordinator.time, "sleep", lambda _: None)
    args = argparse.Namespace(root=tmp_path, script=str(child), python=sys.executable,
                              wait_seconds=0, kometa_args=[])
    assert coordinator.run_once(args) == 0
    after = path.stat()
    assert (after.st_ino, after.st_mode, after.st_uid, after.st_gid) == (
        before.st_ino, before.st_mode, before.st_uid, before.st_gid,
    )


def test_lock_io_failure_returns_failed_job_and_releases_first_lock(tmp_path, monkeypatch, caplog):
    import logging

    import metafusion
    from helper.runtime import JobRunLock

    monkeypatch.setattr(metafusion, "BASE_CONFIG_DIR", tmp_path)
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("fixture")
    config = {"settings": {"path": str(blocked_root)}}
    assert metafusion.run_metafusion_job(config, logging.getLogger("scheduler")) is False
    assert "[Lock] Job could not start" in caplog.text
    with JobRunLock(tmp_path / ".metafusion-run.lock"):
        pass
