"""Private SQLite state for the Formula 1 extension."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
FILE_MODE = 0o664


def utc_now():
    return datetime.now(timezone.utc)


class Formula1StateError(RuntimeError):
    """Raised when Formula 1 state cannot be initialized safely."""


class Formula1State:
    """Small extension-owned store; no table is shared with MetaFusion core."""

    def __init__(self, path):
        memory = str(path) == ":memory:"
        self.path = Path(path)
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = False if memory else self.path.exists()
        try:
            self.connection = sqlite3.connect(str(path), timeout=10)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout = 10000")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        except (OSError, sqlite3.Error) as error:
            raise Formula1StateError(
                f"Unable to initialize Formula 1 state: {self.path}"
            ) from error
        if not existed and not memory:
            os.chmod(self.path, FILE_MODE)

    def _initialize(self):
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cache (
                    provider TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (provider, cache_key)
                );
                CREATE TABLE IF NOT EXISTS item_bindings (
                    logical_key TEXT PRIMARY KEY,
                    plex_rating_key TEXT,
                    media_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    naming_profile TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artwork_state (
                    logical_key TEXT PRIMARY KEY,
                    destination TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_history (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cleanup_candidates (
                    logical_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    first_missing_at TEXT NOT NULL,
                    missing_scans INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cleanup_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    logical_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            row = self.connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO schema_info(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise Formula1StateError(
                    f"Unsupported Formula 1 state schema {row['version']}; expected {SCHEMA_VERSION}"
                )

    def close(self):
        self.connection.close()

    def cache_get(self, provider, key, *, allow_expired=False, now=None):
        row = self.connection.execute(
            "SELECT payload, expires_at FROM provider_cache WHERE provider=? AND cache_key=?",
            (provider, key),
        ).fetchone()
        if row is None:
            return None
        current = now or utc_now()
        expires = datetime.fromisoformat(row["expires_at"])
        if expires < current and not allow_expired:
            return None
        return json.loads(row["payload"])

    def cache_put(self, provider, key, payload, ttl_hours, *, now=None):
        current = now or utc_now()
        expires = current + timedelta(hours=float(ttl_hours))
        with self.connection:
            self.connection.execute(
                """INSERT INTO provider_cache(provider, cache_key, payload, fetched_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(provider, cache_key) DO UPDATE SET
                     payload=excluded.payload, fetched_at=excluded.fetched_at,
                     expires_at=excluded.expires_at""",
                (
                    provider,
                    key,
                    json.dumps(payload, sort_keys=True),
                    current.isoformat(),
                    expires.isoformat(),
                ),
            )

    def bindings(self):
        rows = self.connection.execute(
            "SELECT * FROM item_bindings ORDER BY logical_key"
        ).fetchall()
        return {row["logical_key"]: dict(row) for row in rows}

    def bind(self, logical_key, rating_key, path, title, profile, *, now=None):
        current = (now or utc_now()).isoformat()
        with self.connection:
            self.connection.execute(
                """INSERT INTO item_bindings(
                       logical_key, plex_rating_key, media_path, title, naming_profile, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(logical_key) DO UPDATE SET
                     plex_rating_key=excluded.plex_rating_key,
                     media_path=excluded.media_path,
                     title=excluded.title,
                     naming_profile=excluded.naming_profile,
                     updated_at=excluded.updated_at""",
                (logical_key, rating_key, str(path), title, profile, current),
            )

    def artwork(self, logical_key):
        row = self.connection.execute(
            "SELECT * FROM artwork_state WHERE logical_key=?", (logical_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def save_artwork(self, logical_key, destination, fingerprint, checksum, *, now=None):
        current = (now or utc_now()).isoformat()
        with self.connection:
            self.connection.execute(
                """INSERT INTO artwork_state(
                       logical_key, destination, fingerprint, checksum, updated_at
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(logical_key) DO UPDATE SET
                     destination=excluded.destination, fingerprint=excluded.fingerprint,
                     checksum=excluded.checksum, updated_at=excluded.updated_at""",
                (logical_key, str(destination), fingerprint, checksum, current),
            )

    def remove_artwork(self, logical_key):
        with self.connection:
            self.connection.execute("DELETE FROM artwork_state WHERE logical_key=?", (logical_key,))

    def start_run(self, run_id, *, now=None):
        started = (now or utc_now()).isoformat()
        with self.connection:
            self.connection.execute(
                "INSERT INTO run_history VALUES (?, ?, NULL, 'running', '{}')",
                (run_id, started),
            )

    def finish_run(self, run_id, status, summary, *, now=None):
        finished = (now or utc_now()).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE run_history SET finished_at=?, status=?, summary=? WHERE run_id=?",
                (finished, status, json.dumps(summary, sort_keys=True), run_id),
            )

    def reconcile_bindings(
        self, current_keys, *, cleanup, confirmation_scans, grace_hours, now=None
    ):
        """Return managed stale records eligible for explicit extension cleanup."""
        current = now or utc_now()
        existing = self.bindings()
        stale = []
        with self.connection:
            for logical_key in current_keys:
                self.connection.execute(
                    "DELETE FROM cleanup_candidates WHERE logical_key=?", (logical_key,)
                )
            for logical_key, binding in existing.items():
                if logical_key in current_keys:
                    continue
                row = self.connection.execute(
                    "SELECT * FROM cleanup_candidates WHERE logical_key=?", (logical_key,)
                ).fetchone()
                if row is None:
                    self.connection.execute(
                        "INSERT INTO cleanup_candidates VALUES (?, ?, ?, 1)",
                        (logical_key, json.dumps(binding, sort_keys=True), current.isoformat()),
                    )
                    scans, first_missing = 1, current
                else:
                    scans = int(row["missing_scans"]) + 1
                    first_missing = datetime.fromisoformat(row["first_missing_at"])
                    self.connection.execute(
                        "UPDATE cleanup_candidates SET missing_scans=? WHERE logical_key=?",
                        (scans, logical_key),
                    )
                if (
                    cleanup
                    and scans >= int(confirmation_scans)
                    and current - first_missing >= timedelta(hours=float(grace_hours))
                ):
                    stale.append(binding)
        return stale

    def remove_binding(self, logical_key, details, *, now=None):
        current = (now or utc_now()).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM item_bindings WHERE logical_key=?", (logical_key,))
            self.connection.execute("DELETE FROM artwork_state WHERE logical_key=?", (logical_key,))
            self.connection.execute(
                "DELETE FROM cleanup_candidates WHERE logical_key=?", (logical_key,)
            )
            self.connection.execute(
                "INSERT INTO cleanup_history(logical_key, action, details, created_at) VALUES (?, 'automatic', ?, ?)",
                (logical_key, json.dumps(details, sort_keys=True), current),
            )
