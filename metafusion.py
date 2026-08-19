import argparse
import asyncio
import json
import logging
import os
import platform
import re
import signal
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import schedule

from helper.asset_registry import AssetDestinationRegistry
from helper.build_info import build_info
from helper.cache import begin_cache_session, flush_cache, load_cache
from helper.compatibility import evaluate_compatibility, resolve_compatibility_profile
from helper.concurrency import (
    begin_adaptive_concurrency,
    concurrency_ceiling,
    finish_adaptive_concurrency,
)
from helper.config import (
    BASE_CONFIG_DIR,
    ConfigError,
    config_for_library,
    config_source_report,
    get_disabled_features,
    get_feature_flags,
    load_config_file,
    mode_check,
    report_retention,
    validate_config,
)
from helper.database_maintenance import (
    format_maintenance_results,
    maintain_databases,
)
from helper.diagnostics import (
    write_adoption_audit_report,
    write_artwork_gap_report,
    write_asset_audit_report,
    write_change_plan_report,
    write_compatibility_report,
    write_destination_history_report,
    write_library_asset_audit_report,
    write_metadata_audit_report,
    write_release_qualification_report,
    write_support_report,
    write_unresolved_work_report,
)
from helper.fanart import (
    begin_fanart_cache,
    fanart_response_cache,
    flush_fanart_cache,
)
from helper.identity_diagnostics import run_identity_inspection
from helper.incremental import (
    config_fingerprint,
    library_full_scan_decisions,
    mark_library_scan_complete,
    mark_library_scan_started,
)
from helper.item_explanation import run_item_explanation
from helper.logging import (
    check_sys_requirements,
    format_fields,
    get_meta_banner,
    get_setup_logging,
    log_cleanup_event,
    log_final_summary,
    log_main_event,
    log_section,
    redact_secrets,
)
from helper.mapping_diagnostics import run_mapping_diagnosis
from helper.performance import (
    PerformanceTracker,
    begin_performance_tracking,
    log_performance_summary,
    reset_performance_tracking,
    tracker_for,
)
from helper.plex import (
    _plex_cache,
    collect_plex_path_samples,
    connect_plex_library,
    connect_plex_server,
    get_plex_metadata,
    load_plex_library_inventory,
)
from helper.plex_metadata import (
    begin_plex_metadata_run,
    finish_plex_metadata_run,
    restore_plex_metadata,
)
from helper.plex_paths import advise_path_mappings
from helper.provider_credentials import fanart_project_api_key
from helper.provider_replay import write_sanitized_replay_capture
from helper.runtime import (
    JobAlreadyRunningError,
    JobRunLock,
    RuntimeStatus,
    validate_preflight_paths,
    validate_runtime_paths,
)
from helper.state_db import (
    SCHEMA_VERSION as STATE_SCHEMA_VERSION,
)
from helper.state_db import (
    STATE_DATABASE,
    StateDatabaseError,
    load_item_retries,
    load_unresolved_work,
    maintain_state_database,
    missing_library_inventory,
    recent_job_runs,
    reconcile_library_inventory,
    reconcile_unresolved_work,
    retry_queue_summary,
)
from helper.tmdb import (
    begin_tmdb_cache,
    flush_tmdb_cache,
    tmdb_api_request,
    tmdb_response_cache,
)
from modules.cleanup import CleanupError, CleanupResult, cleanup_title_orphans
from modules.processing import plex_metadata_dict, process_library

shutdown_requested = threading.Event()
shutdown_complete = threading.Event()
_active_loop = None
_active_task = None
_shutdown_timeout = 15.0


def cli_version():
    current = build_info()
    return (
        f"MetaFusion {current['version']} ({current['commit']}); "
        f"Python {platform.python_version()}; architecture {platform.machine()}; "
        f"state schema {STATE_SCHEMA_VERSION}; "
        f"TMDb cache schema {tmdb_response_cache.SCHEMA_VERSION}"
    )


def parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="MetaFusion CLI Command Overrides")
    parser.add_argument("--version", action="version", version=cli_version())
    parser.add_argument("--metafusion_run", action="store_true", help="Run MetaFusion job")
    parser.add_argument("--schedule", action="store_true", help="Enable schedule")
    parser.add_argument("--run_times", type=str, help="Comma-separated run times (e.g. 06:00,18:30)")
    parser.add_argument("--dry_run", action="store_true", help="Dry run mode")
    parser.add_argument("--mode", type=str, choices=["kometa", "plex"], help="Run mode")
    parser.add_argument("--run_basic", action="store_true", help="Run basic metadata extraction")
    parser.add_argument("--run_enhanced", action="store_true", help="Run enhanced metadata extraction")
    parser.add_argument("--run_poster", action="store_true", help="Run poster asset download")
    parser.add_argument("--run_season", action="store_true", help="Run season asset download")
    parser.add_argument("--run_background", action="store_true", help="Run background asset download")
    parser.add_argument(
        "--asset-only",
        action="store_true",
        help="Process enabled artwork without regenerating metadata",
    )
    parser.add_argument(
        "--explain-selection",
        action="store_true",
        help="Explain incremental selection without processing or writing",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the current runtime status JSON and exit",
    )
    parser.add_argument(
        "--doctor",
        "--check-config",
        dest="doctor",
        action="store_true",
        help="Validate configuration and report value sources without running",
    )
    parser.add_argument(
        "--support-report",
        action="store_true",
        help="Write a redacted diagnostic report under /config/reports and exit",
    )
    parser.add_argument(
        "--problems",
        action="store_true",
        help="Print the persistent unresolved-work ledger as JSON and exit",
    )
    parser.add_argument(
        "--capture-replay",
        action="store_true",
        help="Capture selected items as a sanitized JSON replay for support",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check Plex, TMDb, libraries, mappings, and storage without processing",
    )
    parser.add_argument(
        "--release-check",
        action="store_true",
        help="Run read-only release qualification and write a redacted report",
    )
    parser.add_argument(
        "--asset-audit",
        action="store_true",
        help="Evaluate every enabled artwork destination and write a report only",
    )
    parser.add_argument(
        "--metadata-audit",
        action="store_true",
        help="Compare TMDb metadata with the selected output without writing it",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Write one read-only metadata, artwork, and cleanup change plan",
    )
    parser.add_argument(
        "--library-audit",
        action="store_true",
        help="Audit Plex libraries and enabled artwork without changing either mode",
    )
    parser.add_argument(
        "--mapping-diagnose",
        action="store_true",
        help="Explain one or more Plex TV episode mappings without changing them",
    )
    parser.add_argument(
        "--identity-inspect",
        action="store_true",
        help="Explain Plex-to-TMDb identity and binding history without changing it",
    )
    parser.add_argument(
        "--explain-item",
        action="store_true",
        help="Unify identity, schedule, policy, mapping, and destination diagnostics",
    )
    parser.add_argument(
        "--library",
        action="append",
        help="Process only this Plex library; repeat or use comma-separated names",
    )
    parser.add_argument(
        "--rating-key",
        action="append",
        help="Process only this Plex rating key; repeat or use comma-separated keys",
    )
    parser.add_argument(
        "--tmdb-id",
        action="append",
        help="Process only items exposing this TMDb GUID; repeat or use commas",
    )
    parser.add_argument(
        "--media-type",
        action="append",
        choices=["movie", "show"],
        help="Process only movie or show libraries; repeat to select both",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Generate metadata without artwork or cleanup",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Bypass incremental skipping and reconcile the selected libraries",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry queued failed items, optionally narrowed by library/rating key",
    )
    parser.add_argument(
        "--retry-status",
        choices=["all", "pending", "parked", "running"],
        default="all",
        help="Select which queued failure state --retry-failed processes",
    )
    parser.add_argument(
        "--sqlite-maintenance",
        choices=["check", "optimize", "checkpoint", "vacuum", "backup"],
        help="Inspect or explicitly maintain MetaFusion SQLite databases",
    )
    parser.add_argument(
        "--sqlite-target",
        choices=["all", "state", "tmdb", "fanart"],
        default="all",
        help="Limit SQLite maintenance to state, TMDb cache, or Fanart.tv cache",
    )
    parser.add_argument(
        "--compatibility-check",
        action="store_true",
        help="Validate the configured output mode against its compatibility profile",
    )
    parser.add_argument(
        "--compatibility-profile",
        choices=["auto", "kometa-2.4", "plex-api-v1"],
        help="Override the compatibility profile for this command or run",
    )
    parser.add_argument(
        "--plex-metadata-restore",
        action="store_true",
        help="Restore MetaFusion-owned Plex fields selected by --rating-key",
    )
    parser.add_argument(
        "--plex-metadata-unlock",
        action="store_true",
        help="Remove only MetaFusion-created Plex locks selected by --rating-key",
    )
    return parser.parse_args(argv)


def override_config_with_cli(config, args):
    if args.metafusion_run:
        config["metafusion_run"] = True
    if args.schedule:
        config["settings"]["schedule"] = True
    if args.run_times is not None:
        config["settings"]["run_times"] = [
            value.strip() for value in args.run_times.split(",") if value.strip()
        ]
    if args.dry_run:
        config["settings"]["dry_run"] = True
    if args.mode is not None:
        config["settings"]["mode"] = args.mode
    if args.run_basic:
        config["metadata"]["run_basic"] = True
    if args.run_enhanced:
        config["metadata"]["run_enhanced"] = True
    if args.run_poster:
        config["assets"]["run_poster"] = True
    if args.run_season:
        config["assets"]["run_season"] = True
    if args.run_background:
        config["assets"]["run_background"] = True
    libraries = []
    for value in args.library or []:
        libraries.extend(part.strip() for part in value.split(",") if part.strip())
    if libraries:
        config["plex_libraries"] = libraries
    rating_keys = []
    for value in args.rating_key or []:
        rating_keys.extend(part.strip() for part in value.split(",") if part.strip())
    tmdb_ids = []
    for value in args.tmdb_id or []:
        tmdb_ids.extend(part.strip() for part in value.split(",") if part.strip())
    media_types = sorted(set(args.media_type or []))
    if args.metadata_only:
        config["assets"].update(
            {"run_poster": False, "run_season": False, "run_background": False}
        )
        config["cleanup"]["run_cleanup"] = False
    if args.asset_only:
        config["metadata"].update({"run_basic": False, "run_enhanced": False})
        config["cleanup"]["run_cleanup"] = False
    if args.asset_audit:
        config["metafusion_run"] = True
        config["settings"]["dry_run"] = True
        config["metadata"].update({"run_basic": False, "run_enhanced": False})
        config["cleanup"]["run_cleanup"] = False
    if args.metadata_audit:
        config["metafusion_run"] = True
        config["settings"]["dry_run"] = True
        config["metadata"]["run_basic"] = True
        config["assets"].update(
            {"run_poster": False, "run_season": False, "run_background": False}
        )
        config["cleanup"]["run_cleanup"] = False
        if mode_check(config, "plex"):
            config["plex_metadata"]["enabled"] = True
    if args.plan:
        config["metafusion_run"] = True
        config["settings"]["dry_run"] = True
        config["metadata"]["run_basic"] = True
        if mode_check(config, "plex"):
            config["plex_metadata"]["enabled"] = True
    if args.library_audit:
        config["metafusion_run"] = True
        config["settings"]["dry_run"] = True
        config["metadata"].update({"run_basic": False, "run_enhanced": False})
        config["cleanup"]["run_cleanup"] = False
    if args.mapping_diagnose:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.identity_inspect:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.explain_item:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.capture_replay:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.preflight:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.explain_selection:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.compatibility_check:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if args.compatibility_profile:
        config.setdefault("compatibility", {})["profile"] = args.compatibility_profile
    if args.retry_failed:
        config["metafusion_run"] = True
        config["cleanup"]["run_cleanup"] = False
    if libraries or rating_keys or tmdb_ids or media_types or args.retry_failed:
        config["cleanup"]["run_cleanup"] = False
    maintenance_action = None
    if args.plex_metadata_restore:
        maintenance_action = "restore"
    elif args.plex_metadata_unlock:
        maintenance_action = "unlock"
    if maintenance_action:
        config["metafusion_run"] = True
        config["plex_metadata"]["enabled"] = True
        config["cleanup"]["run_cleanup"] = False
    execution = {
        "rating_keys": rating_keys,
        "targeted": bool(
            libraries or rating_keys or tmdb_ids or media_types or args.retry_failed
        ),
        "full_scan": bool(
            args.full_scan
            or args.asset_audit
            or args.metadata_audit
            or args.plan
            or args.library_audit
            or tmdb_ids
            or media_types
        ),
        "metadata_only": bool(args.metadata_only),
        "asset_only": bool(args.asset_only or args.asset_audit or args.library_audit),
        "asset_audit": bool(args.asset_audit or args.plan or args.library_audit),
        "metadata_audit": bool(args.metadata_audit or args.plan),
        "explain_selection": bool(args.explain_selection),
    }
    if tmdb_ids:
        execution["tmdb_ids"] = tmdb_ids
    if media_types:
        execution["media_types"] = media_types
    if args.plan:
        execution["plan"] = True
    if args.library_audit:
        execution["library_audit"] = True
    if args.mapping_diagnose:
        execution["mapping_diagnose"] = True
    if args.identity_inspect:
        execution["identity_inspect"] = True
    if args.explain_item:
        execution["explain_item"] = True
    if args.capture_replay:
        execution["capture_replay"] = True
    if args.retry_failed:
        execution["retry_failed"] = True
        execution["retry_status"] = args.retry_status
    if maintenance_action:
        execution["plex_metadata_maintenance"] = maintenance_action
    config["_execution"] = execution


async def preflight_connectors(config, session, require_tmdb=True):
    plex_task = asyncio.create_task(asyncio.to_thread(connect_plex_server, config))
    if not require_tmdb:
        return await plex_task
    tmdb_task = asyncio.create_task(
        tmdb_api_request(
            config,
            "configuration",
            cache=False,
            session=session,
        )
    )
    plex, tmdb_result = await asyncio.gather(
        plex_task,
        tmdb_task,
        return_exceptions=True,
    )
    if isinstance(plex, Exception):
        raise RuntimeError(  # noqa: TRY004 -- connector failure, not bad input type
            "Plex connector preflight failed"
        ) from plex
    if isinstance(tmdb_result, Exception) or not tmdb_result:
        if isinstance(tmdb_result, Exception):
            raise RuntimeError("TMDb connector preflight failed") from tmdb_result
        raise RuntimeError("TMDb connector preflight returned no configuration")
    return plex


async def connector_preflight(config):
    runtime = config.get("runtime", {})
    maximum = concurrency_ceiling(config, "network")
    timeout = aiohttp.ClientTimeout(
        total=max(1.0, float(runtime.get("request_timeout", 30.0))),
        connect=max(1.0, float(runtime.get("connect_timeout", 10.0))),
    )
    connector = aiohttp.TCPConnector(
        limit=maximum,
        limit_per_host=maximum,
    )
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        plex = await preflight_connectors(config, session)
    sections, selected, available = await asyncio.to_thread(
        connect_plex_library,
        config,
        plex=plex,
    )
    available_names = {entry["title"] for entry in available}
    missing = sorted(set(selected) - available_names)
    if missing:
        raise RuntimeError(
            "Configured Plex libraries were not found: " + ", ".join(missing)
        )
    path_samples = await asyncio.to_thread(collect_plex_path_samples, sections)
    path_advice = await asyncio.to_thread(
        advise_path_mappings,
        path_samples,
        config.get("plex", {}).get("path_mappings", []),
    )
    return {
        "plex_version": str(getattr(plex, "version", "unknown")),
        "libraries": [section.title for section in sections],
        "available_count": len(available),
        "library_discovery": (
            "auto" if config.get("_library_discovery_auto", False) else "explicit"
        ),
        "path_advice": path_advice,
    }


async def mapping_diagnosis_connectors(config, rating_keys):
    runtime = config.get("runtime", {})
    maximum = concurrency_ceiling(config, "network")
    timeout = aiohttp.ClientTimeout(
        total=max(1.0, float(runtime.get("request_timeout", 30.0))),
        connect=max(1.0, float(runtime.get("connect_timeout", 10.0))),
    )
    connector = aiohttp.TCPConnector(limit=maximum, limit_per_host=maximum)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        plex = await preflight_connectors(config, session)
        sections, _selected, _available = await asyncio.to_thread(
            connect_plex_library,
            config,
            plex=plex,
        )
        return await run_mapping_diagnosis(
            sections,
            config,
            rating_keys,
            session=session,
        )


async def identity_inspection_connectors(config, rating_keys):
    runtime = config.get("runtime", {})
    maximum = concurrency_ceiling(config, "network")
    timeout = aiohttp.ClientTimeout(
        total=max(1.0, float(runtime.get("request_timeout", 30.0))),
        connect=max(1.0, float(runtime.get("connect_timeout", 10.0))),
    )
    connector = aiohttp.TCPConnector(limit=maximum, limit_per_host=maximum)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        plex = await preflight_connectors(config, session)
        sections, _selected, _available = await asyncio.to_thread(
            connect_plex_library,
            config,
            plex=plex,
        )
        return await run_identity_inspection(
            sections,
            config,
            rating_keys,
            session=session,
        )


async def item_explanation_connectors(config, rating_keys, *, write_report=True):
    runtime = config.get("runtime", {})
    maximum = concurrency_ceiling(config, "network")
    timeout = aiohttp.ClientTimeout(
        total=max(1.0, float(runtime.get("request_timeout", 30.0))),
        connect=max(1.0, float(runtime.get("connect_timeout", 10.0))),
    )
    connector = aiohttp.TCPConnector(limit=maximum, limit_per_host=maximum)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        plex = await preflight_connectors(config, session)
        sections, _selected, _available = await asyncio.to_thread(
            connect_plex_library,
            config,
            plex=plex,
        )
        return await run_item_explanation(
            sections,
            config,
            rating_keys,
            session=session,
            write_report=write_report,
        )


def normalize_library_type(value):
    library_type = (value or "").lower()
    if library_type == "movies":
        return "movie"
    if library_type in {"show", "shows"}:
        return "tv"
    return library_type


def cli_media_type(value):
    normalized = normalize_library_type(value)
    return "show" if normalized == "tv" else normalized


def plex_item_tmdb_ids(item):
    """Extract explicit TMDb GUIDs already exposed by Plex without searching."""
    values = [getattr(item, "guid", None)]
    values.extend(
        getattr(guid, "id", guid) for guid in (getattr(item, "guids", None) or [])
    )
    identifiers = set()
    for value in values:
        match = re.search(r"(?:tmdb|themoviedb)(?:://|:)(\d+)", str(value or ""), re.I)
        if match:
            identifiers.add(match.group(1))
    return identifiers


def target_items_by_tmdb(items, tmdb_ids):
    requested = {str(value) for value in (tmdb_ids or []) if str(value).strip()}
    if not requested:
        return list(items or []), set()
    selected = []
    found = set()
    for item in items or []:
        matches = plex_item_tmdb_ids(item) & requested
        if matches:
            selected.append(item)
            found.update(matches)
    return selected, found


def validate_inventory_snapshot(library_name, inventory, discovery_records):
    """Reject same-size inventory replacement between discovery and processing."""
    if len(inventory) != len(discovery_records):
        raise RuntimeError(
            f"Plex inventory for {library_name} changed between discovery and "
            "processing; cleanup is disabled"
        )
    discovered_keys = {
        str(record["rating_key"])
        for record in discovery_records
        if record.get("rating_key") is not None
    }
    processing_keys = {
        str(item.ratingKey)
        for item in inventory
        if getattr(item, "ratingKey", None) is not None
    }
    if discovered_keys != processing_keys:
        raise RuntimeError(
            f"Plex inventory for {library_name} changed rating keys between "
            "discovery and processing; cleanup is disabled"
        )


def build_scan_scopes(plex, sections, config):
    server_id = getattr(plex, "machineIdentifier", None) or "unknown"
    scopes = []
    for section in sections:
        library_name = section.title
        library_config = config_for_library(config, library_name)
        scopes.append(
            {
                "server_id": str(server_id),
                "library_uuid": str(
                    getattr(section, "uuid", None)
                    or getattr(section, "key", None)
                    or library_name
                ),
                "library_name": library_name,
                "config_fingerprint": config_fingerprint(library_config),
                "item_count": None,
            }
        )
    return scopes


def complete_inventory_types(all_libraries, successful_sections):
    detected = {"movie": set(), "tv": set()}
    successful = {"movie": set(), "tv": set()}
    for library in all_libraries:
        library_type = normalize_library_type(library.get("type"))
        if library_type in detected:
            detected[library_type].add(library.get("title"))
    for section in successful_sections:
        library_type = normalize_library_type(
            getattr(section, "type", None) or getattr(section, "TYPE", None)
        )
        if library_type in successful:
            successful[library_type].add(section.title)
    return {
        library_type
        for library_type in detected
        if detected[library_type]
        and detected[library_type].issubset(successful[library_type])
    }


def missed_schedule_due(run_times, recent_runs, max_hours=24, now=None):
    """Return the latest missed slot that has not had a later successful job."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()
    candidates = []
    for run_time in run_times or []:
        try:
            parsed = datetime.strptime(  # noqa: DTZ007 -- local scheduler wall clock
                str(run_time), "%H:%M"
            ).time()
        except (TypeError, ValueError):
            continue
        for days_ago in (0, 1):
            day = (current - timedelta(days=days_ago)).date()
            candidate = datetime.combine(day, parsed, tzinfo=current.tzinfo)
            if candidate <= current:
                candidates.append(candidate)
    if not candidates:
        return None
    latest_due = max(candidates)
    if current - latest_due > timedelta(hours=max(0.1, float(max_hours))):
        return None

    successful = []
    for run in recent_runs or []:
        if run.get("status") != "success" or not run.get("finished_at"):
            continue
        try:
            finished = datetime.fromisoformat(
                str(run["finished_at"]).replace("Z", "+00:00")
            )
            if finished.tzinfo is None:
                finished = finished.astimezone()
            successful.append(finished.astimezone(current.tzinfo))
        except (TypeError, ValueError):
            continue
    if successful and max(successful) >= latest_due:
        return None
    return latest_due


async def metafusion_main(config, logger):
    _plex_cache.clear()
    plex_metadata_dict.clear()
    try:
        get_meta_banner(logger)
        current_build = build_info()
        logger.info(
            "[Startup] Build | %s",
            format_fields(
                ("Version", current_build["version"]),
                ("Commit", current_build["commit"]),
            ),
        )
        check_sys_requirements(logger, config=config, check_network=False)
        log_main_event(
            "main_started",
            start_time=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        )
        log_section(logger, "Configuration", "Effective run configuration")
        get_disabled_features(config, logger)
        feature_flags = get_feature_flags(config)
        log_section(logger, "Processing", "Library processing")
        start_time = datetime.now().astimezone()
        library_item_counts = {}
        runtime_config = config.get("runtime", {})
        max_concurrency = concurrency_ceiling(config, "network")
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(runtime_config.get("request_timeout", 30.0))),
            connect=max(1.0, float(runtime_config.get("connect_timeout", 10.0))),
        )
        connector = aiohttp.TCPConnector(
            limit=max_concurrency,
            limit_per_host=max_concurrency,
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            execution = config.get("_execution", {})
            maintenance_action = execution.get("plex_metadata_maintenance")
            inventory_started = time.monotonic()
            if maintenance_action:
                plex = await preflight_connectors(config, session, require_tmdb=False)
            else:
                plex = await preflight_connectors(config, session)
            sections, selected_libraries, all_libraries = await asyncio.to_thread(
                connect_plex_library,
                config,
                plex=plex,
            )
            requested_media_types = set(execution.get("media_types") or [])
            if requested_media_types:
                sections = [
                    section
                    for section in sections
                    if cli_media_type(
                        getattr(section, "type", None)
                        or getattr(section, "TYPE", None)
                    )
                    in requested_media_types
                ]
                selected_libraries = [section.title for section in sections]
            profile = resolve_compatibility_profile(config)
            logger.info(
                "[Compatibility] Using %s for %s mode.",
                profile,
                config.get("settings", {}).get("mode", "unknown"),
            )
            metadata_summaries = {}
            library_results = {}
            config["_job_library_results"] = library_results
            library_filesize = {}
            successful_sections = []
            failures = []
            targeted = bool(execution.get("targeted"))
            explain_selection = bool(execution.get("explain_selection"))
            scan_scopes = build_scan_scopes(plex, sections, config)
            if execution.get("retry_failed"):
                retry_status = execution.get("retry_status", "all")
                statuses = None if retry_status == "all" else [retry_status]
                retry_records = await asyncio.to_thread(
                    load_item_retries,
                    server_id=getattr(plex, "machineIdentifier", None) or "unknown",
                    library_uuids=[scope["library_uuid"] for scope in scan_scopes],
                    rating_keys=execution.get("rating_keys"),
                    statuses=statuses,
                )
                if not retry_records:
                    logger.info(
                        "[Recovery] No queued failed items matched the selected scope."
                    )
                    return
                execution["rating_keys"] = sorted(
                    {str(record["rating_key"]) for record in retry_records}
                )
                logger.info(
                    "[Recovery] Selectively retrying %d queued item(s).",
                    len(execution["rating_keys"]),
                )
            scan_decisions = library_full_scan_decisions(
                config,
                targeted=targeted,
                scopes=scan_scopes,
            )
            if execution.get("full_scan"):
                scan_decisions = dict.fromkeys(scan_decisions, True)
            run_feature_flags = dict(feature_flags)
            cleanup_skip_reason = None
            all_full_scan = False

            missing_discovered_libraries = []
            if config.get("_library_discovery_auto", False):
                eligible_libraries = [
                    library
                    for library in all_libraries
                    if normalize_library_type(library.get("type")) in {"movie", "tv"}
                ]
                inventory_function = (
                    missing_library_inventory
                    if feature_flags.get("dry_run", False)
                    else reconcile_library_inventory
                )
                missing_discovered_libraries = await asyncio.to_thread(
                    inventory_function,
                    getattr(plex, "machineIdentifier", None) or "unknown",
                    eligible_libraries,
                )
                if missing_discovered_libraries:
                    logger.warning(
                        "[Plex] Previously discovered libraries are unavailable; "
                        "processing continues but cleanup is disabled: %s",
                        ", ".join(
                            sorted(
                                library.get("library_name") or library.get("library_uuid")
                                for library in missing_discovered_libraries
                            )
                        ),
                    )

            detected_names = {library["title"] for library in all_libraries}
            missing_selected = set(selected_libraries) - detected_names
            if missing_selected:
                failures.append(
                    "Configured Plex libraries were not found: "
                    + ", ".join(sorted(missing_selected))
                )

            if not sections:
                log_main_event("main_no_libraries")
                failures.append("No configured Plex libraries were available")
            else:
                if mode_check(config, "kometa") and not all(scan_decisions.values()):
                    metadata_dir = Path(config.get("settings", {}).get("path", ".")) / "metadata"
                    missing_types = {
                        library_type
                        for library_type, output in (
                            ("movie", metadata_dir / "movie_metadata.yml"),
                            ("tv", metadata_dir / "tv_metadata.yml"),
                        )
                        if not output.exists()
                    }
                    for section, scope in zip(
                        sections, scan_scopes, strict=False
                    ):
                        library_type = normalize_library_type(
                            getattr(section, "type", None)
                            or getattr(section, "TYPE", None)
                        )
                        if library_type in missing_types:
                            scan_decisions[
                                (scope["server_id"], scope["library_uuid"])
                            ] = True
                all_full_scan = bool(scan_decisions) and all(scan_decisions.values())
                if targeted and feature_flags.get("cleanup", False):
                    cleanup_skip_reason = (
                        "targeted run; full reconciliation requires every configured "
                        "library"
                    )
                    run_feature_flags["cleanup"] = False
                elif missing_discovered_libraries:
                    if feature_flags.get("cleanup", False):
                        cleanup_skip_reason = (
                            "previously discovered Plex library unavailable; cleanup "
                            "requires a complete stable inventory"
                        )
                    run_feature_flags["cleanup"] = False
                elif not all_full_scan:
                    if feature_flags.get("cleanup", False):
                        cleanup_skip_reason = (
                            "incremental run; full reconciliation required"
                        )
                    run_feature_flags["cleanup"] = False
                section_inventory_records = {}
                identity_counts = Counter()
                edition_counts = Counter()
                found_rating_keys = set()
                found_tmdb_ids = set()
                for section in sections:
                    try:
                        inventory = await load_plex_library_inventory(
                            section,
                            runtime_config,
                            records_only=True,
                        )
                        section_inventory_records[section.title] = inventory
                        requested_tmdb = {
                            str(value)
                            for value in execution.get("tmdb_ids", [])
                            if str(value).strip()
                        }
                        selected_inventory = [
                            record
                            for record in inventory
                            if not requested_tmdb
                            or str(record.get("tmdb_id")) in requested_tmdb
                        ]
                        found_tmdb_ids.update(
                            str(record["tmdb_id"])
                            for record in selected_inventory
                            if record.get("tmdb_id") is not None
                        )
                        found_rating_keys.update(
                            str(record["rating_key"])
                            for record in selected_inventory
                            if record.get("rating_key") is not None
                        )
                        for scope in scan_scopes:
                            if scope["library_name"] == section.title:
                                scope["item_count"] = len(inventory)
                                break
                        for record in inventory:
                            media_type = normalize_library_type(record.get("media_type"))
                            title = record.get("title")
                            year = record.get("year")
                            identity_counts[(media_type, title, year)] += 1
                            if media_type == "movie":
                                edition = record.get("edition")
                                edition_counts[(title, year, edition)] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append(f"{section.title}: {error}")
                        library_results[section.title] = {
                            "status": "failed",
                            "items_processed": 0,
                            "items_succeeded": 0,
                            "items_failed": None,
                        }
                requested_rating_keys = {
                    str(value) for value in execution.get("rating_keys", [])
                }
                missing_rating_keys = requested_rating_keys - found_rating_keys
                if missing_rating_keys:
                    failures.append(
                        "rating keys not found: "
                        + ", ".join(sorted(missing_rating_keys))
                    )
                requested_tmdb_ids = {
                    str(value) for value in execution.get("tmdb_ids", [])
                }
                missing_tmdb_ids = requested_tmdb_ids - found_tmdb_ids
                if missing_tmdb_ids:
                    failures.append(
                        "TMDb IDs not exposed by Plex GUIDs: "
                        + ", ".join(sorted(missing_tmdb_ids))
                    )
                if execution.get("plan") or execution.get("library_audit"):
                    loaded_counts = {
                        scope["library_name"]: scope.get("item_count")
                        for scope in scan_scopes
                    }
                    selected_names = {section.title for section in sections}
                    config["_library_audit_records"] = [
                        {
                            "library": library.get("title") or "Unknown library",
                            "type": cli_media_type(library.get("type")),
                            "items": loaded_counts.get(library.get("title"), 0) or 0,
                            "selected": library.get("title") in selected_names,
                            "status": (
                                "loaded"
                                if library.get("title") in loaded_counts
                                else "available"
                            ),
                        }
                        for library in all_libraries
                        if normalize_library_type(library.get("type"))
                        in {"movie", "tv"}
                    ]
                performance = tracker_for(config)
                if performance:
                    performance.add_duration(
                        "plex_inventory", time.monotonic() - inventory_started
                    )

                if maintenance_action:
                    if not mode_check(config, "plex"):
                        raise RuntimeError(
                            "Plex metadata restore/unlock requires RUN_MODE=plex"
                        )
                    target_keys = {
                        str(value) for value in execution.get("rating_keys", [])
                    }
                    if not target_keys:
                        raise RuntimeError(
                            "Plex metadata restore/unlock requires --rating-key"
                        )
                    found_keys = set()
                    maintenance_failures = list(failures)
                    for section in sections:
                        library_config = config_for_library(config, section.title)
                        inventory = await load_plex_library_inventory(
                            section,
                            runtime_config,
                        )
                        validate_inventory_snapshot(
                            section.title,
                            inventory,
                            section_inventory_records.get(section.title, []),
                        )
                        selected_inventory, _matched = target_items_by_tmdb(
                            inventory,
                            execution.get("tmdb_ids"),
                        )
                        for item in selected_inventory:
                            rating_key = str(getattr(item, "ratingKey", ""))
                            if rating_key not in target_keys:
                                continue
                            found_keys.add(rating_key)
                            meta = await get_plex_metadata(
                                item,
                                _runtime_config=runtime_config,
                                _plex_config=library_config.get("plex", {}),
                            )
                            result = await restore_plex_metadata(
                                item,
                                library_config,
                                meta,
                                unlock_only=maintenance_action == "unlock",
                            )
                            if result.get("failures"):
                                maintenance_failures.append(
                                    f"{section.title}/{rating_key}"
                                )
                    missing_keys = target_keys - found_keys
                    if missing_keys:
                        maintenance_failures.append(
                            "rating keys not found: " + ", ".join(sorted(missing_keys))
                        )
                    if maintenance_failures:
                        raise RuntimeError("; ".join(maintenance_failures))
                    logger.info(
                        "[Metadata] Plex maintenance | Action: %s | Items: %d",
                        maintenance_action,
                        len(found_keys),
                    )
                    return

                asset_destination_registry = AssetDestinationRegistry()
                cache_store = load_cache()
                if hasattr(cache_store, "asset_destination_records"):
                    asset_destination_registry = AssetDestinationRegistry(
                        cache_store.asset_destination_records()
                    )
                for section in sections:
                    if section.title not in section_inventory_records:
                        continue
                    try:
                        inventory = await load_plex_library_inventory(
                            section,
                            runtime_config,
                        )
                        validate_inventory_snapshot(
                            section.title,
                            inventory,
                            section_inventory_records.get(section.title, []),
                        )
                        selected_inventory, _matched = target_items_by_tmdb(
                            inventory,
                            execution.get("tmdb_ids"),
                        )
                        library_config = config_for_library(config, section.title)
                        library_config["_asset_destination_registry"] = (
                            asset_destination_registry
                        )
                        library_config["_artwork_gaps"] = config.setdefault(
                            "_artwork_gaps", []
                        )
                        library_config["_asset_audit_records"] = config.setdefault(
                            "_asset_audit_records", []
                        )
                        library_config["_metadata_audit_records"] = config.setdefault(
                            "_metadata_audit_records", []
                        )
                        library_config["_adoption_audit_records"] = config.setdefault(
                            "_adoption_audit_records", []
                        )
                        scope = next(
                            candidate
                            for candidate in scan_scopes
                            if candidate["library_name"] == section.title
                        )
                        section_full_scan = scan_decisions.get(
                            (scope["server_id"], scope["library_uuid"]), True
                        )
                        library_flags = dict(get_feature_flags(library_config))
                        if not section_full_scan:
                            library_flags["cleanup"] = False
                        if not targeted and not explain_selection:
                            mark_library_scan_started(
                                [scope],
                                full_scan=section_full_scan,
                                dry_run=feature_flags.get("dry_run", False),
                            )
                        library_started = time.monotonic()
                        try:
                            await process_library(
                                library_section=section,
                                config=library_config,
                                library_item_counts=library_item_counts,
                                metadata_summaries=metadata_summaries,
                                library_filesize=library_filesize,
                                season_cache={},
                                episode_cache={},
                                movie_cache={},
                                session=session,
                                feature_flags=library_flags,
                                full_scan=section_full_scan,
                                rating_keys=execution.get("rating_keys"),
                                incremental_fingerprint=config_fingerprint(library_config),
                                all_items=selected_inventory,
                                global_identity_counts=identity_counts,
                                global_edition_counts=edition_counts,
                                explain_selection=explain_selection,
                            )
                        finally:
                            performance = tracker_for(config)
                            if performance:
                                performance.add_duration(
                                    "library_processing",
                                    time.monotonic() - library_started,
                                )
                        successful_sections.append(section)
                        if section_full_scan:
                            config.setdefault(
                                "_successful_full_scan_libraries", []
                            ).append(section.title)
                            evaluated_work = set()
                            if any(
                                library_flags.get(name, False)
                                for name in (
                                    "metadata_basic",
                                    "metadata_enhanced",
                                    "plex_metadata",
                                    "poster",
                                    "background",
                                    "season",
                                )
                            ):
                                evaluated_work.add("metadata")
                            evaluated_work.update(
                                name
                                for name in ("poster", "background", "season")
                                if library_flags.get(name, False)
                            )
                            config.setdefault(
                                "_successful_full_scan_work", {}
                            )[section.title] = sorted(evaluated_work)
                        summary = metadata_summaries.get(section.title, {})
                        library_summary = summary.get("library_summary", {})
                        failed_items = int(library_summary.get("item_failures", 0))
                        processed_items = int(summary.get("total_items", 0))
                        library_results[section.title] = {
                            "status": "success",
                            "items_processed": processed_items,
                            "items_succeeded": max(0, processed_items - failed_items),
                            "items_failed": failed_items,
                            "items_unchanged": int(
                                library_summary.get("incremental_skipped", 0)
                            ),
                        }
                        if not targeted and not explain_selection:
                            mark_library_scan_complete(
                                [scope],
                                full_scan=section_full_scan,
                                dry_run=feature_flags.get("dry_run", False),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append(f"{section.title}: {error}")
                        summary = metadata_summaries.get(section.title, {})
                        library_summary = summary.get("library_summary", {})
                        processed_items = int(summary.get("total_items", 0))
                        failed_items = library_summary.get("item_failures")
                        library_results[section.title] = {
                            "status": "failed",
                            "items_processed": processed_items,
                            "items_succeeded": (
                                max(0, processed_items - int(failed_items))
                                if failed_items is not None else 0
                            ),
                            "items_failed": (
                                int(failed_items) if failed_items is not None else None
                            ),
                            "items_unchanged": int(
                                library_summary.get("incremental_skipped", 0)
                            ),
                        }

            cleanup_result = CleanupResult(
                mode=config.get("settings", {}).get("mode", "kometa")
            )
            if run_feature_flags.get("cleanup", False):
                safe_library_types = set()
                if not failures:
                    safe_library_types = complete_inventory_types(
                        all_libraries, successful_sections
                    )
                kometa_root = config.get("settings", {}).get("path", ".")
                asset_path = Path(kometa_root) / "assets"
                try:
                    cleanup_result = await cleanup_title_orphans(
                        config=config,
                        asset_path=asset_path,
                        preloaded_plex_metadata=plex_metadata_dict,
                        feature_flags=run_feature_flags,
                        safe_library_types=safe_library_types,
                    )
                except CleanupError as error:
                    cleanup_result = error.result or CleanupResult(
                        failures=1,
                        failed_reason=str(error),
                        mode=config.get("settings", {}).get("mode", "kometa"),
                    )
                    failures.append(f"Cleanup: {error}")
            elif cleanup_skip_reason:
                cleanup_result.skipped_reason = cleanup_skip_reason
                log_cleanup_event(
                    "cleanup_skipped_run_scope", reason=cleanup_skip_reason
                )
            config["_cleanup_result"] = cleanup_result

            elapsed_time = (datetime.now().astimezone() - start_time).total_seconds()
            log_final_summary(
                logger,
                elapsed_time,
                metadata_summaries,
                library_filesize,
                cleanup_result,
                cleanup_title_orphans,
                selected_libraries,
                all_libraries,
                config,
                run_feature_flags,
            )
            if failures:
                raise RuntimeError("; ".join(failures))
    finally:
        _plex_cache.clear()
        plex_metadata_dict.clear()


def _cancel_active_job():
    if _active_task is not None and not _active_task.done():
        _active_task.cancel()


def _force_exit_after_timeout(timeout):
    if shutdown_complete.wait(timeout):
        return
    logging.getLogger().critical(
        "[MetaFusion] Graceful shutdown exceeded %.1fs; forcing process exit.",
        timeout,
    )
    os._exit(128 + signal.SIGTERM)


def request_shutdown(_signum=None, _frame=None):
    first_request = not shutdown_requested.is_set()
    shutdown_requested.set()
    if _active_loop is not None:
        _active_loop.call_soon_threadsafe(_cancel_active_job)
    if first_request:
        threading.Thread(
            target=_force_exit_after_timeout,
            args=(_shutdown_timeout,),
            name="metafusion-shutdown-watchdog",
            daemon=True,
        ).start()


def run_metafusion_job(config, logger, runtime_status=None):
    global _active_loop, _active_task
    job_lock = None
    if not config.get("settings", {}).get("dry_run", False):
        job_lock = JobRunLock(Path(BASE_CONFIG_DIR) / ".metafusion-run.lock")
        try:
            job_lock.acquire()
        except JobAlreadyRunningError as error:
            log_main_event("main_job_already_running", error=error, logger=logger)
            if runtime_status:
                runtime_status.run_started()
                runtime_status.run_finished(False, error=error)
            return False
    config["_metadata_audit_records"] = []
    try:
        begin_cache_session(
            writable=not config.get("settings", {}).get("dry_run", False)
        )
        begin_tmdb_cache(config)
        begin_fanart_cache(config)
        begin_plex_metadata_run(config)
    except Exception:
        if job_lock is not None:
            job_lock.release()
        raise
    if runtime_status:
        runtime_status.run_started()

    success = False
    error = None
    config["_job_library_results"] = {}
    config["_artwork_gaps"] = []
    config["_asset_audit_records"] = []
    config["_library_audit_records"] = []
    config["_adoption_audit_records"] = []
    config["_successful_full_scan_libraries"] = []
    config["_successful_full_scan_work"] = {}
    config["_cleanup_result"] = CleanupResult(
        mode=config.get("settings", {}).get("mode", "kometa")
    )
    performance_tracker = PerformanceTracker()

    async def run_active_job():
        global _active_loop, _active_task
        _active_loop = asyncio.get_running_loop()
        _active_task = asyncio.current_task()
        performance_token = begin_performance_tracking(performance_tracker)
        concurrency_controller, concurrency_token = begin_adaptive_concurrency(config)
        try:
            await metafusion_main(config, logger)
        finally:
            finish_adaptive_concurrency(
                concurrency_controller,
                concurrency_token,
            )
            reset_performance_tracking(performance_token)

    try:
        asyncio.run(run_active_job())
        success = True
    except asyncio.CancelledError:
        error = "Shutdown requested; active run was cancelled"
        log_main_event("main_shutdown_requested", logger=logger)
    except Exception as caught:
        error = redact_secrets(
            caught,
            config.get("plex", {}).get("token"),
            config.get("tmdb", {}).get("api_key"),
            fanart_project_api_key(),
        )
        log_main_event("main_unhandled_exception", error=error, logger=logger)
    finally:
        _active_loop = None
        _active_task = None
        try:
            try:
                report_path = finish_plex_metadata_run(config)
                if report_path:
                    logger.info(
                        "[Diagnostics] Plex metadata report saved to %s",
                        report_path,
                    )
            except Exception as caught:
                success = False
                error = f"Failed to write Plex metadata report: {caught}"
                log_main_event("main_unhandled_exception", error=error, logger=logger)
            if not config.get("settings", {}).get("dry_run", False):
                try:
                    retention = report_retention(config)
                    gap_report = write_artwork_gap_report(
                        config.get("_artwork_gaps"), retention=retention
                    )
                    if gap_report:
                        logger.info(
                            "[Diagnostics] Artwork gap report saved to %s", gap_report
                        )
                    destination_report = write_destination_history_report(
                        load_cache(),
                        retention=retention,
                        config=config,
                    )
                    if destination_report:
                        logger.info(
                            "[Diagnostics] Artwork destination history saved to %s",
                            destination_report,
                        )
                    ledger = reconcile_unresolved_work(
                        config.get("_artwork_gaps"),
                        resolved_work=config.get("_successful_full_scan_work", {}),
                    )
                    ledger_report = write_unresolved_work_report(
                        ledger,
                        retention=retention,
                    )
                    if ledger_report:
                        logger.info(
                            "[Diagnostics] Unresolved-work ledger saved to %s",
                            ledger_report,
                        )
                    adoption_report = write_adoption_audit_report(
                        config.get("_adoption_audit_records"),
                        retention=retention,
                    )
                    if adoption_report:
                        logger.info(
                            "[Diagnostics] Post-application adoption audit saved to %s",
                            adoption_report,
                        )
                except (OSError, StateDatabaseError) as caught:
                    logger.warning(
                        "[Diagnostics] Unable to complete persistent reports: %s",
                        caught,
                    )
            execution = config.get("_execution", {})
            if (
                execution.get("asset_audit", False)
                and not execution.get("plan")
                and not execution.get("library_audit")
            ):
                try:
                    audit_report = write_asset_audit_report(
                        config.get("_asset_audit_records"),
                        config.get("_artwork_gaps"),
                        retention=report_retention(config),
                    )
                    logger.info(
                        "[Diagnostics] Asset audit report saved to %s", audit_report
                    )
                except OSError as caught:
                    success = False
                    error = f"Failed to write asset audit report: {caught}"
                    logger.error("[Diagnostics] %s", error)
            if execution.get("metadata_audit", False) and not execution.get("plan"):
                try:
                    metadata_audit_report = write_metadata_audit_report(
                        config.get("_metadata_audit_records"),
                        config.get("_artwork_gaps"),
                        mode=config.get("settings", {}).get("mode", "unknown"),
                        retention=report_retention(config),
                    )
                    logger.info(
                        "[Diagnostics] Metadata audit report saved to %s",
                        metadata_audit_report,
                    )
                except OSError as caught:
                    success = False
                    error = f"Failed to write metadata audit report: {caught}"
                    logger.error("[Diagnostics] %s", error)
            if execution.get("plan"):
                try:
                    plan_report = write_change_plan_report(
                        config.get("_metadata_audit_records"),
                        config.get("_asset_audit_records"),
                        config.get("_library_audit_records"),
                        config.get("_artwork_gaps"),
                        config.get("_cleanup_result"),
                        mode=config.get("settings", {}).get("mode", "unknown"),
                        retention=report_retention(config),
                    )
                    logger.info(
                        "[Diagnostics] Read-only change plan saved to %s",
                        plan_report,
                    )
                except OSError as caught:
                    success = False
                    error = f"Failed to write read-only change plan: {caught}"
                    logger.error("[Diagnostics] %s", error)
            if execution.get("library_audit"):
                try:
                    library_report = write_library_asset_audit_report(
                        config.get("_library_audit_records"),
                        config.get("_asset_audit_records"),
                        config.get("_artwork_gaps"),
                        mode=config.get("settings", {}).get("mode", "unknown"),
                        retention=report_retention(config),
                    )
                    logger.info(
                        "[Diagnostics] Library and asset audit saved to %s",
                        library_report,
                    )
                except OSError as caught:
                    success = False
                    error = f"Failed to write library and asset audit: {caught}"
                    logger.error("[Diagnostics] %s", error)
            flush_cache()
            flush_tmdb_cache()
            flush_fanart_cache()
            if not config.get("settings", {}).get("dry_run", False):
                try:
                    tmdb_maintenance = tmdb_response_cache.maintain()
                    state_maintenance = maintain_state_database()
                    retry_summary = retry_queue_summary()
                    logger.info(
                        "[Maintenance] SQLite optimization complete | %s",
                        format_fields(
                            ("State checkpoint", state_maintenance.get("checkpointed", False)),
                            ("TMDb checkpoint", tmdb_maintenance.get("checkpointed", False)),
                            ("Fanart.tv checkpoint", fanart_response_cache.maintain().get("checkpointed", False)),
                            ("Retry queue", retry_summary or "Empty"),
                        ),
                    )
                except Exception as maintenance_error:
                    logger.warning(
                        "[Maintenance] Deferred optional SQLite maintenance: %s",
                        maintenance_error,
                    )
        except Exception as caught:
            success = False
            error = f"Failed to flush persistent cache: {caught}"
            log_main_event("main_unhandled_exception", error=error, logger=logger)
        finally:
            log_performance_summary(logger, performance_tracker)
            tmdb_response_cache.reset_memory()
            fanart_response_cache.reset_memory()
        try:
            if runtime_status:
                runtime_status.run_finished(
                    success,
                    error=error,
                    library_results=config.get("_job_library_results", {}),
                )
        finally:
            if job_lock is not None:
                job_lock.release()
    return success


def main(argv=None):
    global _shutdown_timeout
    shutdown_requested.clear()
    shutdown_complete.clear()
    args = parse_cli_args(argv)
    if args.problems:
        try:
            records = load_unresolved_work(statuses=["open"], path=STATE_DATABASE)
        except StateDatabaseError as error:
            print(f"Unable to read unresolved-work ledger: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"open": len(records), "items": records}, indent=2, sort_keys=True))
        return 0
    audit_commands = sum(
        bool(value)
        for value in (
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
        )
    )
    if args.metadata_audit and args.asset_audit:
        print(
            "Configuration error: --metadata-audit and --asset-audit "
            "cannot be combined",
            file=sys.stderr,
        )
        return 2
    if audit_commands > 1:
        print(
            "Configuration error: choose only one audit or plan command",
            file=sys.stderr,
        )
        return 2
    if args.retry_failed and any(
        (
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
            args.mapping_diagnose,
            args.identity_inspect,
            args.explain_item,
            args.capture_replay,
        )
    ):
        print(
            "Configuration error: --retry-failed cannot be combined with audits or --plan",
            file=sys.stderr,
        )
        return 2
    if args.retry_status != "all" and not args.retry_failed:
        print(
            "Configuration error: --retry-status requires --retry-failed",
            file=sys.stderr,
        )
        return 2
    if args.sqlite_target != "all" and not args.sqlite_maintenance:
        print(
            "Configuration error: --sqlite-target requires --sqlite-maintenance",
            file=sys.stderr,
        )
        return 2
    if args.sqlite_maintenance:
        conflicting = any(
            (
                args.metafusion_run,
                args.schedule,
                args.preflight,
                args.release_check,
                args.asset_audit,
                args.metadata_audit,
                args.plan,
                args.library_audit,
                args.mapping_diagnose,
                args.identity_inspect,
                args.explain_item,
                args.capture_replay,
                args.retry_failed,
                args.compatibility_check,
            )
        )
        if conflicting:
            print(
                "Configuration error: SQLite maintenance must run as a standalone command",
                file=sys.stderr,
            )
            return 2
        maintenance_lock = None
        try:
            if args.sqlite_maintenance != "check":
                maintenance_lock = JobRunLock(
                    Path(BASE_CONFIG_DIR) / ".metafusion-run.lock"
                )
                maintenance_lock.acquire()
            results = maintain_databases(
                args.sqlite_maintenance,
                args.sqlite_target,
            )
        except (JobAlreadyRunningError, OSError, ValueError) as error:
            print(f"SQLite maintenance failed: {error}", file=sys.stderr)
            return 1
        finally:
            if maintenance_lock is not None:
                maintenance_lock.release()
        print(format_maintenance_results(results))
        return 0 if all(result.get("healthy") for result in results) else 1
    if args.metadata_only and (args.asset_only or args.asset_audit):
        print("Configuration error: --metadata-only and --asset-only cannot be combined", file=sys.stderr)
        return 2
    if args.metadata_audit and (args.asset_only or args.asset_audit):
        print(
            "Configuration error: --metadata-audit cannot be combined with artwork audits",
            file=sys.stderr,
        )
        return 2
    if args.plex_metadata_restore and args.plex_metadata_unlock:
        print(
            "Configuration error: choose only one Plex metadata maintenance action",
            file=sys.stderr,
        )
        return 2
    if (args.plex_metadata_restore or args.plex_metadata_unlock) and not args.rating_key:
        print(
            "Configuration error: Plex metadata restore/unlock requires --rating-key",
            file=sys.stderr,
        )
        return 2
    if args.mapping_diagnose and not args.rating_key:
        print(
            "Configuration error: --mapping-diagnose requires --rating-key",
            file=sys.stderr,
        )
        return 2
    if args.identity_inspect and not args.rating_key:
        print(
            "Configuration error: --identity-inspect requires --rating-key",
            file=sys.stderr,
        )
        return 2
    if args.explain_item and not args.rating_key:
        print(
            "Configuration error: --explain-item requires --rating-key",
            file=sys.stderr,
        )
        return 2
    if args.capture_replay and not args.rating_key:
        print(
            "Configuration error: --capture-replay requires --rating-key",
            file=sys.stderr,
        )
        return 2
    if sum(
        bool(value)
        for value in (
            args.mapping_diagnose,
            args.identity_inspect,
            args.explain_item,
            args.capture_replay,
        )
    ) > 1:
        print(
            "Configuration error: choose only one item diagnostic command",
            file=sys.stderr,
        )
        return 2
    if args.capture_replay and any(
        (
            args.metafusion_run,
            args.schedule,
            args.run_times is not None,
            args.run_basic,
            args.run_enhanced,
            args.run_poster,
            args.run_season,
            args.run_background,
            args.asset_only,
            args.metadata_only,
            args.full_scan,
            args.explain_selection,
            args.status,
            args.doctor,
            args.support_report,
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
            args.preflight,
            args.release_check,
            args.compatibility_check,
            args.plex_metadata_restore,
            args.plex_metadata_unlock,
            bool(args.tmdb_id),
            bool(args.media_type),
        )
    ):
        print(
            "Configuration error: --capture-replay must run as a standalone diagnostic",
            file=sys.stderr,
        )
        return 2
    if args.mapping_diagnose and any(
        (
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
            args.preflight,
            args.release_check,
            args.compatibility_check,
            args.plex_metadata_restore,
            args.plex_metadata_unlock,
            args.identity_inspect,
            args.explain_item,
            args.capture_replay,
        )
    ):
        print(
            "Configuration error: --mapping-diagnose must run as a standalone diagnostic",
            file=sys.stderr,
        )
        return 2
    if args.identity_inspect and any(
        (
            args.metafusion_run,
            args.schedule,
            args.run_times is not None,
            args.run_basic,
            args.run_enhanced,
            args.run_poster,
            args.run_season,
            args.run_background,
            args.asset_only,
            args.metadata_only,
            args.full_scan,
            args.explain_selection,
            args.status,
            args.doctor,
            args.support_report,
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
            args.mapping_diagnose,
            args.explain_item,
            args.preflight,
            args.release_check,
            args.compatibility_check,
            args.plex_metadata_restore,
            args.plex_metadata_unlock,
            bool(args.tmdb_id),
            bool(args.media_type),
        )
    ):
        print(
            "Configuration error: --identity-inspect must run as a standalone diagnostic",
            file=sys.stderr,
        )
        return 2
    if args.explain_item and any(
        (
            args.metafusion_run,
            args.schedule,
            args.run_times is not None,
            args.run_basic,
            args.run_enhanced,
            args.run_poster,
            args.run_season,
            args.run_background,
            args.asset_only,
            args.metadata_only,
            args.full_scan,
            args.explain_selection,
            args.status,
            args.doctor,
            args.support_report,
            args.asset_audit,
            args.metadata_audit,
            args.plan,
            args.library_audit,
            args.mapping_diagnose,
            args.identity_inspect,
            args.preflight,
            args.release_check,
            args.compatibility_check,
            args.plex_metadata_restore,
            args.plex_metadata_unlock,
            bool(args.tmdb_id),
            bool(args.media_type),
        )
    ):
        print(
            "Configuration error: --explain-item must run as a standalone diagnostic",
            file=sys.stderr,
        )
        return 2
    if args.status:
        status_path = Path(
            os.environ.get("STATUS_FILE", "/tmp/metafusion-status.json")
        )
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"Unable to read runtime status: {error}", file=sys.stderr)
            return 1
        try:
            recent_runs = recent_job_runs(path=STATE_DATABASE)
        except StateDatabaseError as state_error:
            status["history_error"] = str(state_error)
            recent_runs = []
        if recent_runs:
            status["recent_jobs"] = recent_runs
        try:
            status["retry_queue"] = retry_queue_summary(path=STATE_DATABASE)
        except StateDatabaseError as state_error:
            status["retry_queue_error"] = str(state_error)
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    try:
        config, sources = load_config_file(
            create_if_missing=not (
                args.dry_run
                or args.doctor
                or args.preflight
                or args.asset_audit
                or args.metadata_audit
                or args.plan
                or args.library_audit
                or args.mapping_diagnose
                or args.identity_inspect
                or args.explain_item
                or args.capture_replay
                or args.compatibility_check
                or args.release_check
            ),
            return_sources=True,
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    override_config_with_cli(config, args)
    cli_sources = {
        ("metafusion_run",): args.metafusion_run,
        ("settings", "schedule"): args.schedule,
        ("settings", "run_times"): args.run_times is not None,
        ("settings", "dry_run"): args.dry_run,
        ("settings", "mode"): args.mode is not None,
        ("metadata", "run_basic"): args.run_basic or args.asset_only,
        ("metadata", "run_enhanced"): args.run_enhanced or args.asset_only,
        ("assets", "run_poster"): args.run_poster or args.metadata_only,
        ("assets", "run_season"): args.run_season or args.metadata_only,
        ("assets", "run_background"): args.run_background or args.metadata_only,
        ("cleanup", "run_cleanup"): args.metadata_only or args.asset_only or bool(args.rating_key),
        ("plex_libraries",): bool(args.library),
        ("compatibility", "profile"): args.compatibility_profile is not None,
    }
    for path, used in cli_sources.items():
        if used:
            sources[path] = "CLI"

    validation_errors = validate_config(config)
    if args.support_report:
        try:
            report = write_support_report(config, validation_errors)
        except OSError as error:
            print(f"Unable to write support report: {error}", file=sys.stderr)
            return 1
        print(f"Support report saved to {report}")
        return 0
    if args.doctor:
        print("MetaFusion configuration sources:")
        for line in config_source_report(config, sources):
            print(f"  {line}")
        if validation_errors:
            print("Configuration errors:", file=sys.stderr)
            for error in validation_errors:
                print(f"  - {error}", file=sys.stderr)
            return 2
        print("Configuration is valid.")
        return 0
    if validation_errors:
        for error in validation_errors:
            print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if args.mapping_diagnose:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            begin_tmdb_cache(config)
            records, report = asyncio.run(
                mapping_diagnosis_connectors(
                    config,
                    config.get("_execution", {}).get("rating_keys", []),
                )
            )
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Mapping diagnosis failed: {message}", file=sys.stderr)
            return 1
        finally:
            _plex_cache.clear()
            tmdb_response_cache.reset_memory()
        unresolved = sum(
            record.get("status")
            not in {"aligned", "configured_override", "episode_group", "split_series"}
            for record in records
        )
        print(
            f"Mapping diagnosis completed for {len(records)} item(s); "
            f"{unresolved} require review."
        )
        print(f"Report saved to {report}")
        return 0
    if args.capture_replay:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            records, _explanation_report = asyncio.run(
                item_explanation_connectors(
                    config,
                    config.get("_execution", {}).get("rating_keys", []),
                    write_report=False,
                )
            )
            report = write_sanitized_replay_capture(
                records,
                retention=report_retention(config),
            )
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Replay capture failed: {message}", file=sys.stderr)
            return 1
        finally:
            _plex_cache.clear()
            tmdb_response_cache.reset_memory()
        print(f"Sanitized replay captured for {len(records)} item(s).")
        print(f"JSON report saved to {report}")
        return 0
    if args.identity_inspect:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            records, report = asyncio.run(
                identity_inspection_connectors(
                    config,
                    config.get("_execution", {}).get("rating_keys", []),
                )
            )
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Identity inspection failed: {message}", file=sys.stderr)
            return 1
        finally:
            _plex_cache.clear()
            tmdb_response_cache.reset_memory()
        review = sum(
            record.get("status") not in {"accepted"} for record in records
        )
        print(
            f"Identity inspection completed for {len(records)} item(s); "
            f"{review} require review."
        )
        print(f"Report saved to {report}")
        return 0
    if args.explain_item:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            records, report = asyncio.run(
                item_explanation_connectors(
                    config,
                    config.get("_execution", {}).get("rating_keys", []),
                )
            )
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Item explanation failed: {message}", file=sys.stderr)
            return 1
        finally:
            _plex_cache.clear()
            tmdb_response_cache.reset_memory()
        review = sum(record.get("status") != "accepted" for record in records)
        print(
            f"Item explanation completed for {len(records)} item(s); "
            f"{review} require review."
        )
        print(f"Report saved to {report}")
        return 0
    if args.compatibility_check:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            preflight = asyncio.run(connector_preflight(config))
            compatibility = evaluate_compatibility(config, preflight)
            report = write_compatibility_report(
                compatibility,
                retention=report_retention(config),
            )
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Compatibility check failed: {message}", file=sys.stderr)
            return 1
        print(
            f"Compatibility profile {compatibility['profile']} "
            f"{'passed' if compatibility['passed'] else 'failed'}."
        )
        print(f"Report saved to {report}")
        return 0 if compatibility["passed"] else 1
    if args.release_check:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            result = asyncio.run(connector_preflight(config))
            report, passed = write_release_qualification_report(config, result)
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Release qualification failed: {message}", file=sys.stderr)
            return 1
        print(f"Release qualification {'passed' if passed else 'failed'}.")
        print(f"Report saved to {report}")
        return 0 if passed else 1
    if args.preflight:
        try:
            validate_preflight_paths(config, BASE_CONFIG_DIR)
            result = asyncio.run(connector_preflight(config))
        except Exception as error:
            message = redact_secrets(
                error,
                config.get("plex", {}).get("token"),
                config.get("tmdb", {}).get("api_key"),
                fanart_project_api_key(),
            )
            print(f"Preflight failed: {message}", file=sys.stderr)
            return 1
        print("MetaFusion preflight passed.")
        print(f"Plex version: {result['plex_version']}")
        print(f"Configured libraries: {', '.join(result['libraries'])}")
        print(f"Available Plex libraries: {result['available_count']}")
        print(f"Library selection: {result.get('library_discovery', 'explicit')}")
        path_advice = result.get("path_advice", {})
        suggestions = path_advice.get("suggestions", [])
        unresolved = sum(
            record.get("status") == "unresolved"
            for record in path_advice.get("records", [])
        )
        if suggestions:
            print(
                "Suggested PLEX_PATH_MAPPINGS: " + ";".join(suggestions)
            )
        elif path_advice.get("records") and not unresolved:
            print("Plex media paths are visible inside the container.")
        elif unresolved:
            print(
                f"Plex path mapping needs attention for {unresolved} sample(s)."
            )
        if unresolved:
            print(
                "TMDb authentication and storage checks passed; "
                "resolve the Plex path mapping advice above before processing."
            )
        else:
            print("TMDb authentication, mappings, and storage checks passed.")
        return 0
    _shutdown_timeout = max(
        1.0,
        float(config.get("runtime", {}).get("shutdown_timeout", 15.0)),
    )
    validate_runtime_paths(config, BASE_CONFIG_DIR)
    logger = get_setup_logging(config)

    dry_run = config.get("settings", {}).get("dry_run", False)
    default_status_file = (
        f"/tmp/metafusion-status-{os.getpid()}.json"
        if dry_run
        else "/tmp/metafusion-status.json"
    )
    runtime_status = RuntimeStatus(
        os.environ.get("STATUS_FILE", default_status_file),
        state_database=None if dry_run else STATE_DATABASE,
    )
    settings = config.get("settings", {})
    run_times = settings.get("run_times", [])
    schedule_enabled = settings.get("schedule", False)
    metafusion_run = config.get("metafusion_run", True)
    mode = "oneshot" if metafusion_run else "scheduler"

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    exit_code = 0
    status_started = False
    try:
        runtime_status.start(mode)
        status_started = True
        if metafusion_run:
            if not shutdown_requested.is_set():
                log_main_event(
                    "main_force_run",
                    start_time=datetime.now()
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    logger=logger,
                )
                if not run_metafusion_job(config, logger, runtime_status):
                    exit_code = 1
        elif schedule_enabled and run_times:
            run_on_start = settings.get("run_on_start", False)
            if run_on_start and not shutdown_requested.is_set():
                run_metafusion_job(config, logger, runtime_status)
            elif (
                settings.get("schedule_catch_up", True)
                and not shutdown_requested.is_set()
            ):
                try:
                    job_history = recent_job_runs(path=STATE_DATABASE)
                except StateDatabaseError as state_error:
                    logger.warning(
                        "[Scheduler] Unable to read job history for catch-up: %s",
                        state_error,
                    )
                    job_history = []
                missed_slot = missed_schedule_due(
                    run_times,
                    job_history,
                    max_hours=settings.get("schedule_catch_up_max_hours", 24),
                )
                if missed_slot is not None:
                    logger.info(
                        "[Scheduler] Running missed schedule from %s.",
                        missed_slot.isoformat(),
                    )
                    run_metafusion_job(config, logger, runtime_status)
            scheduled_count = 0
            for run_time in run_times:
                try:
                    schedule.every().day.at(run_time).do(
                        run_metafusion_job, config, logger, runtime_status
                    )
                    scheduled_count += 1
                except schedule.ScheduleValueError as error:
                    log_main_event(
                        "main_invalid_schedule_time",
                        run_time=run_time,
                        error=error,
                        logger=logger,
                    )
            if not scheduled_count:
                exit_code = 1
            else:
                runtime_status.idle()
                log_main_event(
                    "main_scheduled_run", run_time=", ".join(run_times), logger=logger
                )
                while not shutdown_requested.is_set():
                    schedule.run_pending()
                    shutdown_requested.wait(30)
        else:
            log_main_event("main_processing_disabled", logger=logger)
    finally:
        try:
            if status_started:
                try:
                    runtime_status.stopping()
                finally:
                    runtime_status.stop()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            shutdown_complete.set()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
