import copy
import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
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


def _tmdb_cache_status(path):
    path = Path(path)
    if not path.exists():
        return "missing"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            row = connection.execute(
                "SELECT entry_count, stored_bytes FROM tmdb_cache_meta "
                "WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return "unreadable (metadata row missing)"
        return (
            f"present, health {integrity}, schema {version}, entries {int(row[0])}, "
            f"compressed {int(row[1])} bytes, disk {path.stat().st_size} bytes"
        )
    except (OSError, sqlite3.Error) as error:
        return f"unreadable ({type(error).__name__})"


def write_artwork_gap_report(gaps, base_dir=None, retention=10):
    """Write a bounded, value-safe list of artwork and identity gaps."""
    unique = {}
    for gap in gaps or []:
        if not isinstance(gap, dict):
            continue
        key = (
            str(gap.get("library") or "Unknown library"),
            str(gap.get("media_type") or "Unknown"),
            str(gap.get("title") or "Unknown title"),
            str(gap.get("asset_type") or "metadata"),
            str(gap.get("category") or "unknown"),
        )
        unique[key] = str(gap.get("detail") or "")
    if not unique:
        return None

    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"artwork-gaps-{timestamp}.txt"
    current_build = build_info()
    lines = [
        "MetaFusion artwork and identity gaps",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Entries: {len(unique)}",
        "",
    ]
    for key in sorted(unique, key=lambda value: tuple(part.casefold() for part in value)):
        library, media_type, title, asset_type, category = key
        detail = unique[key]
        line = (
            f"- [{category}] {library} | {media_type} | {title} | {asset_type}"
        )
        if detail:
            line += f" | {detail}"
        lines.append(line)
    atomic_write_text(path, "\n".join(lines) + "\n")

    reports = sorted(report_dir.glob("artwork-gaps-*.txt"), reverse=True)
    for stale in reports[max(1, int(retention)):]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


def write_asset_audit_report(records, gaps=None, base_dir=None, retention=10):
    """Write the explicit read-only artwork audit without exposing host paths."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"asset-audit-{timestamp}.txt"
    current_build = build_info()
    ordered = sorted(
        (record for record in (records or []) if isinstance(record, dict)),
        key=lambda record: (
            str(record.get("library") or "").casefold(),
            str(record.get("title") or "").casefold(),
            str(record.get("asset_type") or ""),
            int(record.get("season_number") or -1),
        ),
    )
    lines = [
        "MetaFusion read-only asset audit",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Candidates: {len(ordered)}",
        f"Gaps: {len(gaps or [])}",
        "",
        "Candidate decisions",
    ]
    if not ordered:
        lines.append("- none")
    for record in ordered:
        candidate = record.get("candidate") or {}
        asset = str(record.get("asset_type") or "artwork")
        if record.get("season_number") is not None:
            asset += f" {record['season_number']}"
        existing = ""
        if record.get("existing_width") and record.get("existing_height"):
            existing = (
                f" | existing {record['existing_width']}x"
                f"{record['existing_height']}"
            )
        lines.append(
            f"- [{record.get('action') or 'unknown'}] "
            f"{record.get('library') or 'Unknown library'} | "
            f"{record.get('media_type') or 'Unknown'} | "
            f"{record.get('title') or 'Unknown title'} | {asset} | "
            f"candidate {candidate.get('width', 0)}x{candidate.get('height', 0)} "
            f"lang={candidate.get('language', 'untagged')} "
            f"vote={candidate.get('vote', 0):g} | "
            f"ownership={record.get('ownership') or 'unknown'}{existing}"
        )
    lines.extend(("", "Missing, rejected, and failed candidates"))
    if not gaps:
        lines.append("- none")
    for gap in gaps or []:
        if not isinstance(gap, dict):
            continue
        detail = f" | {gap.get('detail')}" if gap.get("detail") else ""
        lines.append(
            f"- [{gap.get('category') or 'unknown'}] "
            f"{gap.get('library') or 'Unknown library'} | "
            f"{gap.get('media_type') or 'Unknown'} | "
            f"{gap.get('title') or 'Unknown title'} | "
            f"{gap.get('asset_type') or 'metadata'}{detail}"
        )
    atomic_write_text(path, "\n".join(lines) + "\n")

    reports = sorted(report_dir.glob("asset-audit-*.txt"), reverse=True)
    for stale in reports[max(1, int(retention)):]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


def write_destination_history_report(cache, base_dir=None, retention=10):
    """Report renamed artwork destinations without deleting either location."""
    pending = []
    for cache_key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        for index, event in enumerate(entry.get("destination_history") or []):
            if isinstance(event, dict) and not event.get("reported_at"):
                pending.append((cache_key, entry, index, event))
    if not pending:
        return None

    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated_at = datetime.now(timezone.utc).isoformat()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"destination-history-{timestamp}.txt"
    current_build = build_info()
    lines = [
        "MetaFusion artwork destination history",
        f"Generated: {generated_at}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Entries: {len(pending)}",
        "",
        "Old destinations are reported for manual review and are never deleted automatically.",
        "",
    ]
    for cache_key, entry, _index, event in sorted(
        pending,
        key=lambda value: (
            str(value[1].get("title") or "").casefold(),
            str(value[3].get("asset_type") or ""),
        ),
    ):
        asset_label = str(event.get("asset_type") or "artwork")
        if event.get("season_number") is not None:
            asset_label += f" season {event['season_number']}"
        lines.append(
            f"- {entry.get('title') or cache_key} ({entry.get('year') or 'unknown year'}) "
            f"| {asset_label} | old: {event.get('previous_destination')} "
            f"| current: {event.get('new_destination')}"
        )
    atomic_write_text(path, "\n".join(lines) + "\n")

    changed = {}
    for cache_key, entry, index, _event in pending:
        updated = changed.setdefault(cache_key, copy.deepcopy(entry))
        updated["destination_history"][index]["reported_at"] = generated_at
    for cache_key, entry in changed.items():
        cache[cache_key] = entry

    reports = sorted(report_dir.glob("destination-history-*.txt"), reverse=True)
    for stale in reports[max(1, int(retention)):]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


def write_support_report(config, validation_errors=None, base_dir=None, environ=None):
    """Write a value-free diagnostic report suitable for a GitHub issue."""
    environ = os.environ if environ is None else environ
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"support-report-{timestamp}.txt"
    current_build = build_info(environ)
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
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
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
        f"TMDb cache database: {_tmdb_cache_status(CACHE_DIR / 'tmdb_cache.sqlite3')}",
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
