import copy
import os
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
from helper.config import (
    BASE_CONFIG_DIR,
    CACHE_DIR,
    ENV_BINDINGS,
    SECRET_FILE_BINDINGS,
    report_retention,
)
from helper.io import sha256_file
from helper.report_identity import item_report_record, item_report_records
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import SCHEMA_VERSION as STATE_SCHEMA_VERSION
from helper.state_db import STATE_DATABASE
from helper.tmdb_cache import PersistentTTLCache


def _write_report(path, lines, report_type, data, retention):
    path = write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type=report_type,
        data=data,
    )
    retain_diagnostic_reports(path.parent, path.stem.rsplit("-", 2)[0], retention)
    return path


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
    identity=None,
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
            item_report_record({
                "library": library,
                "media_type": media_type,
                "title": title,
                "child": "item",
                "field": field,
                "state": state,
                "policy": "kometa_merge",
                "proposed_action": action,
                "target": "Kometa YAML",
            }, identity)
        )
    removed = int((diagnostics or {}).get("deprecated_removed", 0))
    if removed:
        records.append(
            item_report_record({
                "library": library,
                "media_type": media_type,
                "title": title,
                "child": "item",
                "field": "deprecated generated fields",
                "state": "unsupported",
                "policy": "kometa_schema",
                "proposed_action": f"remove ({removed})",
                "target": "Kometa YAML",
            }, identity)
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
        item_report_records(records),
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
        for gap in item_report_records(gaps)
        if str(gap.get("category") or "").startswith(("identity", "tmdb"))
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
    return _write_report(
        path,
        lines,
        "metadata_audit",
        {
            "mode": mode,
            "field_decisions": ordered,
            "identity_source_gaps": relevant_gaps,
            "summary": counts,
        },
        retention,
    )


def _append_artwork_selection_details(lines, candidate, indent="  "):
    if candidate.get("provider"):
        lines.append(
            f"{indent}provider: {candidate.get('provider')}"
            + (
                f" | image ID={candidate.get('provider_image_id')}"
                if candidate.get("provider_image_id")
                else ""
            )
        )
    if candidate.get("selection_reason"):
        lines.append(
            f"{indent}selection reason: {candidate.get('selection_reason')}"
        )
    attempts = candidate.get("provider_attempts") or []
    if attempts:
        lines.append(f"{indent}providers attempted:")
        for attempt in attempts:
            lines.append(
                f"{indent}- {attempt.get('provider', 'unknown')}: "
                f"{attempt.get('status', 'unknown')} "
                f"({attempt.get('candidates', 0)} candidate(s))"
            )
    components = candidate.get("quality_components") or {}
    if components:
        lines.append(
            f"{indent}selected components: "
            f"resolution={components.get('resolution', 0):g}, "
            f"vote={components.get('vote', 0):g}, "
            f"aspect={components.get('aspect', 0):g}, "
            f"language={components.get('language', 0):g}"
        )
    rejected = candidate.get("rejected_candidates") or []
    if rejected:
        lines.append(f"{indent}highest-scoring rejected candidates:")
    for alternative in rejected:
        reasons = ", ".join(alternative.get("reasons") or ["not selected"])
        lines.append(
            f"{indent}- {alternative.get('width', 0)}x"
            f"{alternative.get('height', 0)} "
            f"lang={alternative.get('language', 'untagged')} "
            f"vote={alternative.get('vote', 0):g} "
            f"score={alternative.get('quality_score', 0):g}: {reasons}"
        )


def write_change_plan_report(
    metadata_records,
    asset_records,
    library_records,
    gaps=None,
    cleanup_result=None,
    *,
    mode,
    base_dir=None,
    retention=10,
):
    """Write one value-safe plan covering metadata, artwork, and cleanup."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"change-plan-{timestamp}.txt"
    current_build = build_info()
    metadata = item_report_records(metadata_records)
    assets = item_report_records(asset_records)
    normalized_gaps = item_report_records(gaps)
    libraries = [item for item in (library_records or []) if isinstance(item, dict)]
    actionable_metadata = sum(
        str(item.get("proposed_action") or "none")
        not in {"none", "preserve_existing", "skip_locked", "preserve"}
        for item in metadata
    )
    actionable_assets = sum(
        str(item.get("action") or "")
        in {"would_download", "would_consider_upgrade", "would_verify_for_adoption"}
        for item in assets
    )

    def cleanup_value(name):
        if cleanup_result is None:
            return 0
        if isinstance(cleanup_result, dict):
            return cleanup_result.get(name, 0)
        return getattr(cleanup_result, name, 0)

    lines = [
        "MetaFusion read-only change plan",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Mode: {mode}",
        "No metadata, artwork, cache, ownership, retry, or incremental state was written.",
        "The report itself is the only deliberate output.",
        "",
        "Summary",
        f"- Libraries inspected: {len(libraries)}",
        f"- Metadata field decisions: {len(metadata)} ({actionable_metadata} actionable)",
        f"- Artwork decisions: {len(assets)} ({actionable_assets} actionable)",
        f"- Gaps/rejections: {len(normalized_gaps)}",
        "- Cleanup candidates: "
        f"titles={cleanup_value('titles')}, seasons={cleanup_value('seasons')}, "
        f"episodes={cleanup_value('episodes')}, assets={cleanup_value('assets')}, "
        f"cache_entries={cleanup_value('cache_entries')}, "
        f"yaml_entries={cleanup_value('yaml_entries')}, "
        f"pending_confirmation={cleanup_value('candidates_pending')}, "
        f"assets_preserved={cleanup_value('assets_preserved')}, "
        f"assets_unchanged={cleanup_value('assets_skipped')}, "
        f"failures={cleanup_value('failures')}",
        "",
        "Libraries",
    ]
    if not libraries:
        lines.append("- none")
    for record in sorted(libraries, key=lambda item: str(item.get("library") or "").casefold()):
        lines.append(
            f"- {record.get('library') or 'Unknown library'} | "
            f"type={record.get('type') or 'unknown'} | items={record.get('items', 0)} | "
            f"selected={bool(record.get('selected'))} | status={record.get('status') or 'unknown'}"
        )
    lines.extend(("", "Metadata changes"))
    planned_metadata = [
        item
        for item in metadata
        if str(item.get("proposed_action") or "none") != "none"
    ]
    if not planned_metadata:
        lines.append("- none")
    for record in sorted(
        planned_metadata,
        key=lambda item: (
            str(item.get("library") or "").casefold(),
            str(item.get("title") or "").casefold(),
            str(item.get("field") or ""),
        ),
    ):
        lines.append(
            f"- {record.get('library') or 'Unknown library'} | "
            f"{record.get('title') or 'Unknown title'} | {record.get('field') or 'unknown'} | "
            f"state={record.get('state') or 'unknown'} | "
            f"proposed={record.get('proposed_action') or 'none'}"
        )
    lines.extend(("", "Artwork changes"))
    planned_assets = [
        item for item in assets if str(item.get("action") or "") != "managed"
    ]
    if not planned_assets:
        lines.append("- none")
    for record in sorted(
        planned_assets,
        key=lambda item: (
            str(item.get("library") or "").casefold(),
            str(item.get("title") or "").casefold(),
            str(item.get("asset_type") or ""),
        ),
    ):
        candidate = record.get("candidate") or {}
        lines.append(
            f"- [{record.get('action') or 'unknown'}] "
            f"{record.get('library') or 'Unknown library'} | "
            f"{record.get('title') or 'Unknown title'} | "
            f"{record.get('asset_type') or 'artwork'} | "
            f"score={candidate.get('quality_score', 0):g} | "
            f"ownership={record.get('ownership') or 'unknown'}"
        )
        _append_artwork_selection_details(lines, candidate)
    return _write_report(
        path,
        lines,
        "change_plan",
        {
            "mode": mode,
            "libraries": libraries,
            "metadata": metadata,
            "artwork": assets,
            "gaps": normalized_gaps,
            "cleanup": {
                name: cleanup_value(name)
                for name in (
                    "titles",
                    "seasons",
                    "episodes",
                    "assets",
                    "cache_entries",
                    "yaml_entries",
                    "assets_preserved",
                    "assets_skipped",
                    "failures",
                )
            },
        },
        retention,
    )


def write_library_asset_audit_report(
    library_records,
    asset_records,
    gaps=None,
    *,
    mode,
    base_dir=None,
    retention=10,
):
    """Write a cross-mode library inventory and artwork health report."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"library-asset-audit-{timestamp}.txt"
    libraries = [item for item in (library_records or []) if isinstance(item, dict)]
    assets = item_report_records(asset_records)
    normalized_gaps = item_report_records(gaps)
    current_build = build_info()
    action_counts = {}
    for record in assets:
        action = str(record.get("action") or "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
    lines = [
        "MetaFusion read-only library and asset audit",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Mode: {mode}",
        f"Libraries discovered: {len(libraries)}",
        f"Artwork candidates: {len(assets)}",
        f"Gaps/rejections: {len(normalized_gaps)}",
        "No metadata, artwork, cache, ownership, retry, or incremental state was written.",
        "",
        "Libraries",
    ]
    if not libraries:
        lines.append("- none")
    for record in sorted(libraries, key=lambda item: str(item.get("library") or "").casefold()):
        lines.append(
            f"- {record.get('library') or 'Unknown library'} | "
            f"type={record.get('type') or 'unknown'} | items={record.get('items', 0)} | "
            f"selected={bool(record.get('selected'))} | status={record.get('status') or 'unknown'}"
        )
    lines.extend(("", "Artwork decision summary"))
    if not action_counts:
        lines.append("- none")
    for action, count in sorted(action_counts.items()):
        lines.append(f"- {action}: {count}")
    lines.extend(("", "Artwork decisions"))
    if not assets:
        lines.append("- none")
    for record in sorted(
        assets,
        key=lambda item: (
            str(item.get("library") or "").casefold(),
            str(item.get("title") or "").casefold(),
            str(item.get("asset_type") or ""),
        ),
    ):
        candidate = record.get("candidate") or {}
        lines.append(
            f"- [{record.get('action') or 'unknown'}] "
            f"{record.get('library') or 'Unknown library'} | "
            f"{record.get('title') or 'Unknown title'} | "
            f"{record.get('asset_type') or 'artwork'} | "
            f"score={candidate.get('quality_score', 0):g} | "
            f"{candidate.get('width', 0)}x{candidate.get('height', 0)} | "
            f"ownership={record.get('ownership') or 'unknown'}"
        )
        _append_artwork_selection_details(lines, candidate)
    return _write_report(
        path,
        lines,
        "library_asset_audit",
        {
            "mode": mode,
            "libraries": libraries,
            "artwork": assets,
            "gaps": normalized_gaps,
            "action_summary": action_counts,
        },
        retention,
    )


def write_compatibility_report(result, *, base_dir=None, retention=10):
    """Write a value-safe compatibility profile assessment."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"compatibility-{timestamp}.txt"
    current_build = build_info()
    lines = [
        "MetaFusion compatibility profile",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Result: {'PASS' if result.get('passed') else 'FAIL'}",
        f"Profile: {result.get('profile')}",
        f"Mode: {result.get('mode')}",
        f"Contract: {result.get('contract')}",
        "",
        "Checks",
    ]
    for check in result.get("checks", []):
        lines.append(
            f"- [{'PASS' if check.get('passed') else 'FAIL'}] "
            f"{check.get('name')}: {check.get('detail')}"
        )
    lines.extend(("", "Capabilities"))
    for capability in result.get("capabilities", []):
        lines.append(f"- {capability}")
    if result.get("warnings"):
        lines.extend(("", "Warnings"))
        lines.extend(f"- {warning}" for warning in result["warnings"])
    return _write_report(path, lines, "compatibility", result, retention)


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
        unique[key] = item_report_record(gap)
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
        detail = unique[key].get("detail") or ""
        line = (
            f"- [{category}] {library} | {media_type} | {title} | {asset_type}"
        )
        if detail:
            line += f" | {detail}"
        lines.append(line)
    records = [record for _key, record in sorted(unique.items())]
    return _write_report(
        path,
        lines,
        "artwork_gaps",
        {"entries": records},
        retention,
    )


def write_asset_audit_report(records, gaps=None, base_dir=None, retention=10):
    """Write the explicit read-only artwork audit without exposing host paths."""
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"asset-audit-{timestamp}.txt"
    current_build = build_info()
    ordered = sorted(
        item_report_records(records),
        key=lambda record: (
            str(record.get("library") or "").casefold(),
            str(record.get("title") or "").casefold(),
            str(record.get("asset_type") or ""),
            int(record.get("season_number") or -1),
        ),
    )
    normalized_gaps = item_report_records(gaps)
    lines = [
        "MetaFusion read-only asset audit",
        f"Generated: {generated.isoformat()}",
        f"Version: {current_build['version']}",
        f"Commit: {current_build['commit']}",
        f"Candidates: {len(ordered)}",
        f"Gaps: {len(normalized_gaps)}",
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
            f"vote={candidate.get('vote', 0):g} "
            f"score={candidate.get('quality_score', 0):g} | "
            f"ownership={record.get('ownership') or 'unknown'}{existing}"
        )
        _append_artwork_selection_details(lines, candidate)
    lines.extend(("", "Missing, rejected, and failed candidates"))
    if not normalized_gaps:
        lines.append("- none")
    for gap in normalized_gaps:
        detail = f" | {gap.get('detail')}" if gap.get("detail") else ""
        lines.append(
            f"- [{gap.get('category') or 'unknown'}] "
            f"{gap.get('library') or 'Unknown library'} | "
            f"{gap.get('media_type') or 'Unknown'} | "
            f"{gap.get('title') or 'Unknown title'} | "
            f"{gap.get('asset_type') or 'metadata'}{detail}"
        )
    return _write_report(
        path,
        lines,
        "asset_audit",
        {"candidates": ordered, "gaps": normalized_gaps},
        retention,
    )


def _managed_asset_roots(config):
    if not config:
        return []
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    if mode == "kometa":
        return [
            (
                Path(config.get("settings", {}).get("path", ".")) / "assets"
            ).resolve(strict=False)
        ]
    roots = []
    for mapping in config.get("plex", {}).get("path_mappings", []):
        _source, separator, destination = str(mapping).partition("=>")
        if separator and destination.strip():
            roots.append(Path(destination.strip()).resolve(strict=False))
    return roots


def _current_asset_destinations(cache):
    destinations = set()
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        paths = [entry.get("poster_path"), entry.get("background_path")]
        paths.extend(
            season.get("season_path")
            for season in (entry.get("seasons") or {}).values()
            if isinstance(season, dict)
        )
        destinations.update(
            Path(str(path)).resolve(strict=False) for path in paths if path
        )
    return destinations


def _reconcile_managed_destination(
    event,
    config,
    current_checksum=None,
    claimed_destinations=None,
):
    """Remove only a checksum-proven old managed file inside a configured root."""
    if not config:
        return "preserved", "reconciliation was not enabled"
    if str(config.get("assets", {}).get("update_policy", "managed")).lower() != "managed":
        return "preserved", "artwork policy is not managed"
    old_path = Path(str(event.get("previous_destination") or "")).resolve(
        strict=False
    )
    new_path = Path(str(event.get("new_destination") or "")).resolve(strict=False)
    if old_path == new_path:
        return "preserved", "old and current destinations resolve to the same file"
    if old_path in (claimed_destinations or set()):
        return "preserved", "old destination is still claimed by managed state"
    roots = _managed_asset_roots(config)
    if not any(old_path.is_relative_to(root) for root in roots):
        return "preserved", "old destination is outside configured managed roots"
    if not new_path.is_file() or new_path.is_symlink():
        return "preserved", "current destination is not a regular installed file"
    if not old_path.exists():
        return "already_absent", "old destination no longer exists"
    if not old_path.is_file() or old_path.is_symlink():
        return "preserved", "old destination is not a regular managed file"
    previous_checksum = str(event.get("previous_checksum") or "")
    if not previous_checksum:
        return "preserved", "no prior managed checksum is available"
    if not current_checksum:
        return "preserved", "no current managed checksum is available"
    try:
        if sha256_file(new_path) != str(current_checksum):
            return "preserved", "current destination no longer matches managed state"
        if sha256_file(old_path) != previous_checksum:
            return "preserved", "old destination was modified after MetaFusion wrote it"
        old_path.unlink()
    except OSError as error:
        return "preserved", f"safe removal failed: {type(error).__name__}"
    return "removed", "checksum-proven obsolete managed destination"


def write_destination_history_report(
    cache,
    base_dir=None,
    retention=10,
    *,
    config=None,
):
    """Report and safely reconcile checksum-proven renamed managed artwork."""
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
        "Only checksum-proven old files inside configured managed roots are removed.",
        "Modified, unproven, symlinked, or out-of-scope files are preserved.",
        "",
    ]
    results = []
    claimed_destinations = _current_asset_destinations(cache)
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
        asset_type = str(event.get("asset_type") or "")
        if asset_type == "season":
            current_record = (entry.get("seasons") or {}).get(
                str(event.get("season_number")), {}
            )
            current_checksum = current_record.get("season_checksum")
        else:
            current_checksum = entry.get(f"{asset_type}_checksum")
        status, reason = _reconcile_managed_destination(
            event,
            config,
            current_checksum,
            claimed_destinations,
        )
        event["reconciliation_status"] = status
        event["reconciliation_reason"] = reason
        event["reconciled_at"] = generated_at
        results.append(
            item_report_record({
                "cache_key": cache_key,
                "title": entry.get("title") or cache_key,
                "year": entry.get("year"),
                "asset_type": event.get("asset_type") or "artwork",
                "season_number": event.get("season_number"),
                "previous_destination": event.get("previous_destination"),
                "new_destination": event.get("new_destination"),
                "status": status,
                "reason": reason,
            }, entry)
        )
        lines.append(
            f"- {entry.get('title') or cache_key} ({entry.get('year') or 'unknown year'}) "
            f"| {asset_label} | old: {event.get('previous_destination')} "
            f"| current: {event.get('new_destination')} | {status}: {reason}"
        )

    changed = {}
    for cache_key, entry, index, event in pending:
        updated = changed.setdefault(cache_key, copy.deepcopy(entry))
        updated["destination_history"][index]["reported_at"] = generated_at
        updated["destination_history"][index]["reconciled_at"] = event.get(
            "reconciled_at"
        )
        updated["destination_history"][index]["reconciliation_status"] = event.get(
            "reconciliation_status"
        )
        updated["destination_history"][index]["reconciliation_reason"] = event.get(
            "reconciliation_reason"
        )
    for cache_key, entry in changed.items():
        cache[cache_key] = entry
    return _write_report(
        path,
        lines,
        "destination_history",
        {"entries": results},
        retention,
    )


def write_unresolved_work_report(records, base_dir=None, retention=10):
    """Write the durable open/resolved problem ledger without provider secrets."""
    records = item_report_records(records)
    if not records:
        return None
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    path = report_dir / (
        f"unresolved-work-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    open_records = [record for record in records if record.get("status") == "open"]
    resolved_records = [
        record for record in records if record.get("status") == "resolved"
    ]
    lines = [
        "MetaFusion persistent unresolved-work ledger",
        f"Generated: {generated.isoformat()}",
        f"Open: {len(open_records)}",
        f"Resolved history: {len(resolved_records)}",
        "",
        "Open work",
    ]
    if not open_records:
        lines.append("- none")
    for record in open_records:
        lines.append(
            f"- [{record.get('category')}] {record.get('library_name')} | "
            f"{record.get('media_type')} | {record.get('title')} | "
            f"{record.get('asset_type')} | occurrences={record.get('occurrences')} | "
            f"last seen={record.get('last_seen')}"
            + (f" | {record.get('detail')}" if record.get("detail") else "")
        )
    lines.extend(("", "Recently resolved"))
    for record in resolved_records[:100]:
        lines.append(
            f"- {record.get('library_name')} | {record.get('title')} | "
            f"{record.get('asset_type')} | resolved={record.get('resolved_at')}"
        )
    if not resolved_records:
        lines.append("- none")
    return _write_report(
        path,
        lines,
        "unresolved_work",
        {"entries": records},
        retention,
    )


def write_adoption_audit_report(records, base_dir=None, retention=10):
    """Report post-write local installation and ownership verification."""
    records = item_report_records(records)
    if not records:
        return None
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    path = report_dir / (
        f"adoption-audit-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    counts = {}
    for record in records:
        status = str(record.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        "MetaFusion post-application artwork adoption audit",
        f"Generated: {generated.isoformat()}",
        f"Entries: {len(records)}",
        "Plex visibility is reported as pending when normal Plex discovery is still required; no forced refresh is performed.",
        "",
        "Summary",
    ]
    lines.extend(f"- {status}: {count}" for status, count in sorted(counts.items()))
    lines.extend(("", "Applied artwork"))
    for record in records:
        asset = str(record.get("asset_type") or "artwork")
        if record.get("season_number") is not None:
            asset += f" season {record.get('season_number')}"
        lines.append(
            f"- [{record.get('status')}] {record.get('library')} | "
            f"{record.get('title')} | {asset} | provider={record.get('provider')} | "
            f"Plex visibility={record.get('plex_visibility')} | "
            f"destination={record.get('destination')}"
        )
    return _write_report(
        path,
        lines,
        "adoption_audit",
        {"summary": counts, "entries": records},
        retention,
    )


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
        f"Fanart.tv cache database: {_tmdb_cache_status(CACHE_DIR / 'fanart_cache.sqlite3')}",
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
            "Do not attach config.yml, container inspection output, tokens, or API keys.",
        )
    )
    return _write_report(
        path,
        lines,
        "support_report",
        {
            "build": current_build,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
            "architecture": platform.machine(),
            "mode": settings.get("mode"),
            "dry_run": bool(settings.get("dry_run")),
            "configured_library_count": len(config.get("plex_libraries", [])),
            "path_mapping_count": len(config.get("plex", {}).get("path_mappings", [])),
            "plex_metadata_enabled": bool(plex_metadata.get("enabled")),
            "plex_metadata_policy": plex_metadata.get("policy"),
            "environment_bindings": sorted(environment_names),
            "secret_file_bindings": sorted(secret_file_names),
            "configuration_error_count": len(errors),
        },
        report_retention(config),
    )


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
    fanart_cache_status = _tmdb_cache_status(CACHE_DIR / "fanart_cache.sqlite3")
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
        (
            "Disposable Fanart.tv cache",
            fanart_cache_status == "missing" or "health ok" in fanart_cache_status,
            fanart_cache_status,
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
        f"Fanart.tv cache schema supported: {PersistentTTLCache.SCHEMA_VERSION}",
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
    report = _write_report(
        path,
        lines,
        "release_qualification",
        {
            "passed": passed,
            "build": current_build,
            "mode": settings.get("mode"),
            "checks": [
                {"name": name, "passed": success, "detail": detail}
                for name, success, detail in checks
            ],
        },
        report_retention(config),
    )
    return report, passed
