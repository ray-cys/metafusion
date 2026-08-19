"""Human-readable, read-only reports derived only from durable SQLite state."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR
from helper.database_maintenance import DATABASES, inspect_database
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import (
    STATE_DATABASE,
    find_media_state,
    load_asset_ownership,
    load_cleanup_candidates,
    load_cleanup_history,
    load_identity_overrides,
    load_identity_reviews,
    load_item_exceptions,
    load_item_retries,
    load_library_rebinding_history,
    recent_job_runs,
)

STATE_TABLES = (
    "media_state",
    "season_state",
    "library_scan_state",
    "job_runs",
    "asset_ownership",
    "plex_metadata_ownership",
    "item_retry_queue",
    "plex_library_inventory",
    "identity_bindings",
    "identity_binding_history",
    "unresolved_work",
    "artwork_analysis",
    "cleanup_candidates",
    "cleanup_history",
    "item_exceptions",
    "identity_overrides",
    "identity_review_queue",
    "library_rebinding_history",
)


def _readonly_connection(path):
    database = Path(path)
    if not database.exists():
        return None
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def _database_counts(path=STATE_DATABASE):
    connection = _readonly_connection(path)
    if connection is None:
        return {}, [], []
    try:
        available = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in STATE_TABLES
            if table in available
        }
        scans = (
            [dict(row) for row in connection.execute(
                "SELECT * FROM library_scan_state ORDER BY library_name"
            ).fetchall()]
            if "library_scan_state" in available
            else []
        )
        inventory = (
            [dict(row) for row in connection.execute(
                "SELECT * FROM plex_library_inventory ORDER BY library_name"
            ).fetchall()]
            if "plex_library_inventory" in available
            else []
        )
        return counts, scans, inventory
    finally:
        connection.close()


def _format_bytes(value):
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def _filter_values(values):
    return [str(value) for value in (values or []) if str(value).strip()]


def write_state_report(
    *,
    libraries=None,
    rating_keys=None,
    tmdb_ids=None,
    media_types=None,
    section="all",
    include_items=False,
    base_dir=None,
    retention=10,
    path=STATE_DATABASE,
):
    """Write a consistent report without initializing, migrating, or updating SQLite."""
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    report_path = report_dir / f"state-report-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    counts, scans, inventory = _database_counts(path)
    items = find_media_state(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        tmdb_ids=_filter_values(tmdb_ids),
        media_types=_filter_values(media_types),
        path=path,
    )
    cache_keys = [item["cache_key"] for item in items]
    assets = load_asset_ownership(cache_keys, path=path) if cache_keys else []
    exceptions = load_item_exceptions(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        path=path,
    )
    overrides = load_identity_overrides(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        include_inactive=True,
        path=path,
    )
    reviews = load_identity_reviews(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        path=path,
    )
    retries = load_item_retries(
        library_names=_filter_values(libraries) or None,
        rating_keys=_filter_values(rating_keys) or None,
        path=path,
    )
    cleanup_candidates = load_cleanup_candidates(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        path=path,
    )
    cleanup_history = load_cleanup_history(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        limit=250,
        path=path,
    )
    rebinding = load_library_rebinding_history(limit=100, path=path)
    jobs = recent_job_runs(limit=10, path=path)
    health = {
        name: inspect_database(database, schema)
        for name, (database, schema) in DATABASES.items()
    }
    build = build_info()
    sections = {"all", section}
    lines = [
        "MetaFusion SQLite state report",
        f"Generated: {generated.isoformat()}",
        f"Version: {build['version']}",
        f"Commit: {build['commit']}",
        "Source: SQLite only; Plex, TMDb, Fanart.tv, YAML, and artwork files were not contacted.",
        "Recorded state is historical evidence and is not proof of current external state.",
        "",
    ]
    if "all" in sections or "database" in sections:
        lines.append("Database health")
        for name, result in health.items():
            lines.append(
                f"- {name}: {result.get('status')} | schema={result.get('schema')} "
                f"| database={_format_bytes(result.get('bytes'))} "
                f"| WAL={_format_bytes(result.get('wal_bytes'))}"
            )
        lines.extend(("", "State table counts"))
        lines.extend(f"- {name}: {value}" for name, value in sorted(counts.items()))
        lines.append("")
    if "all" in sections or "libraries" in sections:
        scan_index = {
            (str(row.get("server_id")), str(row.get("library_uuid"))): row
            for row in scans
        }
        lines.append("Recorded libraries")
        if not inventory:
            lines.append("- none")
        for library in inventory:
            scan = scan_index.get(
                (str(library.get("server_id")), str(library.get("library_uuid"))), {}
            )
            lines.append(
                f"- {library.get('library_name')} | type={library.get('library_type')} "
                f"| active={bool(library.get('active'))} | last seen={library.get('last_seen')} "
                f"| full scan={scan.get('last_full_scan_completed') or 'never'} "
                f"| incremental={scan.get('last_successful_incremental') or 'never'}"
            )
        lines.append("")
    if "all" in sections or "jobs" in sections:
        lines.append("Recent jobs")
        if not jobs:
            lines.append("- none")
        for job in reversed(jobs):
            lines.append(
                f"- [{job.get('status')}] {job.get('finished_at')} | mode={job.get('mode')}"
                + (f" | error={job.get('error')}" if job.get("error") else "")
            )
        lines.append("")
    if "all" in sections or "ownership" in sections:
        asset_counts = Counter(row.get("asset_type") for row in assets)
        lines.extend((
            "Recorded ownership",
            f"- selected media records: {len(items)}",
            f"- poster claims: {asset_counts.get('poster', 0)}",
            f"- background claims: {asset_counts.get('background', 0)}",
            f"- season-poster claims: {asset_counts.get('season', 0)}",
            f"- persistent exceptions: {len(exceptions)}",
            f"- active identity overrides: {sum(bool(row.get('active')) for row in overrides)}",
            "",
        ))
    if "all" in sections or "problems" in sections:
        retry_counts = Counter(str(row.get("status") or "unknown") for row in retries)
        review_counts = Counter(str(row.get("status") or "unknown") for row in reviews)
        lines.extend((
            "Recorded problems and cleanup",
            f"- retries: {len(retries)} ({', '.join(f'{key}={value}' for key, value in sorted(retry_counts.items())) or 'none'})",
            f"- identity reviews: {len(reviews)} ({', '.join(f'{key}={value}' for key, value in sorted(review_counts.items())) or 'none'})",
            f"- pending cleanup candidates: {len(cleanup_candidates)}",
            f"- recent cleanup history entries: {len(cleanup_history)}",
            f"- recent rebinding history entries: {len(rebinding)}",
            "",
        ))
    targeted = bool(libraries or rating_keys or tmdb_ids or media_types)
    if include_items or targeted or section == "items":
        lines.append("Selected item records")
        if not items:
            lines.append("- none")
        for item in items:
            lines.append(
                f"- {item.get('library_name')} | {item.get('media_type')} | "
                f"{item.get('title')} ({item.get('year')}) | rating key={item.get('rating_key')} "
                f"| TMDb={item.get('tmdb_id') or 'none'} | IMDb={item.get('imdb_id') or 'none'} "
                f"| TVDB={item.get('tvdb_id') or 'none'} | cache key={item.get('cache_key')}"
            )
    data = {
        "notice": "SQLite-only recorded state; external resources were not verified.",
        "health": health,
        "table_counts": counts,
        "libraries": inventory,
        "scan_state": scans,
        "recent_jobs": jobs,
        "items": items if include_items or targeted or section == "items" else [],
        "asset_ownership": assets if targeted or section == "ownership" else [],
        "exceptions": exceptions,
        "identity_overrides": overrides,
        "identity_reviews": reviews,
        "retries": retries,
        "cleanup_candidates": cleanup_candidates,
        "cleanup_history": cleanup_history,
        "library_rebinding_history": rebinding,
    }
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type="sqlite_state",
        data=data,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "state-report", retention)
    return report_path


def write_cleanup_history_report(
    *,
    libraries=None,
    rating_keys=None,
    sources=None,
    base_dir=None,
    retention=10,
    path=STATE_DATABASE,
):
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    report_path = report_dir / (
        f"cleanup-history-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    history = load_cleanup_history(
        sources=_filter_values(sources),
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        limit=10_000,
        path=path,
    )
    candidates = load_cleanup_candidates(
        libraries=_filter_values(libraries),
        rating_keys=_filter_values(rating_keys),
        path=path,
    )
    source_counts = Counter(row.get("source") or "unknown" for row in history)
    lines = [
        "MetaFusion cleanup history report",
        f"Generated: {generated.isoformat()}",
        "Source: SQLite only; no current Plex or filesystem state was checked.",
        f"History entries: {len(history)}",
        f"Pending confirmation candidates: {len(candidates)}",
        "Sources: " + (", ".join(f"{key}={value}" for key, value in sorted(source_counts.items())) or "none"),
        "",
        "Pending confirmation/grace candidates",
    ]
    if not candidates:
        lines.append("- none")
    for candidate in candidates:
        lines.append(
            f"- {candidate.get('library_name')} | {candidate.get('title')} "
            f"| {candidate.get('scope')} | confirmations={candidate.get('confirmations')} "
            f"| eligible after={candidate.get('eligible_after')}"
        )
    lines.extend(("", "Completed and cancelled actions"))
    if not history:
        lines.append("- none")
    for record in history:
        lines.append(
            f"- [{record.get('source')}/{record.get('status')}] {record.get('occurred_at')} "
            f"| {record.get('library_name') or 'unknown library'} "
            f"| {record.get('title') or record.get('cache_key') or 'unknown item'} "
            f"| rating key={record.get('rating_key') or 'none'} "
            f"| TMDb={record.get('tmdb_id') or 'none'} "
            f"| IMDb={record.get('imdb_id') or 'none'} "
            f"| TVDB={record.get('tvdb_id') or 'none'} "
            f"| {record.get('action')}:{record.get('output_type') or 'state'}"
            + (f" | destination={record.get('destination')}" if record.get("destination") else "")
            + (f" | {record.get('reason')}" if record.get("reason") else "")
        )
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type="cleanup_history",
        data={"pending_candidates": candidates, "history": history},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "cleanup-history", retention)
    return report_path


def write_identity_review_report(records, *, base_dir=None, retention=10):
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    report_path = report_dir / (
        f"identity-review-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    lines = [
        "MetaFusion identity review queue",
        f"Generated: {generated.isoformat()}",
        "Source: SQLite only.",
        f"Entries: {len(records)}",
        "",
    ]
    if not records:
        lines.append("- none")
    for record in records:
        lines.append(
            f"- [{record.get('status')}] {record.get('library_name')} | "
            f"{record.get('title')} | rating key={record.get('rating_key') or 'none'} "
            f"| proposed TMDb={record.get('proposed_tmdb_id') or 'none'} "
            f"| {record.get('category')}: {record.get('reason') or 'no detail'}"
        )
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type="identity_review",
        data={"entries": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "identity-review", retention)
    return report_path


def write_rebinding_report(records, *, applied=False, base_dir=None, retention=10):
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    report_path = report_dir / (
        f"library-rebinding-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    lines = [
        "MetaFusion library migration and rebinding report",
        f"Generated: {generated.isoformat()}",
        f"Mode: {'applied' if applied else 'read-only plan'}",
        "Matching requires one unique media-type, TMDb-ID, and edition destination.",
        "Plex metadata ownership and active Plex identity fingerprints are never transferred.",
        "",
    ]
    for record in records:
        source = record.get("source") or {}
        destination = record.get("destination") or {}
        lines.append(
            f"- [{record.get('status')}] {record.get('title')} ({record.get('year')}) "
            f"| TMDb={record.get('tmdb_id')} | {source.get('library_name')}/"
            f"{source.get('rating_key')} -> {destination.get('library_name') or 'unmatched'}/"
            f"{destination.get('rating_key') or '-'} | {record.get('reason')}"
        )
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type="library_rebinding",
        data={"applied": applied, "entries": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "library-rebinding", retention)
    return report_path
