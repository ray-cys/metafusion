import argparse
import asyncio
import os
import schedule
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path

import aiohttp

from helper.cache import begin_cache_session, flush_cache
from helper.config import (
    BASE_CONFIG_DIR,
    get_disabled_features,
    get_feature_flags,
    load_config_file,
)
from helper.logging import (
    check_sys_requirements,
    get_meta_banner,
    get_setup_logging,
    log_final_summary,
    log_main_event,
    redact_secrets,
)
from helper.plex import _plex_cache, connect_plex_library
from helper.runtime import RuntimeStatus, validate_runtime_paths
from helper.tmdb import tmdb_response_cache
from modules.cleanup import cleanup_title_orphans
from modules.processing import process_library, plex_metadata_dict


shutdown_requested = threading.Event()
_active_loop = None
_active_task = None


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
    tmdb_response_cache.clear()
    try:
        get_meta_banner(logger)
        check_sys_requirements(logger, config=config)
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
            sections, selected_libraries, all_libraries = connect_plex_library(config)
            metadata_summaries = {}
            library_filesize = {}
            successful_sections = []
            failures = []

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
                for section in sections:
                    try:
                        await process_library(
                            library_section=section,
                            config=config,
                            library_item_counts=library_item_counts,
                            metadata_summaries=metadata_summaries,
                            library_filesize=library_filesize,
                            season_cache={},
                            episode_cache={},
                            movie_cache={},
                            session=session,
                            feature_flags=feature_flags,
                        )
                        successful_sections.append(section)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        failures.append(f"{section.title}: {error}")

            orphans_removed = 0
            if feature_flags.get("cleanup", False):
                safe_library_types = set()
                if not failures:
                    safe_library_types = complete_inventory_types(
                        all_libraries, successful_sections
                    )
                kometa_root = config.get("settings", {}).get("path", ".")
                asset_path = Path(kometa_root) / "assets"
                orphans_removed = await cleanup_title_orphans(
                    config=config,
                    asset_path=asset_path,
                    preloaded_plex_metadata=plex_metadata_dict,
                    feature_flags=feature_flags,
                    safe_library_types=safe_library_types,
                )

            elapsed_time = (datetime.now() - start_time).total_seconds()
            log_final_summary(
                logger,
                elapsed_time,
                metadata_summaries,
                library_filesize,
                orphans_removed,
                cleanup_title_orphans,
                selected_libraries,
                all_libraries,
                config,
                feature_flags,
            )
            if failures:
                raise RuntimeError("; ".join(failures))
    finally:
        _plex_cache.clear()
        plex_metadata_dict.clear()
        tmdb_response_cache.clear()


def _cancel_active_job():
    if _active_task is not None and not _active_task.done():
        _active_task.cancel()


def request_shutdown(_signum=None, _frame=None):
    shutdown_requested.set()
    if _active_loop is not None:
        _active_loop.call_soon_threadsafe(_cancel_active_job)


def run_metafusion_job(config, logger, runtime_status=None):
    global _active_loop, _active_task
    begin_cache_session()
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
        except Exception as caught:
            success = False
            error = f"Failed to flush cache: {caught}"
            log_main_event("main_unhandled_exception", error=error, logger=logger)
        if runtime_status:
            runtime_status.run_finished(success, error=error)
    return success


def main(argv=None):
    shutdown_requested.clear()
    args = parse_cli_args(argv)
    config = load_config_file(create_if_missing=not args.dry_run)
    override_config_with_cli(config, args)
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
    runtime_status.start(mode)

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    exit_code = 0
    try:
        if metafusion_run:
            log_main_event(
                "main_force_run",
                start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                logger=logger,
            )
            if not run_metafusion_job(config, logger, runtime_status):
                exit_code = 1
        elif schedule_enabled and run_times:
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
        runtime_status.stopping()
        runtime_status.stop()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
