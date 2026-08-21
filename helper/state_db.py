import copy
import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import Counter
from collections.abc import MutableMapping
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from helper.asset_registry import normalize_destination
from helper.config import CACHE_DIR
from helper.report_identity import report_identity

STATE_DATABASE = CACHE_DIR / "meta_db.sqlite3"
SCHEMA_VERSION = 8
FILE_MODE = 0o664
IDENTITY_HISTORY_LIMIT = 10_000
UNRESOLVED_HISTORY_LIMIT = 10_000
CLEANUP_HISTORY_LIMIT = 20_000
IDENTITY_REVIEW_LIMIT = 10_000
REBIND_HISTORY_LIMIT = 10_000
JOB_HISTORY_LIMIT = 500
_database_setup_lock = threading.Lock()
_initialized_databases: set[tuple[str, int, int]] = set()
_integrity_checked_databases: set[tuple[str, int, int]] = set()
_backed_up_databases: set[tuple[str, int, int, int]] = set()

_RETRY_DELAYS = (
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
)
_PERMANENT_FAILURE_MARKERS = (
    "ambiguous artwork destination",
    "ambiguous edition",
    "cannot uniquely match",
    "identity mismatch",
    "identity rejected",
    "invalid path mapping",
    "mapping contains unsafe",
    "mapping source must be absolute",
    "recorded_path_mismatch",
    "unsupported library type",
    "unsupported metadata",
)


class StateDatabaseError(RuntimeError):
    """Raised when durable MetaFusion state cannot be read or updated safely."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _as_utc(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _backup_before_schema_upgrade(connection, path, version):
    """Create a bounded SQLite backup before changing an existing schema."""
    if version <= 0 or version >= SCHEMA_VERSION:
        return None
    stat = Path(path).stat()
    identity = (str(Path(path).absolute()), stat.st_dev, stat.st_ino, version)
    if identity in _backed_up_databases:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = Path(f"{path}.pre-v{version}-{timestamp}.bak")
    backup = sqlite3.connect(backup_path)
    try:
        connection.backup(backup)
    finally:
        backup.close()
    os.chmod(backup_path, FILE_MODE)
    backups = sorted(
        Path(path).parent.glob(f"{Path(path).name}.pre-v*.bak"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )
    for expired in backups[2:]:
        with suppress(OSError):
            expired.unlink()
    _backed_up_databases.add(identity)
    return backup_path


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
        database_identity = None
        if writable:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            if not existed:
                os.chmod(path, FILE_MODE)
        stat = path.stat()
        database_identity = (str(path.absolute()), stat.st_dev, stat.st_ino)
        with _database_setup_lock:
            if writable and database_identity not in _initialized_databases:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                _backup_before_schema_upgrade(connection, path, version)
                _initialize_schema(connection)
                _initialized_databases.add(database_identity)
            elif not writable:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in range(1, SCHEMA_VERSION + 1):
                    raise StateDatabaseError(
                        f"unsupported MetaFusion state schema version {version}"
                    )
            if database_identity not in _integrity_checked_databases:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise StateDatabaseError(
                        f"MetaFusion state integrity check failed: {integrity}"
                    )
                _integrity_checked_databases.add(database_identity)
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
    if version not in range(0, SCHEMA_VERSION + 1):
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
            error TEXT,
            summary TEXT,
            metrics TEXT
        );

        CREATE TABLE IF NOT EXISTS provider_health (
            provider_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            open_until TEXT,
            last_success_at TEXT,
            updated_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS asset_ownership (
            cache_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id TEXT,
            asset_type TEXT NOT NULL,
            season_number TEXT NOT NULL DEFAULT '',
            source_path TEXT,
            destination TEXT NOT NULL,
            checksum TEXT,
            PRIMARY KEY (cache_key, asset_type, season_number),
            FOREIGN KEY (cache_key) REFERENCES media_state(cache_key)
                ON DELETE CASCADE
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS asset_ownership_destination
            ON asset_ownership(destination);
        CREATE INDEX IF NOT EXISTS asset_ownership_canonical
            ON asset_ownership(
                media_type, tmdb_id, asset_type, season_number, source_path,
                destination
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

        CREATE TABLE IF NOT EXISTS item_retry_queue (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT,
            rating_key TEXT NOT NULL,
            media_type TEXT,
            plex_updated_at TEXT,
            status TEXT NOT NULL,
            failure_class TEXT,
            error_type TEXT,
            error_message TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            first_failed_at TEXT,
            last_failed_at TEXT,
            next_retry_at TEXT,
            started_at TEXT,
            PRIMARY KEY (server_id, library_uuid, rating_key)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS item_retry_due
            ON item_retry_queue(server_id, library_uuid, status, next_retry_at);

        CREATE TABLE IF NOT EXISTS plex_library_inventory (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT NOT NULL,
            library_type TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            missing_since TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (server_id, library_uuid)
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS identity_bindings (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            rating_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id TEXT NOT NULL,
            plex_fingerprint TEXT NOT NULL,
            confidence TEXT NOT NULL,
            source TEXT,
            match_reason TEXT,
            title TEXT,
            year INTEGER,
            validated_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            PRIMARY KEY (server_id, library_uuid, rating_key)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS identity_bindings_tmdb
            ON identity_bindings(media_type, tmdb_id);

        CREATE TABLE IF NOT EXISTS identity_binding_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            rating_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            previous_tmdb_id TEXT,
            tmdb_id TEXT,
            previous_plex_fingerprint TEXT,
            plex_fingerprint TEXT,
            confidence TEXT,
            source TEXT,
            reason_code TEXT NOT NULL,
            reason TEXT,
            occurred_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS identity_history_item
            ON identity_binding_history(
                server_id, library_uuid, rating_key, id DESC
            );

        CREATE TABLE IF NOT EXISTS unresolved_work (
            fingerprint TEXT PRIMARY KEY,
            library_name TEXT NOT NULL,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            category TEXT NOT NULL,
            detail TEXT,
            plex_rating_key TEXT,
            tmdb_id TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            edition TEXT,
            season_number INTEGER,
            identity_source TEXT,
            status TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            resolved_at TEXT
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS unresolved_work_status
            ON unresolved_work(status, library_name, category);

        CREATE TABLE IF NOT EXISTS artwork_analysis (
            source_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            source_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            image_format TEXT NOT NULL,
            sharpness REAL NOT NULL,
            blank INTEGER NOT NULL,
            perceptual_hash TEXT NOT NULL,
            analyzed_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS artwork_analysis_content
            ON artwork_analysis(content_sha256);

        CREATE TABLE IF NOT EXISTS cleanup_candidates (
            candidate_key TEXT PRIMARY KEY,
            server_id TEXT,
            library_uuid TEXT,
            library_name TEXT,
            rating_key TEXT,
            cache_key TEXT,
            media_type TEXT NOT NULL,
            title TEXT,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            edition TEXT,
            scope TEXT NOT NULL,
            season_number INTEGER,
            episode_number INTEGER,
            first_missing_at TEXT NOT NULL,
            last_confirmed_at TEXT NOT NULL,
            eligible_after TEXT NOT NULL,
            confirmations INTEGER NOT NULL DEFAULT 1,
            last_observation_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS cleanup_candidates_scope
            ON cleanup_candidates(status, server_id, library_uuid, media_type);

        CREATE TABLE IF NOT EXISTS cleanup_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            source TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            server_id TEXT,
            library_uuid TEXT,
            library_name TEXT,
            rating_key TEXT,
            cache_key TEXT,
            media_type TEXT,
            title TEXT,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            edition TEXT,
            output_type TEXT,
            season_number INTEGER,
            episode_number INTEGER,
            destination TEXT,
            checksum TEXT,
            reason TEXT,
            details TEXT
        );

        CREATE INDEX IF NOT EXISTS cleanup_history_item
            ON cleanup_history(library_name, rating_key, id DESC);
        CREATE INDEX IF NOT EXISTS cleanup_history_source
            ON cleanup_history(source, occurred_at DESC);

        CREATE TABLE IF NOT EXISTS cleanup_quarantine (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL,
            server_id TEXT,
            library_uuid TEXT,
            library_name TEXT,
            rating_key TEXT,
            cache_key TEXT,
            media_type TEXT,
            title TEXT,
            year INTEGER,
            tmdb_id TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            edition TEXT,
            output_type TEXT NOT NULL,
            season_number INTEGER,
            source_path TEXT NOT NULL,
            quarantine_path TEXT NOT NULL,
            checksum TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            quarantined_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            restored_at TEXT,
            purged_at TEXT,
            reason TEXT
        );

        CREATE INDEX IF NOT EXISTS cleanup_quarantine_status
            ON cleanup_quarantine(status, expires_at, id);

        CREATE TABLE IF NOT EXISTS item_exceptions (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT,
            rating_key TEXT NOT NULL,
            output_type TEXT NOT NULL,
            season_number INTEGER NOT NULL DEFAULT -1,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                server_id, library_uuid, rating_key, output_type, season_number
            )
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS item_exceptions_library
            ON item_exceptions(server_id, library_uuid, rating_key);

        CREATE TABLE IF NOT EXISTS identity_overrides (
            server_id TEXT NOT NULL,
            library_uuid TEXT NOT NULL,
            library_name TEXT,
            rating_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (server_id, library_uuid, rating_key)
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS identity_overrides_tmdb
            ON identity_overrides(media_type, tmdb_id);

        CREATE TABLE IF NOT EXISTS identity_review_queue (
            review_key TEXT PRIMARY KEY,
            server_id TEXT,
            library_uuid TEXT,
            library_name TEXT,
            rating_key TEXT,
            media_type TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER,
            proposed_tmdb_id TEXT,
            imdb_id TEXT,
            tvdb_id TEXT,
            category TEXT NOT NULL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            occurrences INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            resolved_at TEXT
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS identity_review_status
            ON identity_review_queue(status, library_name, last_seen DESC);

        CREATE TABLE IF NOT EXISTS library_rebinding_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            source_server_id TEXT,
            source_library_uuid TEXT NOT NULL,
            source_library_name TEXT,
            source_rating_key TEXT NOT NULL,
            destination_server_id TEXT,
            destination_library_uuid TEXT NOT NULL,
            destination_library_name TEXT,
            destination_rating_key TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            details TEXT
        );

        CREATE INDEX IF NOT EXISTS library_rebinding_recent
            ON library_rebinding_history(occurred_at DESC, id DESC);
        """
    )
    binding_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(identity_bindings)")
    }
    if "source" not in binding_columns:
        connection.execute("ALTER TABLE identity_bindings ADD COLUMN source TEXT")
    if "match_reason" not in binding_columns:
        connection.execute(
            "ALTER TABLE identity_bindings ADD COLUMN match_reason TEXT"
        )
    job_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(job_runs)")
    }
    if "summary" not in job_columns:
        connection.execute("ALTER TABLE job_runs ADD COLUMN summary TEXT")
    if "metrics" not in job_columns:
        connection.execute("ALTER TABLE job_runs ADD COLUMN metrics TEXT")
    unresolved_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(unresolved_work)")
    }
    for name, kind in (
        ("plex_rating_key", "TEXT"),
        ("tmdb_id", "TEXT"),
        ("imdb_id", "TEXT"),
        ("tvdb_id", "TEXT"),
        ("edition", "TEXT"),
        ("season_number", "INTEGER"),
        ("identity_source", "TEXT"),
    ):
        if name not in unresolved_columns:
            connection.execute(
                f"ALTER TABLE unresolved_work ADD COLUMN {name} {kind}"
            )
    if version < 3:
        _backfill_asset_ownership(connection)
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


def _source_key(provider, source_path):
    value = f"{str(provider or 'unknown').lower()}\0{source_path or ''!s}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def save_artwork_analysis(
    provider,
    source_path,
    analysis,
    *,
    path=None,
):
    """Persist content-derived artwork properties for later selection runs."""
    if not source_path or not isinstance(analysis, dict):
        return False
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO artwork_analysis(
                    source_key, provider, source_path, content_sha256,
                    width, height, image_format, sharpness, blank,
                    perceptual_hash, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    content_sha256 = excluded.content_sha256,
                    width = excluded.width,
                    height = excluded.height,
                    image_format = excluded.image_format,
                    sharpness = excluded.sharpness,
                    blank = excluded.blank,
                    perceptual_hash = excluded.perceptual_hash,
                    analyzed_at = excluded.analyzed_at
                """,
                (
                    _source_key(provider, source_path),
                    str(provider or "unknown").lower(),
                    str(source_path),
                    str(analysis.get("content_sha256") or ""),
                    max(0, int(analysis.get("width") or 0)),
                    max(0, int(analysis.get("height") or 0)),
                    str(analysis.get("format") or "unknown"),
                    float(analysis.get("sharpness") or 0.0),
                    int(bool(analysis.get("blank"))),
                    str(analysis.get("perceptual_hash") or ""),
                    str(analysis.get("analyzed_at") or utc_now()),
                ),
            )
        return True
    finally:
        connection.close()


def load_artwork_analysis(provider, source_path, *, path=None):
    """Load cached content properties without downloading the image again."""
    if not source_path:
        return None
    connection = _connect(path, writable=False)
    if connection is None:
        return None
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'artwork_analysis'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT content_sha256, width, height, image_format, sharpness, "
            "blank, perceptual_hash, analyzed_at FROM artwork_analysis "
            "WHERE source_key = ?",
            (_source_key(provider, source_path),),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["format"] = result.pop("image_format")
        result["blank"] = bool(result.get("blank"))
        return result
    finally:
        connection.close()


def _unresolved_fingerprint(record):
    fields = (
        str(record.get("library") or "Unknown library").strip(),
        str(record.get("media_type") or "Unknown").strip(),
        str(record.get("title") or "Unknown title").strip(),
        str(record.get("asset_type") or "metadata").strip(),
        str(record.get("category") or "unknown").strip(),
    )
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest(), fields


def _unresolved_work_class(asset_type):
    normalized = str(asset_type or "metadata").strip().lower()
    if normalized.startswith("season"):
        return "season"
    if normalized in {"poster", "background", "metadata"}:
        return normalized
    return "metadata"


def reconcile_unresolved_work(
    records,
    *,
    resolved_libraries=None,
    resolved_work=None,
    path=None,
    now=None,
):
    """Upsert current problems and resolve absent ones only for full-scan libraries."""
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    normalized = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        fingerprint, fields = _unresolved_fingerprint(record)
        library, media_type, title, asset_type, category = fields
        identity = report_identity(record)
        normalized[fingerprint] = (
            fingerprint,
            library,
            media_type,
            title,
            asset_type,
            category,
            str(record.get("detail") or ""),
            identity["plex_rating_key"],
            identity["tmdb_id"],
            identity["imdb_id"],
            identity["tvdb_id"],
            identity["edition"],
            identity["season_number"],
            identity["identity_source"],
        )
    resolved_scope = {
        str(value).casefold() for value in (resolved_libraries or []) if str(value)
    }
    work_scope = {
        str(library).casefold(): {
            _unresolved_work_class(asset_type)
            for asset_type in (asset_types or [])
        }
        for library, asset_types in (resolved_work or {}).items()
        if str(library)
    }
    resolved_scope.update(work_scope)
    try:
        with connection:
            for row in normalized.values():
                connection.execute(
                    """
                    INSERT INTO unresolved_work(
                        fingerprint, library_name, media_type, title,
                        asset_type, category, detail, plex_rating_key, tmdb_id,
                        imdb_id, tvdb_id, edition, season_number,
                        identity_source, status, occurrences, first_seen,
                        last_seen, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'open', 1, ?, ?, NULL)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        detail = excluded.detail,
                        plex_rating_key = COALESCE(
                            excluded.plex_rating_key, unresolved_work.plex_rating_key
                        ),
                        tmdb_id = COALESCE(excluded.tmdb_id, unresolved_work.tmdb_id),
                        imdb_id = COALESCE(excluded.imdb_id, unresolved_work.imdb_id),
                        tvdb_id = COALESCE(excluded.tvdb_id, unresolved_work.tvdb_id),
                        edition = COALESCE(excluded.edition, unresolved_work.edition),
                        season_number = COALESCE(
                            excluded.season_number, unresolved_work.season_number
                        ),
                        identity_source = COALESCE(
                            excluded.identity_source, unresolved_work.identity_source
                        ),
                        status = 'open',
                        occurrences = unresolved_work.occurrences + 1,
                        last_seen = excluded.last_seen,
                        resolved_at = NULL
                    """,
                    (*row, current, current),
                )
            if resolved_scope:
                open_rows = connection.execute(
                    "SELECT fingerprint, library_name, asset_type "
                    "FROM unresolved_work "
                    "WHERE status = 'open'"
                ).fetchall()
                resolved = [
                    str(row["fingerprint"])
                    for row in open_rows
                    if str(row["library_name"]).casefold() in resolved_scope
                    and (
                        not work_scope
                        or _unresolved_work_class(row["asset_type"])
                        in work_scope.get(
                            str(row["library_name"]).casefold(), set()
                        )
                    )
                    and str(row["fingerprint"]) not in normalized
                ]
                connection.executemany(
                    "UPDATE unresolved_work SET status = 'resolved', "
                    "resolved_at = ? WHERE fingerprint = ?",
                    ((current, fingerprint) for fingerprint in resolved),
                )
            connection.execute(
                "DELETE FROM unresolved_work WHERE status = 'resolved' "
                "AND fingerprint NOT IN (SELECT fingerprint FROM unresolved_work "
                "WHERE status = 'resolved' ORDER BY resolved_at DESC LIMIT ?)",
                (UNRESOLVED_HISTORY_LIMIT,),
            )
        return load_unresolved_work(path=path)
    finally:
        connection.close()


def load_unresolved_work(*, statuses=None, path=None, limit=10_000):
    """Return the bounded durable unresolved-work ledger for diagnostics."""
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    allowed = {
        str(value).lower()
        for value in (statuses or [])
        if str(value).lower() in {"open", "resolved"}
    }
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'unresolved_work'"
        ).fetchone()
        if table is None:
            return []
        where = ""
        parameters = []
        if allowed:
            placeholders = ", ".join("?" for _status in allowed)
            where = f" WHERE status IN ({placeholders})"
            parameters.extend(sorted(allowed))
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(unresolved_work)")
        }
        identity_columns = ", ".join(
            name if name in columns else f"NULL AS {name}"
            for name in (
                "plex_rating_key",
                "tmdb_id",
                "imdb_id",
                "tvdb_id",
                "edition",
                "season_number",
                "identity_source",
            )
        )
        rows = connection.execute(
            "SELECT fingerprint, library_name, media_type, title, asset_type, "
            f"category, detail, {identity_columns}, status, occurrences, "
            "first_seen, last_seen, resolved_at FROM unresolved_work" + where + " ORDER BY "
            "CASE status WHEN 'open' THEN 0 ELSE 1 END, last_seen DESC LIMIT ?",
            (*parameters, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _asset_rows(cache_key, entry, seasons=None):
    media_type = str(entry.get("media_type") or "unknown").lower()
    if media_type == "show":
        media_type = "tv"
    tmdb_id = entry.get("tmdb_id")
    tmdb_id = None if tmdb_id is None else str(tmdb_id)
    rows = []
    for asset_type in ("poster", "background"):
        destination = entry.get(f"{asset_type}_path")
        if not destination:
            continue
        rows.append(
            (
                str(cache_key),
                media_type,
                tmdb_id,
                asset_type,
                "",
                entry.get(f"{asset_type}_source_path"),
                normalize_destination(destination),
                entry.get(f"{asset_type}_checksum"),
            )
        )
    for season_number, season in (seasons or {}).items():
        if not isinstance(season, dict) or not season.get("season_path"):
            continue
        rows.append(
            (
                str(cache_key),
                media_type,
                tmdb_id,
                "season",
                str(season_number),
                season.get("season_source_path"),
                normalize_destination(season["season_path"]),
                season.get("season_checksum"),
            )
        )
    return rows


def _backfill_asset_ownership(connection):
    connection.execute("DELETE FROM asset_ownership")
    seasons: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT cache_key, season_number, payload FROM season_state"
    ):
        try:
            payload = json.loads(row[2])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            seasons.setdefault(row[0], {})[str(row[1])] = payload
    rows = []
    for row in connection.execute(
        "SELECT cache_key, payload FROM media_state"
    ):
        try:
            payload = json.loads(row[1])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            rows.extend(_asset_rows(row[0], payload, seasons.get(row[0])))
    if rows:
        connection.executemany(
            "INSERT OR REPLACE INTO asset_ownership("
            "cache_key, media_type, tmdb_id, asset_type, season_number, "
            "source_path, destination, checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


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


def load_application_record(key, *, path=None):
    """Load one JSON object from durable application state without creating it."""
    connection = _connect(path, writable=False)
    if connection is None:
        return None
    try:
        raw = _state_value(connection, str(key))
        if raw is None:
            return None
        return _json_load(raw, f"application state {key!r}")
    finally:
        connection.close()


def save_application_record(key, value, *, path=None):
    """Atomically replace one JSON object in durable application state."""
    if not isinstance(value, dict):
        raise TypeError("application state records must be dictionaries")
    connection = _connect(path, writable=True)
    try:
        with connection:
            _set_state_value(connection, str(key), _json_dump(value))
        return True
    finally:
        connection.close()


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

    def entries_for_scope(self, server_id, library_uuid, rating_keys=None):
        """Load only the media rows needed to plan one Plex library."""
        server_id = str(server_id or "unknown")
        library_uuid = str(library_uuid)
        wanted = {
            str(value) for value in (rating_keys or []) if str(value).strip()
        }
        entries = {}
        if self._connection is not None:
            if wanted:
                ordered = sorted(wanted)
                for offset in range(0, len(ordered), 500):
                    chunk = ordered[offset : offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    query = (
                        "SELECT cache_key, payload FROM media_state "
                        "WHERE server_id = ? AND library_uuid = ? "
                        f"AND rating_key IN ({placeholders})"
                    )
                    for row in self._connection.execute(
                        query, (server_id, library_uuid, *chunk)
                    ):
                        entries[row[0]] = _json_load(
                            row[1], f"media state {row[0]!r}"
                        )
            else:
                for row in self._connection.execute(
                    "SELECT cache_key, payload FROM media_state "
                    "WHERE server_id = ? AND library_uuid = ?",
                    (server_id, library_uuid),
                ):
                    entries[row[0]] = _json_load(
                        row[1], f"media state {row[0]!r}"
                    )
            keys = sorted(entries)
            for offset in range(0, len(keys), 500):
                chunk = keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in self._connection.execute(
                    "SELECT cache_key, season_number, payload FROM season_state "
                    f"WHERE cache_key IN ({placeholders})",
                    chunk,
                ):
                    entries[row[0]].setdefault("seasons", {})[str(row[1])] = (
                        _json_load(
                            row[2], f"season state {row[0]!r}/{row[1]!r}"
                        )
                    )

        for source in (self._memory, self._pending):
            for key, value in source.items():
                if (
                    str(value.get("server_id") or "unknown") == server_id
                    and str(value.get("library_uuid")) == library_uuid
                    and (
                        not wanted
                        or str(value.get("rating_key")) in wanted
                    )
                ):
                    entries[key] = copy.deepcopy(value)
        for key in self._deleted:
            entries.pop(key, None)
        return entries

    def asset_destination_records(self, scopes=None):
        """Return indexed artwork ownership, optionally limited to scopes."""
        records: list[dict[str, Any]] = []
        if self._connection is not None:
            table_exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'asset_ownership'"
            ).fetchone()
            if table_exists is None:
                return records
            if not scopes:
                return [
                    dict(row)
                    for row in self._connection.execute(
                        "SELECT cache_key, media_type, tmdb_id, asset_type, "
                        "season_number, source_path, destination, checksum "
                        "FROM asset_ownership"
                    )
                ]
            for scope in scopes or []:
                server_id = str(scope.get("server_id") or "unknown")
                library_uuid = str(
                    scope.get("library_uuid") or scope.get("library_name")
                )
                for row in self._connection.execute(
                    "SELECT assets.cache_key, assets.media_type, assets.tmdb_id, "
                    "assets.asset_type, assets.season_number, assets.source_path, "
                    "assets.destination, assets.checksum "
                    "FROM asset_ownership AS assets "
                    "JOIN media_state AS media ON media.cache_key = assets.cache_key "
                    "WHERE media.server_id = ? AND media.library_uuid = ?",
                    (server_id, library_uuid),
                ):
                    records.append(dict(row))
        return records

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
                    self._connection.execute(
                        "DELETE FROM asset_ownership WHERE cache_key = ?", (key,)
                    )
                    asset_rows = _asset_rows(key, entry, seasons)
                    if asset_rows:
                        self._connection.executemany(
                            "INSERT INTO asset_ownership("
                            "cache_key, media_type, tmdb_id, asset_type, "
                            "season_number, source_path, destination, checksum) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            asset_rows,
                        )
            self._pending.clear()
            self._deleted.clear()
            return True
        except (sqlite3.Error, TypeError, ValueError) as error:
            with suppress(sqlite3.Error):
                self._connection.rollback()
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


def save_plex_metadata_ownership(
    records,
    deleted=None,
    path=None,
    prune_scope=None,
    valid_child_keys=None,
):
    """Persist an item's Plex ownership ledger in one durable transaction."""
    records = list(records or [])
    deleted = list(deleted or [])
    if not records and not deleted and prune_scope is None:
        return False
    connection = _connect(path, writable=True)
    try:
        with connection:
            if prune_scope is not None:
                valid_children = {str(value) for value in (valid_child_keys or [])}
                server_id, library_uuid, rating_key = (
                    str(value) for value in prune_scope
                )
                existing_children = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT child_key FROM plex_metadata_ownership "
                        "WHERE server_id = ? AND library_uuid = ? AND rating_key = ?",
                        (server_id, library_uuid, rating_key),
                    )
                }
                stale_children = existing_children - valid_children
                connection.executemany(
                    "DELETE FROM plex_metadata_ownership WHERE server_id = ? "
                    "AND library_uuid = ? AND rating_key = ? AND child_key = ?",
                    (
                        (server_id, library_uuid, rating_key, child_key)
                        for child_key in stale_children
                    ),
                )
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


def classify_item_failure(error):
    """Classify failures for bounded automatic retry without endless loops."""
    message = str(error or "").strip()
    lowered = message.casefold()
    if any(marker in lowered for marker in _PERMANENT_FAILURE_MARKERS):
        return "permanent"
    transient_markers = (
        "429",
        "5xx",
        "circuit",
        "connection",
        "temporar",
        "timeout",
        "timed out",
        "rate limit",
        "disk pressure",
        "no space",
        "unavailable",
        "interrupted",
        "empty or rejected response",
    )
    if any(marker in lowered for marker in transient_markers):
        return "transient"
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return "transient"
    return "transient"


def mark_item_started(
    server_id,
    library_uuid,
    rating_key,
    *,
    library_name=None,
    media_type=None,
    plex_updated_at=None,
    path=None,
    now=None,
):
    """Persist an in-flight marker so abrupt container stops are recoverable."""
    return mark_items_started(
        server_id,
        library_uuid,
        [
            {
                "rating_key": rating_key,
                "library_name": library_name,
                "media_type": media_type,
                "plex_updated_at": plex_updated_at,
            }
        ],
        path=path,
        now=now,
    )


def mark_items_started(
    server_id,
    library_uuid,
    items,
    *,
    path=None,
    now=None,
):
    """Persist one library's selected work in a single durable transaction."""
    items = list(items or [])
    if not items:
        return False
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    server_id = str(server_id or "unknown")
    library_uuid = str(library_uuid)
    try:
        with connection:
            connection.executemany(
                """
                INSERT INTO item_retry_queue(
                    server_id, library_uuid, library_name, rating_key,
                    media_type, plex_updated_at, status, attempts, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', 0, ?)
                ON CONFLICT(server_id, library_uuid, rating_key) DO UPDATE SET
                    library_name = excluded.library_name,
                    media_type = excluded.media_type,
                    attempts = CASE
                        WHEN item_retry_queue.plex_updated_at IS NOT excluded.plex_updated_at
                        THEN 0 ELSE item_retry_queue.attempts END,
                    first_failed_at = CASE
                        WHEN item_retry_queue.plex_updated_at IS NOT excluded.plex_updated_at
                        THEN NULL ELSE item_retry_queue.first_failed_at END,
                    plex_updated_at = excluded.plex_updated_at,
                    status = 'running',
                    started_at = excluded.started_at
                """,
                (
                    (
                        server_id,
                        library_uuid,
                        item.get("library_name"),
                        str(item.get("rating_key")),
                        item.get("media_type"),
                        item.get("plex_updated_at"),
                        current,
                    )
                    for item in items
                    if item.get("rating_key") is not None
                ),
            )
        return True
    finally:
        connection.close()


def record_item_failure(
    server_id,
    library_uuid,
    rating_key,
    error,
    *,
    library_name=None,
    media_type=None,
    plex_updated_at=None,
    failure_class=None,
    path=None,
    now=None,
):
    """Persist a bounded retry or parked permanent failure for one Plex item."""
    connection = _connect(path, writable=True)
    current = _as_utc(now)
    normalized_class = failure_class or classify_item_failure(error)
    normalized_class = (
        normalized_class if normalized_class in {"transient", "permanent"}
        else "transient"
    )
    key = (
        str(server_id or "unknown"),
        str(library_uuid),
        str(rating_key),
    )
    try:
        existing = connection.execute(
            "SELECT attempts, first_failed_at, plex_updated_at FROM item_retry_queue "
            "WHERE server_id = ? AND library_uuid = ? AND rating_key = ?",
            key,
        ).fetchone()
        changed_item = bool(
            existing
            and plex_updated_at is not None
            and existing[2] is not None
            and str(existing[2]) != str(plex_updated_at)
        )
        attempts = 1 if existing is None or changed_item else int(existing[0] or 0) + 1
        first_failed = (
            current.isoformat()
            if existing is None or changed_item or not existing[1]
            else str(existing[1])
        )
        if normalized_class == "permanent" or attempts > len(_RETRY_DELAYS):
            status = "parked"
            next_retry = None
        else:
            status = "pending"
            next_retry = (current + _RETRY_DELAYS[attempts - 1]).isoformat()
        message = str(error or "Unknown item failure").replace("\n", " ")[:500]
        with connection:
            connection.execute(
                """
                INSERT INTO item_retry_queue(
                    server_id, library_uuid, library_name, rating_key,
                    media_type, plex_updated_at, status, failure_class,
                    error_type, error_message, attempts, first_failed_at,
                    last_failed_at, next_retry_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(server_id, library_uuid, rating_key) DO UPDATE SET
                    library_name = excluded.library_name,
                    media_type = excluded.media_type,
                    plex_updated_at = excluded.plex_updated_at,
                    status = excluded.status,
                    failure_class = excluded.failure_class,
                    error_type = excluded.error_type,
                    error_message = excluded.error_message,
                    attempts = excluded.attempts,
                    first_failed_at = excluded.first_failed_at,
                    last_failed_at = excluded.last_failed_at,
                    next_retry_at = excluded.next_retry_at,
                    started_at = NULL
                """,
                (
                    *key[:2],
                    library_name,
                    key[2],
                    media_type,
                    plex_updated_at,
                    status,
                    normalized_class,
                    type(error).__name__ if error is not None else "RuntimeError",
                    message,
                    attempts,
                    first_failed,
                    current.isoformat(),
                    next_retry,
                ),
            )
        result = {
            "status": status,
            "attempts": attempts,
            "next_retry_at": next_retry,
            "failure_class": normalized_class,
        }
        logger = logging.getLogger(__name__)
        if status == "pending":
            logger.info(
                "[Recovery] Deferred %s/%s for automatic retry %s "
                "(attempt %d).",
                library_name or library_uuid,
                rating_key,
                next_retry,
                attempts,
            )
        else:
            logger.warning(
                "[Recovery] Parked %s/%s after %d attempt(s); deadline retries "
                "stop until the item changes or a full, targeted, or "
                "configuration-triggered evaluation succeeds.",
                library_name or library_uuid,
                rating_key,
                attempts,
            )
        return result
    finally:
        connection.close()


def clear_item_retry(server_id, library_uuid, rating_key, path=None):
    return clear_item_retries(
        server_id,
        library_uuid,
        [rating_key],
        path=path,
    )


def clear_item_retries(server_id, library_uuid, rating_keys, path=None):
    rating_keys = {
        str(rating_key)
        for rating_key in (rating_keys or [])
        if rating_key is not None
    }
    if not rating_keys:
        return 0
    connection = _connect(path, writable=True)
    try:
        with connection:
            before = connection.total_changes
            connection.executemany(
                "DELETE FROM item_retry_queue WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ?",
                (
                    (str(server_id or "unknown"), str(library_uuid), rating_key)
                    for rating_key in rating_keys
                ),
            )
            removed = connection.total_changes - before
        return max(0, int(removed))
    finally:
        connection.close()


def load_due_item_retries(server_id, library_uuid, path=None, now=None):
    """Return interrupted or deadline-due rating keys for one library."""
    connection = _connect(path, writable=False)
    if connection is None:
        return {}
    current = _as_utc(now).isoformat()
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'item_retry_queue'"
        ).fetchone() is None:
            return {}
        rows = connection.execute(
            "SELECT * FROM item_retry_queue WHERE server_id = ? "
            "AND library_uuid = ? AND (status = 'running' OR "
            "(status = 'pending' AND next_retry_at <= ?))",
            (str(server_id or "unknown"), str(library_uuid), current),
        ).fetchall()
        return {str(row["rating_key"]): dict(row) for row in rows}
    finally:
        connection.close()


def retry_queue_summary(path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return {}
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'item_retry_queue'"
        ).fetchone() is None:
            return {}
        return {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM item_retry_queue GROUP BY status"
            )
        }
    finally:
        connection.close()


def load_item_retries(
    *,
    server_id=None,
    library_uuids=None,
    library_names=None,
    rating_keys=None,
    statuses=None,
    path=None,
):
    """Return value-safe retry rows for an explicit selective retry command."""
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    allowed_uuids = {
        str(value) for value in (library_uuids or []) if str(value).strip()
    }
    allowed_names = {
        str(value).casefold() for value in (library_names or []) if str(value).strip()
    }
    allowed_keys = {
        str(value) for value in (rating_keys or []) if str(value).strip()
    }
    allowed_statuses = {
        str(value).lower() for value in (statuses or []) if str(value).strip()
    }
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'item_retry_queue'"
        ).fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT server_id, library_uuid, library_name, rating_key, "
            "media_type, status, failure_class, attempts, next_retry_at "
            "FROM item_retry_queue ORDER BY library_name, rating_key"
        ).fetchall()
        selected = []
        for row in rows:
            record = dict(row)
            if server_id is not None and str(record["server_id"]) != str(server_id):
                continue
            if allowed_uuids and str(record["library_uuid"]) not in allowed_uuids:
                continue
            if allowed_names and str(record.get("library_name") or "").casefold() not in allowed_names:
                continue
            if allowed_keys and str(record["rating_key"]) not in allowed_keys:
                continue
            if allowed_statuses and str(record["status"]).lower() not in allowed_statuses:
                continue
            selected.append(record)
        return selected
    finally:
        connection.close()


def reconcile_library_inventory(server_id, libraries, path=None, now=None):
    """Persist auto-discovered Plex libraries without treating absence as deletion."""
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    server_id = str(server_id or "unknown")
    normalized = []
    for library in libraries or []:
        library_uuid = str(
            library.get("uuid") or library.get("key") or library.get("title")
        )
        normalized.append(
            (
                server_id,
                library_uuid,
                str(library.get("title") or library_uuid),
                str(library.get("type") or "unknown"),
            )
        )
    seen = {row[1] for row in normalized}
    try:
        previous = {
            str(row["library_uuid"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM plex_library_inventory WHERE server_id = ?",
                (server_id,),
            )
        }
        with connection:
            for row in normalized:
                connection.execute(
                    """
                    INSERT INTO plex_library_inventory(
                        server_id, library_uuid, library_name, library_type,
                        first_seen, last_seen, missing_since, active
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1)
                    ON CONFLICT(server_id, library_uuid) DO UPDATE SET
                        library_name = excluded.library_name,
                        library_type = excluded.library_type,
                        last_seen = excluded.last_seen,
                        missing_since = NULL,
                        active = 1
                    """,
                    (*row, current, current),
                )
            missing = sorted(set(previous) - seen)
            for library_uuid in missing:
                connection.execute(
                    "UPDATE plex_library_inventory SET active = 0, "
                    "missing_since = COALESCE(missing_since, ?) "
                    "WHERE server_id = ? AND library_uuid = ?",
                    (current, server_id, library_uuid),
                )
        return [previous[key] for key in sorted(set(previous) - seen)]
    finally:
        connection.close()


def missing_library_inventory(server_id, libraries, path=None):
    """Compare auto-discovery with durable inventory without writing state."""
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    server_id = str(server_id or "unknown")
    seen = {
        str(library.get("uuid") or library.get("key") or library.get("title"))
        for library in libraries or []
    }
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'plex_library_inventory'"
        ).fetchone() is None:
            return []
        rows = connection.execute(
            "SELECT * FROM plex_library_inventory WHERE server_id = ?",
            (server_id,),
        ).fetchall()
        return [dict(row) for row in rows if str(row["library_uuid"]) not in seen]
    finally:
        connection.close()


def _append_identity_history(
    connection,
    *,
    server_id,
    library_uuid,
    rating_key,
    media_type,
    event_type,
    reason_code,
    occurred_at,
    previous_tmdb_id=None,
    tmdb_id=None,
    previous_plex_fingerprint=None,
    plex_fingerprint=None,
    confidence=None,
    source=None,
    reason=None,
):
    values = (
        str(server_id or "unknown"),
        str(library_uuid),
        str(rating_key),
        str(media_type),
        str(event_type),
        None if previous_tmdb_id is None else str(previous_tmdb_id),
        None if tmdb_id is None else str(tmdb_id),
        (
            None
            if previous_plex_fingerprint is None
            else str(previous_plex_fingerprint)
        ),
        None if plex_fingerprint is None else str(plex_fingerprint),
        None if confidence is None else str(confidence),
        None if source is None else str(source),
        str(reason_code),
        None if reason is None else str(reason),
        str(occurred_at),
    )
    previous = connection.execute(
        "SELECT event_type, previous_tmdb_id, tmdb_id, "
        "previous_plex_fingerprint, plex_fingerprint, reason_code "
        "FROM identity_binding_history WHERE server_id = ? "
        "AND library_uuid = ? AND rating_key = ? ORDER BY id DESC LIMIT 1",
        values[:3],
    ).fetchone()
    signature = (
        values[4],
        values[5],
        values[6],
        values[7],
        values[8],
        values[11],
    )
    if previous is not None and tuple(previous) == signature:
        return False
    connection.execute(
        """
        INSERT INTO identity_binding_history(
            server_id, library_uuid, rating_key, media_type, event_type,
            previous_tmdb_id, tmdb_id, previous_plex_fingerprint,
            plex_fingerprint, confidence, source, reason_code, reason,
            occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    connection.execute(
        "DELETE FROM identity_binding_history WHERE id NOT IN "
        "(SELECT id FROM identity_binding_history ORDER BY id DESC LIMIT ?)",
        (IDENTITY_HISTORY_LIMIT,),
    )
    return True


def save_identity_binding(
    server_id,
    library_uuid,
    rating_key,
    media_type,
    tmdb_id,
    plex_fingerprint,
    *,
    title=None,
    year=None,
    confidence="high",
    source="trusted_external_id",
    match_reason=None,
    path=None,
    now=None,
):
    if not tmdb_id or not plex_fingerprint or confidence != "high":
        return False
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            identity = (
                str(server_id or "unknown"),
                str(library_uuid),
                str(rating_key),
            )
            previous = connection.execute(
                "SELECT * FROM identity_bindings WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ?",
                identity,
            ).fetchone()
            previous = dict(previous) if previous is not None else None
            core_changed = previous is None or any(
                str(previous.get(field) or "") != str(value or "")
                for field, value in (
                    ("tmdb_id", tmdb_id),
                    ("plex_fingerprint", plex_fingerprint),
                    ("confidence", confidence),
                )
            )
            if previous is None:
                connection.execute(
                    """
                    INSERT INTO identity_bindings(
                        server_id, library_uuid, rating_key, media_type,
                        tmdb_id, plex_fingerprint, confidence, source,
                        match_reason, title, year, validated_at, last_used_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *identity,
                        str(media_type),
                        str(tmdb_id),
                        str(plex_fingerprint),
                        confidence,
                        source,
                        match_reason,
                        title,
                        year,
                        current,
                        current,
                    ),
                )
                _append_identity_history(
                    connection,
                    server_id=identity[0],
                    library_uuid=identity[1],
                    rating_key=identity[2],
                    media_type=media_type,
                    event_type="established",
                    tmdb_id=tmdb_id,
                    plex_fingerprint=plex_fingerprint,
                    confidence=confidence,
                    source=source,
                    reason_code="first_high_confidence_match",
                    reason=match_reason,
                    occurred_at=current,
                )
            elif core_changed:
                tmdb_changed = str(previous.get("tmdb_id")) != str(tmdb_id)
                fingerprint_changed = str(previous.get("plex_fingerprint")) != str(
                    plex_fingerprint
                )
                if tmdb_changed and fingerprint_changed:
                    reason_code = "tmdb_and_plex_guid_changed"
                elif tmdb_changed:
                    reason_code = "tmdb_identity_changed"
                elif fingerprint_changed:
                    reason_code = "plex_guid_fingerprint_changed"
                else:
                    reason_code = "confidence_changed"
                _append_identity_history(
                    connection,
                    server_id=identity[0],
                    library_uuid=identity[1],
                    rating_key=identity[2],
                    media_type=previous.get("media_type") or media_type,
                    event_type="invalidated",
                    previous_tmdb_id=previous.get("tmdb_id"),
                    previous_plex_fingerprint=previous.get("plex_fingerprint"),
                    plex_fingerprint=plex_fingerprint,
                    confidence=previous.get("confidence"),
                    source=previous.get("source"),
                    reason_code=reason_code,
                    reason=match_reason,
                    occurred_at=current,
                )
                connection.execute(
                    """
                    UPDATE identity_bindings SET media_type = ?, tmdb_id = ?,
                        plex_fingerprint = ?, confidence = ?, source = ?,
                        match_reason = ?, title = ?, year = ?,
                        validated_at = ?, last_used_at = ?
                    WHERE server_id = ? AND library_uuid = ? AND rating_key = ?
                    """,
                    (
                        str(media_type),
                        str(tmdb_id),
                        str(plex_fingerprint),
                        confidence,
                        source,
                        match_reason,
                        title,
                        year,
                        current,
                        current,
                        *identity,
                    ),
                )
                _append_identity_history(
                    connection,
                    server_id=identity[0],
                    library_uuid=identity[1],
                    rating_key=identity[2],
                    media_type=media_type,
                    event_type="established",
                    previous_tmdb_id=previous.get("tmdb_id"),
                    tmdb_id=tmdb_id,
                    previous_plex_fingerprint=previous.get("plex_fingerprint"),
                    plex_fingerprint=plex_fingerprint,
                    confidence=confidence,
                    source=source,
                    reason_code="replacement_high_confidence_match",
                    reason=match_reason,
                    occurred_at=current,
                )
            elif previous.get("source") is None or previous.get("match_reason") is None:
                connection.execute(
                    "UPDATE identity_bindings SET source = COALESCE(source, ?), "
                    "match_reason = COALESCE(match_reason, ?) WHERE server_id = ? "
                    "AND library_uuid = ? AND rating_key = ?",
                    (source, match_reason, *identity),
                )
        return True
    finally:
        connection.close()


def load_identity_binding(
    server_id,
    library_uuid,
    rating_key,
    plex_fingerprint,
    path=None,
    now=None,
    touch=False,
    record_mismatch=False,
):
    connection = _connect(path, writable=False)
    if connection is None:
        return None
    result = None
    mismatch = False
    try:
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'identity_bindings'"
        ).fetchone() is None:
            return None
        row = connection.execute(
            "SELECT * FROM identity_bindings WHERE server_id = ? "
            "AND library_uuid = ? AND rating_key = ?",
            (str(server_id or "unknown"), str(library_uuid), str(rating_key)),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        mismatch = bool(
            result.get("confidence") != "high"
            or result.get("plex_fingerprint") != str(plex_fingerprint)
        )
    finally:
        connection.close()
    if mismatch:
        if record_mismatch:
            try:
                record_identity_binding_mismatch(
                    server_id,
                    library_uuid,
                    rating_key,
                    plex_fingerprint,
                    path=path,
                    now=now,
                )
            except StateDatabaseError as error:
                logging.getLogger(__name__).warning(
                    "[Identity] Binding history | Failed to record mismatch | "
                    "Error: %s",
                    error,
                )
        return None
    if not touch:
        return result
    writable = _connect(path, writable=True)
    try:
        with writable:
            writable.execute(
                "UPDATE identity_bindings SET last_used_at = ? WHERE "
                "server_id = ? AND library_uuid = ? AND rating_key = ? "
                "AND last_used_at != ?",
                (
                    _as_utc(now).isoformat(),
                    str(server_id or "unknown"),
                    str(library_uuid),
                    str(rating_key),
                    _as_utc(now).isoformat(),
                ),
            )
    finally:
        writable.close()
    return result


def record_identity_binding_mismatch(
    server_id,
    library_uuid,
    rating_key,
    plex_fingerprint,
    *,
    path=None,
    now=None,
):
    """Record one non-mutating bypass when current Plex GUIDs changed."""
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    identity = (
        str(server_id or "unknown"),
        str(library_uuid),
        str(rating_key),
    )
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM identity_bindings WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ?",
                identity,
            ).fetchone()
            if previous is None:
                return False
            previous = dict(previous)
            if str(previous.get("plex_fingerprint")) == str(plex_fingerprint):
                return False
            return _append_identity_history(
                connection,
                server_id=identity[0],
                library_uuid=identity[1],
                rating_key=identity[2],
                media_type=previous.get("media_type") or "unknown",
                event_type="bypassed",
                previous_tmdb_id=previous.get("tmdb_id"),
                previous_plex_fingerprint=previous.get("plex_fingerprint"),
                plex_fingerprint=plex_fingerprint,
                confidence=previous.get("confidence"),
                source=previous.get("source"),
                reason_code="plex_guid_fingerprint_changed",
                reason=(
                    "The current Plex provider GUID fingerprint differs from the "
                    "stored high-confidence binding; the stored binding was not reused."
                ),
                occurred_at=current,
            )
    finally:
        connection.close()


def inspect_identity_binding(
    server_id,
    library_uuid,
    rating_key,
    *,
    current_fingerprint=None,
    path=None,
    history_limit=50,
):
    """Return active binding and bounded history without touching durable state."""
    connection = _connect(path, writable=False)
    if connection is None:
        return {
            "status": "missing",
            "active": None,
            "history": [],
            "history_available": False,
            "schema_version": None,
        }
    identity = (
        str(server_id or "unknown"),
        str(library_uuid),
        str(rating_key),
    )
    try:
        schema_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        active_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'identity_bindings'"
        ).fetchone()
        active = None
        if active_table is not None:
            row = connection.execute(
                "SELECT * FROM identity_bindings WHERE server_id = ? "
                "AND library_uuid = ? AND rating_key = ?",
                identity,
            ).fetchone()
            active = dict(row) if row is not None else None
        history_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'identity_binding_history'"
        ).fetchone()
        history = []
        if history_table is not None:
            limit = max(1, min(100, int(history_limit)))
            history = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM identity_binding_history WHERE server_id = ? "
                    "AND library_uuid = ? AND rating_key = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (*identity, limit),
                ).fetchall()
            ]
    finally:
        connection.close()
    if active is None:
        status = "missing"
    elif current_fingerprint is None:
        status = "unverifiable"
    elif str(active.get("plex_fingerprint")) == str(current_fingerprint):
        status = "current"
    else:
        status = "stale"
    return {
        "status": status,
        "active": active,
        "history": history,
        "history_available": history_table is not None,
        "schema_version": schema_version,
    }


def find_media_state(
    *,
    libraries=None,
    rating_keys=None,
    tmdb_ids=None,
    media_types=None,
    path=None,
    limit=100_000,
):
    """Return normalized durable item records without touching their timestamps."""
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values: list[Any] = []
    filters = (
        ("library_name", libraries),
        ("rating_key", rating_keys),
        ("tmdb_id", tmdb_ids),
        ("media_type", media_types),
    )
    for column, requested in filters:
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = "SELECT * FROM media_state"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY library_name, media_type, title, year, rating_key LIMIT ?"
    values.append(max(1, min(1_000_000, int(limit))))
    try:
        rows = connection.execute(query, values).fetchall()
        results = []
        for row in rows:
            record = dict(row)
            payload = _json_load(record.pop("payload"), "media state")
            record["payload"] = payload
            for key in ("imdb_id", "tvdb_id", "edition", "identity_source"):
                record[key] = payload.get(key)
            results.append(record)
        return results
    finally:
        connection.close()


def load_asset_ownership(cache_keys=None, *, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    requested = [str(value) for value in (cache_keys or []) if str(value).strip()]
    query = "SELECT * FROM asset_ownership"
    parameters = []
    if requested:
        query += f" WHERE cache_key IN ({','.join('?' for _ in requested)})"
        parameters.extend(requested)
    query += " ORDER BY cache_key, asset_type, season_number"
    try:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    finally:
        connection.close()


def remove_asset_ownership(cache_key, asset_type, season_number=None, *, path=None):
    """Forget one artwork claim while retaining the media's incremental state."""
    normalized_type = str(asset_type).lower()
    normalized_season = "" if season_number is None else str(int(season_number))
    connection = _connect(path, writable=True)
    try:
        with connection:
            row = connection.execute(
                "SELECT payload FROM media_state WHERE cache_key = ?",
                (str(cache_key),),
            ).fetchone()
            if row is None:
                return False
            payload = _json_load(row[0], "media state")
            if normalized_type in {"poster", "background"}:
                for suffix in (
                    "path", "checksum", "provider", "source_path", "average",
                    "candidate_source_path", "candidate_fingerprint",
                ):
                    payload.pop(f"{normalized_type}_{suffix}", None)
            elif normalized_type == "season":
                season_row = connection.execute(
                    "SELECT payload FROM season_state WHERE cache_key = ? "
                    "AND season_number = ?",
                    (str(cache_key), normalized_season),
                ).fetchone()
                season = (
                    _json_load(season_row[0], "season state")
                    if season_row is not None
                    else None
                )
                if isinstance(season, dict):
                    for key in list(season):
                        if key.startswith("season_") and key not in {
                            "season_last_checked", "season_last_upgraded"
                        }:
                            season.pop(key, None)
                    connection.execute(
                        "UPDATE season_state SET payload = ? WHERE cache_key = ? "
                        "AND season_number = ?",
                        (_json_dump(season), str(cache_key), normalized_season),
                    )
            else:
                raise ValueError(f"Unsupported artwork output type: {asset_type}")
            connection.execute(
                "DELETE FROM asset_ownership WHERE cache_key = ? "
                "AND asset_type = ? AND season_number = ?",
                (str(cache_key), normalized_type, normalized_season),
            )
            connection.execute(
                "UPDATE media_state SET payload = ? WHERE cache_key = ?",
                (_json_dump(payload), str(cache_key)),
            )
        return True
    finally:
        connection.close()


def load_item_exceptions(
    server_id=None,
    library_uuid=None,
    *,
    libraries=None,
    rating_keys=None,
    path=None,
):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values: list[Any] = []
    for column, value in (("server_id", server_id), ("library_uuid", library_uuid)):
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(str(value))
    for column, requested in (("library_name", libraries), ("rating_key", rating_keys)):
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = "SELECT * FROM item_exceptions"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY library_name, rating_key, output_type, season_number"
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_exceptions'"
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(query, values).fetchall()]
    finally:
        connection.close()


def save_item_exception(
    server_id,
    library_uuid,
    rating_key,
    output_type,
    *,
    library_name=None,
    season_number=None,
    reason=None,
    path=None,
    now=None,
):
    current = _as_utc(now).isoformat()
    output = str(output_type).lower()
    if output not in {"all", "metadata", "plex_metadata", "poster", "background", "season", "cleanup"}:
        raise ValueError(f"Unsupported exception output: {output_type}")
    season = -1 if season_number is None else int(season_number)
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO item_exceptions(
                    server_id, library_uuid, library_name, rating_key,
                    output_type, season_number, reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, library_uuid, rating_key, output_type, season_number)
                DO UPDATE SET library_name = excluded.library_name,
                    reason = excluded.reason, updated_at = excluded.updated_at
                """,
                (
                    str(server_id or "unknown"), str(library_uuid), library_name,
                    str(rating_key), output, season, reason, current, current,
                ),
            )
        return True
    finally:
        connection.close()


def remove_item_exception(
    server_id,
    library_uuid,
    rating_key,
    output_type=None,
    *,
    season_number=None,
    path=None,
):
    connection = _connect(path, writable=True)
    try:
        with connection:
            if output_type is None:
                cursor = connection.execute(
                    "DELETE FROM item_exceptions WHERE server_id = ? "
                    "AND library_uuid = ? AND rating_key = ?",
                    (str(server_id or "unknown"), str(library_uuid), str(rating_key)),
                )
            else:
                season = -1 if season_number is None else int(season_number)
                cursor = connection.execute(
                    "DELETE FROM item_exceptions WHERE server_id = ? "
                    "AND library_uuid = ? AND rating_key = ? AND output_type = ? "
                    "AND season_number = ?",
                    (
                        str(server_id or "unknown"), str(library_uuid), str(rating_key),
                        str(output_type).lower(), season,
                    ),
                )
        return int(cursor.rowcount)
    finally:
        connection.close()


def load_identity_overrides(
    server_id=None,
    library_uuid=None,
    *,
    libraries=None,
    rating_keys=None,
    include_inactive=False,
    path=None,
):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = [] if include_inactive else ["active = 1"]
    values = []
    for column, value in (("server_id", server_id), ("library_uuid", library_uuid)):
        if value is not None:
            clauses.append(f"{column} = ?")
            values.append(str(value))
    for column, requested in (("library_name", libraries), ("rating_key", rating_keys)):
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = "SELECT * FROM identity_overrides"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY library_name, rating_key"
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_overrides'"
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(query, values).fetchall()]
    finally:
        connection.close()


def save_identity_override(
    server_id,
    library_uuid,
    rating_key,
    media_type,
    tmdb_id,
    *,
    library_name=None,
    reason=None,
    path=None,
    now=None,
):
    normalized_type = "tv" if str(media_type).lower() in {"show", "shows", "tv"} else "movie"
    if not str(tmdb_id).isdigit():
        raise ValueError("Identity override TMDb ID must be numeric")
    current = _as_utc(now).isoformat()
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO identity_overrides(
                    server_id, library_uuid, library_name, rating_key,
                    media_type, tmdb_id, reason, created_at, updated_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(server_id, library_uuid, rating_key) DO UPDATE SET
                    library_name = excluded.library_name,
                    media_type = excluded.media_type,
                    tmdb_id = excluded.tmdb_id,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at,
                    active = 1
                """,
                (
                    str(server_id or "unknown"), str(library_uuid), library_name,
                    str(rating_key), normalized_type, str(tmdb_id), reason,
                    current, current,
                ),
            )
            connection.execute(
                "UPDATE identity_review_queue SET status='resolved', resolved_at=? "
                "WHERE server_id=? AND library_uuid=? AND rating_key=? "
                "AND status='open'",
                (
                    current,
                    str(server_id or "unknown"),
                    str(library_uuid),
                    str(rating_key),
                ),
            )
        return True
    finally:
        connection.close()


def remove_identity_override(server_id, library_uuid, rating_key, *, path=None):
    connection = _connect(path, writable=True)
    try:
        with connection:
            cursor = connection.execute(
                "DELETE FROM identity_overrides WHERE server_id=? "
                "AND library_uuid=? AND rating_key=?",
                (str(server_id or "unknown"), str(library_uuid), str(rating_key)),
            )
        return int(cursor.rowcount)
    finally:
        connection.close()


def reconcile_identity_reviews(gaps, *, path=None, now=None):
    """Persist identity failures as a bounded human review queue."""
    current = _as_utc(now).isoformat()
    relevant = []
    for gap in gaps or []:
        if not isinstance(gap, dict) or gap.get("category") not in {
            "identity_rejected", "tmdb_missing", "tmdb_failure"
        }:
            continue
        identity = {
            "server_id": gap.get("server_id"),
            "library_uuid": gap.get("library_uuid"),
            "library_name": gap.get("library") or gap.get("library_name"),
            "rating_key": gap.get("plex_rating_key"),
            "media_type": str(gap.get("media_type") or "unknown").lower(),
            "title": str(gap.get("title") or "Unknown title"),
            "year": gap.get("year"),
            "proposed_tmdb_id": gap.get("tmdb_id"),
            "imdb_id": gap.get("imdb_id"),
            "tvdb_id": gap.get("tvdb_id"),
            "category": str(gap.get("category")),
            "reason": gap.get("detail"),
        }
        raw_key = "\0".join(
            str(identity.get(name) or "")
            for name in ("library_name", "rating_key", "media_type", "title", "category")
        )
        identity["review_key"] = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        relevant.append(identity)
    if not relevant:
        return []
    connection = _connect(path, writable=True)
    try:
        with connection:
            for record in relevant:
                connection.execute(
                    """
                    INSERT INTO identity_review_queue(
                        review_key, server_id, library_uuid, library_name,
                        rating_key, media_type, title, year, proposed_tmdb_id,
                        imdb_id, tvdb_id, category, reason, status,
                        occurrences, first_seen, last_seen, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, NULL)
                    ON CONFLICT(review_key) DO UPDATE SET
                        proposed_tmdb_id=excluded.proposed_tmdb_id,
                        imdb_id=COALESCE(excluded.imdb_id, identity_review_queue.imdb_id),
                        tvdb_id=COALESCE(excluded.tvdb_id, identity_review_queue.tvdb_id),
                        reason=excluded.reason,
                        status='open', occurrences=identity_review_queue.occurrences + 1,
                        last_seen=excluded.last_seen, resolved_at=NULL
                    """,
                    (
                        record["review_key"], record["server_id"],
                        record["library_uuid"], record["library_name"],
                        record["rating_key"], record["media_type"], record["title"],
                        record["year"], record["proposed_tmdb_id"],
                        record["imdb_id"], record["tvdb_id"], record["category"],
                        record["reason"], current, current,
                    ),
                )
            connection.execute(
                "DELETE FROM identity_review_queue WHERE review_key NOT IN "
                "(SELECT review_key FROM identity_review_queue ORDER BY last_seen DESC LIMIT ?)",
                (IDENTITY_REVIEW_LIMIT,),
            )
        return load_identity_reviews(path=path)
    finally:
        connection.close()


def load_identity_reviews(*, statuses=None, libraries=None, rating_keys=None, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values = []
    for column, requested in (
        ("status", statuses), ("library_name", libraries), ("rating_key", rating_keys)
    ):
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = "SELECT * FROM identity_review_queue"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY status, last_seen DESC"
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_review_queue'"
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(query, values).fetchall()]
    finally:
        connection.close()


def resolve_identity_reviews(
    server_id,
    library_uuid,
    rating_key,
    *,
    reason="identity validated successfully",
    path=None,
    now=None,
):
    """Close open review entries only after this exact Plex identity validates."""
    connection = _connect(path, writable=True)
    current = _as_utc(now).isoformat()
    try:
        with connection:
            cursor = connection.execute(
                "UPDATE identity_review_queue SET status='resolved', "
                "resolved_at=?, reason=COALESCE(reason, ?) WHERE server_id=? "
                "AND library_uuid=? AND rating_key=? AND status='open'",
                (
                    current,
                    reason,
                    str(server_id or "unknown"),
                    str(library_uuid),
                    str(rating_key),
                ),
            )
        return int(cursor.rowcount)
    finally:
        connection.close()


def observe_cleanup_candidate(
    candidate_key,
    record,
    scope,
    *,
    season_number=None,
    episode_number=None,
    confirmations_required=2,
    grace_hours=48.0,
    observation_id=None,
    path=None,
    now=None,
    writable=True,
):
    """Record one authoritative absence and return its current eligibility."""
    current_dt = _as_utc(now)
    current = current_dt.isoformat()
    required = max(1, int(confirmations_required))
    grace = max(0.0, float(grace_hours))
    connection = _connect(path, writable=writable)
    if connection is None:
        first = current_dt
        confirmations = 1
        eligible_after = (first + timedelta(hours=grace)).isoformat()
        return {
            "candidate_key": str(candidate_key), "confirmations": confirmations,
            "eligible_after": eligible_after,
            "eligible": confirmations >= required and current >= eligible_after,
            "status": "preview",
        }
    try:
        row = connection.execute(
            "SELECT * FROM cleanup_candidates WHERE candidate_key=?",
            (str(candidate_key),),
        ).fetchone()
        if row is None:
            first = current_dt
            confirmations = 1
            eligible_after = (first + timedelta(hours=grace)).isoformat()
            candidate = {
                "candidate_key": str(candidate_key),
                "first_missing_at": current,
                "last_confirmed_at": current,
                "eligible_after": eligible_after,
                "confirmations": confirmations,
                "last_observation_id": observation_id,
                "status": "pending",
            }
            if writable:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO cleanup_candidates(
                            candidate_key, server_id, library_uuid, library_name,
                            rating_key, cache_key, media_type, title, year,
                            tmdb_id, imdb_id, tvdb_id, edition, scope,
                            season_number, episode_number, first_missing_at,
                            last_confirmed_at, eligible_after, confirmations,
                            last_observation_id, status, reason
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            str(candidate_key), record.get("server_id"),
                            record.get("library_uuid"), record.get("library_name"),
                            record.get("rating_key"), record.get("cache_key"),
                            str(record.get("media_type") or "unknown"),
                            record.get("title"), record.get("year"),
                            record.get("tmdb_id"), record.get("imdb_id"),
                            record.get("tvdb_id"), record.get("edition"), str(scope),
                            season_number, episode_number, current, current,
                            eligible_after, confirmations, observation_id,
                            record.get("reason"),
                        ),
                    )
        else:
            candidate = dict(row)
            confirmations = int(candidate.get("confirmations") or 0)
            eligible_after = (
                _as_utc(candidate.get("first_missing_at"))
                + timedelta(hours=grace)
            ).isoformat()
            if observation_id is None or candidate.get("last_observation_id") != observation_id:
                confirmations += 1
            candidate.update(
                confirmations=confirmations,
                last_confirmed_at=current,
                eligible_after=eligible_after,
                last_observation_id=observation_id,
                status="pending",
            )
            if writable:
                with connection:
                    connection.execute(
                        "UPDATE cleanup_candidates SET last_confirmed_at=?, "
                        "confirmations=?, last_observation_id=?, status='pending', "
                        "eligible_after=?, reason=? WHERE candidate_key=?",
                        (
                            current, confirmations, observation_id, eligible_after,
                            record.get("reason"), str(candidate_key),
                        ),
                    )
        candidate["eligible"] = bool(
            int(candidate.get("confirmations") or 0) >= required
            and current_dt >= _as_utc(candidate.get("eligible_after"))
        )
        candidate["confirmations_required"] = required
        return candidate
    finally:
        connection.close()


def record_cleanup_history(
    source,
    action,
    status,
    record=None,
    *,
    output_type=None,
    season_number=None,
    episode_number=None,
    destination=None,
    checksum=None,
    reason=None,
    details=None,
    path=None,
    now=None,
):
    record = record or {}
    current = _as_utc(now).isoformat()
    connection = _connect(path, writable=True)
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO cleanup_history(
                    occurred_at, source, action, status, server_id, library_uuid,
                    library_name, rating_key, cache_key, media_type, title, year,
                    tmdb_id, imdb_id, tvdb_id, edition, output_type,
                    season_number, episode_number, destination, checksum, reason,
                    details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current, str(source), str(action), str(status),
                    record.get("server_id"), record.get("library_uuid"),
                    record.get("library_name"), record.get("rating_key"),
                    record.get("cache_key"), record.get("media_type"),
                    record.get("title"), record.get("year"), record.get("tmdb_id"),
                    record.get("imdb_id"), record.get("tvdb_id"),
                    record.get("edition"), output_type, season_number,
                    episode_number, None if destination is None else str(destination),
                    checksum, reason,
                    None if details is None else _json_dump(details),
                ),
            )
            connection.execute(
                "DELETE FROM cleanup_history WHERE id NOT IN "
                "(SELECT id FROM cleanup_history ORDER BY id DESC LIMIT ?)",
                (CLEANUP_HISTORY_LIMIT,),
            )
        return int(cursor.lastrowid)
    finally:
        connection.close()


def record_cleanup_quarantine(
    record,
    *,
    output_type,
    source_path,
    quarantine_path,
    checksum,
    size_bytes,
    retention_days=14,
    season_number=None,
    reason="confirmed absent from Plex",
    path=None,
    now=None,
):
    """Atomically record an automated cleanup history event and restorable file."""
    record = record or {}
    current_dt = _as_utc(now)
    current = current_dt.isoformat()
    expires_at = (current_dt + timedelta(days=max(1, int(retention_days)))).isoformat()
    connection = _connect(path, writable=True)
    try:
        with connection:
            history = connection.execute(
                """
                INSERT INTO cleanup_history(
                    occurred_at, source, action, status, server_id, library_uuid,
                    library_name, rating_key, cache_key, media_type, title, year,
                    tmdb_id, imdb_id, tvdb_id, edition, output_type,
                    season_number, destination, checksum, reason, details
                ) VALUES (?, 'automated', 'quarantine', 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current, record.get("server_id"), record.get("library_uuid"),
                    record.get("library_name"), record.get("rating_key"),
                    record.get("cache_key"), record.get("media_type"),
                    record.get("title"), record.get("year"), record.get("tmdb_id"),
                    record.get("imdb_id"), record.get("tvdb_id"),
                    record.get("edition"), str(output_type), season_number,
                    str(source_path), str(checksum), str(reason),
                    _json_dump(
                        {
                            "quarantine_path": str(quarantine_path),
                            "expires_at": expires_at,
                            "size_bytes": int(size_bytes),
                        }
                    ),
                ),
            )
            history_id = int(history.lastrowid)
            connection.execute(
                """
                INSERT INTO cleanup_quarantine(
                    history_id, status, server_id, library_uuid, library_name,
                    rating_key, cache_key, media_type, title, year, tmdb_id,
                    imdb_id, tvdb_id, edition, output_type, season_number,
                    source_path, quarantine_path, checksum, size_bytes,
                    quarantined_at, expires_at, reason
                ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history_id, record.get("server_id"), record.get("library_uuid"),
                    record.get("library_name"), record.get("rating_key"),
                    record.get("cache_key"), record.get("media_type"),
                    record.get("title"), record.get("year"), record.get("tmdb_id"),
                    record.get("imdb_id"), record.get("tvdb_id"),
                    record.get("edition"), str(output_type), season_number,
                    str(source_path), str(quarantine_path), str(checksum),
                    max(0, int(size_bytes)), current, expires_at, str(reason),
                ),
            )
            connection.execute(
                "DELETE FROM cleanup_history WHERE id NOT IN "
                "(SELECT id FROM cleanup_history ORDER BY id DESC LIMIT ?)",
                (CLEANUP_HISTORY_LIMIT,),
            )
        return history_id
    finally:
        connection.close()


def load_cleanup_quarantine(
    *, statuses=None, history_id=None, expired_before=None, path=None
):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values: list[object] = []
    normalized = [str(value) for value in (statuses or []) if str(value).strip()]
    if normalized:
        clauses.append(f"status IN ({','.join('?' for _ in normalized)})")
        values.extend(normalized)
    if history_id is not None:
        clauses.append("history_id=?")
        values.append(int(history_id))
    if expired_before is not None:
        clauses.append("expires_at<=?")
        values.append(_as_utc(expired_before).isoformat())
    query = "SELECT * FROM cleanup_quarantine"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC"
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='cleanup_quarantine'"
        ).fetchone()
        return [] if table is None else [dict(row) for row in connection.execute(query, values)]
    finally:
        connection.close()


def complete_cleanup_quarantine(
    history_id,
    action,
    *,
    status=None,
    source="manual",
    reason=None,
    path=None,
    now=None,
):
    """Close one quarantine record and append a linked cleanup history event."""
    action = str(action)
    if action not in {"restore", "purge"}:
        raise StateDatabaseError(f"unsupported quarantine action {action}")
    completed_status = status or ("restored" if action == "restore" else "purged")
    timestamp_column = "restored_at" if action == "restore" else "purged_at"
    current = _as_utc(now).isoformat()
    connection = _connect(path, writable=True)
    try:
        with connection:
            row = connection.execute(
                "SELECT * FROM cleanup_quarantine WHERE history_id=?",
                (int(history_id),),
            ).fetchone()
            if row is None:
                return False
            record = dict(row)
            connection.execute(
                f"UPDATE cleanup_quarantine SET status=?, {timestamp_column}=? "
                "WHERE history_id=?",
                (completed_status, current, int(history_id)),
            )
            connection.execute(
                """
                INSERT INTO cleanup_history(
                    occurred_at, source, action, status, server_id, library_uuid,
                    library_name, rating_key, cache_key, media_type, title, year,
                    tmdb_id, imdb_id, tvdb_id, edition, output_type,
                    season_number, destination, checksum, reason, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current, str(source), action, completed_status,
                    record.get("server_id"),
                    record.get("library_uuid"), record.get("library_name"),
                    record.get("rating_key"), record.get("cache_key"),
                    record.get("media_type"), record.get("title"), record.get("year"),
                    record.get("tmdb_id"), record.get("imdb_id"),
                    record.get("tvdb_id"), record.get("edition"),
                    record.get("output_type"), record.get("season_number"),
                    record.get("source_path"), record.get("checksum"),
                    reason or f"cleanup quarantine {action}",
                    _json_dump({"quarantine_history_id": int(history_id)}),
                ),
            )
            connection.execute(
                "DELETE FROM cleanup_quarantine WHERE status != 'active' AND id NOT IN "
                "(SELECT id FROM cleanup_quarantine WHERE status != 'active' "
                "ORDER BY id DESC LIMIT ?)",
                (CLEANUP_HISTORY_LIMIT,),
            )
        return True
    finally:
        connection.close()


def complete_cleanup_candidate(candidate_key, *, path=None):
    connection = _connect(path, writable=True)
    try:
        with connection:
            cursor = connection.execute(
                "DELETE FROM cleanup_candidates WHERE candidate_key=?",
                (str(candidate_key),),
            )
        return int(cursor.rowcount)
    finally:
        connection.close()


def cancel_cleanup_candidate(candidate_key, *, reason="media returned", path=None, now=None):
    connection = _connect(path, writable=True)
    try:
        row = connection.execute(
            "SELECT * FROM cleanup_candidates WHERE candidate_key=?",
            (str(candidate_key),),
        ).fetchone()
        if row is None:
            return False
        record = dict(row)
    finally:
        connection.close()
    record_cleanup_history(
        "automated", "candidate_cancelled", "cancelled", record,
        output_type=record.get("scope"), season_number=record.get("season_number"),
        episode_number=record.get("episode_number"), reason=reason, path=path, now=now,
    )
    complete_cleanup_candidate(candidate_key, path=path)
    return True


def load_cleanup_candidates(*, statuses=None, libraries=None, rating_keys=None, path=None):
    return _load_cleanup_rows(
        "cleanup_candidates", statuses=statuses, libraries=libraries,
        rating_keys=rating_keys, path=path,
    )


def load_cleanup_history(*, sources=None, libraries=None, rating_keys=None, limit=1000, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values: list[Any] = []
    for column, requested in (
        ("source", sources), ("library_name", libraries), ("rating_key", rating_keys)
    ):
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = "SELECT * FROM cleanup_history"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    values.append(max(1, min(CLEANUP_HISTORY_LIMIT, int(limit))))
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cleanup_history'"
        ).fetchone()
        if table is None:
            return []
        results = []
        for row in connection.execute(query, values).fetchall():
            record = dict(row)
            if record.get("details"):
                record["details"] = _json_load(record["details"], "cleanup history")
            results.append(record)
        return results
    finally:
        connection.close()


def _load_cleanup_rows(table_name, *, statuses=None, libraries=None, rating_keys=None, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    clauses = []
    values = []
    for column, requested in (
        ("status", statuses), ("library_name", libraries), ("rating_key", rating_keys)
    ):
        normalized = [str(value) for value in (requested or []) if str(value).strip()]
        if normalized:
            clauses.append(f"{column} IN ({','.join('?' for _ in normalized)})")
            values.extend(normalized)
    query = f"SELECT * FROM {table_name}"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY last_confirmed_at DESC"
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(query, values).fetchall()]
    finally:
        connection.close()


def plan_library_rebinding(source_library, destination_library, *, path=None):
    """Match already-inventoried libraries by unambiguous provider identity."""
    records = find_media_state(path=path)

    def selected(record, selector):
        return str(record.get("library_uuid")) == str(selector) or str(
            record.get("library_name")
        ) == str(selector)

    sources = [record for record in records if selected(record, source_library)]
    destinations = [
        record for record in records if selected(record, destination_library)
    ]
    destination_index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in destinations:
        key = (
            str(record.get("media_type") or ""),
            str(record.get("tmdb_id") or ""),
            str(record.get("edition") or "").casefold(),
        )
        destination_index.setdefault(key, []).append(record)
    ownership_counts = Counter(
        row["cache_key"] for row in load_asset_ownership(path=path)
    )
    plan = []
    for source in sources:
        key = (
            str(source.get("media_type") or ""),
            str(source.get("tmdb_id") or ""),
            str(source.get("edition") or "").casefold(),
        )
        matches = destination_index.get(key, []) if key[1] else []
        if not key[1]:
            status, reason, destination = (
                "unmatched", "source has no durable TMDb identity", None
            )
        elif len(matches) == 1:
            destination = matches[0]
            status, reason = "ready", "unique media type, TMDb ID, and edition match"
        elif len(matches) > 1:
            destination = None
            status, reason = "ambiguous", "multiple destination records share the identity"
        else:
            destination = None
            status, reason = "unmatched", "no destination record shares the identity"
        plan.append(
            {
                "status": status,
                "reason": reason,
                "media_type": source.get("media_type"),
                "tmdb_id": source.get("tmdb_id"),
                "title": source.get("title"),
                "year": source.get("year"),
                "edition": source.get("edition"),
                "asset_claims": int(ownership_counts.get(source["cache_key"], 0)),
                "source": {
                    key: source.get(key)
                    for key in (
                        "server_id", "library_uuid", "library_name", "rating_key",
                        "cache_key",
                    )
                },
                "destination": None if destination is None else {
                    key: destination.get(key)
                    for key in (
                        "server_id", "library_uuid", "library_name", "rating_key",
                        "cache_key",
                    )
                },
            }
        )
    return plan


def _merge_rebound_payload(source, destination):
    merged = copy.deepcopy(destination)
    for prefix in ("poster_", "background_"):
        for key, value in source.items():
            if key.startswith(prefix) and value not in (None, ""):
                merged.setdefault(key, copy.deepcopy(value))
    if source.get("destination_history"):
        history = list(merged.get("destination_history") or [])
        for event in source["destination_history"]:
            if event not in history:
                history.append(copy.deepcopy(event))
        merged["destination_history"] = history[-100:]
    source_seasons = source.get("seasons") or {}
    destination_seasons = merged.setdefault("seasons", {})
    for season_number, source_season in source_seasons.items():
        if not isinstance(source_season, dict):
            continue
        destination_season = destination_seasons.setdefault(str(season_number), {})
        if not isinstance(destination_season, dict):
            destination_season = {}
            destination_seasons[str(season_number)] = destination_season
        for key, value in source_season.items():
            if key.startswith("season_") and value not in (None, ""):
                destination_season.setdefault(key, copy.deepcopy(value))
    return merged


def apply_library_rebinding(plan, *, path=None, now=None):
    """Transfer only non-conflicting durable ownership to new Plex identities."""
    current = _as_utc(now).isoformat()
    connection = _connect(path, writable=True)
    results = []
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for proposal in plan or []:
                result = copy.deepcopy(proposal)
                source = proposal.get("source") or {}
                destination = proposal.get("destination") or {}
                if proposal.get("status") != "ready" or not destination:
                    result["applied"] = False
                    results.append(result)
                    continue
                source_row = connection.execute(
                    "SELECT payload FROM media_state WHERE cache_key=?",
                    (source.get("cache_key"),),
                ).fetchone()
                destination_row = connection.execute(
                    "SELECT payload FROM media_state WHERE cache_key=?",
                    (destination.get("cache_key"),),
                ).fetchone()
                if source_row is None or destination_row is None:
                    result.update(
                        applied=False,
                        status="stale_plan",
                        reason="source or destination state changed after planning",
                    )
                    results.append(result)
                    continue
                source_assets = connection.execute(
                    "SELECT * FROM asset_ownership WHERE cache_key=?",
                    (source["cache_key"],),
                ).fetchall()
                conflict = None
                for asset in source_assets:
                    existing = connection.execute(
                        "SELECT destination, checksum FROM asset_ownership "
                        "WHERE cache_key=? AND asset_type=? AND season_number=?",
                        (
                            destination["cache_key"], asset["asset_type"],
                            asset["season_number"],
                        ),
                    ).fetchone()
                    if existing is not None and (
                        normalize_destination(existing["destination"])
                        != normalize_destination(asset["destination"])
                        or str(existing["checksum"] or "") != str(asset["checksum"] or "")
                    ):
                        conflict = (
                            f"destination already owns different {asset['asset_type']} "
                            f"season {asset['season_number'] or 'root'}"
                        )
                        break
                if conflict:
                    result.update(applied=False, status="conflict", reason=conflict)
                    results.append(result)
                    continue
                merged_payload = _merge_rebound_payload(
                    _json_load(source_row[0], "source media state"),
                    _json_load(destination_row[0], "destination media state"),
                )
                connection.execute(
                    "UPDATE media_state SET payload=? WHERE cache_key=?",
                    (_json_dump(merged_payload), destination["cache_key"]),
                )
                for asset in source_assets:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO asset_ownership(
                            cache_key, media_type, tmdb_id, asset_type,
                            season_number, source_path, destination, checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            destination["cache_key"], asset["media_type"],
                            asset["tmdb_id"], asset["asset_type"],
                            asset["season_number"], asset["source_path"],
                            asset["destination"], asset["checksum"],
                        ),
                    )
                for table in ("item_exceptions", "identity_overrides"):
                    rows = connection.execute(
                        f"SELECT * FROM {table} WHERE server_id=? AND library_uuid=? "
                        "AND rating_key=?",
                        (
                            source.get("server_id"), source.get("library_uuid"),
                            source.get("rating_key"),
                        ),
                    ).fetchall()
                    for row in rows:
                        values = dict(row)
                        values.update(
                            server_id=destination.get("server_id"),
                            library_uuid=destination.get("library_uuid"),
                            library_name=destination.get("library_name"),
                            rating_key=destination.get("rating_key"),
                            updated_at=current,
                        )
                        columns = list(values)
                        connection.execute(
                            f"INSERT OR IGNORE INTO {table} "
                            f"({','.join(columns)}) VALUES "
                            f"({','.join('?' for _ in columns)})",
                            [values[column] for column in columns],
                        )
                    connection.execute(
                        f"DELETE FROM {table} WHERE server_id=? AND library_uuid=? "
                        "AND rating_key=?",
                        (
                            source.get("server_id"), source.get("library_uuid"),
                            source.get("rating_key"),
                        ),
                    )
                connection.execute(
                    "UPDATE unresolved_work SET library_name=?, plex_rating_key=? "
                    "WHERE library_name=? AND plex_rating_key=?",
                    (
                        destination.get("library_name"), destination.get("rating_key"),
                        source.get("library_name"), source.get("rating_key"),
                    ),
                )
                connection.execute(
                    "UPDATE identity_review_queue SET library_name=?, library_uuid=?, "
                    "server_id=?, rating_key=? WHERE library_name=? AND rating_key=?",
                    (
                        destination.get("library_name"), destination.get("library_uuid"),
                        destination.get("server_id"), destination.get("rating_key"),
                        source.get("library_name"), source.get("rating_key"),
                    ),
                )
                connection.execute(
                    "DELETE FROM item_retry_queue WHERE server_id=? AND library_uuid=? "
                    "AND rating_key=?",
                    (
                        source.get("server_id"), source.get("library_uuid"),
                        source.get("rating_key"),
                    ),
                )
                connection.execute(
                    "DELETE FROM identity_bindings WHERE server_id=? AND library_uuid=? "
                    "AND rating_key=?",
                    (
                        source.get("server_id"), source.get("library_uuid"),
                        source.get("rating_key"),
                    ),
                )
                connection.execute(
                    "DELETE FROM cleanup_candidates WHERE cache_key=?",
                    (source.get("cache_key"),),
                )
                connection.execute(
                    "DELETE FROM media_state WHERE cache_key=?",
                    (source.get("cache_key"),),
                )
                details = {
                    "asset_claims_transferred": len(source_assets),
                    "plex_metadata_ownership_transferred": False,
                    "active_identity_binding_transferred": False,
                }
                connection.execute(
                    """
                    INSERT INTO library_rebinding_history(
                        occurred_at, source_server_id, source_library_uuid,
                        source_library_name, source_rating_key,
                        destination_server_id, destination_library_uuid,
                        destination_library_name, destination_rating_key,
                        media_type, tmdb_id, status, reason, details
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)
                    """,
                    (
                        current, source.get("server_id"), source.get("library_uuid"),
                        source.get("library_name"), source.get("rating_key"),
                        destination.get("server_id"), destination.get("library_uuid"),
                        destination.get("library_name"), destination.get("rating_key"),
                        proposal.get("media_type"), proposal.get("tmdb_id"),
                        proposal.get("reason"), _json_dump(details),
                    ),
                )
                result.update(applied=True, status="applied", details=details)
                results.append(result)
            connection.execute(
                "DELETE FROM library_rebinding_history WHERE id NOT IN "
                "(SELECT id FROM library_rebinding_history ORDER BY id DESC LIMIT ?)",
                (REBIND_HISTORY_LIMIT,),
            )
        return results
    finally:
        connection.close()


def load_library_rebinding_history(*, limit=1000, path=None):
    connection = _connect(path, writable=False)
    if connection is None:
        return []
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='library_rebinding_history'"
        ).fetchone()
        if table is None:
            return []
        results = []
        for row in connection.execute(
            "SELECT * FROM library_rebinding_history ORDER BY id DESC LIMIT ?",
            (max(1, min(REBIND_HISTORY_LIMIT, int(limit))),),
        ).fetchall():
            record = dict(row)
            if record.get("details"):
                record["details"] = _json_load(record["details"], "rebinding history")
            results.append(record)
        return results
    finally:
        connection.close()


def maintain_state_database(path=None, wal_threshold_mb=8):
    """Run bounded post-job SQLite maintenance without rebuilding durable state."""
    database = Path(path or STATE_DATABASE)
    connection = _connect(database, writable=True)
    result = {"optimized": False, "checkpointed": False, "wal_bytes": 0}
    try:
        connection.execute("PRAGMA optimize")
        result["optimized"] = True
        wal_path = Path(f"{database}-wal")
        try:
            result["wal_bytes"] = wal_path.stat().st_size
        except OSError:
            result["wal_bytes"] = 0
        if result["wal_bytes"] >= max(1, int(wal_threshold_mb)) * 1024 * 1024:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["checkpointed"] = bool(row is not None and int(row[0]) == 0)
        return result
    finally:
        connection.close()


def load_provider_health(provider_keys, *, path=None, now=None):
    """Load bounded cross-run provider cooldown state without exposing credentials."""
    keys = [str(value) for value in (provider_keys or []) if str(value).strip()]
    if not keys:
        return {}
    connection = _connect(path, writable=False)
    if connection is None:
        return {}
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_health'"
        ).fetchone()
        if table is None:
            return {}
        rows = connection.execute(
            "SELECT * FROM provider_health WHERE provider_key IN "
            f"({','.join('?' for _ in keys)})",
            keys,
        ).fetchall()
        current = _as_utc(now)
        results = {}
        for row in rows:
            record = dict(row)
            open_until = record.get("open_until")
            try:
                remaining = max(0.0, (_as_utc(open_until) - current).total_seconds())
            except (TypeError, ValueError):
                remaining = 0.0
            record["cooldown_seconds"] = remaining
            if remaining <= 0:
                record["consecutive_failures"] = 0
            results[record["provider_key"]] = record
        return results
    finally:
        connection.close()


def save_provider_health(
    provider_key,
    provider,
    *,
    consecutive_failures=0,
    cooldown_seconds=0.0,
    successful=False,
    path=None,
    now=None,
):
    current_dt = _as_utc(now)
    current = current_dt.isoformat()
    cooldown = max(0.0, float(cooldown_seconds or 0.0))
    open_until = (
        (current_dt + timedelta(seconds=cooldown)).isoformat() if cooldown else None
    )
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider_key, provider, consecutive_failures, open_until,
                    last_success_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_key) DO UPDATE SET
                    provider=excluded.provider,
                    consecutive_failures=excluded.consecutive_failures,
                    open_until=excluded.open_until,
                    last_success_at=CASE
                        WHEN excluded.last_success_at IS NOT NULL
                        THEN excluded.last_success_at
                        ELSE provider_health.last_success_at
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    str(provider_key), str(provider),
                    max(0, int(consecutive_failures)), open_until,
                    current if successful else None, current,
                ),
            )
        return True
    finally:
        connection.close()


def record_job_run(
    mode,
    started_at,
    finished_at,
    status,
    error=None,
    summary=None,
    metrics=None,
    history_limit=JOB_HISTORY_LIMIT,
    path=None,
):
    connection = _connect(path, writable=True)
    try:
        with connection:
            connection.execute(
                "INSERT INTO job_runs("
                "mode, started_at, finished_at, status, error, summary, metrics"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    mode,
                    started_at,
                    finished_at,
                    status,
                    error,
                    None if summary is None else _json_dump(summary),
                    None if metrics is None else _json_dump(metrics),
                ),
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
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(job_runs)")
        }
        summary_column = "summary" if "summary" in columns else "NULL AS summary"
        metrics_column = "metrics" if "metrics" in columns else "NULL AS metrics"
        rows = connection.execute(
            "SELECT mode, started_at, finished_at, status, error, "
            f"{summary_column}, {metrics_column} "
            "FROM job_runs ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        results = []
        for row in reversed(rows):
            result = dict(row)
            raw_summary = result.pop("summary", None)
            raw_metrics = result.pop("metrics", None)
            if raw_summary:
                result["library_results"] = _json_load(
                    raw_summary, "job run library summary"
                )
            if raw_metrics:
                result["metrics"] = _json_load(raw_metrics, "job run metrics")
            results.append(result)
        return results
    finally:
        connection.close()
