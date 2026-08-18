"""Explicit, bounded SQLite maintenance commands for MetaFusion databases."""

import os
import shutil
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from helper.config import BASE_CONFIG_DIR, CACHE_DIR
from helper.state_db import SCHEMA_VERSION as STATE_SCHEMA_VERSION
from helper.state_db import STATE_DATABASE
from helper.tmdb_cache import PersistentTTLCache

DATABASES = {
    "state": (STATE_DATABASE, STATE_SCHEMA_VERSION),
    "tmdb": (CACHE_DIR / "tmdb_cache.sqlite3", PersistentTTLCache.SCHEMA_VERSION),
}
FILE_MODE = 0o664


def selected_databases(target="all"):
    if target == "all":
        return DATABASES
    if target not in DATABASES:
        raise ValueError(f"Unsupported SQLite target: {target}")
    return {target: DATABASES[target]}


def inspect_database(path, expected_schema):
    database = Path(path)
    result = {
        "path": str(database),
        "exists": database.exists(),
        "healthy": True,
        "status": "missing",
        "schema": None,
        "expected_schema": int(expected_schema),
        "bytes": 0,
        "wal_bytes": 0,
        "page_count": 0,
        "free_pages": 0,
    }
    if not database.exists():
        return result
    try:
        uri = f"file:{quote(str(database), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("PRAGMA query_only = ON")
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
            result.update(
                schema=schema,
                page_count=int(connection.execute("PRAGMA page_count").fetchone()[0]),
                free_pages=int(connection.execute("PRAGMA freelist_count").fetchone()[0]),
            )
        result["bytes"] = database.stat().st_size
        wal = Path(f"{database}-wal")
        result["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
        result["healthy"] = integrity == "ok" and schema == int(expected_schema)
        result["status"] = (
            "ok"
            if result["healthy"]
            else f"check={integrity}, schema={schema}, expected={expected_schema}"
        )
    except (OSError, sqlite3.Error) as error:
        result["healthy"] = False
        result["status"] = f"{type(error).__name__}: {error}"
    return result


def _backup_database(database, backup_dir, retention):
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    destination = backup_dir / f"{database.stem}-{timestamp}.sqlite3"
    temporary = backup_dir / f".{destination.name}.tmp"
    source = None
    backup = None
    try:
        try:
            source = sqlite3.connect(database, timeout=5)
            backup = sqlite3.connect(temporary)
            source.backup(backup)
            integrity = backup.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(
                    f"backup quick_check returned {integrity}"
                )
        finally:
            if backup is not None:
                backup.close()
            if source is not None:
                source.close()
        os.chmod(temporary, FILE_MODE)
        os.replace(temporary, destination)
    except Exception:
        with suppress(OSError):
            temporary.unlink()
        raise
    backups = sorted(
        backup_dir.glob(f"{database.stem}-*.sqlite3"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[max(1, int(retention)) :]:
        with suppress(OSError):
            stale.unlink()
    return destination


def maintain_databases(
    action,
    target="all",
    *,
    backup_dir=None,
    retention=3,
):
    """Inspect or explicitly maintain one or both SQLite databases."""
    if action not in {"check", "optimize", "checkpoint", "vacuum", "backup"}:
        raise ValueError(f"Unsupported SQLite maintenance action: {action}")
    results = []
    for name, (path, expected_schema) in selected_databases(target).items():
        database = Path(path)
        before = inspect_database(database, expected_schema)
        result = {"database": name, "action": action, **before}
        if action == "check" or not database.exists():
            if action != "check":
                result["status"] = "skipped (database missing)"
            results.append(result)
            continue
        try:
            if action == "backup":
                destination = _backup_database(
                    database,
                    backup_dir or (Path(BASE_CONFIG_DIR) / "backups"),
                    retention,
                )
                result["backup"] = str(destination)
            else:
                if action == "vacuum":
                    required = max(database.stat().st_size * 2, 16 * 1024 * 1024)
                    if shutil.disk_usage(database.parent).free < required:
                        raise OSError("insufficient free space for VACUUM")
                with sqlite3.connect(database, timeout=10) as connection:
                    connection.execute("PRAGMA busy_timeout = 10000")
                    integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                    if integrity != "ok":
                        raise sqlite3.DatabaseError(f"quick_check returned {integrity}")
                    if action == "optimize":
                        connection.execute("PRAGMA optimize")
                    elif action == "checkpoint":
                        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    elif action == "vacuum":
                        connection.execute("VACUUM")
                    connection.commit()
            after = inspect_database(database, expected_schema)
            result.update(after)
            result["status"] = "completed" if after["healthy"] else after["status"]
        except (OSError, sqlite3.Error) as error:
            result["healthy"] = False
            result["status"] = f"{type(error).__name__}: {error}"
        results.append(result)
    return results


def format_maintenance_results(results):
    lines = ["MetaFusion SQLite maintenance"]
    for result in results:
        details = [
            f"schema={result.get('schema')}",
            f"bytes={result.get('bytes', 0)}",
            f"wal={result.get('wal_bytes', 0)}",
        ]
        if result.get("backup"):
            details.append(f"backup={result['backup']}")
        state = "PASS" if result.get("healthy") else "FAIL"
        lines.append(
            f"- [{state}] {result['database']} {result['action']}: "
            f"{result.get('status')} ({', '.join(details)})"
        )
    return "\n".join(lines)
