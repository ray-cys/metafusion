"""Shared producer/consumer lock; the Kometa launcher uses this same inode."""

import time
from pathlib import Path

from helper.runtime import JobAlreadyRunningError, JobRunLock


def acquire_kometa_output_lock(config, *, wait_seconds=0):
    if config.get("settings", {}).get("dry_run", False):
        return None
    if config.get("settings", {}).get("mode", "kometa").lower() != "kometa":
        return None
    output_root = config.get("settings", {}).get("path")
    if not output_root:
        return None  # Read-only/diagnostic fixtures may have no output configured.
    root = Path(output_root)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            return JobRunLock(root / ".metafusion" / "output.lock").acquire()
        except JobAlreadyRunningError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
