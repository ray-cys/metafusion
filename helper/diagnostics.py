import copy
import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR, CACHE_DIR, ENV_BINDINGS, SECRET_FILE_BINDINGS
from helper.io import atomic_write_text
from helper.state_db import SCHEMA_VERSION as STATE_SCHEMA_VERSION
from helper.state_db import STATE_DATABASE
from helper.tmdb_cache import PersistentTTLCache


def _flatten_metadata_fields(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten_metadata_fields(child, (*prefix, str(key)))
        return
    yield ".".join(prefix), value


def _metadata_path_value(document, path):
    current = document
    for part in path.split(".") if path else []:
        if not isinstance(current, dict):
            return None, False
        candidates = (part, int(part)) if part.isdigit() else (part,)
        for candidate in candidates:
            if candidate in current:
                current = current[candidate]
                break
        else:
            return None, False
    return current, True


def record_kometa_metadata_audit(
    config,
    *,
    library,
    media_type,
    title,
    existing,
    generated,
    diagnostics=None,
):
    """Record field-level TMDb-to-Kometa comparisons without retaining values."""
    if not config.get("_execution", {}).get("metadata_audit", False):
        return 0
    records = config.setdefault("_metadata_audit_records", [])
    for field, desired in _flatten_metadata_fields(generated):
        current, present = _metadata_path_value(existing or {}, field)
        if desired in (None, "", []):
            state = "source_missing"
            action = "preserve_existing" if present else "none"
        elif not present or current in (None, "", []):
            state = "missing"
            action = "add"
        elif current == desired:
            state = "unchanged"
            action = "none"
        else:
            state = "different"
            action = "update"
        records.append(
            {
                "library": library,
                "media_type": media_type,
                "title": title,
                "child": "item",
                "field": field,
                "state": state,
                "policy": "kometa_merge",
                "proposed_action": action,
                "target": "Kometa YAML",
            }
        )
    removed = int((diagnostics or {}).get("deprecated_removed", 0))
    if removed:
        records.append(
            {
                "library": library,
                "media_type": media_type,
                "title": title,
                "child": "item",
                "field": "deprecated generated fields",
                "state": "unsupported",
                "policy": "kometa_schema",
                "proposed_action": f"remove ({removed})",
                "target": "Kometa YAML",
            }
        )
    return len(records)


def write_metadata_audit_report(
    records,
    gaps=None,
    *,
    mode,
    base_dir=None,
    retention=10,
):
    """Write a bounded read-only metadata comparison and proposed-action report."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"metadata-audit-{timestamp}.txt"
    current_build = build_info()
    ordered = sorted(
        (record for record in (records or []) if isinstance(record, dict)),
        key=lambda record: (
            str(record.get("library") or "").casefold(),
            str(record.get("title") or "").casefold(),
            str(record.get("child") or ""),
            str(record.get("field") or ""),
        ),
    )
    counts = {}
    for record in ordered:
        state = str(record.get("state") or record.get("action") or "unknown")
        counts[state] = counts.get(state, 0) + 1
    relevant_gaps = [
        gap
        for gap in (gaps or [])
        if isinstance(gap, dict)
        and str(gap.get("category") or "").startswith(("identity", "tmdb"))
    ]
    lines = [
        "MetaFusion read-only metadata audit",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Mode: {mode}",
        f"Field decisions: {len(ordered)}",
        f"Identity/source gaps: {len(relevant_gaps)}",
        "Values are intentionally omitted; no metadata, artwork, cache, or ownership state was written.",
        "",
        "Summary",
    ]
    lines.extend(
        f"- {state}: {count}" for state, count in sorted(counts.items())
    )
    if not counts:
        lines.append("- no eligible metadata fields")
    lines.extend(("", "Field decisions"))
    if not ordered:
        lines.append("- none")
    for record in ordered:
        state = record.get("state") or record.get("action") or "unknown"
        lines.append(
            f"- [{state}] {record.get('library') or 'Unknown library'} | "
            f"{record.get('media_type') or 'Unknown'} | "
            f"{record.get('title') or 'Unknown title'} | "
            f"{record.get('child') or 'item'} | {record.get('field') or 'unknown'} | "
            f"policy={record.get('policy') or 'unknown'} | "
            f"proposed={record.get('proposed_action') or record.get('detail') or 'none'}"
        )
    lines.extend(("", "Rejected identities and unavailable TMDb sources"))
    if not relevant_gaps:
        lines.append("- none")
    for gap in relevant_gaps:
        detail = f" | {gap.get('detail')}" if gap.get("detail") else ""
        lines.append(
            f"- [{gap.get('category')}] {gap.get('library') or 'Unknown library'} | "
            f"{gap.get('media_type') or 'Unknown'} | "
            f"{gap.get('title') or 'Unknown title'}{detail}"
        )
    atomic_write_text(path, "\n".join(lines) + "\n")
    reports = sorted(report_dir.glob("metadata-audit-*.txt"), reverse=True)
    for stale in reports[max(1, int(retention)) :]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


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


def write_release_qualification_report(
    config,
    preflight,
    *,
    base_dir=None,
    environ=None,
):
    """Write a redacted pass/fail release qualification report."""
    environ = os.environ if environ is None else environ
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"release-qualification-{timestamp}.txt"
    current_build = build_info(environ)
    state_status = _database_status(STATE_DATABASE)
    cache_status = _tmdb_cache_status(CACHE_DIR / "tmdb_cache.sqlite3")
    path_advice = (preflight or {}).get("path_advice") or {}
    unresolved = sum(
        record.get("status") == "unresolved"
        for record in path_advice.get("records", [])
        if isinstance(record, dict)
    )
    checks = [
        ("Configuration", True, "valid"),
        ("Plex and TMDb connectors", True, "authenticated and reachable"),
        (
            "Supported libraries",
            bool((preflight or {}).get("available_count", 0)),
            f"{int((preflight or {}).get('available_count', 0))} available",
        ),
        (
            "Plex media path samples",
            unresolved == 0,
            "resolved" if unresolved == 0 else f"{unresolved} unresolved",
        ),
        (
            "Durable state database",
            state_status == "missing" or "check ok" in state_status,
            state_status,
        ),
        (
            "Disposable TMDb cache",
            cache_status == "missing" or "health ok" in cache_status,
            cache_status,
        ),
    ]
    passed = all(check[1] for check in checks)
    settings = config.get("settings", {})
    lines = [
        "MetaFusion release qualification (values and secrets omitted)",
        f"Generated: {generated.isoformat()}",
        f"Result: {'PASS' if passed else 'FAIL'}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Python: {platform.python_version()}",
        f"Platform: {platform.system()} {platform.release()}",
        f"Architecture: {platform.machine()}",
        f"Run mode: {settings.get('mode')}",
        f"State schema supported: {STATE_SCHEMA_VERSION}",
        f"TMDb cache schema supported: {PersistentTTLCache.SCHEMA_VERSION}",
        "",
        "Automated checks",
    ]
    for name, success, detail in checks:
        lines.append(f"- [{'PASS' if success else 'FAIL'}] {name}: {detail}")
    lines.extend(
        (
            "",
            "Manual release gates still required",
            "- Complete one full scan with cleanup disabled or its dry-run reviewed.",
            "- Confirm an immediate unchanged incremental run selects only due work.",
            "- Confirm scheduled restart/catch-up and graceful stop on the deployment host.",
            "- Back up /config and generated output while the container is stopped.",
            "",
            "This report contains no connector URLs, tokens, keys, library names, or host paths.",
        )
    )
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path, passed
