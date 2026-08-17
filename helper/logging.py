import os, sys, platform, psutil, logging, textwrap, requests, datetime, time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

BASE_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
LOGS_DIR = BASE_CONFIG_DIR / "logs"
LOG_FILE = LOGS_DIR / "metafusion.log"
MIN_PYTHON = (3, 10)
MIN_CPU_CORES = 4
MIN_RAM_GB = 4


def redact_secrets(value, *secrets):
    redacted = str(value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(str(secret), "***")
    return redacted


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record):
        record.msg = redact_secrets(record.getMessage(), *self.secrets)
        record.args = ()
        return True

def get_setup_logging(config):
    log_file = LOG_FILE
    dry_run = config.get("settings", {}).get("dry_run", False)

    log_level_str = config["settings"].get("log_level", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logger = logging.getLogger()
    logger.setLevel(log_level)

    if logger.hasHandlers():
        logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    secret_filter = SecretRedactionFilter(
        (
            config.get("plex", {}).get("token"),
            config.get("tmdb", {}).get("api_key"),
        )
    )
    console_handler.addFilter(secret_filter)

    if not dry_run:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_file, when="midnight", interval=1, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

def get_meta_banner(logger=None):
    width = 80
    border = "=" * width
    title = " ".join("METAFUSION").center(width - 6)
    centered = f"| {title} |"
    lines = [
        border,
        centered,
        border,
    ]
    if logger:
        for line in lines:
            logger.info(line)
    else:
        for line in lines:
            print(line)

def check_sys_requirements(logger, config, check_network=True):
    os_info = f"{platform.system()} {platform.release()}"
    py_version = sys.version_info
    cpu_cores = os.cpu_count()
    mem = psutil.virtual_memory()
    total_gb = mem.total / (1024 ** 3)
    used_gb = mem.used / (1024 ** 3)
    free_gb = mem.available / (1024 ** 3)
    cpu_percent = psutil.cpu_percent(interval=None)

    box_width = 80
    lines = []
    header = "=" * box_width
    title = "SYSTEM CONFIGURATION"
    lines.append(header)
    lines.append(f"| {title.center(box_width - 4)} |")
    lines.append(header)

    def box_line(text, width=box_width):
        import textwrap
        wrapped = textwrap.wrap(text, width=width - 4)
        return [f"| {line.ljust(width - 4)} |" for line in wrapped]

    lines.extend(box_line(f"[System] Operating System detected: {os_info}", box_width))
    if py_version < MIN_PYTHON:
        lines.extend(box_line(f"[System] Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required. Detected: {platform.python_version()}. Exiting.", box_width))
        for line in lines:
            logger.error(line)
        raise RuntimeError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required")
    else:
        lines.extend(box_line(f"[System] Python version detected: {platform.python_version()}", box_width))

    if cpu_cores is not None and cpu_cores < MIN_CPU_CORES:
        lines.extend(box_line(f"[System] {cpu_cores} CPU cores detected; {MIN_CPU_CORES}+ is recommended for best performance.", box_width))
    else:
        lines.extend(box_line(f"[System] CPU Cores detected: {cpu_cores} (Usage: {cpu_percent}%)", box_width))

    if total_gb < MIN_RAM_GB:
        lines.extend(box_line(f"[System] {total_gb:.2f} GB RAM detected; {MIN_RAM_GB} GB is recommended for large libraries.", box_width))
    else:
        lines.extend(box_line(f"[System] RAM Memory detected: {total_gb:.2f} GB (Used: {used_gb:.2f} GB, Free: {free_gb:.2f} GB)", box_width))

    if not check_network:
        lines.append(header)
        for line in lines:
            logger.info(line)
        return True

    plex_url = config.get('plex', {}).get('url')
    plex_token = config.get('plex', {}).get('token')
    internal_up = False
    invalid_plex_tokens = {"PLEX_TOKEN", "YOUR_PLEX_TOKEN"}
    if plex_url and plex_token and plex_token not in invalid_plex_tokens:
        try:
            resp = requests.get(
                plex_url,
                headers={"X-Plex-Token": plex_token},
                timeout=2,
            )
            internal_up = resp.status_code in (200, 401)
            if internal_up:
                lines.extend(box_line("[Network] Plex Media Server connection: UP", box_width))
            else:
                lines.extend(box_line("[Network] Plex Media Server connection: DOWN", box_width))
        except Exception as e:
            safe_error = redact_secrets(e, plex_token)
            lines.extend(box_line(f"[Network] Plex Media Server connection check failed: {safe_error}", box_width))
    else:
        lines.extend(box_line("[Network] Plex Media Server URL or token not set. Check configuration...", box_width))

    tmdb_api_key = config.get('tmdb', {}).get('api_key')
    tmdb_up = False
    invalid_tmdb_keys = {"TMDB_API_KEY", "YOUR_TMDB_API_KEY"}
    if tmdb_api_key and tmdb_api_key not in invalid_tmdb_keys:
        try:
            resp = requests.get(
                "https://api.themoviedb.org/3/configuration",
                params={"api_key": tmdb_api_key},
                timeout=3,
            )
            tmdb_up = resp.status_code == 200
            if tmdb_up:
                lines.extend(box_line("[Network] TMDb API connection: UP", box_width))
            else:
                lines.extend(box_line("[Network] TMDb API connection: DOWN", box_width))
        except Exception as e:
            safe_error = redact_secrets(e, tmdb_api_key)
            lines.extend(box_line(f"[Network] TMDb API connection check failed: {safe_error}", box_width))
    else:
        lines.extend(box_line("[Network] TMDb API key not set in config.", box_width))

    lines.append(header)
    for line in lines:
        logger.info(line)

    if not internal_up or not tmdb_up:
        logger.warning(
            "[System] One or more external services are unavailable; "
            "the run will continue and normal request retries will apply."
        )
    return internal_up and tmdb_up

def log_main_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "main_started": "[MetaFusion] Processing started on {start_time}",
        "main_force_run": "[MetaFusion] Force run started on {start_time}",
        "main_processing_disabled": "[MetaFusion] Processing is set to False. Exiting without changes.",
        "main_no_libraries": "[MetaFusion] No libraries scheduled for processing.",
        "main_unhandled_exception": "[MetaFusion] Unhandled exception: {error}",
        "main_scheduled_run": "[MetaFusion] Scheduled run at {run_time}",
        "main_invalid_schedule_time": "[MetaFusion] Invalid schedule time '{run_time}': {error}",
        "main_shutdown_requested": "[MetaFusion] Shutdown requested; stopping safely.",
        "main_job_already_running": "[MetaFusion] Job skipped: {error}",
    }
    levels = {
        "main_started": "info",
        "main_force_run": "info",
        "main_processing_disabled": "info",
        "main_no_libraries": "info",
        "main_unhandled_exception": "error",
        "main_scheduled_run": "info",
        "main_invalid_schedule_time": "error",
        "main_shutdown_requested": "warning",
        "main_job_already_running": "warning",
    }
    msg = messages.get(event, "[MetaFusion] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if event == "main_scheduled_run":
        print(msg)
        return
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_config_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "invalid_env_var": "[Configuration] Invalid environment variable for {key}: '{value}'. Using default: {default}",
        "feature_disabled": "[Configuration] {feature} is DISABLED and will not be processed.",
        "feature_enabled": "[Configuration] {feature} is ENABLED and will be processed.",
        "unknown_feature": "[Configuration] Unknown configuration settings: {feature}",
        "unknown_key": "[Configuration] Unknown configuration key: {key}",
        "yaml_not_found": "[Configuration] YAML not found at {config_file}. Copying template to {config_file}...",
        "yaml_missing": "[Configuration] YAMLs not found at {config_file}. Using default...",
        "yaml_parse_error": "[Configuration] Failed to parse YAML at {config_file}. Using default...",
        "config_missing": "[Configuration] Config file {config_file} does not exist. Using default...",
        "config_loaded": "[Configuration] Successfully loaded configuration from {config_file}.",
    }
    levels = {
        "invalid_env_var": "error",
        "feature_disabled": "info",
        "feature_enabled": "info",
        "unknown_feature": "warning",
        "unknown_key": "warning",
        "yaml_not_found": "warning",
        "yaml_missing": "error",
        "yaml_parse_error": "error",
        "config_missing": "warning",
        "config_loaded": "debug",
    }
    msg = messages.get(event, "[Config] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_cache_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "cache_loaded": "[Cache] Loaded {count} entries from {cache_file}",
        "cache_empty": "[Cache] No cache file found at {cache_file}, starting with empty cache.",
        "cache_load_failed": "[Cache] Failed to load {cache_file}: {error}. Starting with an empty cache.",
        "cache_saved": "[Cache] Saved {count} entries to {cache_file}",
        "cache_updated": "[Cache] Updated cache for key '{cache_key}' ({media_type}): {title} ({year})",
    }
    levels = {
        "cache_loaded": "debug",
        "cache_empty": "debug",
        "cache_load_failed": "error",
        "cache_saved": "debug",
        "cache_updated": "debug",
    }
    msg = messages.get(event, "[Cache] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_plex_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "plex_connected": "[Plex] Successfully connected to server version: {version}.",
        "plex_connect_failed": "[Plex] Failed to connect to server: {error}",
        "plex_libraries_retrieved_failed": "[Plex] Failed to retrieve libraries: {error}",
        "plex_detected_and_skipped_libraries": "[Plex] Libraries - Detected [ {detected} ] Skipped [ {skipped} ]",
        "plex_no_libraries_found": "[Plex] No libraries found. Exiting.",
        "plex_failed_extract_item_id": "[Plex] Failed to extract item ID for {title} ({year}): {error}",
        "plex_failed_extract_library_type": "[Plex] Failed to extract library type for {library_name}: {error}",
        "plex_failed_extract_ids": "[Plex] Failed to extract TMDb, IMDb, TVDb IDs for {title} ({year}): {error}",
        "plex_missing_ids": "[Plex] Missing IDs for {title} ({year}): {missing_ids}. Extracted: {found_ids}",
        "plex_failed_extract_movie_path": "[Plex] Failed to extract movie path for {title} ({year}): {error}",
        "plex_failed_extract_show_path": "[Plex] Failed to extract show path for {title} ({year}): {error}",
        "plex_failed_extract_seasons_episodes": "[Plex] Failed to extract seasons/episodes for {title} ({year}): {error}",
        "plex_operation_failed": "[Plex] {description} failed (attempt {attempt}/{retries}): {error}",
        "plex_critical_metadata_missing": "[Plex] Critical metadata missing for item [ratingKey={item_key}]: {missing_critical}. Extracted: {result}",
    }
    levels = {
        "plex_connected": "info",
        "plex_connect_failed": "error",
        "plex_libraries_retrieved_failed": "error",
        "plex_detected_and_skipped_libraries": "info",
        "plex_no_libraries_found": "warning",
        "plex_failed_extract_item_id": "warning",
        "plex_failed_extract_library_type": "warning",
        "plex_failed_extract_ids": "warning",
        "plex_missing_ids": "debug",
        "plex_failed_extract_movie_path": "warning",
        "plex_failed_extract_show_path": "warning",
        "plex_failed_extract_seasons_episodes": "warning",
        "plex_operation_failed": "warning",
        "plex_critical_metadata_missing": "warning",
    }
    msg = messages.get(event, "[Plex] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_tmdb_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "tmdb_no_api_key": "[TMDb] No API key found in config: {tmdb_config}",
        "tmdb_cache_hit": "[TMDb] Returning cached response for {url} params: {params}",
        "tmdb_request": "[TMDb] Requesting {url} with params: {query} (Attempt {attempt}/{retries})",
        "tmdb_success": "[TMDb] Successful response for {url} (Attempt {attempt})",
        "tmdb_rate_limited": "[TMDb] Rate limited (HTTP 429). Sleeping {retry_after}s before retry... Params: {query}",
        "tmdb_non_200": "[TMDb] Non-200 response {status} for {url} params: {query} body: {body}",
        "tmdb_response_too_large": "[TMDb] Response rejected for {url} params {query}: {error}",
        "tmdb_request_failed": "[TMDb] Attempt {attempt}: Request failed for URL {url} with params {query}: {error}",
        "tmdb_retrying": "[TMDb] Retrying in {sleep_time}s... (Attempt {next_attempt}/{retries})",
        "tmdb_failed": "[TMDb] Failed after {retries} attempts for {url} with params {query}",
        "tmdb_cache_stats": "[TMDb] SQLite cache entries: {entries}, compressed: {stored_mib:.1f} MiB, disk: {disk_mib:.1f} MiB, hits: {hits}, misses: {misses}, evictions: {evictions}, recoveries: {recoveries}",
        "tmdb_cache_degraded": "[TMDb] Persistent cache is degraded; continuing with memory cache: {error}",
    }
    levels = {
        "tmdb_no_api_key": "error",
        "tmdb_cache_hit": "debug",
        "tmdb_request": "debug",
        "tmdb_success": "debug",
        "tmdb_rate_limited": "warning",
        "tmdb_non_200": "warning",
        "tmdb_response_too_large": "error",
        "tmdb_request_failed": "warning",
        "tmdb_retrying": "info",
        "tmdb_failed": "error",
        "tmdb_cache_stats": "debug",
        "tmdb_cache_degraded": "warning",
    }
    msg = messages.get(event, "[TMDb] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)
        
def log_processing_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "processing_no_item": "[Processing] No item found. Skipping...",
        "processing_unsupported_type": "[Processing] Unsupported library type for {full_title}. Skipping...",
        "processing_failed_item": "[Processing] Failed to process {full_title}: {error}",
        "processing_library_items": "[Processing] {library_name} library with {total_items} items detected.",
        "processing_failed_metadata": "[Processing] Failed to process {media_type} for {title} ({year}): {error}",
        "processing_failed_parse_yaml": "[Processing] Failed to parse YAML file: {output_path} ({error})",
        "processing_metadata_saved": "[Processing] YAML successfully saved to {output_path}",
        "processing_cache_saved": "[Processing] Cache files saved.",
        "processing_failed_write_metadata": "[Processing] Failed to write YAML: {error}",
        "processing_metadata_dry_run": "[Dry Run] Metadata for {library_name} generated but not saved.",
        "processing_failed_library": "[Processing] Failed to process library '{library_name}': {error}",
        "processing_selection_reason": "[Processing] Selected {title} (rating key {rating_key}) from {library_name}: {reasons}",
        "processing_ambiguous_editions": "[Processing] Unsafe duplicate editions in '{library_name}': {description}",
        "processing_ambiguous_editions_allowed": "[Processing] Ambiguous editions allowed in '{library_name}': {description}",
    }
    levels = {
        "processing_no_item": "warning",
        "processing_unsupported_type": "warning",
        "processing_failed_item": "error",
        "processing_library_items": "info",
        "processing_failed_metadata": "error",
        "processing_failed_parse_yaml": "error",
        "processing_metadata_saved": "info",
        "processing_cache_saved": "debug",
        "processing_failed_write_metadata": "error",
        "processing_metadata_dry_run": "info",
        "processing_failed_library": "error",
        "processing_selection_reason": "info",
        "processing_ambiguous_editions": "error",
        "processing_ambiguous_editions_allowed": "warning",
    }
    msg = messages.get(event, "[Processing] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_builder_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "builder_missing_tmdb_and_imdb_id": "[{media_type}] Missing TMDb or IMDb ID: {full_title}. Skipping...",
        "builder_missing_tvdb_id_and_tmdb_id": "[{media_type}] Missing TVDb and TMDb ID: {full_title}. Skipping...",
        "builder_missing_tvdb_id_and_imdb_id": "[{media_type}] Missing TVDb and IMDb ID: {full_title}. Skipping...",
        "builder_no_tmdb_id": "[{media_type}] No TMDb identity could be resolved for {full_title}. Skipping...",
        "builder_invalid_tmdb_id": "[{media_type}] TMDb returned no data for {full_title}. Skipping...",
        "builder_tmdb_id_recovered": "[{media_type}] Recovered stale TMDb identity for {full_title}: {old_id} -> {new_id}.",
        "builder_tmdb_identity_mismatch": "[{media_type}] Rejected TMDb identity for {full_title}: {reason}.",
        "builder_tmdb_identity_warning": "[{media_type}] TMDb identity warning for {full_title}: {reason}.",
        "builder_tmdb_identity_alias": "[{media_type}] Accepted TMDb identity alias for {full_title}: {reason}.",
        "builder_no_tmdb_season_data": "[{media_type}] Missing TMDb data: {full_title} of Season {season_number}. Skipping...",
        "builder_episode_group_fallback": "[{media_type}] Resolved alternate episode ordering for {full_title} with TMDb group {group_id}.",
        "builder_split_series_mapping": "[{media_type}] Applied cross-provider season mapping for {full_title}: Plex season(s) {seasons}.",
        "builder_split_series_show_preserved": "[{media_type}] Preserving top-level metadata and artwork for split series {full_title}; mapped season and episode updates remain enabled.",
        "builder_episode_overrides": "[{media_type}] Applied {count} configured episode-number override(s) for {full_title}.",
        "builder_episode_metadata_pending": "[{media_type}] TMDb metadata is not available yet for {count} Plex episode(s) in {full_title} ({episodes}); existing metadata is preserved.",
        "builder_episode_order_unresolved": "[{media_type}] Could not safely map {count} Plex episode(s) for {full_title} ({episodes}); existing metadata is preserved.",
        "builder_metadata_diagnostics": "[{media_type}] Metadata diagnostics for {full_title}: {diagnostics}",
        "builder_no_metadata_changes": "[Kometa Metadata] No changes for {media_type}: {full_title}. Completeness: {percent}% present, {incomplete_percent}% missing.",
        "build_metadata_changed": "[Kometa Metadata] Updated {media_type} entry prepared: {full_title} ({percent}% complete), TMDb ID: {tmdb_id}, {changes}",
        "builder_no_existing_metadata": "[Kometa Metadata] New {media_type} entry prepared: {full_title}, TMDb ID: {tmdb_id}.",
        "builder_plex_candidate_ready": "[Plex Metadata] Prepared TMDb candidate for {full_title}. Completeness: {percent}% present, {incomplete_percent}% missing.",
        "builder_dry_run_metadata": "[Dry Run] Would build metadata for {media_type}: {full_title}",
        "builder_metadata_cached": "[{media_type}] {full_title} cached as {cache_key}...",
        "builder_dry_run_asset": "[Dry Run] Would build {asset_type} asset for {media_type}: {full_title}",
        "builder_dry_run_asset_selected": "[Dry Run] Selected TMDb {asset_type} for {media_type}: {full_title} ({source_path})",
        "builder_artwork_language_fallback": "[{media_type}] Selected unrestricted-language TMDb {asset_type} for {full_title}: {language}.",
        "builder_reusing_shared_asset": "[{media_type}] Reusing shared TMDb {asset_type} for {full_title} at {destination}.",
        "builder_asset_ownership_adopted": "[{media_type}] Adopted existing {asset_type} ownership for {full_title} at {destination}; exact TMDb source {source_path} matched without rewriting the file.",
        "builder_preserving_existing_asset": "[{media_type}] Preserving existing {asset_type} for {full_title} at {destination}: {reason}.",
        "builder_asset_destination_collision": "[{media_type}] Refusing {asset_type} destination collision for {full_title} at {destination}; already claimed by {owner}.",
        "builder_no_asset_path": "[{media_type}] Asset path could not be determined: {full_title} {extra}. Skipping...",
        "builder_no_suitable_asset": "[{media_type}] No suitable TMDb {asset_type} found: {full_title} {extra}. Skipping...",
        "builder_downloading_asset": "[{media_type}] Downloading TMDb {asset_type}: {full_title} ({filesize})...",
        "builder_asset_download_failed": "[{media_type}] Downloading TMDb {asset_type} failed: {full_title} (Status: {status}) Error: {error}",
        "builder_asset_upgraded": "[{media_type}] Upgrading TMDb {asset_type}: {full_title} ({filesize}), {reason}",
        "builder_force_upgrade_stale": "[{media_type}] Force upgrade due to stale image: {full_title} ({filesize}), Last upgraded: {last_upgraded} on {stale_days} days ago",
        "builder_already_up_to_date": "[{media_type}] No {asset_type} changes detected: {full_title} ({filesize}). Skipping...",
        "builder_no_upgrade_needed": "[{media_type}] No {asset_type} changes detected: {full_title} ({filesize}). Skipping...",
        "builder_stale_candidate_downgrade": "[{media_type}] Preserving higher-quality {asset_type} for {full_title}; the stale replacement candidate is lower quality.",
        "builder_no_image_for_compare": "[{media_type}] No image comparison: {full_title} {extra}. Skipping...",
        "builder_error_image_compare": "[{media_type}] Failed to compare temp image checksum: {full_title} {extra}, {error}",
        "builder_dry_run_asset_season": "[Dry Run] Would build {asset_type} asset for {media_type} Season {season_number}: {full_title}",
        "builder_no_asset_path_season": "[{media_type}] Asset path could not be determined: {full_title} Season {season_number}. Skipping...",
        "builder_no_season_details": "[{media_type}] No season details in library: {full_title} Season {season_number}. Skipping...",
        "builder_no_suitable_asset_season": "[{media_type}] No suitable TMDb season {asset_type} found: {full_title} Season {season_number}. Skipping...",
        "builder_downloading_asset_season": "[{media_type}] Downloading TMDb season {asset_type}: {full_title} Season {season_number} ({filesize})...",
        "builder_asset_download_failed_season": "[{media_type}] Downloading TMDb season {asset_type} failed: {full_title} Season {season_number} (Status: {status}) Error: {error}",
        "builder_asset_upgraded_season": "[{media_type}] Upgrading TMDb season {asset_type}: {full_title} Season {season_number} ({filesize}), {reason}",
        "builder_force_upgrade_stale_season": "[{media_type}] Force upgrade due to stale image: {full_title} Season {season_number} ({filesize}), Last upgraded: {last_upgraded} on {stale_days} days ago",
        "builder_already_up_to_date_season": "[{media_type}] No season {asset_type} changes detected: {full_title} Season {season_number} ({filesize}). Skipping...",
        "builder_no_upgrade_needed_season": "[{media_type}] No season {asset_type} changes detected: {full_title} Season {season_number} ({filesize}). Skipping...",
        "builder_stale_candidate_downgrade_season": "[{media_type}] Preserving higher-quality season {asset_type} for {full_title} Season {season_number}; the stale replacement candidate is lower quality.",
        "builder_no_image_for_compare_season": "[{media_type}] No image comparison: {full_title} Season {season_number}. Skipping...",
        "builder_error_image_compare_season": "[{media_type}] Failed to compare temp image checksum: {full_title} Season {season_number}: {error}",
    }
    levels = {
        "builder_missing_tmdb_and_imdb_id": "warning",
        "builder_missing_tvdb_id_and_tmdb_id": "warning",
        "builder_missing_tvdb_id_and_imdb_id": "warning",
        "builder_no_tmdb_id": "warning",
        "builder_invalid_tmdb_id": "warning",
        "builder_tmdb_id_recovered": "warning",
        "builder_tmdb_identity_mismatch": "error",
        "builder_tmdb_identity_warning": "warning",
        "builder_no_tmdb_season_data": "warning",
        "builder_episode_group_fallback": "info",
        "builder_episode_order_unresolved": "warning",
        "builder_metadata_diagnostics": "debug",
        "builder_no_metadata_changes": "debug",
        "builder_no_existing_metadata": "info",
        "build_metadata_changed": "info",
        "builder_plex_candidate_ready": "debug",
        "builder_dry_run_metadata": "info",
        "builder_metadata_cached": "debug",
        "builder_dry_run_asset": "info",
        "builder_dry_run_asset_selected": "info",
        "builder_artwork_language_fallback": "info",
        "builder_reusing_shared_asset": "info",
        "builder_asset_ownership_adopted": "info",
        "builder_preserving_existing_asset": "warning",
        "builder_asset_destination_collision": "error",
        "builder_no_asset_path": "error",
        "builder_no_suitable_asset": "info",
        "builder_downloading_asset": "info",
        "builder_asset_download_failed": "error",
        "builder_asset_upgraded": "info",
        "builder_force_upgrade_stale": "info",
        "builder_already_up_to_date": "debug",
        "builder_no_upgrade_needed": "debug",
        "builder_stale_candidate_downgrade": "warning",
        "builder_no_image_for_compare": "warning",
        "builder_error_image_compare": "error",
        "builder_dry_run_asset_season": "info",
        "builder_no_asset_path_season": "warning",
        "builder_no_season_details": "info",
        "builder_no_suitable_asset_season": "info",
        "builder_downloading_asset_season": "info",
        "builder_asset_download_failed_season": "error",
        "builder_asset_upgraded_season": "info",
        "builder_force_upgrade_stale_season": "info",
        "builder_already_up_to_date_season": "debug",
        "builder_no_upgrade_needed_season": "debug",
        "builder_stale_candidate_downgrade_season": "warning",
        "builder_no_image_for_compare_season": "warning",
        "builder_error_image_compare_season": "error",
    }
    if "filesize" in kwargs and isinstance(kwargs["filesize"], (int, float)):
            kwargs["filesize"] = human_readable_size(kwargs["filesize"])
            
    if event == "builder_asset_upgraded":
        status_code = kwargs.get("status_code")
        context = kwargs.get("context", {})
        if status_code == "UPGRADE_VOTES":
            reason = f"TMDb vote: {context.get('new_votes')} (Cached: {context.get('cached_votes')})"
        elif status_code == "UPGRADE_STRICT":
            reason = f"TMDb vote: {context.get('new_votes')} (Cached: {context.get('cached_votes')}, Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_THRESHOLD":
            reason = f"TMDb vote: {context.get('new_votes')} (Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_RELAXED":
            reason = f"TMDb vote: {context.get('new_votes')} (Relaxed: {context.get('vote_relaxed')})"
        elif status_code == "UPGRADE_DIMENSIONS":
            reason = f"TMDb dimensions: {context.get('new_width')}x{context.get('new_height')}, Existing: {context.get('existing_width', '?')}x{context.get('existing_height', '?')}"
        else:
            reason = ""
        kwargs["reason"] = reason
    if event == "builder_asset_upgraded_season":
        status_code = kwargs.get("status_code")
        context = kwargs.get("context", {})
        if status_code == "UPGRADE_VOTES_SEASON":
            reason = f"TMDb vote: {context.get('new_votes')} (Cached: {context.get('cached_votes')})"
        elif status_code == "UPGRADE_ZERO_VOTE_SEASON":
            reason = f"(Cached: {context.get('cached_votes')}) Upgrade dimensions {context.get('new_width')}x{context.get('new_height')}"
        elif status_code == "UPGRADE_STRICT_SEASON":
            reason = f"TMDb vote: {context.get('new_votes')} (Cached: {context.get('cached_votes')}, Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_THRESHOLD_SEASON":
            reason = f"TMDb vote: {context.get('new_votes')} (Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_RELAXED_SEASON":
            reason = f"TMDb vote: {context.get('new_votes')} (Relaxed: {context.get('vote_relaxed')})"
        elif status_code == "UPGRADE_DIMENSIONS_SEASON":
            reason = f"TMDb dimensions: {context.get('new_width')}x{context.get('new_height')}, Existing: {context.get('existing_width', '?')}x{context.get('existing_height', '?')}"
        else:
            reason = ""
        kwargs["reason"] = reason
        
    msg = messages.get(event, "[Builder] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)

def log_asset_status(
    status_code, *, media_type, asset_type, full_title, filesize=None, 
    error=None, extra=None, season_number=None
):
    event_map = {
        "FORCE_UPGRADE_STALE": "builder_force_upgrade_stale",
        "ALREADY_UP_TO_DATE": "builder_already_up_to_date",
        "NO_UPGRADE_NEEDED": "builder_no_upgrade_needed",
        "STALE_CANDIDATE_DOWNGRADE": "builder_stale_candidate_downgrade",
        "NO_IMAGE_FOR_COMPARE": "builder_no_image_for_compare",
        "ERROR_IMAGE_COMPARE": "builder_error_image_compare",
        "FORCE_UPGRADE_STALE_SEASON": "builder_force_upgrade_stale_season",
        "ALREADY_UP_TO_DATE_SEASON": "builder_already_up_to_date_season",
        "NO_UPGRADE_NEEDED_SEASON": "builder_no_upgrade_needed_season",
        "STALE_CANDIDATE_DOWNGRADE_SEASON": "builder_stale_candidate_downgrade_season",
        "NO_IMAGE_FOR_COMPARE_SEASON": "builder_no_image_for_compare_season",
        "ERROR_IMAGE_COMPARE_SEASON": "builder_error_image_compare_season",
    }
    event = event_map.get(status_code)
    if not event:
        return
    kwargs = {
        "media_type": media_type,
        "asset_type": asset_type,
        "full_title": full_title,
    }
    if filesize is not None:
        kwargs["filesize"] = filesize
    if error is not None:
        kwargs["error"] = error
    if extra is not None:
        kwargs["extra"] = extra
    if season_number is not None:
        kwargs["season_number"] = season_number
    log_builder_event(event, **kwargs)

def log_cleanup_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "cleanup_start": "[Cleanup] Libraries cleanup process starting...",
        "cleanup_error": "[Cleanup] Plex metadata is required but was not provided. Cleanup aborted...",
        "cleanup_unsafe_scope": "[Cleanup] No fully scanned library type is available. Cleanup aborted.",
        "cleanup_incomplete_episode_inventory": "[Cleanup] Plex season/episode inventory is incomplete for {titles}. Cleanup aborted.",
        "cleanup_skipped_run_scope": "[Cleanup] Cleanup skipped: {reason}.",
        "cleanup_removed_cache_entry": "[Cleanup] Removing TMDb cache entry: {key}",
        "cleanup_removed_orphaned_season_cache": "[Cleanup] Removing orphaned season cache: {show} ({year}) Season {season}",
        "cleanup_skipped_plex_mode": "[Cleanup] Skipping metadata and asset removal in Plex mode.",
        "cleanup_skipping_nonpreferred": "[Cleanup] Skipping non-preferred library: {filename}",
        "cleanup_removed_orphans": "[Cleanup] Removing {orphans_in_file} entries: {filename}",
        "cleanup_removed_orphaned_season_yaml": "[Cleanup] Removing orphaned season metadata: {show} ({year}) Season {season}",
        "cleanup_removed_orphaned_episode_yaml": "[Cleanup] Removing orphaned episode metadata: {show} ({year}) S{season}E{episode}",
        "cleanup_failed_remove_metadata": "[Cleanup] Failed to remove {filename}: {error}",
        "cleanup_skipping_valid_asset": "[Cleanup] Skipping valid asset {description}: {path}",
        "cleanup_preserving_modified_asset": "[Cleanup] Preserving {description} {path}: {reason}.",
        "cleanup_removing_asset": "[Cleanup] Removing {description} asset: {path}",
        "cleanup_removing_empty_dir": "[Cleanup] Removing empty asset path: {parent}",
        "cleanup_failed_remove_asset": "[Cleanup] Failed to remove {description} {path}: {error}",
        "cleanup_consolidated_removed": "[Cleanup] {summary}",
        "cleanup_totals": "[Cleanup] {action} - Titles: {titles}, Seasons: {seasons}, Episodes: {episodes}, Assets: {assets}",
        "cleanup_dry_run": "[Cleanup] [Dry Run] Would remove {description}: {path}",
    }
    levels = {
        "cleanup_start": "info",
        "cleanup_error": "error",
        "cleanup_unsafe_scope": "warning",
        "cleanup_incomplete_episode_inventory": "warning",
        "cleanup_skipped_run_scope": "info",
        "cleanup_removed_cache_entry": "debug",
        "cleanup_removed_orphaned_season_cache": "debug",
        "cleanup_skipped_plex_mode": "info",
        "cleanup_skipping_nonpreferred": "info",
        "cleanup_removed_orphans": "debug",
        "cleanup_removed_orphaned_season_yaml": "debug",
        "cleanup_removed_orphaned_episode_yaml": "debug",
        "cleanup_failed_remove_metadata": "error",
        "cleanup_skipping_valid_asset": "info",
        "cleanup_preserving_modified_asset": "warning",
        "cleanup_removing_asset": "debug",
        "cleanup_removing_empty_dir": "debug",
        "cleanup_failed_remove_asset": "warning",
        "cleanup_consolidated_removed": "info",
        "cleanup_totals": "info",
        "cleanup_dry_run": "info",
    }
    
    if event == "cleanup_consolidated_removed" and "removed_summary" in kwargs:
        summary_lines = []
        for (title, year), types in kwargs["removed_summary"].items():
            parts = []
            if types.get("cache"):
                parts.append("cache entry")
            if types.get("yaml"):
                parts.append("YAML entry")
            for asset_type in types.get("asset", []):
                parts.append(f"asset ({asset_type})")
            if parts:
                summary_lines.append(f"{title} {year} " + ", ".join(parts) + " removed.")
        kwargs["summary"] = "\n[Cleanup] ".join(summary_lines)
    
    msg = messages.get(event, "[Cleanup] Unknown event")
    try:
        msg = msg.format(**kwargs)
    except Exception:
        pass
    level = levels.get(event, "info")
    if event == "cleanup_consolidated_removed" and "removed_summary" in kwargs:
        for line in msg.splitlines():
            if level == "info":
                logger.info(line)
            elif level == "warning":
                logger.warning(line)
            elif level == "error":
                logger.error(line)
            else:
                logger.debug(line)
    else:
        if level == "info":
            logger.info(msg)
        elif level == "warning":
            logger.warning(msg)
        elif level == "error":
            logger.error(msg)
        else:
            logger.debug(msg)

def metadata_action_summary(library_summary, feature_flags):
    """Return a mode-specific summary of metadata mutations."""
    feature_flags = feature_flags or {}
    library_summary = library_summary or {}
    if feature_flags.get("plex_metadata", False):
        return (
            "Plex Metadata - Items changed: "
            f"{library_summary.get('meta_upgraded', 0)}, "
            f"API batches: {library_summary.get('plex_metadata_writes', 0)}, "
            f"Unchanged: {library_summary.get('meta_skipped', 0)}, "
            f"Failed: {library_summary.get('meta_failed', 0)}"
        )
    if feature_flags.get("metadata_basic", False) or feature_flags.get(
        "metadata_enhanced", False
    ):
        return (
            "Kometa Metadata - Created: "
            f"{library_summary.get('meta_downloaded', 0)}, "
            f"Updated: {library_summary.get('meta_upgraded', 0)}, "
            f"Unchanged: {library_summary.get('meta_skipped', 0)}, "
            f"Failed: {library_summary.get('meta_failed', 0)}"
        )
    return None


def plex_progress_item_interval(total_items):
    """Return the built-in top-level item interval for Plex progress logs."""
    total_items = max(0, int(total_items))
    if total_items <= 100:
        return max(5, (total_items + 9) // 10)
    if total_items <= 1000:
        return max(25, (total_items + 19) // 20)
    return max(100, (total_items + 19) // 20)


class PlexMetadataProgress:
    """Rate-limited INFO progress for direct Plex metadata processing."""

    def __init__(
        self,
        library_name,
        total_items,
        *,
        logger=None,
        clock=None,
        minimum_seconds=30,
        heartbeat_seconds=60,
    ):
        self.library_name = str(library_name)
        self.total_items = max(0, int(total_items))
        self.logger = logger or logging.getLogger()
        self.clock = clock or time.monotonic
        self.minimum_seconds = max(0, float(minimum_seconds))
        self.heartbeat_seconds = max(
            self.minimum_seconds, float(heartbeat_seconds)
        )
        self.item_interval = plex_progress_item_interval(self.total_items)
        self.last_logged_at = None
        self.last_logged_completed = 0

    def _emit(self, completed, changed, api_batches, unchanged, failed, now):
        percent = (
            round((completed / self.total_items) * 100, 1)
            if self.total_items
            else 100.0
        )
        self.logger.info(
            "[Plex Metadata] %s: %d/%d checked (%.1f%%); "
            "items changed: %d, API batches: %d, unchanged: %d, failed: %d.",
            self.library_name,
            completed,
            self.total_items,
            percent,
            changed,
            api_batches,
            unchanged,
            failed,
        )
        self.last_logged_at = now
        self.last_logged_completed = completed

    def start(self):
        if not self.total_items or self.last_logged_at is not None:
            return False
        self._emit(0, 0, 0, 0, 0, self.clock())
        return True

    def update(
        self,
        completed,
        *,
        changed,
        api_batches,
        unchanged,
        failed,
        force=False,
    ):
        if not self.total_items:
            return False
        if self.last_logged_at is None:
            self.start()
        completed = min(self.total_items, max(0, int(completed)))
        now = self.clock()
        elapsed = now - self.last_logged_at
        item_due = (
            completed - self.last_logged_completed >= self.item_interval
        )
        time_due = elapsed >= self.heartbeat_seconds
        if not force and not (
            time_due or (item_due and elapsed >= self.minimum_seconds)
        ):
            return False
        self._emit(
            completed,
            int(changed),
            int(api_batches),
            int(unchanged),
            int(failed),
            now,
        )
        return True


def log_library_summary(
    library_name, completed, incomplete, total_items, percent_complete, percent_incomplete, poster_size=0, 
    background_size=0, season_poster_size=0, feature_flags=None, library_filesize=None, run_metadata=None,
    library_summary=None, logger=None, library_type=None, season_count=None, episode_count=None
):
    logger = logger or logging.getLogger()
    box_width = 80
    def box_line(text, width=box_width):
        wrapped = textwrap.wrap(text, width=width - 4)
        return [f"| {line.ljust(width - 4)} |" for line in wrapped]

    library_type = (library_type or "unknown").strip().lower()
    if library_type not in ("movie", "tv", "show"):
        if "movie" in (library_name or "").lower():
            library_type = "movie"
        elif "tv" in (library_name or "").lower() or "show" in (library_name or "").lower():
            library_type = "tv"
        else:
            library_type = "unknown"
            
    header = "=" * box_width
    title = "LIBRARY PROCESSING SUMMARY"
    lines = [
        header,
        f"| {title.center(box_width - 4)} |",
        header,
        (
            f"| {library_name} - Titles: {total_items}"
            + (
                f" | Seasons: {season_count or 0} | Episodes: {episode_count or 0}"
                if library_type in ("tv", "show") and (season_count is not None or episode_count is not None)
                else ""
            )
        ).ljust(box_width - 1) + "|"
        ]
    
    metadata_summary = metadata_action_summary(library_summary, feature_flags)
    if metadata_summary:
        lines.extend(box_line(metadata_summary, box_width))
    if run_metadata:
        meta_line = (
            f"Metadata - Complete: {completed}/{total_items} ({percent_complete}%), "
            f"Incomplete: {incomplete} ({percent_incomplete}%)"
        )
        lines.extend(box_line(meta_line, box_width))
       
    if feature_flags and feature_flags.get("poster", False) and (library_type in ("movie", "tv", "show")):
        lines.extend(box_line(
            f"Poster - Downloaded: {library_summary.get('poster_downloaded', 0)}, "
            f"Upgraded: {library_summary.get('poster_upgraded', 0)}, "
            f"Adopted: {library_summary.get('poster_adopted', 0)}, "
            f"Skipped: {library_summary.get('poster_skipped', 0)}, "
            f"Missing: {library_summary.get('poster_missing', 0)}, "
            f"Failed: {library_summary.get('poster_failed', 0)}", box_width))
    if feature_flags and feature_flags.get("background", False) and (library_type in ("movie", "tv", "show")):
        lines.extend(box_line(
            f"Background - Downloaded: {library_summary.get('background_downloaded', 0)}, "
            f"Upgraded: {library_summary.get('background_upgraded', 0)}, "
            f"Adopted: {library_summary.get('background_adopted', 0)}, "
            f"Skipped: {library_summary.get('background_skipped', 0)}, "
            f"Missing: {library_summary.get('background_missing', 0)}, "
            f"Failed: {library_summary.get('background_failed', 0)}", box_width))
    if (
        feature_flags and feature_flags.get("season", False)
        and library_type in ("tv", "show")
        and (
            library_summary.get('season_poster_downloaded', 0) > 0 or
            library_summary.get('season_poster_upgraded', 0) > 0 or
            library_summary.get('season_poster_adopted', 0) > 0 or
            library_summary.get('season_poster_skipped', 0) > 0 or
            library_summary.get('season_poster_missing', 0) > 0 or
            library_summary.get('season_poster_failed', 0) > 0
        )
    ):
        lines.extend(box_line(
            f"Season - Downloaded: {library_summary.get('season_poster_downloaded', 0)}, "
            f"Upgraded: {library_summary.get('season_poster_upgraded', 0)}, "
            f"Adopted: {library_summary.get('season_poster_adopted', 0)}, "
            f"Skipped: {library_summary.get('season_poster_skipped', 0)}, "
            f"Missing: {library_summary.get('season_poster_missing', 0)}, "
            f"Failed: {library_summary.get('season_poster_failed', 0)}", box_width))

    asset_summaries = []
    if feature_flags and feature_flags.get("poster") and poster_size > 0:
        asset_summaries.append(f"Poster: {human_readable_size(poster_size)}")
    if feature_flags and feature_flags.get("background") and background_size > 0:
        asset_summaries.append(f"Background: {human_readable_size(background_size)}")
    if feature_flags and feature_flags.get("season") and season_poster_size > 0:
        asset_summaries.append(f"Season: {human_readable_size(season_poster_size)}")
    if asset_summaries:
        total_size = ""
        if library_filesize is not None and library_filesize.get(library_name, 0) > 0:
            total_size = f", Total: {human_readable_size(library_filesize[library_name])}"
        lines.extend(box_line(f"Assets - {', '.join(asset_summaries)}{total_size}", box_width))

    lines.append(header)
    for line in lines:
        logger.info(line)

def log_final_summary(
    logger, elapsed_time, metadata_summaries, library_filesize, cleanup_result, cleanup_title_orphans,
    selected_libraries, libraries, config, feature_flags=None
):
    box_width = 80
    def box_line(text, width=box_width):
        wrapped = textwrap.wrap(text, width=width - 4)
        return [f"| {line.ljust(width - 4)} |" for line in wrapped]

    border = "=" * box_width
    title = "METAFUSION SUMMARY REPORT".center(box_width - 4)
    lines = [
        "",
        "",
        border,
        f"| {title.center(box_width - 4)} |",
        border
    ]
    minutes, seconds = divmod(int(elapsed_time), 60)
    run_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.extend(box_line(f"Executed on {run_date} in {minutes} mins {seconds} secs.", box_width))
    processed_libraries = [lib["title"] for lib in libraries if lib["title"] in selected_libraries]
    skipped_libraries = [lib["title"] for lib in libraries if lib["title"] not in selected_libraries]
    lines.extend(box_line(
        f"Processed - {', '.join(processed_libraries) if processed_libraries else 'None'} ({len(processed_libraries)})"
        f" | Skipped: {', '.join(skipped_libraries) if skipped_libraries else 'None'} ({len(skipped_libraries)})",
        box_width
    ))

    total_asset_size = sum(library_filesize.values())
    for lib, summary in metadata_summaries.items():
        if summary is None:
            continue
        libsum = summary.get("library_summary", {})
        asset_size = library_filesize.get(lib, 0)
        library_type = (summary.get("library_type", "") or "unknown").strip().lower()
        if library_type not in ("movie", "tv", "show"):
            if "movie" in lib.lower():
                library_type = "movie"
            elif "tv" in lib.lower() or "show" in lib.lower():
                library_type = "tv"
            else:
                library_type = "unknown"
                
        lines.append(border)
        season_count = summary.get("season_count")
        episode_count = summary.get("episode_count")
        summary_line = (
            f"{lib} - Titles: {summary['total_items']}"
            + (
                f" | Seasons: {season_count or 0} | Episodes: {episode_count or 0}"
                if library_type in ("tv", "show") and (season_count is not None or episode_count is not None)
                else ""
            )
        )
        lines.extend(box_line(summary_line, box_width))
        library_items = summary.get("library_items", summary["total_items"])
        incremental_skipped = libsum.get("incremental_skipped", 0)
        if incremental_skipped:
            lines.extend(
                box_line(
                    f"Incremental - Library items: {library_items}, "
                    f"processed: {summary['total_items']}, unchanged skipped: {incremental_skipped}",
                    box_width,
                )
            )
        metadata_summary = metadata_action_summary(libsum, feature_flags)
        if metadata_summary:
            lines.extend(box_line(metadata_summary, box_width))
        percent_incomplete = summary.get('percent_incomplete', 100 - summary['percent_complete'])
        lines.extend(box_line(
            f"Metadata - Complete: {summary['complete']}/{summary['total_items']} ({summary['percent_complete']}%), "
            f"Incomplete: {summary['incomplete']} ({percent_incomplete}%)", box_width))

        if feature_flags and feature_flags.get("poster", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Poster - Downloaded: {libsum.get('poster_downloaded', 0)}, "
                f"Upgraded: {libsum.get('poster_upgraded', 0)}, "
                f"Adopted: {libsum.get('poster_adopted', 0)}, "
                f"Skipped: {libsum.get('poster_skipped', 0)}, "
                f"Missing: {libsum.get('poster_missing', 0)}, "
                f"Failed: {libsum.get('poster_failed', 0)}", box_width))

        if feature_flags and feature_flags.get("background", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Background - Downloaded: {libsum.get('background_downloaded', 0)}, "
                f"Upgraded: {libsum.get('background_upgraded', 0)}, "
                f"Adopted: {libsum.get('background_adopted', 0)}, "
                f"Skipped: {libsum.get('background_skipped', 0)}, "
                f"Missing: {libsum.get('background_missing', 0)}, "
                f"Failed: {libsum.get('background_failed', 0)}", box_width))

        if (
            feature_flags and feature_flags.get("season", False)
            and library_type in ("tv", "show")
            and (
                libsum.get('season_poster_downloaded', 0) > 0 or
                libsum.get('season_poster_upgraded', 0) > 0 or
                libsum.get('season_poster_adopted', 0) > 0 or
                libsum.get('season_poster_skipped', 0) > 0 or
                libsum.get('season_poster_missing', 0) > 0 or
                libsum.get('season_poster_failed', 0) > 0
            )
        ):
            lines.extend(box_line(
                f"Season - Downloaded: {libsum.get('season_poster_downloaded', 0)}, "
                f"Upgraded: {libsum.get('season_poster_upgraded', 0)}, "
                f"Adopted: {libsum.get('season_poster_adopted', 0)}, "
                f"Skipped: {libsum.get('season_poster_skipped', 0)}, "
                f"Missing: {libsum.get('season_poster_missing', 0)}, "
                f"Failed: {libsum.get('season_poster_failed', 0)}", box_width))

        lines.extend(box_line(
            f"Assets - {human_readable_size(asset_size)} / {human_readable_size(total_asset_size)}", box_width))
        lines.append(border)

    if cleanup_result is not None and getattr(cleanup_result, "skipped_reason", None):
        lines.extend(
            box_line(f"Cleanup - Skipped ({cleanup_result.skipped_reason})", box_width)
        )
    elif feature_flags and feature_flags.get("cleanup", False):
        if hasattr(cleanup_result, "titles"):
            action = "Would remove" if cleanup_result.dry_run else "Removed"
            lines.extend(
                box_line(
                    f"Cleanup - {action}: Titles: {cleanup_result.titles}, "
                    f"Seasons: {cleanup_result.seasons}, "
                    f"Episodes: {cleanup_result.episodes}, "
                    f"Assets: {cleanup_result.assets}",
                    box_width,
                )
            )
        else:
            lines.extend(
                box_line(f"Cleanup - {cleanup_result or 0} Titles Removed", box_width)
            )
    if config["settings"].get("dry_run", False):
        lines.extend(box_line("[Dry Run] Completed. No files were written.", box_width))
    lines.append(border)
    for line in lines:
        logger.info(line)
            
def human_readable_size(size, decimal_places=2):
    for unit in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0 or unit == 'TB':
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
