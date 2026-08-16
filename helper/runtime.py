import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from helper.io import atomic_write_json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_runtime_paths(config, config_dir):
    """Create and verify only the writable paths required by a real run."""
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


class RuntimeStatus:
    def __init__(self, path, heartbeat_seconds=30):
        self.path = Path(path)
        self.heartbeat_seconds = heartbeat_seconds
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
        self._update(**values)

    def stopping(self):
        self._update(state="stopping")

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
