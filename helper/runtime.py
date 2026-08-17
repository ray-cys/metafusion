import fcntl
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from helper.io import atomic_write_json
from helper.state_db import StateDatabaseError, record_job_run


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_runtime_paths(config, config_dir):
    """Create and verify only the writable paths required by a real run."""
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is not None and get_effective_uid() == 0:
        raise RuntimeError(
            "MetaFusion refuses to run as root because generated files would become "
            "root-owned. Keep the Docker entrypoint enabled and set PUID/PGID to the "
            "host owner (99:100 on standard Unraid installations)."
        )
    if config.get("settings", {}).get("dry_run", False):
        return

    required = [Path(config_dir), Path(config_dir) / "logs", Path(config_dir) / "cache"]
    if config.get("settings", {}).get("mode", "kometa").lower() == "kometa":
        required.append(Path(config.get("settings", {}).get("path", "/kometa")))

    for path in required:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".metafusion-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as error:
            raise RuntimeError(
                f"Required path is not writable: {path}. "
                "Check the bind-mount owner and PUID/PGID settings."
            ) from error


class JobAlreadyRunningError(RuntimeError):
    pass


class JobRunLock:
    """Cross-process lock protecting shared cache, state, YAML, and assets."""

    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise JobAlreadyRunningError(
                "Another MetaFusion job is already using the configured output"
            ) from error
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        os.chmod(self.path, 0o664)
        return self

    def release(self):
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


class RuntimeStatus:
    def __init__(
        self,
        path,
        heartbeat_seconds=30,
        history_limit=10,
        state_database=None,
    ):
        self.path = Path(path)
        self.heartbeat_seconds = heartbeat_seconds
        self.history_limit = max(1, int(history_limit))
        self.state_database = None if state_database is None else Path(state_database)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._data = self._load_existing()

    def _load_existing(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _update(self, **values):
        with self._lock:
            self._data.update(values)
            self._data["pid"] = os.getpid()
            self._data["heartbeat_at"] = utc_now()
            atomic_write_json(self.path, self._data)

    def start(self, mode):
        self._update(state="starting", mode=mode, last_error=None)
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="metafusion-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self):
        while not self._stop.wait(self.heartbeat_seconds):
            self._update()

    def idle(self):
        self._update(state="idle")

    def run_started(self):
        self._update(state="running", last_run_started=utc_now(), last_error=None)

    def run_finished(self, success, error=None):
        now = utc_now()
        values = {
            "state": "idle",
            "last_run_finished": now,
            "last_run_status": "success" if success else "failed",
            "last_error": None if success else str(error or "Unknown run failure"),
        }
        if success:
            values["last_success"] = now
        if self.state_database is not None:
            try:
                record_job_run(
                    mode=self._data.get("mode"),
                    started_at=self._data.get("last_run_started"),
                    finished_at=now,
                    status=values["last_run_status"],
                    error=values["last_error"],
                    history_limit=self.history_limit,
                    path=self.state_database,
                )
            except StateDatabaseError as state_error:
                logging.getLogger().warning(
                    "[Runtime] Unable to persist completed job history: %s",
                    state_error,
                )
        self._update(**values)

    def stopping(self):
        self._update(state="stopping")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
