import json
import time
from pathlib import Path

from helper.io import atomic_write_json, backup_path_for, read_json_with_backup


class PersistentTTLCache(dict):
    """Small JSON-backed cache that remains dict-compatible for the builders."""

    SCHEMA_VERSION = 2

    def __init__(self):
        super().__init__()
        self.path = None
        self.ttl_seconds = 24 * 3600
        self.max_entries = 5000
        self.enabled = True
        self.writable = True
        self._expires = {}
        self._touched = {}
        self._dirty = False
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def configure(self, path, ttl_hours=24, max_entries=5000, enabled=True, writable=True):
        self.reset_memory()
        self.path = Path(path)
        self.ttl_seconds = max(1.0, float(ttl_hours) * 3600)
        self.max_entries = max(1, int(max_entries))
        self.enabled = bool(enabled)
        self.writable = bool(writable)
        if not self.enabled or not (
            self.path.exists() or backup_path_for(self.path).exists()
        ):
            return
        try:
            document = read_json_with_backup(self.path, default={})
            if document.get("schema_version") != self.SCHEMA_VERSION:
                return
            now = time.time()
            for key, entry in document.get("entries", {}).items():
                expires_at = float(entry.get("expires_at", 0))
                if expires_at <= now:
                    self._dirty = True
                    continue
                dict.__setitem__(self, key, entry.get("value"))
                self._expires[key] = expires_at
                self._touched[key] = float(entry.get("touched_at", now))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            self.reset_memory()

    def reset_memory(self):
        dict.clear(self)
        self._expires = {}
        self._touched = {}
        self._dirty = False
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def _expire(self, key):
        if dict.__contains__(self, key) and self._expires.get(key, 0) <= time.time():
            dict.__delitem__(self, key)
            self._expires.pop(key, None)
            self._touched.pop(key, None)
            self._dirty = True
            return True
        return False

    def __contains__(self, key):
        present = dict.__contains__(self, key)
        if not present:
            self._misses += 1
            return False
        if self._expire(key):
            self._misses += 1
            return False
        return True

    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        self._hits += 1
        self._touched[key] = time.time()
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value):
        if not self.enabled:
            return
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            return
        now = time.time()
        dict.__setitem__(self, key, value)
        self._expires[key] = now + self.ttl_seconds
        self._touched[key] = now
        self._dirty = True
        self._trim()

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        self._expires.pop(key, None)
        self._touched.pop(key, None)
        self._dirty = True

    def clear(self):
        if self:
            self._dirty = True
        dict.clear(self)
        self._expires.clear()
        self._touched.clear()

    def _trim(self):
        overflow = len(self) - self.max_entries
        if overflow <= 0:
            return
        oldest = sorted(self._touched, key=self._touched.get)[:overflow]
        for key in oldest:
            self.__delitem__(key)
            self._evictions += 1

    def stats(self):
        return {
            "entries": len(self),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
        }

    def flush(self):
        if not self.enabled or not self.writable or not self.path or not self._dirty:
            return False
        now = time.time()
        for key in list(dict.keys(self)):
            if self._expires.get(key, 0) <= now:
                self.__delitem__(key)
        document = {
            "schema_version": self.SCHEMA_VERSION,
            "entries": {
                key: {
                    "expires_at": self._expires[key],
                    "touched_at": self._touched.get(key, now),
                    "value": dict.__getitem__(self, key),
                }
                for key in dict.keys(self)
            },
        }
        atomic_write_json(self.path, document, backup=True)
        self._dirty = False
        return True


tmdb_response_cache = PersistentTTLCache()
