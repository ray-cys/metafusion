import copy
import json
import os
import sqlite3
from collections.abc import MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from helper.config import CACHE_DIR
STATE_DATABASE = CACHE_DIR / "meta_db.sqlite3"
SCHEMA_VERSION = 2
FILE_MODE = 0o664


class StateDatabaseError(RuntimeError):
    """Raised when durable MetaFusion state cannot be read or updated safely."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _connect(path=None, writable=True):
    path = Path(path or STATE_DATABASE)
    if not writable and not path.exists():
        return None

    existed = path.exists()
    connection = None
    try:
        if writable:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=10)
        else:
            uri = f"file:{quote(str(path), safe='/')}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=10)
            connection.execute("PRAGMA query_only = ON")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        if writable:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            _initialize_schema(connection)
            if not existed:
                os.chmod(path, FILE_MODE)
        else:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, SCHEMA_VERSION):
                raise StateDatabaseError(
                    f"unsupported MetaFusion state schema version {version}"
                )
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise StateDatabaseError(
                f"MetaFusion state integrity check failed: {integrity}"
            )
        return connection
    except StateDatabaseError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as error:
        if connection is not None:
            connection.close()
        raise StateDatabaseError(
            f"Unable to open durable MetaFusion state database {path}: {error}"
        ) from error


def _initialize_schema(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, SCHEMA_VERSION):
        raise StateDatabaseError(
            f"unsupported MetaFusion state schema version {version}"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS media_state (
            cache_key TEXT PRIMARY KEY,
            server_id TEXT,
            library_uuid TEXT,
            library_name TEXT,
            rating_key TEXT,
            media_type TEXT,
            tmdb_id TEXT,
            title TEXT,
            year INTEGER,
            plex_updated_at TEXT,
            config_fingerprint TEXT,
            payload TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS media_state_rating_key
            ON media_state(server_id, library_uuid, rating_key);
        CREATE INDEX IF NOT EXISTS media_state_library
            ON media_state(server_id, library_uuid, media_type);
        CREATE UNIQUE INDEX IF NOT EXISTS media_state_plex_identity
            ON media_state(server_id, library_uuid, rating_key)
            WHERE server_id IS NOT NULL
              AND library_uuid IS NOT NULL
              AND rating_key IS NOT NULL;

        CREATE TABLE IF NOT EXISTS season_state (
            cache_key TEXT NOT NULL,
            season_number TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (cache_key, season_number),
            FOREIGN KEY (cache_key) REFERENCES media_state(cache_key)
                ON DELETE CASCADE
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS application_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS library_scan_state (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT,
            config_fingerprint TEXT,
            item_count INTEGER,
            last_full_scan_started TEXT,
            last_full_scan_completed TEXT,
            last_successful_incremental TEXT,
            PRIMARY KEY (server_id, library_uuid)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT,
            started_at TEXT,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS plex_metadata_ownership (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT,
            rating_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            child_key TEXT NOT NULL DEFAULT '',
            field_name TEXT NOT NULL,
            field_kind TEXT NOT NULL,
            original_value TEXT NOT NULL,
            applied_value TEXT NOT NULL,
            owned_values TEXT NOT NULL,
            original_locked INTEGER NOT NULL DEFAULT 0,
            metafusion_locked INTEGER NOT NULL DEFAULT 0,
            last_checked TEXT NOT NULL,
            last_updated TEXT NOT NULL,
            PRIMARY KEY (
                server_id, library_uuid, rating_key, child_key, field_name
            )
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS plex_metadata_ownership_library
            ON plex_metadata_ownership(server_id, library_uuid, media_type);
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (SCHEMA_VERSION, utc_now()),
    )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()


def _json_dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value, description):
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise StateDatabaseError(f"Invalid JSON stored for {description}") from error
    if not isinstance(decoded, dict):
        raise StateDatabaseError(f"Invalid object stored for {description}")
    return decoded


def _state_value(connection, key):
    row = connection.execute(
        "SELECT state_value FROM application_state WHERE state_key = ?", (key,)
    ).fetchone()
    return None if row is None else row[0]


def _set_state_value(connection, key, value):
    connection.execute(
        """
        INSERT INTO application_state(state_key, state_value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(state_key) DO UPDATE SET
            state_value = excluded.state_value,
            updated_at = excluded.updated_at
        """,
        (key, str(value), utc_now()),
    )


class MediaStateStore(MutableMapping):
    """Dictionary-compatible, transactionally batched media state."""

    def __init__(
        self,
        path=None,
        writable=True,
    ):
        self.path = Path(path or STATE_DATABASE)
        self.writable = bool(writable)
        self._connection = _connect(self.path, writable=self.writable)
        self._pending = {}
        self._deleted = set()
        self._memory = {}

    def _database_entry(self, key):
        if self._connection is None:
            raise KeyError(key)
        row = self._connection.execute(
            "SELECT payload FROM media_state WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        entry = _json_load(row[0], f"media state {key!r}")
        seasons = {}
        for season in self._connection.execute(
            "SELECT season_number, payload FROM season_state "
            "WHERE cache_key = ? ORDER BY season_number",
            (key,),
        ):
            seasons[str(season[0])] = _json_load(
                season[1], f"season state {key!r}/{season[0]!r}"
            )
        if seasons:
            entry["seasons"] = seasons
        return entry

    def __getitem__(self, key):
        key = str(key)
        if key in self._deleted:
            raise KeyError(key)
        if key in self._pending:
            return self._pending[key]
        if key in self._memory:
            return self._memory[key]
        return self._database_entry(key)

    def __setitem__(self, key, value):
        key = str(key)
        if not isinstance(value, dict):
            raise TypeError("media state entries must be dictionaries")
        value = copy.deepcopy(value)
        try:
            current = self[key]
        except KeyError:
            current = None
        if current == value:
            return
        self._deleted.discard(key)
        if self._connection is None or not self.writable:
            self._memory[key] = value
            return
        self._pending[key] = value

    def __delitem__(self, key):
        key = str(key)
        if key not in self:
            raise KeyError(key)
        self._pending.pop(key, None)
        self._memory.pop(key, None)
        if self._connection is not None and self.writable:
            self._deleted.add(key)

    def __iter__(self):
        keys = set(self._memory) | set(self._pending)
        if self._connection is not None:
            keys.update(
                row[0]
                for row in self._connection.execute(
                    "SELECT cache_key FROM media_state ORDER BY cache_key"
                )
            )
        keys.difference_update(self._deleted)
        return iter(sorted(keys))

    def __len__(self):
        return sum(1 for _ in self)

    def _all_entries(self):
        entries = {}
        if self._connection is not None:
            for row in self._connection.execute(
                "SELECT cache_key, payload FROM media_state ORDER BY cache_key"
            ):
                entries[row[0]] = _json_load(row[1], f"media state {row[0]!r}")
            for row in self._connection.execute(
                "SELECT cache_key, season_number, payload FROM season_state "
                "ORDER BY cache_key, season_number"
            ):
                if row[0] in entries:
                    entries[row[0]].setdefault("seasons", {})[str(row[1])] = (
                        _json_load(
                            row[2], f"season state {row[0]!r}/{row[1]!r}"
                        )
                    )
        entries.update(copy.deepcopy(self._memory))
        entries.update(copy.deepcopy(self._pending))
        for key in self._deleted:
            entries.pop(key, None)
        return entries

    def items(self):
        return self._all_entries().items()

    def values(self):
        return self._all_entries().values()

    def flush(self):
        if (
            not self.writable
            or self._connection is None
            or (not self._pending and not self._deleted)
        ):
            return False
        try:
            with self._connection:
                if self._deleted:
                    self._connection.executemany(
                        "DELETE FROM media_state WHERE cache_key = ?",
                        ((key,) for key in self._deleted),
                    )
                for key, raw_entry in self._pending.items():
                    entry = copy.deepcopy(raw_entry)
                    seasons = entry.pop("seasons", {})
                    if entry.get("media_type") == "tv":
                        entry.pop("season_average", None)
                        entry.pop("season_number", None)
                    self._connection.execute(
                        """
                        INSERT INTO media_state(
                            cache_key, server_id, library_uuid, library_name,
                            rating_key, media_type, tmdb_id, title, year,
                            plex_updated_at, config_fingerprint, payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            server_id = excluded.server_id,
                            library_uuid = excluded.library_uuid,
                            library_name = excluded.library_name,
                            rating_key = excluded.rating_key,
                            media_type = excluded.media_type,
                            tmdb_id = excluded.tmdb_id,
                            title = excluded.title,
                            year = excluded.year,
                            plex_updated_at = excluded.plex_updated_at,
                            config_fingerprint = excluded.config_fingerprint,
                            payload = excluded.payload
                        """,
                        (
                            key,
                            entry.get("server_id"),
                            entry.get("library_uuid"),
                            entry.get("library_name"),
                            entry.get("rating_key"),
                            entry.get("media_type"),
                            None
                            if entry.get("tmdb_id") is None
                            else str(entry.get("tmdb_id")),
                            entry.get("title"),
                            entry.get("year"),
                            entry.get("plex_updated_at"),
                            entry.get("config_fingerprint"),
                            _json_dump(entry),
                        ),
                    )
                    self._connection.execute(
                        "DELETE FROM season_state WHERE cache_key = ?", (key,)
                    )
                    if isinstance(seasons, dict):
                        self._connection.executemany(
                            "INSERT INTO season_state(cache_key, season_number, payload) "
                            "VALUES (?, ?, ?)",
                            (
                                (key, str(number), _json_dump(season_entry))
                                for number, season_entry in seasons.items()
                                if isinstance(season_entry, dict)
                            ),
                        )
            self._pending.clear()
            self._deleted.clear()
            return True
        except (sqlite3.Error, TypeError, ValueError) as error:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            raise StateDatabaseError(
                f"Unable to flush durable media state {self.path}: {error}"
            ) from error

    def replace_all(self, entries):
        if not isinstance(entries, dict):
            entries = dict(entries)
        existing = set(self)
        replacement = {
            str(key): copy.deepcopy(value)
            for key, value in entries.items()
            if isinstance(value, dict)
        }
        for key in existing - set(replacement):
            del self[key]
        for key, value in replacement.items():
            self[key] = value
        return self.flush()

    def close(self):
        if self._connection is None:
            return
        try:
            self._connection.close()
        finally:
            self._connection = None


def load_scan_states(scopes, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return {}
    try:
        states = {}
        for scope in scopes or []:
            server_id = str(scope.get("server_id") or "unknown")
            library_uuid = str(scope.get("library_uuid") or scope.get("library_name"))
            row = connection.execute(
                "SELECT * FROM library_scan_state "
                "WHERE server_id = ? AND library_uuid = ?",
                (server_id, library_uuid),
            ).fetchone()
            if row is not None:
                states[(server_id, library_uuid)] = dict(row)
        return states
    finally:
        connection.close()


def load_global_full_scan(path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return None
    try:
        return _state_value(connection, "global_last_full_scan")
    finally:
        connection.close()


def mark_scan_started(scopes, full_scan, path=None, now=None):
    connection = _connect(path, writable=True)
    now = now or utc_now()
    try:
        with connection:
            for scope in scopes or []:
                server_id = str(scope.get("server_id") or "unknown")
                library_uuid = str(
                    scope.get("library_uuid") or scope.get("library_name")
                )
                connection.execute(
                    """
                    INSERT INTO library_scan_state(
                        server_id, library_uuid, library_name,
                        config_fingerprint, item_count, last_full_scan_started
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(server_id, library_uuid) DO UPDATE SET
                        library_name = excluded.library_name,
                        last_full_scan_started = CASE
                            WHEN ? THEN excluded.last_full_scan_started
                            ELSE library_scan_state.last_full_scan_started
                        END
                    """,
                    (
                        server_id,
                        library_uuid,
                        scope.get("library_name"),
                        scope.get("config_fingerprint"),
                        scope.get("item_count"),
                        now if full_scan else None,
                        1 if full_scan else 0,
                    ),
                )
        return True
    finally:
        connection.close()


def mark_scan_complete(scopes, full_scan, path=None, now=None):
    connection = _connect(path, writable=True)
    now = now or utc_now()
    try:
        with connection:
            for scope in scopes or []:
                server_id = str(scope.get("server_id") or "unknown")
                library_uuid = str(
                    scope.get("library_uuid") or scope.get("library_name")
                )
                connection.execute(
                    """
                    INSERT INTO library_scan_state(
                        server_id, library_uuid, library_name,
                        config_fingerprint, item_count,
                        last_full_scan_completed, last_successful_incremental
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(server_id, library_uuid) DO UPDATE SET
                        library_name = excluded.library_name,
                        config_fingerprint = excluded.config_fingerprint,
                        item_count = excluded.item_count,
                        last_full_scan_completed = CASE
                            WHEN ? THEN excluded.last_full_scan_completed
                            ELSE library_scan_state.last_full_scan_completed
                        END,
                        last_successful_incremental = excluded.last_successful_incremental
                    """,
                    (
                        server_id,
                        library_uuid,
                        scope.get("library_name"),
                        scope.get("config_fingerprint"),
                        scope.get("item_count"),
                        now if full_scan else None,
                        now,
                        1 if full_scan else 0,
                    ),
                )
        return True
    finally:
        connection.close()


def mark_global_full_scan(value, path=None):
    connection = _connect(path, writable=True)
    try:
        with connection:
            _set_state_value(connection, "global_last_full_scan", value)
        return True
    finally:
        connection.close()


def load_plex_metadata_ownership(
    server_id, library_uuid, rating_key, path=None
):
    """Load only the Plex fields previously written by MetaFusion."""
    connection = _connect(path, writable=False)
    if connection is None:
        return {}
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'plex_metadata_ownership'"
        ).fetchone()
        if table_exists is None:
            return {}
        rows = connection.execute(
            "SELECT * FROM plex_metadata_ownership "
            "WHERE server_id = ? AND library_uuid = ? AND rating_key = ?",
            (str(server_id), str(library_uuid), str(rating_key)),
        ).fetchall()
        records = {}
        for row in rows:
            record = dict(row)
            for name in ("original_value", "applied_value", "owned_values"):
                record[name] = _json_load(
                    record[name], f"Plex ownership {rating_key}/{record['field_name']}"
                )
            records[(record["child_key"], record["field_name"])] = record
        return records
    finally:
        connection.close()


def save_plex_metadata_ownership(records, deleted=None, path=None):
    """Persist an item's Plex ownership ledger in one durable transaction."""
    records = list(records or [])
    deleted = list(deleted or [])
    if not records and not deleted:
        return False
    connection = _connect(path, writable=True)
    try:
        with connection:
            for key in deleted:
                connection.execute(
                    "DELETE FROM plex_metadata_ownership WHERE "
                    "server_id = ? AND library_uuid = ? AND rating_key = ? "
                    "AND child_key = ? AND field_name = ?",
                    tuple(str(value) for value in key),
                )
            for record in records:
                connection.execute(
                    """
                    INSERT INTO plex_metadata_ownership(
                        server_id, library_uuid, library_name, rating_key,
                        media_type, child_key, field_name, field_kind,
                        original_value, applied_value, owned_values,
                        original_locked, metafusion_locked, last_checked,
                        last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        server_id, library_uuid, rating_key, child_key, field_name
                    ) DO UPDATE SET
                        library_name = excluded.library_name,
                        media_type = excluded.media_type,
                        field_kind = excluded.field_kind,
                        original_value = excluded.original_value,
                        applied_value = excluded.applied_value,
                        owned_values = excluded.owned_values,
                        original_locked = excluded.original_locked,
                        metafusion_locked = excluded.metafusion_locked,
                        last_checked = excluded.last_checked,
                        last_updated = excluded.last_updated
                    """,
                    (
                        str(record["server_id"]),
                        str(record["library_uuid"]),
                        record.get("library_name"),
                        str(record["rating_key"]),
                        str(record["media_type"]),
                        str(record.get("child_key") or ""),
                        str(record["field_name"]),
                        str(record["field_kind"]),
                        _json_dump(record.get("original_value") or {}),
                        _json_dump(record.get("applied_value") or {}),
                        _json_dump(record.get("owned_values") or {}),
                        int(bool(record.get("original_locked"))),
                        int(bool(record.get("metafusion_locked"))),
                        str(record.get("last_checked") or utc_now()),
                        str(record.get("last_updated") or utc_now()),
                    ),
                )
        return True
    finally:
        connection.close()


def prune_plex_metadata_children(
    server_id, library_uuid, rating_key, valid_child_keys, path=None
):
    """Drop ownership for seasons or episodes no longer present in Plex."""
    connection = _connect(path, writable=True)
    valid = {str(value) for value in valid_child_keys}
    try:
        rows = connection.execute(
            "SELECT DISTINCT child_key FROM plex_metadata_ownership "
            "WHERE server_id = ? AND library_uuid = ? AND rating_key = ?",
            (str(server_id), str(library_uuid), str(rating_key)),
        ).fetchall()
        stale = [row[0] for row in rows if row[0] not in valid]
        if not stale:
            return 0
        with connection:
            connection.executemany(
                "DELETE FROM plex_metadata_ownership WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ? AND child_key = ?",
                (
                    (str(server_id), str(library_uuid), str(rating_key), child_key)
                    for child_key in stale
                ),
            )
        return len(stale)
    finally:
        connection.close()


def prune_plex_metadata_library(
    server_id, library_uuid, valid_rating_keys, path=None
):
    """Drop ownership for Plex items absent from a complete library scan."""
    connection = _connect(path, writable=True)
    valid = {str(value) for value in valid_rating_keys}
    try:
        rows = connection.execute(
            "SELECT DISTINCT rating_key FROM plex_metadata_ownership "
            "WHERE server_id = ? AND library_uuid = ?",
            (str(server_id), str(library_uuid)),
        ).fetchall()
        stale = [row[0] for row in rows if row[0] not in valid]
        if not stale:
            return 0
        with connection:
            connection.executemany(
                "DELETE FROM plex_metadata_ownership WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ?",
                (
                    (str(server_id), str(library_uuid), rating_key)
                    for rating_key in stale
                ),
            )
        return len(stale)
    finally:
        connection.close()


def record_job_run(
    mode,
    started_at,
    finished_at,
    status,
    error=None,
    history_limit=10,
    path=None,
):
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                "INSERT INTO job_runs(mode, started_at, finished_at, status, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (mode, started_at, finished_at, status, error),
            )
            connection.execute(
                "DELETE FROM job_runs WHERE id NOT IN "
                "(SELECT id FROM job_runs ORDER BY id DESC LIMIT ?)",
                (max(1, int(history_limit)),),
            )
        return True
    finally:
        connection.close()


def recent_job_runs(limit=10, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT mode, started_at, finished_at, status, error "
            "FROM job_runs ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        connection.close()
