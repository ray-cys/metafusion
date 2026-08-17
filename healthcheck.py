import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def check_status(path, max_heartbeat_age=120, fail_on_job_error=False):
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
        heartbeat = parse_time(status.get("heartbeat_at"))
        if heartbeat is None:
            return False, "missing heartbeat"
        age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
        if age > max_heartbeat_age:
            return False, f"stale heartbeat ({int(age)}s)"

        pid = int(status.get("pid"))
        os.kill(pid, 0)
        if fail_on_job_error and status.get("last_run_status") == "failed":
            return False, status.get("last_error") or "last run failed"
        state = status.get("state", "unknown")
        if status.get("last_run_status") == "failed":
            return True, f"{state}; last run failed: {status.get('last_error') or 'unknown error'}"
        return True, state
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return False, str(error)


if __name__ == "__main__":
    status_path = os.environ.get("STATUS_FILE", "/tmp/metafusion-status.json")
    max_age = int(os.environ.get("HEALTH_MAX_HEARTBEAT_AGE", "120"))
    fail_on_job_error = os.environ.get("HEALTH_FAIL_ON_JOB_ERROR", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    healthy, message = check_status(
        status_path,
        max_heartbeat_age=max_age,
        fail_on_job_error=fail_on_job_error,
    )
    print(message)
    sys.exit(0 if healthy else 1)
