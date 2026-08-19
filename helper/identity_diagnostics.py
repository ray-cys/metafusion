"""Read-only explanation of Plex-to-TMDb identities and durable bindings."""

import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR, mode_check, report_retention
from helper.identity import metadata_key_for_meta, plex_identity_fingerprint
from helper.plex import get_plex_metadata, load_plex_library_inventory
from helper.provider_mappings import resolve_split_series_mapping
from helper.report_identity import item_report_record, item_report_records
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import inspect_identity_binding
from helper.tmdb import (
    resolve_tmdb_id,
    tmdb_api_request,
    tmdb_external_id_consensus,
    tmdb_identity_consistent,
)


def _normalized_media_type(value):
    return "tv" if str(value or "").lower() in {"show", "shows", "tv"} else "movie"


def _provider_guids(item):
    values = [getattr(item, "guid", None)]
    values.extend(
        getattr(value, "id", value) for value in (getattr(item, "guids", None) or [])
    )
    return list(dict.fromkeys(str(value) for value in values if value))[:20]


async def _resolve_without_binding(config, media_type, meta, session, excluded_ids=None):
    excluded = {str(value) for value in (excluded_ids or set()) if value is not None}
    provider_tmdb_id = meta.get("plex_provider_tmdb_id")
    if provider_tmdb_id and str(provider_tmdb_id) not in excluded:
        return str(provider_tmdb_id), "plex_tmdb_guid"
    if meta.get("imdb_id"):
        resolved = await resolve_tmdb_id(
            config,
            media_type,
            imdb_id=meta.get("imdb_id"),
            session=session,
            excluded_ids=excluded,
            cache=False,
        )
        if resolved:
            return resolved, "imdb_external_id"
    if media_type == "tv" and meta.get("tvdb_id"):
        resolved = await resolve_tmdb_id(
            config,
            media_type,
            tvdb_id=meta.get("tvdb_id"),
            session=session,
            excluded_ids=excluded,
            cache=False,
        )
        if resolved:
            return resolved, "tvdb_external_id"
    resolved = await resolve_tmdb_id(
        config,
        media_type,
        title=meta.get("title"),
        year=meta.get("year"),
        session=session,
        excluded_ids=excluded,
        cache=False,
    )
    return (resolved, "title_year_search") if resolved else (None, "unresolved")


async def _fetch_tmdb_details(config, media_type, tmdb_id, session):
    if not tmdb_id:
        return None
    return await tmdb_api_request(
        config,
        f"{media_type}/{tmdb_id}",
        params={
            "append_to_response": "external_ids",
            "language": config.get("tmdb", {}).get("language", "en-US"),
            "region": config.get("tmdb", {}).get("region", "US"),
        },
        session=session,
        cache=False,
    )


def _metadata_destination(config, meta):
    if mode_check(config, "plex"):
        return {
            "kind": "plex_api",
            "path": f"/library/metadata/{meta.get('ratingKey')}",
            "entry": "Plex item fields",
        }
    media_type = _normalized_media_type(meta.get("library_type"))
    root = Path(config.get("settings", {}).get("path", "."))
    return {
        "kind": "kometa_yaml",
        "path": str(root / "metadata" / f"{media_type}_metadata.yml"),
        "entry": metadata_key_for_meta(meta),
    }


def _destination_record(directory, filename):
    if not directory:
        return {"path": None, "directory_exists": False, "directory_writable": False}
    directory = Path(directory)
    return {
        "path": str(directory / filename),
        "directory_exists": directory.is_dir(),
        "directory_writable": directory.is_dir() and os.access(directory, os.W_OK),
    }


def _artwork_destinations(config, meta):
    media_type = _normalized_media_type(meta.get("library_type"))
    if mode_check(config, "plex"):
        root = meta.get("movie_dir") if media_type == "movie" else meta.get("show_dir")
        destinations = {
            "poster": _destination_record(root, "poster.jpg"),
            "background": _destination_record(root, "fanart.jpg"),
            "seasons": [],
        }
        if media_type == "tv":
            season_dirs = meta.get("season_dirs") or {}
            for season in sorted(
                (int(value) for value in season_dirs if str(value).lstrip("-").isdigit())
            )[:100]:
                directory = season_dirs.get(season, season_dirs.get(str(season)))
                filename = (
                    "season-specials-poster.jpg"
                    if season == 0
                    else f"Season{season:02}.jpg"
                )
                destinations["seasons"].append(
                    {"season": season, **_destination_record(directory, filename)}
                )
        return destinations

    root = Path(config.get("settings", {}).get("path", ".")) / "assets" / media_type
    relative = meta.get("movie_path") if media_type == "movie" else meta.get("show_path")
    directory = root / relative if relative else None
    destinations = {
        "poster": _destination_record(directory, "poster.jpg"),
        "background": _destination_record(directory, "fanart.jpg"),
        "seasons": [],
    }
    if media_type == "tv" and directory is not None:
        seasons = sorted(
            int(value)
            for value in (meta.get("seasons_episodes") or {})
            if str(value).lstrip("-").isdigit()
        )
        for season in seasons[:100]:
            destinations["seasons"].append(
                {
                    "season": season,
                    **_destination_record(directory, f"Season{season:02}.jpg"),
                }
            )
    return destinations


def _tmdb_names(details, media_type):
    details = details or {}
    date_value = (
        details.get("release_date") if media_type == "movie" else details.get("first_air_date")
    )
    return {
        "localized_title": details.get("title") if media_type == "movie" else details.get("name"),
        "original_title": (
            details.get("original_title")
            if media_type == "movie"
            else details.get("original_name")
        ),
        "year": str(date_value)[:4] if date_value else None,
    }


async def diagnose_identity(item, config, session=None, *, identity_counts=None, edition_counts=None):
    """Explain the current identity decision without changing providers or state."""
    meta = await get_plex_metadata(
        item,
        _runtime_config=config.get("runtime", {}),
        _plex_config=config.get("plex", {}),
    )
    media_type = _normalized_media_type(meta.get("library_type"))
    group = (media_type, meta.get("title"), meta.get("year"))
    meta["requires_unique_key"] = (identity_counts or Counter()).get(group, 0) > 1
    if media_type == "movie" and meta.get("edition_title"):
        edition_group = (meta.get("title"), meta.get("year"), meta.get("edition_title"))
        meta["edition_key_collision"] = (edition_counts or Counter()).get(edition_group, 0) > 1

    fingerprint = plex_identity_fingerprint(meta)
    binding = inspect_identity_binding(
        meta.get("server_id") or "unknown",
        meta.get("library_uuid") or meta.get("library_name") or "unknown",
        meta.get("ratingKey"),
        current_fingerprint=fingerprint,
    )
    active = binding.get("active") or {}
    provider_tmdb_id = meta.get("plex_provider_tmdb_id")
    if (
        binding.get("status") == "current"
        and active.get("confidence") == "high"
        and active.get("tmdb_id")
        and str(active.get("tmdb_id")) != str(provider_tmdb_id or "")
    ):
        tmdb_id = str(active["tmdb_id"])
        source = "learned_binding"
    else:
        tmdb_id, source = await _resolve_without_binding(
            config, media_type, meta, session
        )

    details = await _fetch_tmdb_details(config, media_type, tmdb_id, session)
    split_mapping = resolve_split_series_mapping(
        config,
        tmdb_id=tmdb_id,
        tvdb_id=meta.get("tvdb_id"),
        imdb_id=meta.get("imdb_id"),
    )
    consensus_ok = False
    consensus_trusted = False
    consensus_reason = "TMDb details were unavailable"
    if isinstance(details, dict) and details:
        consensus_ok, consensus_trusted, consensus_reason = tmdb_external_id_consensus(
            media_type,
            details,
            imdb_id=meta.get("imdb_id"),
            tvdb_id=meta.get("tvdb_id"),
            allow_tvdb_mismatch=bool(split_mapping),
        )
    if not details or not consensus_ok:
        replacement_id, replacement_source = await _resolve_without_binding(
            config,
            media_type,
            meta,
            session,
            excluded_ids={tmdb_id} if tmdb_id else set(),
        )
        replacement = await _fetch_tmdb_details(
            config, media_type, replacement_id, session
        )
        if replacement:
            tmdb_id = replacement_id
            details = replacement
            source = f"stale_identity_recovery_via_{replacement_source}"
            split_mapping = resolve_split_series_mapping(
                config,
                tmdb_id=tmdb_id,
                tvdb_id=meta.get("tvdb_id"),
                imdb_id=meta.get("imdb_id"),
            )
            consensus_ok, consensus_trusted, consensus_reason = tmdb_external_id_consensus(
                media_type,
                details,
                imdb_id=meta.get("imdb_id"),
                tvdb_id=meta.get("tvdb_id"),
                allow_tvdb_mismatch=bool(split_mapping),
            )

    if details and consensus_ok:
        identity_ok, identity_reason = tmdb_identity_consistent(
            media_type,
            meta.get("title"),
            meta.get("year"),
            details,
            trusted_external_id=consensus_trusted or bool(split_mapping),
        )
    else:
        identity_ok = False
        identity_reason = consensus_reason
    if not tmdb_id or not details:
        confidence = "unresolved"
    elif not consensus_ok or not identity_ok:
        confidence = "rejected"
    elif (
        source == "learned_binding"
        or consensus_trusted
        or bool(split_mapping)
        or (provider_tmdb_id and str(provider_tmdb_id) == str(tmdb_id))
    ):
        confidence = "high"
    else:
        confidence = "medium"

    return item_report_record(
        {
            "status": "accepted" if confidence in {"high", "medium"} else confidence,
            "library": meta.get("library_name") or "Unknown library",
            "rating_key": str(meta.get("ratingKey") or "unknown"),
            "media_type": media_type,
            "plex": {
                "localized_title": meta.get("title"),
                "original_title": getattr(item, "originalTitle", None),
                "year": meta.get("year"),
                "guids": _provider_guids(item),
                "tmdb_id": provider_tmdb_id,
                "imdb_id": meta.get("imdb_id"),
                "tvdb_id": meta.get("tvdb_id"),
                "fingerprint": fingerprint,
                "edition": meta.get("edition_title"),
            },
            "selection": {
                "tmdb_id": tmdb_id,
                "source": source,
                "confidence": confidence,
                "external_id_reason": consensus_reason,
                "identity_reason": identity_reason,
            },
            "tmdb": _tmdb_names(details, media_type),
            "binding": binding,
            "metadata_destination": _metadata_destination(config, meta),
            "artwork_destinations": _artwork_destinations(config, meta),
        },
        meta,
        tmdb_id=tmdb_id,
        identity_source=source,
    )


def _display(value):
    return "none" if value in (None, "", []) else str(value)


def write_identity_inspection_report(records, *, base_dir=None, retention=10):
    records = item_report_records(records)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"identity-inspection-{timestamp}.txt"
    current = build_info()
    lines = [
        "MetaFusion read-only Plex/TMDb identity and binding inspection",
        f"Generated: {generated.isoformat()}",
        f"Version: {current['version']}",
        f"Commit: {current['commit']}",
        f"Items: {len(records)}",
        "",
        "No binding, cache, metadata, artwork, ownership, or provider value was changed.",
        "Binding history starts when the identity-history extension is installed; earlier events cannot be reconstructed.",
        "",
    ]
    for record in records:
        plex = record.get("plex") or {}
        selection = record.get("selection") or {}
        tmdb = record.get("tmdb") or {}
        binding = record.get("binding") or {}
        active = binding.get("active") or {}
        lines.extend(
            (
                f"## {_display(plex.get('localized_title'))} ({_display(plex.get('year'))})",
                f"Status: {_display(record.get('status'))}",
                f"Library: {_display(record.get('library'))}",
                f"Plex rating key: {_display(record.get('rating_key'))}",
                f"Media type: {_display(record.get('media_type'))}",
                f"Plex localized title: {_display(plex.get('localized_title'))}",
                f"Plex original title: {_display(plex.get('original_title'))}",
                f"Plex year: {_display(plex.get('year'))}",
                f"Edition/version: {_display(plex.get('edition'))}",
                f"Plex GUIDs: {', '.join(plex.get('guids') or []) or 'none'}",
                f"Plex TMDb ID: {_display(plex.get('tmdb_id'))}",
                f"Plex IMDb ID: {_display(plex.get('imdb_id'))}",
                f"Plex TVDB ID: {_display(plex.get('tvdb_id'))}",
                f"Current provider fingerprint: {_display(plex.get('fingerprint'))}",
                f"Selected TMDb ID: {_display(selection.get('tmdb_id'))}",
                f"Current resolution source: {_display(selection.get('source'))}",
                f"Match confidence: {_display(selection.get('confidence'))}",
                f"External-ID result: {_display(selection.get('external_id_reason'))}",
                f"Title/year result: {_display(selection.get('identity_reason'))}",
                f"TMDb localized title: {_display(tmdb.get('localized_title'))}",
                f"TMDb original title: {_display(tmdb.get('original_title'))}",
                f"TMDb year: {_display(tmdb.get('year'))}",
                f"Active binding status: {_display(binding.get('status'))}",
                f"Active binding TMDb ID: {_display(active.get('tmdb_id'))}",
                f"Binding established by: {_display(active.get('source'))}",
                f"Binding confidence: {_display(active.get('confidence'))}",
                f"Binding match reason: {_display(active.get('match_reason'))}",
                f"Binding validated: {_display(active.get('validated_at'))}",
                f"Binding last used: {_display(active.get('last_used_at'))}",
            )
        )
        if binding.get("status") == "stale":
            lines.append(
                "Binding invalidation: current Plex provider GUID fingerprint differs; "
                "ordinary processing will not reuse the stored binding."
            )
        metadata = record.get("metadata_destination") or {}
        lines.extend(
            (
                f"Metadata destination type: {_display(metadata.get('kind'))}",
                f"Metadata destination: {_display(metadata.get('path'))}",
                f"Metadata entry: {_display(metadata.get('entry'))}",
                "Artwork destinations:",
            )
        )
        artwork = record.get("artwork_destinations") or {}
        for asset_type in ("poster", "background"):
            destination = artwork.get(asset_type) or {}
            lines.append(
                f"- {asset_type}: {_display(destination.get('path'))} "
                f"(directory exists={bool(destination.get('directory_exists'))}, "
                f"writable={bool(destination.get('directory_writable'))})"
            )
        for destination in artwork.get("seasons") or []:
            lines.append(
                f"- season {destination.get('season')}: "
                f"{_display(destination.get('path'))} "
                f"(directory exists={bool(destination.get('directory_exists'))}, "
                f"writable={bool(destination.get('directory_writable'))})"
            )
        history = binding.get("history") or []
        lines.append("Binding history (newest first):")
        if not history:
            lines.append(
                "- none recorded; the active binding may predate identity history"
            )
        for event in history:
            lines.append(
                f"- {event.get('occurred_at')} | {event.get('event_type')} | "
                f"{event.get('reason_code')} | old TMDb "
                f"{_display(event.get('previous_tmdb_id'))} -> new TMDb "
                f"{_display(event.get('tmdb_id'))} | "
                f"source={_display(event.get('source'))} | "
                f"reason={_display(event.get('reason'))}"
            )
        lines.append("")
    write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type="identity_inspection",
        data={"items": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "identity-inspection", retention)
    return path


async def run_identity_inspection(
    sections, config, rating_keys, session=None, *, base_dir=None
):
    requested = {str(value) for value in rating_keys or [] if str(value).strip()}
    identity_counts = Counter()
    edition_counts = Counter()
    section_records = {}
    for section in sections:
        records = await load_plex_library_inventory(
            section, config.get("runtime", {}), records_only=True
        )
        section_records[section.title] = records
        for record in records:
            media_type = _normalized_media_type(record.get("media_type"))
            identity_counts[(media_type, record.get("title"), record.get("year"))] += 1
            if media_type == "movie":
                edition_counts[
                    (record.get("title"), record.get("year"), record.get("edition"))
                ] += 1

    found = set()
    results = []
    for section in sections:
        available = {
            str(record.get("rating_key"))
            for record in section_records.get(section.title, [])
            if record.get("rating_key") is not None
        }
        if not requested.intersection(available):
            continue
        items = await load_plex_library_inventory(section, config.get("runtime", {}))
        for item in items:
            rating_key = str(getattr(item, "ratingKey", ""))
            if rating_key not in requested:
                continue
            found.add(rating_key)
            results.append(
                await diagnose_identity(
                    item,
                    config,
                    session=session,
                    identity_counts=identity_counts,
                    edition_counts=edition_counts,
                )
            )
    for rating_key in sorted(requested - found):
        results.append(
            {
                "status": "not_found",
                "library": "not found",
                "rating_key": rating_key,
                "media_type": "unknown",
                "plex": {"localized_title": "Unknown title", "year": None},
                "selection": {},
                "tmdb": {},
                "binding": {"status": "missing", "history": []},
                "metadata_destination": {},
                "artwork_destinations": {},
            }
        )
    report = write_identity_inspection_report(
        results,
        base_dir=base_dir,
        retention=report_retention(config),
    )
    return results, report
