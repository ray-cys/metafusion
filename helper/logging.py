import os, sys, platform, psutil, logging, textwrap, requests, datetime
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
        "tmdb_cache_stats": "[TMDb] Cache entries: {entries}, hits: {hits}, misses: {misses}, evictions: {evictions}",
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
        "processing_metadata_saved": "debug",
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
        "builder_no_tmdb_season_data": "[{media_type}] Missing TMDb data: {full_title} of Season {season_number}. Skipping...",
        "builder_no_metadata_changes": "[{media_type}] No metadata changes detected: {full_title}, ({percent}%/{incomplete_percent}%) completed. Skipping updates...",
        "build_metadata_changed": "[{media_type}] Metadata updated: {full_title} ({percent}%), TMDb ID: {tmdb_id}, {changes}",
        "builder_no_existing_metadata": "[{media_type}] No existing metadata: {full_title}. Creating new entries using TMDb ID {tmdb_id}...",
        "builder_dry_run_metadata": "[Dry Run] Would build metadata for {media_type}: {full_title}",
        "builder_metadata_cached": "[{media_type}] {full_title} cached as {cache_key}...",
        "builder_dry_run_asset": "[Dry Run] Would build {asset_type} asset for {media_type}: {full_title}",
        "builder_no_asset_path": "[{media_type}] Asset path could not be determined: {full_title} {extra}. Skipping...",
        "builder_no_suitable_asset": "[{media_type}] No suitable TMDb {asset_type} found: {full_title} {extra}. Skipping...",
        "builder_downloading_asset": "[{media_type}] Downloading TMDb {asset_type}: {full_title} ({filesize})...",
        "builder_asset_download_failed": "[{media_type}] Downloading TMDb {asset_type} failed: {full_title} (Status: {status}) Error: {error}",
        "builder_asset_upgraded": "[{media_type}] Upgrading TMDb {asset_type}: {full_title} ({filesize}), {reason}",
        "builder_force_upgrade_stale": "[{media_type}] Force upgrade due to stale image: {full_title} ({filesize}), Last upgraded: {last_upgraded} on {stale_days} days ago",
        "builder_already_up_to_date": "[{media_type}] No {asset_type} changes detected: {full_title} ({filesize}). Skipping...",
        "builder_no_upgrade_needed": "[{media_type}] No {asset_type} changes detected: {full_title} ({filesize}). Skipping...",
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
        "builder_no_image_for_compare_season": "[{media_type}] No image comparison: {full_title} Season {season_number}. Skipping...",
        "builder_error_image_compare_season": "[{media_type}] Failed to compare temp image checksum: {full_title} Season {season_number}: {error}",
    }
    levels = {
        "builder_missing_tmdb_and_imdb_id": "warning",
        "builder_missing_tvdb_id_and_tmdb_id": "warning",
        "builder_no_tmdb_season_data": "warning",
        "builder_no_metadata_changes": "info",
        "builder_no_existing_metadata": "info",
        "build_metadata_changed": "info",
        "builder_dry_run_metadata": "info",
        "builder_metadata_cached": "debug",
        "builder_dry_run_asset": "info",
        "builder_no_asset_path": "error",
        "builder_no_suitable_asset": "info",
        "builder_downloading_asset": "debug",
        "builder_asset_download_failed": "error",
        "builder_asset_upgraded": "info",
        "builder_force_upgrade_stale": "info",
        "builder_already_up_to_date": "info",
        "builder_no_upgrade_needed": "info",
        "builder_no_image_for_compare": "warning",
        "builder_error_image_compare": "error",
        "builder_dry_run_asset_season": "info",
        "builder_no_asset_path_season": "warning",
        "builder_no_season_details": "info",
        "builder_no_suitable_asset_season": "info",
        "builder_asset_download_failed_season": "error",
        "builder_asset_upgraded_season": "info",
        "builder_force_upgrade_stale_season": "info",
        "builder_already_up_to_date_season": "info",
        "builder_no_upgrade_needed_season": "info",
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
        "NO_IMAGE_FOR_COMPARE": "builder_no_image_for_compare",
        "ERROR_IMAGE_COMPARE": "builder_error_image_compare",
        "FORCE_UPGRADE_STALE_SEASON": "builder_force_upgrade_stale_season",
        "ALREADY_UP_TO_DATE_SEASON": "builder_already_up_to_date_season",
        "NO_UPGRADE_NEEDED_SEASON": "builder_no_upgrade_needed_season",
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
        "cleanup_removed_cache_entry": "[Cleanup] Removing TMDb cache entry: {key}",
        "cleanup_skipped_plex_mode": "[Cleanup] Skipping metadata and asset removal in Plex mode.",
        "cleanup_skipping_nonpreferred": "[Cleanup] Skipping non-preferred library: {filename}",
        "cleanup_removed_orphans": "[Cleanup] Removing {orphans_in_file} entries: {filename}",
        "cleanup_failed_remove_metadata": "[Cleanup] Failed to remove {filename}: {error}",
        "cleanup_skipping_valid_asset": "[Cleanup] Skipping valid asset {description}: {path}",
        "cleanup_removing_asset": "[Cleanup] Removing {description} asset: {path}",
        "cleanup_removing_empty_dir": "[Cleanup] Removing empty asset path: {parent}",
        "cleanup_failed_remove_asset": "[Cleanup] Failed to remove {description} {path}: {error}",
        "cleanup_consolidated_removed": "[Cleanup] {summary}",
        "cleanup_total_removed": "[Cleanup] Total titles removed: {orphans_removed}",
        "cleanup_dry_run": "[Cleanup] [Dry Run] Would remove {description}: {path}",
    }
    levels = {
        "cleanup_start": "info",
        "cleanup_error": "error",
        "cleanup_unsafe_scope": "warning",
        "cleanup_removed_cache_entry": "debug",
        "cleanup_skipped_plex_mode": "info",
        "cleanup_skipping_nonpreferred": "info",
        "cleanup_removed_orphans": "debug",
        "cleanup_failed_remove_metadata": "error",
        "cleanup_skipping_valid_asset": "info",
        "cleanup_removing_asset": "debug",
        "cleanup_removing_empty_dir": "debug",
        "cleanup_failed_remove_asset": "warning",
        "cleanup_consolidated_removed": "info",
        "cleanup_total_removed": "info",
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
    
    if library_summary:
        lines.extend(box_line(
            f"Metadata - Downloaded: {library_summary.get('meta_downloaded', 0)}, "
            f"Updated: {library_summary.get('meta_upgraded', 0)}, "
            f"Skipped: {library_summary.get('meta_skipped', 0)}", box_width))
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
            f"Skipped: {library_summary.get('poster_skipped', 0)}, "
            f"Missing: {library_summary.get('poster_missing', 0)}, "
            f"Failed: {library_summary.get('poster_failed', 0)}", box_width))
    if feature_flags and feature_flags.get("background", False) and (library_type in ("movie", "tv", "show")):
        lines.extend(box_line(
            f"Background - Downloaded: {library_summary.get('background_downloaded', 0)}, "
            f"Upgraded: {library_summary.get('background_upgraded', 0)}, "
            f"Skipped: {library_summary.get('background_skipped', 0)}, "
            f"Missing: {library_summary.get('background_missing', 0)}, "
            f"Failed: {library_summary.get('background_failed', 0)}", box_width))
    if (
        feature_flags and feature_flags.get("season", False)
        and library_type in ("tv", "show")
        and (
            library_summary.get('season_poster_downloaded', 0) > 0 or
            library_summary.get('season_poster_upgraded', 0) > 0 or
            library_summary.get('season_poster_skipped', 0) > 0 or
            library_summary.get('season_poster_missing', 0) > 0 or
            library_summary.get('season_poster_failed', 0) > 0
        )
    ):
        lines.extend(box_line(
            f"Season - Downloaded: {library_summary.get('season_poster_downloaded', 0)}, "
            f"Upgraded: {library_summary.get('season_poster_upgraded', 0)}, "
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
    logger, elapsed_time, metadata_summaries, library_filesize, orphans_removed, cleanup_title_orphans, 
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
        lines.extend(box_line(
            f"Metadata - Downloaded: {libsum.get('meta_downloaded', 0)}, "
            f"Updated: {libsum.get('meta_upgraded', 0)}, "
            f"Skipped: {libsum.get('meta_skipped', 0)}", box_width))
        percent_incomplete = summary.get('percent_incomplete', 100 - summary['percent_complete'])
        lines.extend(box_line(
            f"Metadata - Complete: {summary['complete']}/{summary['total_items']} ({summary['percent_complete']}%), "
            f"Incomplete: {summary['incomplete']} ({percent_incomplete}%)", box_width))

        if feature_flags and feature_flags.get("poster", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Poster - Downloaded: {libsum.get('poster_downloaded', 0)}, "
                f"Upgraded: {libsum.get('poster_upgraded', 0)}, "
                f"Skipped: {libsum.get('poster_skipped', 0)}, "
                f"Missing: {libsum.get('poster_missing', 0)}, "
                f"Failed: {libsum.get('poster_failed', 0)}", box_width))

        if feature_flags and feature_flags.get("background", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Background - Downloaded: {libsum.get('background_downloaded', 0)}, "
                f"Upgraded: {libsum.get('background_upgraded', 0)}, "
                f"Skipped: {libsum.get('background_skipped', 0)}, "
                f"Missing: {libsum.get('background_missing', 0)}, "
                f"Failed: {libsum.get('background_failed', 0)}", box_width))

        if (
            feature_flags and feature_flags.get("season", False)
            and library_type in ("tv", "show")
            and (
                libsum.get('season_poster_downloaded', 0) > 0 or
                libsum.get('season_poster_upgraded', 0) > 0 or
                libsum.get('season_poster_skipped', 0) > 0 or
                libsum.get('season_poster_missing', 0) > 0
            )
        ):
            lines.extend(box_line(
                f"Season - Downloaded: {libsum.get('season_poster_downloaded', 0)}, "
                f"Upgraded: {libsum.get('season_poster_upgraded', 0)}, "
                f"Skipped: {libsum.get('season_poster_skipped', 0)}, "
                f"Missing: {libsum.get('season_poster_missing', 0)}, "
                f"Failed: {libsum.get('season_poster_failed', 0)}", box_width))

        lines.extend(box_line(
            f"Assets - {human_readable_size(asset_size)} / {human_readable_size(total_asset_size)}", box_width))
        lines.append(border)

    if feature_flags and feature_flags.get("cleanup", False):
        lines.extend(box_line(f"Cleanup - {orphans_removed} Titles Removed", box_width))
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
