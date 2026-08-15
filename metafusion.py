import sys, asyncio, aiohttp, time, schedule, argparse
from pathlib import Path
from datetime import datetime
from helper.config import load_config_file, get_disabled_features, get_feature_flags
from helper.cache import begin_cache_session, flush_cache
from helper.plex import connect_plex_library, _plex_cache
from helper.tmdb import tmdb_response_cache
from helper.logging import (
    get_setup_logging, get_meta_banner, check_sys_requirements, log_final_summary, log_main_event
)
from modules.processing import process_library, plex_metadata_dict
from modules.cleanup import cleanup_title_orphans

def parse_cli_args():
    parser = argparse.ArgumentParser(description="MetaFusion CLI Command Overrides")
    parser.add_argument("--metafusion_run", action='store_true', help="Run MetaFusion job")
    parser.add_argument("--schedule", action='store_true', help="Enable schedule")
    parser.add_argument("--run_times", type=str, help="Comma-separated run times (e.g. 06:00,18:30)")
    parser.add_argument("--dry_run", action='store_true', help="Dry run mode")
    parser.add_argument("--mode", type=str, choices=["kometa", "plex"], help="Run mode")
    parser.add_argument("--run_basic", action='store_true', help="Run basic metadata extraction")
    parser.add_argument("--run_enhanced", action='store_true', help="Run enhanced metadata extraction")
    parser.add_argument("--run_poster", action='store_true', help="Run poster asset download")
    parser.add_argument("--run_season", action='store_true', help="Run season asset download")
    parser.add_argument("--run_background", action='store_true', help="Run background asset download")
    return parser.parse_args()

def override_config_with_cli(config, args):
    if args.metafusion_run:
        config["metafusion_run"] = args.metafusion_run
    if args.schedule:
        config["settings"]["schedule"] = args.schedule
    if args.run_times is not None:
        config["settings"]["run_times"] = [t.strip() for t in args.run_times.split(",") if t.strip()]
    if args.dry_run:
        config["settings"]["dry_run"] = args.dry_run
    if args.mode is not None:
        config["settings"]["mode"] = args.mode
    if args.run_basic:
        config["metadata"]["run_basic"] = args.run_basic
    if args.run_enhanced:
        config["metadata"]["run_enhanced"] = args.run_enhanced
    if args.run_poster:
        config["assets"]["run_poster"] = args.run_poster
    if args.run_season:
        config["assets"]["run_season"] = args.run_season
    if args.run_background:
        config["assets"]["run_background"] = args.run_background
        
args = parse_cli_args()
config = load_config_file(create_if_missing=not args.dry_run)
override_config_with_cli(config, args)
logger = get_setup_logging(config)

async def metafusion_main():
    _plex_cache.clear()
    plex_metadata_dict.clear()
    tmdb_response_cache.clear()
    get_meta_banner(logger)
    check_sys_requirements(logger, config=config)
    log_main_event(
        "main_started", start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )
    get_disabled_features(config, logger)
    feature_flags = get_feature_flags(config)
    start_time = datetime.now()
    library_item_counts = {}

    async with aiohttp.ClientSession() as session:
        sections, selected_libraries, all_libraries = connect_plex_library(config)
        metadata_summaries = {}
        library_filesize = {}

        if sections:
            for section in sections:
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
        else:
            log_main_event("main_no_libraries")

        orphans_removed = 0
        if feature_flags.get("cleanup", False):
            kometa_root = config.get("settings", {}).get("path", ".")
            asset_path = Path(kometa_root) / "assets"
            orphans_removed = await cleanup_title_orphans(
                config=config, asset_path=asset_path,
                preloaded_plex_metadata=plex_metadata_dict, feature_flags=feature_flags
            )

        end_time = datetime.now()
        elapsed_time = (end_time - start_time).total_seconds()
        log_final_summary(
            logger, elapsed_time, metadata_summaries, library_filesize,
            orphans_removed, cleanup_title_orphans, selected_libraries, all_libraries, config,
            feature_flags
        )
    _plex_cache.clear()
    plex_metadata_dict.clear()
    tmdb_response_cache.clear()

def run_metafusion_job():
    begin_cache_session()
    try:
        asyncio.run(metafusion_main())
        return True
    except Exception as e:
        log_main_event("main_unhandled_exception", error=e)
        return False
    finally:
        flush_cache()

if __name__ == "__main__":
    settings = config.get("settings", {})
    run_times = settings.get("run_times", [])
    schedule_enabled = settings.get("schedule", False)
    metafusion_run = config.get("metafusion_run", True)

    if metafusion_run:
        log_main_event("main_force_run", start_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        if not run_metafusion_job():
            sys.exit(1)
    elif schedule_enabled and run_times:
        scheduled_count = 0
        for t in run_times:
            try:
                schedule.every().day.at(t).do(run_metafusion_job)
                scheduled_count += 1
            except schedule.ScheduleValueError as error:
                log_main_event("main_invalid_schedule_time", run_time=t, error=error)
        if not scheduled_count:
            sys.exit(1)
        log_main_event("main_scheduled_run", run_time=', '.join(run_times))
        while True:
            schedule.run_pending()
            time.sleep(30)
    else:
        log_main_event("main_processing_disabled", logger=logger)
