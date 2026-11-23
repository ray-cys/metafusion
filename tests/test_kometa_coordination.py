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
