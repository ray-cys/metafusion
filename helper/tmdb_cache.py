import json
import os
import shutil
import sqlite3
import time
import zlib
from collections.abc import MutableMapping
from pathlib import Path
from urllib.parse import quote


class PersistentTTLCache(MutableMapping):
    """SQLite-backed, compressed TTL cache with a mapping-compatible API."""

    SCHEMA_VERSION = 1
    FILE_MODE = 0o664
    COMPRESSION_LEVEL = 1

    def __init__(self):
        self.path = None
        self.ttl_seconds = 24 * 3600
        self.max_entries = 0
        self.max_bytes = 0
        self.automatic_limits = True
        self.enabled = True
        self.writable = True
        self._connection = None
        self._memory = {}
        self._memory_expires = {}
        self._memory_touched = {}
        self._pending_touches = {}
        self._ignore_database = False
        self._entry_count = 0
        self._stored_bytes = 0
        self._dirty = False
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._recoveries = 0
        self._last_error = None

    def configure(
        self,
        path,
        ttl_hours=24,
        max_entries=0,
        max_mb=0,
        enabled=True,
        writable=True,
    ):
        self.reset_memory()
        self.path = Path(path)
        self.ttl_seconds = max(1.0, float(ttl_hours) * 3600)
        configured_entries = max(0, int(max_entries))
        configured_bytes = max(0, int(float(max_mb) * 1024 * 1024))
        self.max_entries, self.max_bytes = self._effective_limits(
            configured_entries, configured_bytes
        )
        self.automatic_limits = not configured_entries or not configured_bytes
        self.enabled = bool(enabled)
        self.writable = bool(writable)
        if not self.enabled:
            return

        try:
            self._open_database()
        except sqlite3.Error as error:
            self._last_error = f"{type(error).__name__}: {error}"
            if self.writable and self._is_corruption_error(error):
                self._recover_database()
            else:
                self._close_database()
        except OSError as error:
            self._last_error = f"{type(error).__name__}: {error}"
            self._close_database()

        if self._connection is None:
            return

        if self.writable:
            try:
                self._delete_expired(time.time())
                self._trim_database()
            except sqlite3.Error as error:
                self._handle_database_error(error)

    def _effective_limits(self, configured_entries, configured_bytes):
        parent = self.path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        try:
            free_bytes = shutil.disk_usage(parent).free
        except OSError:
            free_bytes = 4 * 1024 ** 3
        automatic_bytes = min(
            1024 * 1024 ** 2,
            max(64 * 1024 ** 2, int(free_bytes * 0.02)),
        )
        byte_limit = configured_bytes or automatic_bytes
        automatic_entries = max(
            5000,
            min(100000, int(byte_limit / (32 * 1024))),
        )
        return configured_entries or automatic_entries, byte_limit

    @staticmethod
    def _is_corruption_error(error):
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "file is not a database",
                "database disk image is malformed",
                "database corruption",
                "unsupported tmdb cache schema",
                "tmdb cache metadata row is missing",
            )
        )

    def _open_database(self):
        if not self.writable and not self.path.exists():
            return

        existed = self.path.exists()
        if self.writable:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5)
        else:
            database_uri = f"file:{quote(str(self.path), safe='/')}?mode=ro"
            connection = sqlite3.connect(database_uri, uri=True, timeout=5)
            connection.execute("PRAGMA query_only = ON")

        self._connection = connection
        if self.writable:
            connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
            if not existed:
                connection.execute("VACUUM")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version not in (0, self.SCHEMA_VERSION):
                raise sqlite3.DatabaseError(
                    f"unsupported TMDb cache schema version {current_version}"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_cache (
                    cache_key TEXT PRIMARY KEY,
                    response BLOB NOT NULL,
                    expires_at REAL NOT NULL,
                    touched_at REAL NOT NULL,
                    stored_bytes INTEGER NOT NULL
                ) WITHOUT ROWID
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_cache_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    entry_count INTEGER NOT NULL,
                    stored_bytes INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO tmdb_cache_meta "
                "(singleton, entry_count, stored_bytes) VALUES (1, 0, 0)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tmdb_cache_expiry "
                "ON tmdb_cache(expires_at, stored_bytes)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS tmdb_cache_lru "
                "ON tmdb_cache(touched_at, stored_bytes)"
            )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            connection.commit()
            if not existed:
                os.chmod(self.path, self.FILE_MODE)
        else:
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version != self.SCHEMA_VERSION:
                raise sqlite3.DatabaseError(
                    f"unsupported TMDb cache schema version {current_version}"
                )

        self._refresh_totals()

    def _recover_database(self):
        self._close_database()
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        for candidate in self._database_files():
            try:
                target = Path(f"{candidate}.corrupt-{timestamp}")
                candidate.replace(target)
            except FileNotFoundError:
                pass
            except OSError:
                return
        if self.path is not None:
            old_sets = sorted(
                self.path.parent.glob(f"{self.path.name}*.corrupt-*"),
                key=lambda candidate: candidate.stat().st_mtime,
                reverse=True,
            )
            for expired in old_sets[4:]:
                try:
                    expired.unlink()
                except OSError:
                    pass
        try:
            self._open_database()
        except (OSError, sqlite3.Error):
            self._close_database()
            return
        if self._connection is not None:
            self._recoveries += 1

    def _handle_database_error(self, error):
        self._last_error = f"{type(error).__name__}: {error}"
        self._pending_touches.clear()
        self._dirty = False
        if self.writable and self._is_corruption_error(error):
            self._recover_database()
        else:
            self._close_database()
            self._entry_count = 0
            self._stored_bytes = 0

    def _database_files(self):
        if self.path is None:
            return []
        return [
            self.path,
            Path(f"{self.path}-journal"),
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ]

    def _close_database(self):
        if self._connection is None:
            return
        try:
            self._connection.close()
        except sqlite3.Error:
            pass
        finally:
            self._connection = None

    def reset_memory(self):
        self._close_database()
        self._memory = {}
        self._memory_expires = {}
        self._memory_touched = {}
        self._pending_touches = {}
        self._ignore_database = False
        self._entry_count = 0
        self._stored_bytes = 0
        self._dirty = False
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._recoveries = 0
        self._last_error = None

    @staticmethod
    def _encode(value):
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return zlib.compress(serialized, level=PersistentTTLCache.COMPRESSION_LEVEL)

    @staticmethod
    def _decode(payload):
        return json.loads(zlib.decompress(payload).decode("utf-8"))

    def _refresh_totals(self):
        if self._connection is None:
            self._entry_count = 0
            self._stored_bytes = 0
            return
        row = self._connection.execute(
            "SELECT entry_count, stored_bytes FROM tmdb_cache_meta WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("TMDb cache metadata row is missing")
        self._entry_count = int(row[0])
        self._stored_bytes = int(row[1])

    def _delete_expired(self, now):
        if self._connection is None or not self.writable:
            return 0
        row = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(stored_bytes), 0) "
            "FROM tmdb_cache WHERE expires_at <= ?",
            (now,),
        ).fetchone()
        expired_count = int(row[0])
        if not expired_count:
            return 0
        self._connection.execute("DELETE FROM tmdb_cache WHERE expires_at <= ?", (now,))
        self._entry_count -= expired_count
        self._stored_bytes -= int(row[1])
        self._dirty = True
        return expired_count

    def _trim_database(self):
        if self._connection is None or not self.writable:
            return
        while self._entry_count > self.max_entries or (
            self.max_bytes and self._stored_bytes > self.max_bytes
        ):
            excess_entries = max(1, self._entry_count - self.max_entries)
            batch_size = min(100, excess_entries)
            rows = self._connection.execute(
                "SELECT cache_key, stored_bytes FROM tmdb_cache "
                "ORDER BY touched_at ASC LIMIT ?",
                (batch_size,),
            ).fetchall()
            if not rows:
                break
            self._connection.executemany(
                "DELETE FROM tmdb_cache WHERE cache_key = ?",
                ((row[0],) for row in rows),
            )
            self._entry_count -= len(rows)
            self._stored_bytes -= sum(int(row[1]) for row in rows)
            self._evictions += len(rows)
            self._dirty = True

    def _trim_memory(self):
        while len(self._memory) > self.max_entries or (
            self.max_bytes
            and sum(len(self._encode(value)) for value in self._memory.values())
            > self.max_bytes
        ):
            oldest = min(self._memory_touched, key=self._memory_touched.get)
            self._memory.pop(oldest, None)
            self._memory_expires.pop(oldest, None)
            self._memory_touched.pop(oldest, None)
            self._evictions += 1

    def _store_memory(self, key, value, now, ttl_seconds=None):
        self._memory[key] = value
        self._memory_expires[key] = now + (
            self.ttl_seconds if ttl_seconds is None else max(1.0, float(ttl_seconds))
        )
        self._memory_touched[key] = now
        self._trim_memory()

    def _delete_fetched_row(self, key, stored_bytes):
        try:
            self._connection.execute(
                "DELETE FROM tmdb_cache WHERE cache_key = ?", (key,)
            )
        except sqlite3.Error as error:
            self._handle_database_error(error)
            return
        self._entry_count -= 1
        self._stored_bytes -= int(stored_bytes)
        self._dirty = True

    def _fetch(self, key, record_stats=True):
        now = time.time()
        if key in self._memory:
            if self._memory_expires.get(key, 0) <= now:
                self._memory.pop(key, None)
                self._memory_expires.pop(key, None)
                self._memory_touched.pop(key, None)
            else:
                if record_stats:
                    self._hits += 1
                self._memory_touched[key] = now
                return self._memory[key]

        if self._connection is None or self._ignore_database:
            if record_stats:
                self._misses += 1
            raise KeyError(key)

        try:
            row = self._connection.execute(
                "SELECT response, expires_at, touched_at, stored_bytes "
                "FROM tmdb_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        except sqlite3.Error as error:
            self._handle_database_error(error)
            if record_stats:
                self._misses += 1
            raise KeyError(key) from error
        if row is None:
            if record_stats:
                self._misses += 1
            raise KeyError(key)
        if float(row[1]) <= now:
            if self.writable:
                self._delete_fetched_row(key, row[3])
            if record_stats:
                self._misses += 1
            raise KeyError(key)
        try:
            value = self._decode(row[0])
        except (
            TypeError,
            ValueError,
            UnicodeDecodeError,
            zlib.error,
            json.JSONDecodeError,
        ):
            if self.writable:
                self._delete_fetched_row(key, row[3])
            if record_stats:
                self._misses += 1
            raise KeyError(key)

        if record_stats:
            self._hits += 1
        touch_interval = max(60.0, min(3600.0, self.ttl_seconds / 4))
        if self.writable and now - float(row[2]) >= touch_interval:
            self._pending_touches[key] = now
        return value

    def __getitem__(self, key):
        return self._fetch(key)

    def get(self, key, default=None):
        try:
            return self._fetch(key)
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            self._fetch(key, record_stats=False)
            return True
        except KeyError:
            self._misses += 1
            return False

    def __setitem__(self, key, value):
        self.set(key, value)

    def set(self, key, value, ttl_seconds=None):
        """Store a value with the default TTL or a shorter per-entry TTL."""
        if not self.enabled:
            return
        try:
            encoded = self._encode(value)
        except (TypeError, ValueError, OverflowError, RecursionError):
            return
        now = time.time()
        effective_ttl = (
            self.ttl_seconds
            if ttl_seconds is None
            else max(1.0, min(self.ttl_seconds, float(ttl_seconds)))
        )
        if self._connection is None or not self.writable:
            self._store_memory(key, value, now, effective_ttl)
            return

        try:
            existing = self._connection.execute(
                "SELECT stored_bytes FROM tmdb_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO tmdb_cache (
                    cache_key, response, expires_at, touched_at, stored_bytes
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    response = excluded.response,
                    expires_at = excluded.expires_at,
                    touched_at = excluded.touched_at,
                    stored_bytes = excluded.stored_bytes
                """,
                (key, encoded, now + effective_ttl, now, len(encoded)),
            )
        except sqlite3.Error as error:
            self._handle_database_error(error)
            self._store_memory(key, value, now, effective_ttl)
            return
        if existing is None:
            self._entry_count += 1
        else:
            self._stored_bytes -= int(existing[0])
        self._stored_bytes += len(encoded)
        self._pending_touches.pop(key, None)
        self._dirty = True
        try:
            self._trim_database()
        except sqlite3.Error as error:
            self._handle_database_error(error)
            self._store_memory(key, value, now, effective_ttl)

    def __delitem__(self, key):
        if key in self._memory:
            del self._memory[key]
            self._memory_expires.pop(key, None)
            self._memory_touched.pop(key, None)
            return
        if self._connection is None or not self.writable:
            raise KeyError(key)
        existing = self._connection.execute(
            "SELECT stored_bytes FROM tmdb_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if existing is None:
            raise KeyError(key)
        self._connection.execute("DELETE FROM tmdb_cache WHERE cache_key = ?", (key,))
        self._entry_count -= 1
        self._stored_bytes -= int(existing[0])
        self._pending_touches.pop(key, None)
        self._dirty = True

    def __iter__(self):
        now = time.time()
        memory_keys = {
            key for key, expires_at in self._memory_expires.items() if expires_at > now
        }
        database_keys = set()
        if self._connection is not None and not self._ignore_database:
            try:
                database_keys = {
                    row[0]
                    for row in self._connection.execute(
                        "SELECT cache_key FROM tmdb_cache WHERE expires_at > ?",
                        (now,),
                    )
                }
            except sqlite3.Error as error:
                self._handle_database_error(error)
        return iter(database_keys | memory_keys)

    def __len__(self):
        now = time.time()
        memory_keys = {
            key for key, expires_at in self._memory_expires.items() if expires_at > now
        }
        if self._connection is None or self._ignore_database:
            return len(memory_keys)
        try:
            database_count = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM tmdb_cache WHERE expires_at > ?", (now,)
                ).fetchone()[0]
            )
        except sqlite3.Error as error:
            self._handle_database_error(error)
            return len(memory_keys)
        if not memory_keys:
            return database_count
        try:
            duplicates = sum(
                1
                for key in memory_keys
                if self._connection.execute(
                    "SELECT 1 FROM tmdb_cache WHERE cache_key = ? AND expires_at > ?",
                    (key, now),
                ).fetchone()
                is not None
            )
        except sqlite3.Error as error:
            self._handle_database_error(error)
            return len(memory_keys)
        return database_count + len(memory_keys) - duplicates

    def clear(self):
        self._memory.clear()
        self._memory_expires.clear()
        self._memory_touched.clear()
        self._pending_touches.clear()
        if self._connection is None:
            return
        if not self.writable:
            self._ignore_database = True
            return
        if self._entry_count:
            try:
                self._connection.execute("DELETE FROM tmdb_cache")
            except sqlite3.Error as error:
                self._handle_database_error(error)
                return
            self._entry_count = 0
            self._stored_bytes = 0
            self._dirty = True

    def stats(self):
        disk_bytes = 0
        if self.path is not None:
            try:
                disk_bytes = self.path.stat().st_size
            except OSError:
                pass
        if not self.enabled:
            health = "disabled"
        elif self._connection is None:
            health = "degraded" if self._last_error else "memory_only"
        elif self._recoveries:
            health = "recovered"
        else:
            health = "healthy"
        return {
            "entries": len(self),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "recoveries": self._recoveries,
            "stored_bytes": self._stored_bytes,
            "disk_bytes": disk_bytes,
            "stored_mib": self._stored_bytes / (1024 * 1024),
            "disk_mib": disk_bytes / (1024 * 1024),
            "max_entries": self.max_entries,
            "max_mib": self.max_bytes / (1024 * 1024),
            "automatic_limits": self.automatic_limits,
            "health": health,
            "last_error": self._last_error,
        }

    def flush(self):
        if (
            not self.enabled
            or not self.writable
            or self._connection is None
            or (not self._dirty and not self._pending_touches)
        ):
            return False
        try:
            if self._pending_touches:
                self._connection.executemany(
                    "UPDATE tmdb_cache SET touched_at = ? WHERE cache_key = ?",
                    (
                        (touched_at, key)
                        for key, touched_at in self._pending_touches.items()
                    ),
                )
            self._pending_touches.clear()
            self._delete_expired(time.time())
            self._trim_database()
            self._connection.execute(
                "UPDATE tmdb_cache_meta SET entry_count = ?, stored_bytes = ? "
                "WHERE singleton = 1",
                (self._entry_count, self._stored_bytes),
            )
            self._connection.commit()
            if self._evictions:
                self._connection.execute("PRAGMA incremental_vacuum(128)")
            self._dirty = False
            return True
        except sqlite3.Error as error:
            self._last_error = f"{type(error).__name__}: {error}"
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            self._pending_touches.clear()
            self._dirty = False
            try:
                self._refresh_totals()
            except sqlite3.Error as refresh_error:
                self._handle_database_error(refresh_error)
            return False

    def relieve_space(self, destination, required_free_bytes):
        """Prune only disposable LRU rows when cache and destination share a disk."""
        if self._connection is None or not self.writable or self.path is None:
            return 0
        try:
            if self.path.parent.stat().st_dev != Path(destination).stat().st_dev:
                return 0
        except OSError:
            return 0
        removed = 0
        try:
            removed += self._delete_expired(time.time())

            def reclaim_pages():
                if not self.flush() or self._connection is None:
                    return False
                # DELETE makes pages reusable inside SQLite; incremental vacuum
                # plus a WAL checkpoint returns them to the filesystem when
                # disk pressure requires real free space immediately.
                self._connection.execute("PRAGMA incremental_vacuum(250)")
                self._connection.commit()
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                return True

            if removed and not reclaim_pages():
                return removed
            while (
                shutil.disk_usage(destination).free < int(required_free_bytes)
                and self._entry_count > 0
            ):
                rows = self._connection.execute(
                    "SELECT cache_key, stored_bytes FROM tmdb_cache "
                    "ORDER BY touched_at ASC LIMIT 250"
                ).fetchall()
                if not rows:
                    break
                self._connection.executemany(
                    "DELETE FROM tmdb_cache WHERE cache_key = ?",
                    ((row[0],) for row in rows),
                )
                removed += len(rows)
                self._entry_count -= len(rows)
                self._stored_bytes -= sum(int(row[1]) for row in rows)
                self._evictions += len(rows)
                self._dirty = True
                if not reclaim_pages():
                    break
            return removed
        except (OSError, sqlite3.Error):
            return removed

    def maintain(self, wal_threshold_mb=8):
        """Run bounded optimization and checkpoint work after a completed job."""
        result = {"optimized": False, "checkpointed": False, "wal_bytes": 0}
        if self._connection is None or not self.writable:
            return result
        self.flush()
        try:
            self._connection.execute("PRAGMA optimize")
            result["optimized"] = True
            wal_path = Path(f"{self.path}-wal")
            try:
                result["wal_bytes"] = wal_path.stat().st_size
            except OSError:
                result["wal_bytes"] = 0
            if result["wal_bytes"] >= max(1, int(wal_threshold_mb)) * 1024 * 1024:
                row = self._connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                result["checkpointed"] = bool(row is not None and int(row[0]) == 0)
            return result
        except sqlite3.Error as error:
            self._handle_database_error(error)
            return result


tmdb_response_cache = PersistentTTLCache()
