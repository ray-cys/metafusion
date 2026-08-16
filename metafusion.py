import argparse
import asyncio
import json
import logging
import os
import schedule
import signal
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import aiohttp

from helper.cache import begin_cache_session, flush_cache
from helper.config import (
    BASE_CONFIG_DIR,
    ConfigError,
    config_for_library,
    config_source_report,
    get_disabled_features,
    get_feature_flags,
    load_config_file,
    mode_check,
    validate_config,
)
from helper.incremental import (
    config_fingerprint,
    mark_full_scan_complete,
    should_run_full_scan,
)
from helper.logging import (
    check_sys_requirements,
    get_meta_banner,
    get_setup_logging,
    log_cleanup_event,
    log_final_summary,
    log_main_event,
    redact_secrets,
)
from helper.plex import (
    _plex_cache,
    connect_plex_library,
    connect_plex_server,
    plex_operation,
)
from helper.runtime import RuntimeStatus, validate_runtime_paths
from helper.tmdb import (
    begin_tmdb_cache,
    flush_tmdb_cache,
    tmdb_api_request,
    tmdb_response_cache,
)
from modules.cleanup import CleanupResult, cleanup_title_orphans
from modules.processing import process_library, plex_metadata_dict


shutdown_requested = threading.Event()
shutdown_complete = threading.Event()
_active_loop = None
_active_task = None
_shutdown_timeout = 15.0


def parse_cli_args(argv=None):
    parser = argparse.ArgumentParser(description="MetaFusion CLI Command Overrides")
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
        "--metadata-only",
        action="store_true",
        help="Generate metadata without artwork or cleanup",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Bypass incremental skipping and reconcile the selected libraries",
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
    if args.metadata_only:
        config["assets"].update(
            {"run_poster": False, "run_season": False, "run_background": False}
        )
        config["cleanup"]["run_process"] = False
    if args.asset_only:
        config["metadata"].update({"run_basic": False, "run_enhanced": False})
        config["cleanup"]["run_process"] = False
    if args.explain_selection:
        config["settings"]["dry_run"] = True
        config["cleanup"]["run_process"] = False
    if libraries or rating_keys:
        config["cleanup"]["run_process"] = False
    config["_execution"] = {
        "rating_keys": rating_keys,
        "targeted": bool(libraries or rating_keys),
        "full_scan": bool(args.full_scan),
        "metadata_only": bool(args.metadata_only),
        "asset_only": bool(args.asset_only),
        "explain_selection": bool(args.explain_selection),
    }


async def preflight_connectors(config, session):
    plex_task = asyncio.create_task(asyncio.to_thread(connect_plex_server, config))
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
        raise RuntimeError("Plex connector preflight failed") from plex
    if isinstance(tmdb_result, Exception) or not tmdb_result:
        if isinstance(tmdb_result, Exception):
            raise RuntimeError("TMDb connector preflight failed") from tmdb_result
        raise RuntimeError("TMDb connector preflight returned no configuration")
    return plex


def normalize_library_type(value):
    library_type = (value or "").lower()
    if library_type == "movies":
        return "movie"
    if library_type in {"show", "shows"}:
        return "tv"
    return library_type


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


async def metafusion_main(config, logger):
    _plex_cache.clear()
    plex_metadata_dict.clear()
    try:
        get_meta_banner(logger)
        check_sys_requirements(logger, config=config, check_network=False)
        log_main_event(
            "main_started", start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        get_disabled_features(config, logger)
        feature_flags = get_feature_flags(config)
        start_time = datetime.now()
        library_item_counts = {}
        runtime_config = config.get("runtime", {})
        max_concurrency = max(1, int(runtime_config.get("max_concurrency", 8)))
        timeout = aiohttp.ClientTimeout(
            total=max(1.0, float(runtime_config.get("request_timeout", 30.0))),
            connect=max(1.0, float(runtime_config.get("connect_timeout", 10.0))),
        )
        connector = aiohttp.TCPConnector(
            limit=max_concurrency * 4,
            limit_per_host=max_concurrency * 4,
        )

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            plex = await preflight_connectors(config, session)
            sections, selected_libraries, all_libraries = connect_plex_library(
                config,
                plex=plex,
            )
            metadata_summaries = {}
            library_filesize = {}
            successful_sections = []
            failures = []
            execution = config.get("_execution", {})
            targeted = bool(execution.get("targeted"))
            full_scan = bool(execution.get("full_scan")) or should_run_full_scan(
                config,
                targeted=targeted,
            )
            run_feature_flags = dict(feature_flags)
            cleanup_skip_reason = None

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
                if mode_check(config, "kometa") and not full_scan:
                    metadata_dir = Path(config.get("settings", {}).get("path", ".")) / "metadata"
                    expected_outputs = {
                        metadata_dir
                        / (
                            "movie_metadata.yml"
                            if normalize_library_type(
                                getattr(section, "type", None)
                                or getattr(section, "TYPE", None)
                            )
                            == "movie"
                            else "tv_metadata.yml"
                        )
                        for section in sections
                    }
                    if any(not output.exists() for output in expected_outputs):
                        full_scan = True
                if targeted and feature_flags.get("cleanup", False):
                    cleanup_skip_reason = (
                        "targeted run; full reconciliation requires every configured "
                        "library"
                    )
                    run_feature_flags["cleanup"] = False
                elif not full_scan:
                    if feature_flags.get("cleanup", False):
                        cleanup_skip_reason = (
                            "incremental run; full reconciliation required"
                        )
                    run_feature_flags["cleanup"] = False
                section_items = {}
                identity_counts = Counter()
                edition_counts = Counter()
                for section in sections:
                    try:
                        inventory = await plex_operation(
                            lambda current=section: list(current.all()),
                            runtime_config,
                            description=f"List library {section.title}",
                        )
                        section_items[section.title] = inventory
                        for item in inventory:
                            media_type = normalize_library_type(
                                getattr(item, "type", None)
                            )
                            title = getattr(item, "title", None)
                            year = getattr(item, "year", None)
                            identity_counts[(media_type, title, year)] += 1
                            if media_type == "movie":
                                edition = getattr(item, "editionTitle", None) or getattr(
                                    item, "edition", None
                                )
                                edition_counts[(title, year, edition)] += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append(f"{section.title}: {error}")

                for section in sections:
                    if section.title not in section_items:
                        continue
                    try:
                        library_config = config_for_library(config, section.title)
                        library_flags = dict(get_feature_flags(library_config))
                        if not full_scan:
                            library_flags["cleanup"] = False
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
                            full_scan=full_scan,
                            rating_keys=execution.get("rating_keys"),
                            incremental_fingerprint=config_fingerprint(library_config),
                            all_items=section_items[section.title],
                            global_identity_counts=identity_counts,
                            global_edition_counts=edition_counts,
                            explain_selection=bool(execution.get("explain_selection")),
                        )
                        successful_sections.append(section)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append(f"{section.title}: {error}")

            cleanup_result = CleanupResult()
            if run_feature_flags.get("cleanup", False):
                safe_library_types = set()
                if not failures:
                    safe_library_types = complete_inventory_types(
                        all_libraries, successful_sections
                    )
                kometa_root = config.get("settings", {}).get("path", ".")
                asset_path = Path(kometa_root) / "assets"
                cleanup_result = await cleanup_title_orphans(
                    config=config,
                    asset_path=asset_path,
                    preloaded_plex_metadata=plex_metadata_dict,
                    feature_flags=run_feature_flags,
                    safe_library_types=safe_library_types,
                )
            elif cleanup_skip_reason:
                cleanup_result.skipped_reason = cleanup_skip_reason
                log_cleanup_event(
                    "cleanup_skipped_run_scope", reason=cleanup_skip_reason
                )

            elapsed_time = (datetime.now() - start_time).total_seconds()
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
            if full_scan and not targeted:
                mark_full_scan_complete(
                    dry_run=feature_flags.get("dry_run", False),
                )
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
    begin_cache_session()
    begin_tmdb_cache(config)
    if runtime_status:
        runtime_status.run_started()

    success = False
    error = None

    async def run_active_job():
        global _active_loop, _active_task
        _active_loop = asyncio.get_running_loop()
        _active_task = asyncio.current_task()
        await metafusion_main(config, logger)

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
        )
        log_main_event("main_unhandled_exception", error=error, logger=logger)
    finally:
        _active_loop = None
        _active_task = None
        try:
            flush_cache()
            flush_tmdb_cache()
        except Exception as caught:
            success = False
            error = f"Failed to flush persistent cache: {caught}"
            log_main_event("main_unhandled_exception", error=error, logger=logger)
        finally:
            tmdb_response_cache.reset_memory()
        if runtime_status:
            runtime_status.run_finished(success, error=error)
    return success


def main(argv=None):
    global _shutdown_timeout
    shutdown_requested.clear()
    shutdown_complete.clear()
    args = parse_cli_args(argv)
    if args.metadata_only and args.asset_only:
        print("Configuration error: --metadata-only and --asset-only cannot be combined", file=sys.stderr)
        return 2
    if args.status:
        status_path = Path(
            os.environ.get("STATUS_FILE", str(BASE_CONFIG_DIR / "metafusion-status.json"))
        )
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            print(f"Unable to read runtime status: {error}", file=sys.stderr)
            return 1
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    try:
        config, sources = load_config_file(
            create_if_missing=not (args.dry_run or args.doctor),
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
        ("cleanup", "run_process"): args.metadata_only or args.asset_only or bool(args.rating_key),
        ("plex_libraries",): bool(args.library),
    }
    for path, used in cli_sources.items():
        if used:
            sources[path] = "CLI"

    validation_errors = validate_config(config)
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
        else str(BASE_CONFIG_DIR / "metafusion-status.json")
    )
    runtime_status = RuntimeStatus(os.environ.get("STATUS_FILE", default_status_file))
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
                    start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    logger=logger,
                )
                if not run_metafusion_job(config, logger, runtime_status):
                    exit_code = 1
        elif schedule_enabled and run_times:
            if settings.get("run_on_start", False) and not shutdown_requested.is_set():
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
