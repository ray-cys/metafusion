"""Validated configuration reloads between scheduled MetaFusion jobs."""

from __future__ import annotations

import os
from pathlib import Path


def configuration_watch_signature(config_dir, environ=None):
    environ = os.environ if environ is None else environ
    paths = [
        Path(config_dir) / "config.yml",
        Path(config_dir) / "kometa.yml",
        Path(config_dir) / "plex.yml",
    ]
    for name in ("PLEX_TOKEN_FILE", "TMDB_API_KEY_FILE"):
        value = str(environ.get(name) or "").strip()
        if value:
            paths.append(Path(value))
    signature = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append(
                (str(path), True, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            )
        except OSError:
            signature.append((str(path), False, None, None, None, None))
    return tuple(signature)


class ScheduledConfigReloader:
    def __init__(self, config_dir, loader, validator, path_validator, *, environ=None):
        self.config_dir = Path(config_dir)
        self.loader = loader
        self.validator = validator
        self.path_validator = path_validator
        self.environ = os.environ if environ is None else environ
        self.signature = configuration_watch_signature(
            self.config_dir, self.environ
        )

    def reload_if_changed(self, current):
        if not current.get("runtime", {}).get("config_reload", True):
            return current, False, None
        signature = configuration_watch_signature(self.config_dir, self.environ)
        if signature == self.signature:
            return current, False, None
        self.signature = signature
        try:
            candidate = self.loader()
            errors = list(self.validator(candidate) or [])
            if errors:
                return current, False, "; ".join(str(error) for error in errors)
            self.path_validator(candidate)
        except Exception as error:
            return current, False, str(error)
        return candidate, True, None
