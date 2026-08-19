"""Unified read-only explanation for one or more Plex items."""

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
from helper.config import (
    BASE_CONFIG_DIR,
    config_for_library,
    get_feature_flags,
    get_image_upgrade_days,
    mode_check,
    report_retention,
)
from helper.identity_diagnostics import diagnose_identity
from helper.incremental import config_fingerprint, library_full_scan_decisions, plan_items
from helper.mapping_diagnostics import diagnose_mapping
from helper.plex import get_plex_metadata, load_plex_library_inventory
from helper.report_identity import item_report_record, item_report_records
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import STATE_DATABASE, MediaStateStore, load_item_retries


def _normalized_media_type(value):
    return "tv" if str(value or "").lower() in {"show", "shows", "tv"} else "movie"


def _policy_record(config, media_type):
    flags = get_feature_flags(config)
    metadata = config.get("plex_metadata", {})
    return {
        "mode": config.get("settings", {}).get("mode", "kometa"),
        "metadata": {
            "enabled": bool(flags.get("metadata_basic") or flags.get("metadata_enhanced")),
            "target": "Plex API" if mode_check(config, "plex") else "Kometa YAML",
            "basic": bool(flags.get("metadata_basic")),
            "enhanced": bool(flags.get("metadata_enhanced")),
            "plex_api_enabled": bool(flags.get("plex_metadata")),
            "plex_policy": metadata.get("policy") if mode_check(config, "plex") else None,
            "plex_fields": metadata.get("fields") or [],
            "lock_writes": bool(metadata.get("lock_writes", False)),
            "lock_merged_tags": bool(metadata.get("lock_merged_tags", False)),
            "kometa_tag_policy": (
                config.get("kometa", {}).get("tag_policy")
                if mode_check(config, "kometa")
                else None
            ),
        },
        "artwork": {
            "update_policy": config.get("assets", {}).get("update_policy", "managed"),
            "poster": bool(flags.get("poster")),
            "background": bool(flags.get("background")),
            "season": bool(flags.get("season") and media_type == "tv"),
            "item_recheck_days": get_image_upgrade_days(
                config, "series" if media_type == "tv" else "movie"
            ),
            "season_recheck_days": (
                get_image_upgrade_days(config, "season") if media_type == "tv" else None
            ),
        },
    }


def _selection_record(item, meta, config):
    server_id = str(meta.get("server_id") or "unknown")
    library_uuid = str(meta.get("library_uuid") or meta.get("library_name") or "unknown")
    rating_key = str(meta.get("ratingKey") or getattr(item, "ratingKey", ""))
    fingerprint = config_fingerprint(config)
    scope = {
        "server_id": server_id,
        "library_uuid": library_uuid,
        "library_name": meta.get("library_name"),
        "config_fingerprint": fingerprint,
        "item_count": None,
    }
    full_scan = library_full_scan_decisions(
        config,
        scopes=[scope],
        path=STATE_DATABASE,
    ).get((server_id, library_uuid), True)
    retries = load_item_retries(
        server_id=server_id,
        library_uuids=[library_uuid],
        rating_keys=[rating_key],
        path=STATE_DATABASE,
    )
    store = MediaStateStore(path=STATE_DATABASE, writable=False)
    try:
        cached_records = store.entries_for_scope(
            server_id,
            library_uuid,
            rating_keys=[rating_key],
        )
        cached = next(iter(cached_records.values()), None)
        planned = plan_items(
            [item],
            store,
            fingerprint,
            full_scan=bool(full_scan),
            config=config,
            feature_flags=get_feature_flags(config),
            server_id=server_id,
            library_uuid=library_uuid,
            retry_rating_keys=[rating_key] if retries else [],
        )
    finally:
        store.close()
    selected = planned[0] if planned else None
    return {
        "normal_schedule_action": "process" if selected else "skip",
        "full_scan_due": bool(full_scan),
        "work": sorted(selected.reasons) if selected else [],
        "causes": sorted(selected.selection_causes) if selected else [],
        "cache_record_present": isinstance(cached, dict),
        "cached_plex_updated_at": (
            cached.get("plex_updated_at") if isinstance(cached, dict) else None
        ),
        "cached_config_matches": (
            cached.get("config_fingerprint") == fingerprint
            if isinstance(cached, dict)
            else False
        ),
        "cached_artwork_providers": (
            {
                "poster": cached.get("poster_provider"),
                "background": cached.get("background_provider"),
                "seasons": {
                    str(number): season.get("season_provider")
                    for number, season in (cached.get("seasons") or {}).items()
                    if isinstance(season, dict) and season.get("season_provider")
                },
            }
            if isinstance(cached, dict)
            else {}
        ),
        "retry_status": retries[0].get("status") if retries else None,
        "retry_failure_class": retries[0].get("failure_class") if retries else None,
    }


async def explain_item(
    item,
    config,
    session=None,
    *,
    identity_counts=None,
    edition_counts=None,
):
    meta = await get_plex_metadata(
        item,
        _runtime_config=config.get("runtime", {}),
        _plex_config=config.get("plex", {}),
    )
    effective = config_for_library(config, meta.get("library_name") or "Unknown library")
    identity = await diagnose_identity(
        item,
        effective,
        session=session,
        identity_counts=identity_counts,
        edition_counts=edition_counts,
    )
    media_type = _normalized_media_type(meta.get("library_type"))
    mapping = (
        await diagnose_mapping(item, effective, session=session)
        if media_type == "tv"
        else {
            "status": "not_applicable",
            "explanation": "Episode mapping applies only to TV shows.",
        }
    )
    return item_report_record(
        {
            "status": identity.get("status"),
            "library": identity.get("library"),
            "rating_key": identity.get("rating_key"),
            "media_type": media_type,
            "identity": identity,
            "selection": _selection_record(item, meta, effective),
            "policies": _policy_record(effective, media_type),
            "episode_mapping": mapping,
        },
        identity,
    )


def _display(value):
    if value in (None, "", []):
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


def write_item_explanation_report(records, *, base_dir=None, retention=10):
    records = item_report_records(records)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    path = report_dir / f"item-explanation-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    current = build_info()
    lines = [
        "MetaFusion Unified read-only item explanation",
        f"Generated: {generated.isoformat()}",
        f"Version: {current['version']}",
        f"Commit: {current['commit']}",
        f"Items: {len(records)}",
        "",
        "This command reads Plex, TMDb, and SQLite state. It changes no provider value, cache, binding, metadata, artwork, ownership, incremental marker, or cleanup state.",
        "",
    ]
    for record in records:
        identity = record.get("identity") or {}
        plex = identity.get("plex") or {}
        chosen = identity.get("selection") or {}
        binding = identity.get("binding") or {}
        scheduled = record.get("selection") or {}
        policies = record.get("policies") or {}
        metadata = policies.get("metadata") or {}
        artwork_policy = policies.get("artwork") or {}
        mapping = record.get("episode_mapping") or {}
        lines.extend(
            (
                f"## {_display(plex.get('localized_title'))} ({_display(plex.get('year'))})",
                f"Library: {_display(record.get('library'))}",
                f"Plex rating key: {_display(record.get('rating_key'))}",
                f"Media type: {_display(record.get('media_type'))}",
                f"Overall identity status: {_display(record.get('status'))}",
                f"Selected TMDb ID: {_display(chosen.get('tmdb_id'))}",
                f"Identity source: {_display(chosen.get('source'))}",
                f"Identity confidence: {_display(chosen.get('confidence'))}",
                f"External-ID decision: {_display(chosen.get('external_id_reason'))}",
                f"Title/year decision: {_display(chosen.get('identity_reason'))}",
                f"Binding status: {_display(binding.get('status'))}",
                f"Binding history events: {len(binding.get('history') or [])}",
                "",
                "Normal scheduled-run decision:",
                f"- action: {_display(scheduled.get('normal_schedule_action'))}",
                f"- full scan due: {_display(scheduled.get('full_scan_due'))}",
                f"- causes: {_display(scheduled.get('causes'))}",
                f"- selected work: {_display(scheduled.get('work'))}",
                f"- durable item state present: {_display(scheduled.get('cache_record_present'))}",
                f"- cached configuration matches: {_display(scheduled.get('cached_config_matches'))}",
                f"- cached artwork providers: {_display(scheduled.get('cached_artwork_providers'))}",
                f"- retry status/class: {_display(scheduled.get('retry_status'))}/{_display(scheduled.get('retry_failure_class'))}",
                "",
                "Metadata policy:",
                f"- mode/target: {_display(policies.get('mode'))}/{_display(metadata.get('target'))}",
                f"- enabled: {_display(metadata.get('enabled'))}",
                f"- basic/enhanced: {_display(metadata.get('basic'))}/{_display(metadata.get('enhanced'))}",
                f"- Plex API policy: {_display(metadata.get('plex_policy'))}",
                f"- Plex locks writes/merged tags: {_display(metadata.get('lock_writes'))}/{_display(metadata.get('lock_merged_tags'))}",
                f"- Kometa tag policy: {_display(metadata.get('kometa_tag_policy'))}",
                "",
                "Artwork policy:",
                f"- update policy: {_display(artwork_policy.get('update_policy'))}",
                f"- poster/background/season: {_display(artwork_policy.get('poster'))}/{_display(artwork_policy.get('background'))}/{_display(artwork_policy.get('season'))}",
                f"- item/season recheck days: {_display(artwork_policy.get('item_recheck_days'))}/{_display(artwork_policy.get('season_recheck_days'))}",
                "",
                f"Episode mapping status: {_display(mapping.get('status'))}",
                f"Episode mapping explanation: {_display(mapping.get('explanation'))}",
            )
        )
        metadata_destination = identity.get("metadata_destination") or {}
        lines.append(
            f"Metadata destination: {_display(metadata_destination.get('path'))} "
            f"[{_display(metadata_destination.get('entry'))}]"
        )
        artwork = identity.get("artwork_destinations") or {}
        lines.append("Artwork destinations:")
        for asset_type in ("poster", "background"):
            destination = artwork.get(asset_type) or {}
            lines.append(f"- {asset_type}: {_display(destination.get('path'))}")
        for destination in artwork.get("seasons") or []:
            lines.append(
                f"- season {destination.get('season')}: {_display(destination.get('path'))}"
            )
        lines.append("")
    write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type="item_explanation",
        data={"items": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "item-explanation", retention)
    return path


async def run_item_explanation(
    sections,
    config,
    rating_keys,
    session=None,
    *,
    base_dir=None,
    write_report=True,
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
                await explain_item(
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
                "identity": {"plex": {"localized_title": "Unknown title"}},
                "selection": {},
                "policies": {},
                "episode_mapping": {},
            }
        )
    report = None
    if write_report:
        report = write_item_explanation_report(
            results,
            base_dir=base_dir,
            retention=report_retention(config),
        )
    return results, report
