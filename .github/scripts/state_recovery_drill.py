#!/usr/bin/env python3
"""Exercise durable state, interrupted writes, and disaster-recovery restoration."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from helper.database_maintenance import inspect_database
from helper.recovery import create_recovery_bundle, verify_recovery_bundle
from helper.state_db import (
    SCHEMA_VERSION,
    MediaStateStore,
    find_media_state,
    load_cleanup_candidates,
    load_cleanup_history,
    load_item_exceptions,
    observe_cleanup_candidate,
    record_cleanup_history,
    save_item_exception,
)

UTC = timezone.utc


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quick_check(path):
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        return str(connection.execute("PRAGMA quick_check").fetchone()[0])


def _seed_state(database, output_root):
    metadata = output_root / "metadata"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "movie_metadata.yml").write_text(
        "metadata:\n  Example (2020):\n    tmdb_id: 550\n",
        encoding="utf-8",
    )

    store = MediaStateStore(path=database)
    store["movie:plex:10"] = {
        "server_id": "drill-server",
        "library_uuid": "drill-movies",
        "library_name": "Movies",
        "rating_key": "10",
        "media_type": "movie",
        "tmdb_id": "550",
        "imdb_id": "tt0137523",
        "title": "Example",
        "year": 2020,
    }
    store["tv:plex:20"] = {
        "server_id": "drill-server",
        "library_uuid": "drill-shows",
        "library_name": "TV Shows",
        "rating_key": "20",
        "media_type": "tv",
        "tmdb_id": "1399",
        "tvdb_id": "121361",
        "title": "Example Show",
        "year": 2011,
        "seasons": {"1": {"season_last_checked": "2026-08-21T00:00:00+00:00"}},
    }
    store.flush()
    store.close()

    record = {
        "server_id": "drill-server",
        "library_uuid": "drill-movies",
        "library_name": "Movies",
        "rating_key": "10",
        "cache_key": "movie:plex:10",
        "media_type": "movie",
        "title": "Example",
        "year": 2020,
        "tmdb_id": "550",
        "imdb_id": "tt0137523",
    }
    start = datetime(2026, 8, 21, tzinfo=UTC)
    observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        confirmations_required=2,
        grace_hours=48,
        observation_id="drill-scan-1",
        path=database,
        now=start,
    )
    eligible = observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        confirmations_required=2,
        grace_hours=48,
        observation_id="drill-scan-2",
        path=database,
        now=start + timedelta(hours=49),
    )
    if not eligible.get("eligible"):
        raise RuntimeError("cleanup confirmation did not become eligible")
    record_cleanup_history(
        "automated",
        "candidate_observed",
        "pending",
        record,
        output_type="state",
        path=database,
        now=start + timedelta(hours=49),
    )
    record_cleanup_history(
        "manual",
        "preview",
        "reviewed",
        record,
        output_type="poster",
        path=database,
        now=start + timedelta(hours=50),
    )
    save_item_exception(
        "drill-server",
        "drill-movies",
        "10",
        "poster",
        library_name="Movies",
        reason="recovery drill",
        path=database,
    )


def _extract_verified_bundle(bundle, destination):
    verification = verify_recovery_bundle(bundle)
    if not verification.get("valid"):
        raise RuntimeError(f"recovery verification failed: {verification['failures']}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "r:gz") as archive:
        if sys.version_info >= (3, 12):
            archive.extractall(destination, filter="data")
        else:
            archive.extractall(destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("recovery drill extracted an invalid root layout")
    return roots[0], verification


def _writer(database, ready_file):
    for index in range(10_000):
        store = MediaStateStore(path=database)
        store[f"movie:interrupt:{index}"] = {
            "server_id": "interrupt-server",
            "library_uuid": "interrupt-library",
            "library_name": "Interruptions",
            "rating_key": str(index),
            "media_type": "movie",
            "tmdb_id": str(1000 + index),
            "title": f"Interrupted {index}",
            "year": 2020,
        }
        store.flush()
        store.close()
        if index == 2:
            Path(ready_file).write_text("ready\n", encoding="utf-8")
        time.sleep(0.005)
    return 0


def _interrupt_writer(database, work):
    ready = work / "writer-ready"
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--writer", str(database), str(ready)]
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not ready.exists():
        process.kill()
        process.wait(timeout=10)
        raise RuntimeError("interrupted writer did not reach the durable checkpoint")
    process.send_signal(signal.SIGTERM)
    process.wait(timeout=10)
    if _quick_check(database) != "ok":
        raise RuntimeError("SQLite quick_check failed after SIGTERM interruption")
    records = find_media_state(libraries=["Interruptions"], path=database)
    if not records:
        raise RuntimeError("no committed state survived the SIGTERM interruption")
    return len(records), process.returncode


def run_drill(output):
    with tempfile.TemporaryDirectory(prefix="metafusion-drill-") as temporary:
        work = Path(temporary)
        config_root = work / "config"
        output_root = work / "kometa"
        database = config_root / "cache" / "meta_db.sqlite3"
        database.parent.mkdir(parents=True)
        _seed_state(database, output_root)

        source_state = {
            "items": find_media_state(path=database),
            "cleanup_candidates": load_cleanup_candidates(path=database),
            "cleanup_history": load_cleanup_history(path=database),
            "exceptions": load_item_exceptions(path=database),
        }
        config = {
            "settings": {"mode": "kometa", "path": str(output_root)},
            "plex": {"url": "http://private.invalid:32400", "token": "drill-plex-secret"},
            "tmdb": {"api_key": "drill-tmdb-secret"},
        }
        bundle = create_recovery_bundle(
            config,
            base_dir=config_root,
            state_path=database,
        )
        restored_root, verification = _extract_verified_bundle(bundle, work / "restored")
        restored_database = work / "restored-meta_db.sqlite3"
        shutil.copy2(restored_root / "meta_db.sqlite3", restored_database)
        health = inspect_database(restored_database, SCHEMA_VERSION)
        if not health.get("healthy"):
            raise RuntimeError(f"restored database failed inspection: {health['status']}")

        restored_state = {
            "items": find_media_state(path=restored_database),
            "cleanup_candidates": load_cleanup_candidates(path=restored_database),
            "cleanup_history": load_cleanup_history(path=restored_database),
            "exceptions": load_item_exceptions(path=restored_database),
        }
        expected_counts = {key: len(value) for key, value in source_state.items()}
        restored_counts = {key: len(value) for key, value in restored_state.items()}
        if restored_counts != expected_counts:
            raise RuntimeError(
                f"restored state counts changed: {restored_counts} != {expected_counts}"
            )
        if _sha256(database) == _sha256(restored_database):
            copy_relation = "byte-identical"
        else:
            copy_relation = "logically-equivalent-online-backup"

        redacted = (restored_root / "effective-config.redacted.yml").read_text(
            encoding="utf-8"
        )
        if "drill-plex-secret" in redacted or "drill-tmdb-secret" in redacted:
            raise RuntimeError("recovery bundle exposed a configured secret")

        interrupted_database = work / "interrupted.sqlite3"
        interrupted_items, returncode = _interrupt_writer(interrupted_database, work)
        report = {
            "schema": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "passed",
            "source_counts": expected_counts,
            "restored_counts": restored_counts,
            "restored_database": {
                "schema": health.get("schema"),
                "quick_check": "ok",
                "copy_relation": copy_relation,
            },
            "bundle": {
                "valid": verification.get("valid"),
                "files": len((verification.get("manifest") or {}).get("files", {})),
                "secrets_redacted": True,
            },
            "interrupted_writer": {
                "signal_returncode": returncode,
                "committed_items": interrupted_items,
                "quick_check": "ok",
            },
        }
        Path(output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return report


def _write_summary(path, report):
    if not path:
        return
    lines = ["## MetaFusion state and recovery drill", "", f"- Result: **{report['status']}**"]
    if report["status"] == "passed":
        lines.extend(
            (
                f"- Restored items: {report['restored_counts']['items']}",
                f"- Restored cleanup history: {report['restored_counts']['cleanup_history']}",
                f"- Interrupted-write survivors: {report['interrupted_writer']['committed_items']}",
                "- SQLite quick check: ok",
                "- Recovery secrets redacted: yes",
            )
        )
    else:
        lines.append(f"- Error: {report.get('error', 'unknown failure')}")
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="state-recovery-drill.json")
    parser.add_argument("--github-summary")
    parser.add_argument("--writer", nargs=2, metavar=("DATABASE", "READY_FILE"))
    args = parser.parse_args(argv)
    if args.writer:
        return _writer(Path(args.writer[0]), Path(args.writer[1]))
    try:
        report = run_drill(args.output)
    except Exception as error:
        report = {
            "schema": 1,
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "failed",
            "error": f"{type(error).__name__}: {error}",
        }
        Path(args.output).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        _write_summary(args.github_summary, report)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 1
    _write_summary(args.github_summary, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
