import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from helper.config import BASE_CONFIG_DIR, CACHE_DIR, ENV_BINDINGS, SECRET_FILE_BINDINGS
from helper.io import atomic_write_text
from helper.state_db import STATE_DATABASE


def _database_status(path):
    path = Path(path)
    if not path.exists():
        return "missing"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
        return (
            f"present, {path.stat().st_size} bytes, schema {version}, check {integrity}"
        )
    except (OSError, sqlite3.Error) as error:
        return f"unreadable ({type(error).__name__})"


def write_support_report(config, validation_errors=None, base_dir=None, environ=None):
    """Write a value-free diagnostic report suitable for a GitHub issue."""
    environ = os.environ if environ is None else environ
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = report_dir / f"support-report-{timestamp}.txt"
    settings = config.get("settings", {})
    plex_metadata = config.get("plex_metadata", {})
    environment_names = [
        name
        for name, _path, _converter in ENV_BINDINGS
        if str(environ.get(name, "")).strip()
    ]
    secret_file_names = [
        name
        for name, _path, _direct in SECRET_FILE_BINDINGS
        if str(environ.get(name, "")).strip()
    ]
    lines = [
        "MetaFusion support report (values and secrets omitted)",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Python: {platform.python_version()}",
        f"Platform: {platform.system()} {platform.release()}",
        f"Architecture: {platform.machine()}",
        f"Run mode: {settings.get('mode')}",
        f"Dry run: {bool(settings.get('dry_run'))}",
        f"Configured libraries: {len(config.get('plex_libraries', []))}",
        f"Plex path mappings: {len(config.get('plex', {}).get('path_mappings', []))}",
        f"Direct Plex metadata: {bool(plex_metadata.get('enabled'))}",
        f"Plex metadata policy: {plex_metadata.get('policy')}",
        f"Environment bindings set: {', '.join(sorted(environment_names)) or 'none'}",
        f"Secret-file bindings set: {', '.join(sorted(secret_file_names)) or 'none'}",
        f"State database: {_database_status(STATE_DATABASE)}",
        f"TMDb cache database: {_database_status(CACHE_DIR / 'tmdb_cache.sqlite3')}",
        "",
        "Configuration validation",
    ]
    errors = list(validation_errors or [])
    if errors:
        lines.append(
            f"- {len(errors)} error(s); run --doctor locally for value-bearing details"
        )
    else:
        lines.append("- valid")
    lines.extend(
        (
            "",
            "Attach this file with the redacted Plex metadata report and relevant log lines.",
            "Do not attach config.yml, container inspection output, Plex tokens, or TMDb keys.",
        )
    )
    atomic_write_text(path, "\n".join(lines))
    return path
