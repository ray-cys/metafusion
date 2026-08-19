"""Explicit, checksum-guarded maintenance for generated MetaFusion outputs."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from helper.config import BASE_CONFIG_DIR, mode_check
from helper.io import sha256_file
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import (
    find_media_state,
    load_asset_ownership,
    record_cleanup_history,
    remove_asset_ownership,
)
from modules.kometa import validate_metadata_document, write_kometa_metadata


class OutputManagementError(RuntimeError):
    pass


def _managed_roots(config):
    if mode_check(config, "kometa"):
        return [
            (Path(config.get("settings", {}).get("path", ".")) / "assets").resolve(
                strict=False
            )
        ]
    roots = []
    for mapping in config.get("plex", {}).get("path_mappings", []):
        _source, separator, destination = str(mapping).partition("=>")
        if separator and destination.strip():
            roots.append(Path(destination.strip()).resolve(strict=False))
    return roots


def resolve_output_targets(config, *, libraries=None, rating_keys=None, tmdb_ids=None):
    items = find_media_state(
        libraries=libraries,
        rating_keys=rating_keys,
        tmdb_ids=tmdb_ids,
    )
    if not items:
        raise OutputManagementError("No durable item matches the requested target")
    if len(items) > 1:
        raise OutputManagementError(
            "The target matches multiple items; add --library and --rating-key"
        )
    return items[0]


def _safe_asset_decision(config, item, ownership, action):
    destination = Path(str(ownership.get("destination") or ""))
    result = {
        "output_type": ownership.get("asset_type"),
        "season_number": (
            int(ownership["season_number"])
            if str(ownership.get("season_number") or "").lstrip("-").isdigit()
            else None
        ),
        "destination": str(destination),
        "checksum": ownership.get("checksum"),
        "action": action,
        "status": "eligible",
        "reason": "checksum-proven MetaFusion ownership",
    }
    if action == "forget":
        result["reason"] = "ownership will be forgotten; the file will be retained"
        return result
    roots = _managed_roots(config)
    resolved = destination.resolve(strict=False)
    if not roots or not any(resolved.is_relative_to(root) for root in roots):
        result.update(status="protected", reason="destination is outside configured managed roots")
        return result
    if destination.is_symlink():
        result.update(status="protected", reason="destination is a symbolic link")
        return result
    if not destination.exists():
        result.update(status="already_absent", reason="recorded file no longer exists")
        return result
    if not destination.is_file():
        result.update(status="protected", reason="destination is not a regular file")
        return result
    checksum = str(ownership.get("checksum") or "")
    if not checksum:
        result.update(status="protected", reason="ownership record has no checksum")
        return result
    try:
        current = sha256_file(destination)
    except OSError as error:
        result.update(status="protected", reason=f"checksum could not be read: {error}")
        return result
    if current != checksum:
        result.update(status="protected", reason="file was modified after MetaFusion wrote it")
    return result


def _metadata_file(config, item):
    media_type = "tv" if str(item.get("media_type")).lower() in {"tv", "show"} else "movie"
    return Path(config.get("settings", {}).get("path", ".")) / "metadata" / f"{media_type}_metadata.yml"


def _metadata_match(item, name, entry):
    if not isinstance(entry, dict):
        return False
    match = entry.get("match") or {}
    tmdb_id = item.get("tmdb_id")
    if tmdb_id is not None and str(match.get("mapping_id") or "") == str(tmdb_id):
        edition = item.get("edition")
        return not edition or str(match.get("edition") or "") == str(edition)
    expected = f"{item.get('title')} ({item.get('year')})"
    return str(name) == expected


def _metadata_decision(config, item, action):
    path = _metadata_file(config, item)
    result = {
        "output_type": "metadata",
        "season_number": None,
        "destination": str(path),
        "checksum": None,
        "action": action,
        "status": "eligible",
        "reason": "one exact Kometa metadata identity matched",
        "metadata_key": None,
    }
    if mode_check(config, "plex"):
        result.update(status="protected", reason="Plex mode does not own Kometa YAML")
        return result
    if not path.exists():
        result.update(status="already_absent", reason="Kometa metadata file does not exist")
        return result
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        validate_metadata_document(document)
    except (OSError, yaml.YAMLError, ValueError) as error:
        result.update(status="protected", reason=f"metadata document is not safely readable: {error}")
        return result
    matches = [
        name
        for name, entry in (document.get("metadata") or {}).items()
        if _metadata_match(item, name, entry)
    ]
    if len(matches) != 1:
        result.update(
            status="protected",
            reason=f"expected one exact metadata identity but found {len(matches)}",
        )
        return result
    result["metadata_key"] = matches[0]
    return result


def plan_output_management(
    config,
    item,
    *,
    action,
    output_type="all",
    season_number=None,
):
    action = str(action).lower()
    if action not in {"preview", "remove", "forget", "rebuild"}:
        raise OutputManagementError(f"Unsupported output action: {action}")
    output = str(output_type).lower()
    allowed = {"all", "metadata", "poster", "background", "season"}
    if output not in allowed:
        raise OutputManagementError(f"Unsupported output type: {output_type}")
    ownership = load_asset_ownership([item["cache_key"]])
    decisions = []
    for record in ownership:
        asset_type = str(record.get("asset_type") or "")
        if output not in {"all", asset_type}:
            continue
        record_season = (
            int(record["season_number"])
            if str(record.get("season_number") or "").lstrip("-").isdigit()
            else None
        )
        if asset_type == "season" and season_number is not None and record_season != int(season_number):
            continue
        decisions.append(_safe_asset_decision(config, item, record, action))
    if output in {"all", "metadata"}:
        decisions.append(_metadata_decision(config, item, action))
    return decisions


def _remove_metadata(config, item, decision):
    path = Path(decision["destination"])
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    metadata = document.get("metadata") or {}
    metadata.pop(decision["metadata_key"])
    output_config = config.get("output", {})
    write_kometa_metadata(
        path,
        document,
        validate_schema=output_config.get("validate_schema", True),
        backup_count=output_config.get("backup_count", 3),
        library_type="tv" if item.get("media_type") == "tv" else "movie",
        expected_snapshot=(True, sha256_file(path)),
    )


def apply_output_management(
    config,
    item,
    decisions,
    *,
    action,
    acknowledge_metadata_loss=False,
):
    action = str(action).lower()
    if action == "preview":
        return decisions
    results = []
    for decision in decisions:
        result = dict(decision)
        if result.get("status") == "protected":
            results.append(result)
            continue
        output_type = result.get("output_type")
        if output_type == "metadata":
            if action == "forget":
                result.update(
                    status="protected",
                    reason=(
                        "Kometa YAML entries have no separable ownership claim to forget; "
                        "use preview, remove, or rebuild explicitly"
                    ),
                )
                results.append(result)
                continue
            if not acknowledge_metadata_loss and result.get("status") != "already_absent":
                result.update(
                    status="protected",
                    reason=(
                        "whole-entry removal can delete manual Kometa fields; "
                        "repeat with --acknowledge-metadata-loss"
                    ),
                )
                results.append(result)
                continue
            if result.get("status") != "already_absent":
                _remove_metadata(config, item, result)
            result["status"] = "removed"
        else:
            destination = Path(result["destination"])
            if action != "forget" and result.get("status") != "already_absent":
                destination.unlink()
            remove_asset_ownership(
                item["cache_key"], output_type, result.get("season_number")
            )
            result["status"] = "forgotten" if action == "forget" else "removed"
        record_cleanup_history(
            "manual",
            action,
            result["status"],
            item,
            output_type=output_type,
            season_number=result.get("season_number"),
            destination=result.get("destination"),
            checksum=result.get("checksum"),
            reason=result.get("reason"),
            details={"metadata_key": result.get("metadata_key")},
        )
        results.append(result)
    return results


def write_output_management_report(
    item,
    records,
    *,
    action,
    base_dir=None,
    retention=10,
):
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / (
        f"output-management-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    lines = [
        "MetaFusion targeted output management",
        f"Generated: {generated.isoformat()}",
        f"Action: {action}",
        f"Item: {item.get('library_name')} | {item.get('title')} ({item.get('year')})",
        f"Plex rating key: {item.get('rating_key')}",
        f"TMDb ID: {item.get('tmdb_id') or 'none'}",
        "Only checksum-proven managed artwork is removable. Plex media files are never targeted.",
        "",
    ]
    if not records:
        lines.append("- no matching generated output")
    for record in records:
        label = record.get("output_type") or "output"
        if record.get("season_number") is not None:
            label += f" season {record['season_number']}"
        lines.append(
            f"- [{record.get('status')}] {label} | {record.get('destination')} "
            f"| {record.get('reason')}"
        )
    write_diagnostic_report(
        path,
        "\n".join(lines) + "\n",
        report_type="output_management",
        data={"action": action, "item": item, "entries": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "output-management", retention)
    return path
