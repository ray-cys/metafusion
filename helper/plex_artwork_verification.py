"""Read-only verification that Plex currently exposes MetaFusion local artwork."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from helper.config import BASE_CONFIG_DIR
from helper.io import sha256_file
from helper.plex import get_plex_metadata, load_plex_library_inventory
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import find_media_state, load_asset_ownership
from modules.utils import analyze_image_content


def _hash_distance(left, right):
    try:
        return (int(str(left), 16) ^ int(str(right), 16)).bit_count()
    except (TypeError, ValueError):
        return None


async def _download_plex_image(config, path, session):
    if not path:
        return None, "Plex exposes no selected image endpoint"
    source = str(path)
    if not source.startswith(("http://", "https://")):
        source = config.get("plex", {}).get("url", "").rstrip("/") + "/" + source.lstrip("/")
    try:
        async with session.get(
            source,
            params={"X-Plex-Token": config.get("plex", {}).get("token")},
        ) as response:
            if response.status != 200:
                return None, f"Plex returned HTTP {response.status}"
            maximum = max(1, int(config.get("runtime", {}).get("max_image_mb", 25))) * 1024 * 1024
            declared = int(response.headers.get("Content-Length") or 0)
            if declared and declared > maximum:
                return None, "Plex-selected image exceeds the configured download limit"
            content = await response.read()
            if len(content) > maximum:
                return None, "Plex-selected image exceeds the configured download limit"
            return content, None
    except Exception as error:
        return None, f"Plex image request failed: {type(error).__name__}"


def _plex_source(meta, ownership):
    artwork = meta.get("plex_artwork") or {}
    asset_type = ownership.get("asset_type")
    if asset_type == "season":
        seasons = artwork.get("seasons") or {}
        season = ownership.get("season_number")
        if not str(season).lstrip("-").isdigit():
            return None
        return seasons.get(int(season)) or seasons.get(str(int(season)))
    return artwork.get("poster" if asset_type == "poster" else "background")


async def verify_plex_artwork(sections, config, rating_keys, session):
    requested = {str(value) for value in (rating_keys or []) if str(value).strip()}
    records = []
    found = set()
    for section in sections:
        inventory = await load_plex_library_inventory(section, config.get("runtime", {}))
        server = getattr(section, "_server", None)
        server_id = getattr(server, "machineIdentifier", None) or "unknown"
        library_uuid = getattr(section, "uuid", None) or getattr(section, "key", None) or section.title
        state_items = find_media_state(
            libraries=[section.title], rating_keys=requested or None
        )
        state_by_key = {
            str(item.get("rating_key")): item
            for item in state_items
            if str(item.get("server_id")) == str(server_id)
            and str(item.get("library_uuid")) == str(library_uuid)
        }
        ownership = load_asset_ownership(
            [item["cache_key"] for item in state_by_key.values()]
        )
        ownership_by_key = {}
        for claim in ownership:
            ownership_by_key.setdefault(claim["cache_key"], []).append(claim)
        for item in inventory:
            rating_key = str(getattr(item, "ratingKey", ""))
            if requested and rating_key not in requested:
                continue
            found.add(rating_key)
            state = state_by_key.get(rating_key)
            if state is None:
                records.append(
                    {
                        "library": section.title,
                        "plex_rating_key": rating_key,
                        "title": getattr(item, "title", "Unknown"),
                        "status": "unmanaged",
                        "reason": "no MetaFusion artwork ownership is recorded",
                    }
                )
                continue
            meta = await get_plex_metadata(
                item,
                _runtime_config=config.get("runtime", {}),
                _plex_config=config.get("plex", {}),
            )
            claims = ownership_by_key.get(state["cache_key"], [])
            if not claims:
                records.append(
                    {
                        "library": section.title,
                        "plex_rating_key": rating_key,
                        "tmdb_id": state.get("tmdb_id"),
                        "imdb_id": state.get("imdb_id"),
                        "tvdb_id": state.get("tvdb_id"),
                        "title": state.get("title"),
                        "status": "unmanaged",
                        "reason": "item has no recorded artwork claims",
                    }
                )
                continue
            for claim in claims:
                destination = Path(claim["destination"])
                record = {
                    "library": section.title,
                    "plex_rating_key": rating_key,
                    "tmdb_id": state.get("tmdb_id"),
                    "imdb_id": state.get("imdb_id"),
                    "tvdb_id": state.get("tvdb_id"),
                    "title": state.get("title"),
                    "year": state.get("year"),
                    "asset_type": claim.get("asset_type"),
                    "season_number": (
                        int(claim["season_number"])
                        if str(claim.get("season_number") or "").lstrip("-").isdigit()
                        else None
                    ),
                    "destination": str(destination),
                    "status": "unverifiable",
                }
                if not destination.is_file() or destination.is_symlink():
                    record.update(status="local_missing", reason="managed local file is unavailable")
                    records.append(record)
                    continue
                expected_checksum = str(claim.get("checksum") or "")
                try:
                    current_checksum = await asyncio.to_thread(sha256_file, destination)
                    local_content = await asyncio.to_thread(destination.read_bytes)
                    local_analysis = await asyncio.to_thread(
                        analyze_image_content,
                        local_content,
                        asset_type=("background" if claim.get("asset_type") == "background" else "poster"),
                    )
                except (OSError, ValueError) as error:
                    record.update(status="unverifiable", reason=f"local image validation failed: {error}")
                    records.append(record)
                    continue
                if not expected_checksum or current_checksum != expected_checksum:
                    record.update(status="modified", reason="local file no longer matches ownership checksum")
                    records.append(record)
                    continue
                plex_content, error = await _download_plex_image(
                    config, _plex_source(meta, claim), session
                )
                if not plex_content:
                    record.update(status="plex_unavailable", reason=error)
                    records.append(record)
                    continue
                try:
                    plex_analysis = await asyncio.to_thread(
                        analyze_image_content,
                        plex_content,
                        asset_type=("background" if claim.get("asset_type") == "background" else "poster"),
                    )
                except ValueError as validation_error:
                    record.update(status="unverifiable", reason=f"Plex image validation failed: {validation_error}")
                    records.append(record)
                    continue
                distance = _hash_distance(
                    local_analysis.get("perceptual_hash"),
                    plex_analysis.get("perceptual_hash"),
                )
                exact = local_analysis.get("content_sha256") == plex_analysis.get("content_sha256")
                selected = exact or (distance is not None and distance <= 8)
                record.update(
                    status="selected" if selected else "not_selected",
                    reason=(
                        "Plex-selected artwork matches the managed local image"
                        if selected
                        else "Plex currently exposes a different selected image"
                    ),
                    exact_match=exact,
                    perceptual_distance=distance,
                    local_dimensions=[local_analysis["width"], local_analysis["height"]],
                    plex_dimensions=[plex_analysis["width"], plex_analysis["height"]],
                )
                records.append(record)
    missing = requested - found
    for rating_key in sorted(missing):
        records.append(
            {
                "plex_rating_key": rating_key,
                "status": "not_found",
                "reason": "rating key was not found in the selected live Plex libraries",
            }
        )
    return records


def write_plex_artwork_verification_report(records, *, base_dir=None, retention=10):
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / (
        f"plex-artwork-verification-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    lines = [
        "MetaFusion Plex artwork adoption verification",
        f"Generated: {generated.isoformat()}",
        "This command is read-only and does not scan, refresh, or change Plex artwork.",
        f"Entries: {len(records)}",
        "",
    ]
    for record in records:
        asset = record.get("asset_type") or "artwork"
        if record.get("season_number") is not None:
            asset += f" season {record['season_number']}"
        lines.append(
            f"- [{record.get('status')}] {record.get('library') or 'unknown library'} "
            f"| {record.get('title') or 'unknown item'} | rating key={record.get('plex_rating_key')} "
            f"| {asset} | {record.get('reason')}"
        )
    path = write_diagnostic_report(
        path,
        "\n".join(lines) + "\n",
        report_type="plex_artwork_verification",
        data={"entries": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "plex-artwork-verification", retention)
    return path
