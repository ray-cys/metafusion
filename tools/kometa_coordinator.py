#!/usr/bin/env python3
"""Resident Kometa scheduler sharing MetaFusion's exclusive output lock.

Run instead of Kometa's internal scheduler, not in addition to it. Only the child
uses --run. Container stays resident between four-hour runs. No Docker socket,
Plex token, Unraid API, or host Python dependency is required.
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STOP = False
CHILD = None
LAST_STATUS = None
LAST_STATUS_AT = 0.0


def stop(signum, _frame):
    global STOP
    STOP = True
    if CHILD is not None and CHILD.poll() is None:
        CHILD.send_signal(signum)


def write_status(root, **values):
    global LAST_STATUS, LAST_STATUS_AT
    signature = (str(root), values)
    now = time.monotonic()
    if signature == LAST_STATUS and now - LAST_STATUS_AT < 30:
        return
    target = root / ".metafusion" / "kometa-coordinator.json"
    payload = {"schema": 1, "pid": os.getpid(), "epoch": int(time.time()), **values}
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload))
    os.chmod(temporary, 0o664)
    parent = root.stat()
    if os.geteuid() == 0:
        os.chown(temporary, parent.st_uid, parent.st_gid)
    os.replace(temporary, target)
    LAST_STATUS, LAST_STATUS_AT = signature, now


def next_boundary(now):
    boundary = now.replace(minute=0, second=0, microsecond=0)
    boundary += timedelta(hours=4 - now.hour % 4)
    return boundary


def run_once(args):
    global CHILD
    root = Path(args.root)
    directory = root / ".metafusion"
    directory.mkdir(exist_ok=True)
    parent = root.stat()
    if os.geteuid() == 0:
        os.chown(directory, parent.st_uid, parent.st_gid)
    path = directory / "output.lock"
    try:
        handle = path.open("x+")
    except FileExistsError:
        handle = path.open("r+")
        created = False
    else:
        created = True
    with handle as lock:
        if created:
            os.fchmod(lock.fileno(), 0o664)
            if os.geteuid() == 0:
                os.fchown(lock.fileno(), parent.st_uid, parent.st_gid)
        deadline = time.monotonic() + args.wait_seconds
        while not STOP:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                write_status(root, state="waiting_for_metafusion")
                if time.monotonic() >= deadline:
                    write_status(root, state="deferred", reason="producer lock wait exceeded")
                    return 75
                time.sleep(1)
        if STOP:
            return 143
        write_status(root, state="running")
        CHILD = subprocess.Popen([args.python, args.script, "--run", *args.kometa_args])
        while CHILD.poll() is None:
            write_status(root, state="running")
            time.sleep(1)
        rc = CHILD.returncode
        CHILD = None
        write_status(root, state="idle", last_exit=rc)
        return rc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/config")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--script", default="/kometa.py")
    parser.add_argument("--wait-seconds", type=int, default=7200)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("kometa_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.kometa_args[:1] == ["--"]:
        args.kometa_args = args.kometa_args[1:]
    directory = Path(args.root) / ".metafusion"
    directory.mkdir(exist_ok=True)
    if os.geteuid() == 0:
        owner = Path(args.root).stat()
        os.chown(directory, owner.st_uid, owner.st_gid)
    # One resident scheduler per root, including manual duplicate launches.
    with (directory / "kometa-scheduler.lock").open("a+") as scheduler:
        fcntl.flock(scheduler, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, stop)
        if args.once:
            return run_once(args)
        due = next_boundary(datetime.now(timezone.utc).astimezone())
        while not STOP:
            now = datetime.now(timezone.utc).astimezone()
            if now >= due:
                run_once(args)
                due = next_boundary(datetime.now(timezone.utc).astimezone())
            write_status(Path(args.root), state="scheduled", next_run=due.isoformat())
            time.sleep(10)
        write_status(Path(args.root), state="stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
