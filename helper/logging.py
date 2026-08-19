import datetime
import logging
import os
import platform
import shutil
import sys
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

import psutil
import requests

from helper.provider_credentials import fanart_project_api_key

BASE_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
LOGS_DIR = BASE_CONFIG_DIR / "logs"
LOG_FILE = LOGS_DIR / "metafusion.log"
MIN_PYTHON = (3, 10)
MIN_CPU_CORES = 4
MIN_RAM_GB = 4


class SizeAndTimeRotatingFileHandler(logging.FileHandler):
    """Rotate at local midnight or before the active log exceeds its size cap."""

    def __init__(self, filename, max_bytes, backup_count, encoding="utf-8"):
        self.max_bytes = max(0, int(max_bytes))
        self.backup_count = max(1, int(backup_count))
        super().__init__(filename, encoding=encoding)
        self.next_rollover_at = self._next_midnight()

    @staticmethod
    def _next_midnight(now=None):
        current = now or datetime.datetime.now().astimezone()
        tomorrow = current.date() + datetime.timedelta(days=1)
        return datetime.datetime.combine(
            tomorrow, datetime.time.min, tzinfo=current.tzinfo
        ).timestamp()

    def shouldRollover(self, record):
        if time.time() >= self.next_rollover_at:
            return True
        if not self.max_bytes:
            return False
        message_bytes = len(
            (self.format(record) + "\n").encode(self.encoding or "utf-8")
        )
        try:
            current_bytes = os.path.getsize(self.baseFilename)
        except OSError:
            current_bytes = 0
        return current_bytes + message_bytes > self.max_bytes

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        source = Path(self.baseFilename)
        if source.exists() and source.stat().st_size:
            suffix = datetime.datetime.now().astimezone().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )
            destination = source.with_name(f"{source.name}.{suffix}")
            sequence = 1
            while destination.exists():
                destination = source.with_name(
                    f"{source.name}.{suffix}.{sequence:03d}"
                )
                sequence += 1
            os.replace(source, destination)
        backups = sorted(
            source.parent.glob(f"{source.name}.*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for expired in backups[self.backup_count :]:
            with suppress(OSError):
                expired.unlink()
        self.stream = self._open()
        self.next_rollover_at = self._next_midnight()

    def emit(self, record):
        try:
            if self.shouldRollover(record):
                self.doRollover()
        except OSError:
            self.handleError(record)
        super().emit(record)


def redact_secrets(value, *secrets):
    redacted = str(value)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(str(secret), "***")
    return redacted


def _format_event_message(template, values, logger, namespace):
    try:
        return template.format(**values)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
        logger.debug("[%s] Unable to format log event: %s", namespace, error)
        return template


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
            fanart_project_api_key(),
        )
    )
    console_handler.addFilter(secret_filter)

    if not dry_run:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = SizeAndTimeRotatingFileHandler(
            log_file,
            max_bytes=max(
                0,
                int(float(config["settings"].get("log_max_mb", 10)) * 1024 * 1024),
            ),
            backup_count=config["settings"].get("log_backup_count", 14),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(log_level)
        file_handler.addFilter(secret_filter)
        logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger

def get_meta_banner(logger=None):
    line = "[Startup] M E T A F U S I O N"
    if logger:
        logger.info(line)
    else:
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

    logger.info(
        "[System] Runtime | OS=%s | Python=%s | CPUCores=%s | CPUUsage=%.1f%% | "
        "RAMTotal=%.2f GB | RAMUsed=%.2f GB | RAMFree=%.2f GB",
        os_info,
        platform.python_version(),
        cpu_cores if cpu_cores is not None else "unknown",
        cpu_percent,
        total_gb,
        used_gb,
        free_gb,
    )
    if py_version < MIN_PYTHON:
        logger.error(
            "[System] Runtime validation failed | Requirement=Python %d.%d+ | "
            "Detected=%s",
            MIN_PYTHON[0],
            MIN_PYTHON[1],
            platform.python_version(),
        )
        raise RuntimeError(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required")

    if cpu_cores is not None and cpu_cores < MIN_CPU_CORES:
        logger.warning(
            "[System] Resource recommendation | CPUCores=%d | Recommended=%d+",
            cpu_cores,
            MIN_CPU_CORES,
        )

    if total_gb < MIN_RAM_GB:
        logger.warning(
            "[System] Resource recommendation | RAMTotal=%.2f GB | Recommended=%d GB+",
            total_gb,
            MIN_RAM_GB,
        )

    if not check_network:
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
                logger.info("[Connection] Plex validation | Status=UP")
            else:
                logger.warning(
                    "[Connection] Plex validation | Status=DOWN | HTTPStatus=%s",
                    resp.status_code,
                )
        except Exception as e:
            safe_error = redact_secrets(e, plex_token)
            logger.warning(
                "[Connection] Plex validation | Status=DOWN | Error=%s",
                safe_error,
            )
    else:
        logger.error(
            "[Connection] Plex validation | Status=INVALID | "
            "Reason=URL or token is not configured"
        )

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
                logger.info("[Connection] TMDb validation | Status=UP")
            else:
                logger.warning(
                    "[Connection] TMDb validation | Status=DOWN | HTTPStatus=%s",
                    resp.status_code,
                )
        except Exception as e:
            safe_error = redact_secrets(e, tmdb_api_key)
            logger.warning(
                "[Connection] TMDb validation | Status=DOWN | Error=%s",
                safe_error,
            )
    else:
        logger.error(
            "[Connection] TMDb validation | Status=INVALID | "
            "Reason=API key is not configured"
        )

    if not internal_up or not tmdb_up:
        logger.warning(
            "[System] One or more external services are unavailable; "
            "the run will continue and normal request retries will apply."
        )
    return internal_up and tmdb_up

def log_main_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "main_started": "[Startup] Run | StartedAt={start_time}",
        "main_force_run": "[Startup] Forced run | StartedAt={start_time}",
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
    msg = _format_event_message(msg, kwargs, logger, "MetaFusion")
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
        "feature_profile": "[Configuration] Run profile | Mode={mode} | Metadata={metadata} | PlexMetadata={plex_metadata} | Poster={poster} | SeasonPoster={season} | Background={background} | Cleanup={cleanup} | DryRun={dry_run}",
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
        "feature_disabled": "debug",
        "feature_enabled": "debug",
        "feature_profile": "info",
        "unknown_feature": "warning",
        "unknown_key": "warning",
        "yaml_not_found": "warning",
        "yaml_missing": "error",
        "yaml_parse_error": "error",
        "config_missing": "warning",
        "config_loaded": "debug",
    }
    msg = messages.get(event, "[Config] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "Configuration")
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
    msg = _format_event_message(msg, kwargs, logger, "Cache")
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
        "plex_connected": "[Connection] Plex | Connected | ServerVersion={version}",
        "plex_connect_failed": "[Connection] Plex | Attempt failed | Attempt={attempt}/{retries} | Error={error}",
        "plex_libraries_retrieved_failed": "[Inventory] Plex libraries | Retrieval failed | Attempt={attempt}/{retries} | Error={error}",
        "plex_detected_and_skipped_libraries": "[Inventory] Plex libraries | Available={detected} | Selected={selected} | Skipped={skipped} | Selection={selection}",
        "plex_no_libraries_found": "[Inventory] Plex libraries | No processable libraries found",
        "plex_failed_extract_item_id": "[Plex] Failed to extract item ID for {title} ({year}): {error}",
        "plex_failed_extract_library_type": "[Plex] Failed to extract library type for {library_name}: {error}",
        "plex_failed_extract_ids": "[Plex] Failed to extract TMDb, IMDb, TVDb IDs for {title} ({year}): {error}",
        "plex_missing_ids": "[Plex] Missing IDs for {title} ({year}): {missing_ids}. Extracted: {found_ids}",
        "plex_failed_extract_movie_path": "[Plex] Failed to extract movie path for {title} ({year}): {error}",
        "plex_failed_extract_show_path": "[Plex] Failed to extract show path for {title} ({year}): {error}",
        "plex_failed_extract_seasons": "[Plex] Failed to extract explicit season inventory for {title} ({year}); episode-derived inventory will be used: {error}",
        "plex_failed_extract_seasons_episodes": "[Plex] Failed to extract seasons/episodes for {title} ({year}): {error}",
        "plex_operation_failed": "[Plex] {description} failed (attempt {attempt}/{retries}): {error}",
        "plex_circuit_open": "[Plex] {description} skipped while the provider circuit cools down ({retry_after:.1f}s remaining).",
        "plex_critical_metadata_missing": "[Plex] Critical metadata missing for item [ratingKey={item_key}]: {missing_critical}. Extracted: {result}",
        "plex_path_sample_library_failed": "[Plex] Unable to sample paths from library {library_name}: {error}",
        "plex_path_sample_item_failed": "[Plex] Unable to sample a media path for {title}: {error}",
        "plex_inventory_paged": "[Plex] Inventory {library_name}: {items} items across {pages} page(s) (page size {page_size}).",
    }
    levels = {
        "plex_connected": "info",
        "plex_connect_failed": "warning",
        "plex_libraries_retrieved_failed": "warning",
        "plex_detected_and_skipped_libraries": "info",
        "plex_no_libraries_found": "warning",
        "plex_failed_extract_item_id": "warning",
        "plex_failed_extract_library_type": "warning",
        "plex_failed_extract_ids": "warning",
        "plex_missing_ids": "debug",
        "plex_failed_extract_movie_path": "warning",
        "plex_failed_extract_show_path": "warning",
        "plex_failed_extract_seasons": "warning",
        "plex_failed_extract_seasons_episodes": "warning",
        "plex_operation_failed": "warning",
        "plex_circuit_open": "warning",
        "plex_critical_metadata_missing": "warning",
        "plex_path_sample_library_failed": "warning",
        "plex_path_sample_item_failed": "warning",
        "plex_inventory_paged": "debug",
    }
    msg = messages.get(event, "[Plex] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "Plex")
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
        "tmdb_negative_cache_hit": "[TMDb] Skipping recently missing resource {url}; cached HTTP 404 is still valid.",
        "tmdb_negative_cached": "[TMDb] Cached HTTP 404 for {url} for {ttl_hours:g} hour(s).",
        "tmdb_request_coalesced": "[TMDb] Reusing the in-flight request for {url}.",
        "tmdb_circuit_open": "[TMDb] Provider circuit is open; preserving existing data and retrying after the cooldown ({retry_after:.1f}s remaining).",
        "tmdb_request": "[TMDb] Requesting {url} with params: {query} (Attempt {attempt}/{retries})",
        "tmdb_success": "[TMDb] Successful response for {url} (Attempt {attempt})",
        "tmdb_rate_limited": "[TMDb] Rate limited (HTTP 429). Sleeping {retry_after}s before retry... Params: {query}",
        "tmdb_non_200": "[TMDb] Non-200 response {status} for {url} params: {query} body: {body}",
        "tmdb_response_too_large": "[TMDb] Response rejected for {url} params {query}: {error}",
        "tmdb_request_failed": "[TMDb] Attempt {attempt}: Request failed for URL {url} with params {query}: {error}",
        "tmdb_retrying": "[TMDb] Retrying in {sleep_time}s... (Attempt {next_attempt}/{retries})",
        "tmdb_failed": "[TMDb] Failed after {retries} attempts for {url} with params {query}",
        "tmdb_cache_stats": "[Cache] Provider=TMDb | Entries={entries} | Compressed={stored_mib:.1f} MiB | Disk={disk_mib:.1f} MiB | Hits={hits} | Misses={misses} | Evictions={evictions} | Recoveries={recoveries}",
        "tmdb_cache_degraded": "[Cache] Provider=TMDb | Status=Degraded | Fallback=Memory | Error={error}",
    }
    levels = {
        "tmdb_no_api_key": "error",
        "tmdb_cache_hit": "debug",
        "tmdb_negative_cache_hit": "debug",
        "tmdb_negative_cached": "debug",
        "tmdb_request_coalesced": "debug",
        "tmdb_circuit_open": "debug",
        "tmdb_request": "debug",
        "tmdb_success": "debug",
        "tmdb_rate_limited": "warning",
        "tmdb_non_200": "warning",
        "tmdb_response_too_large": "error",
        "tmdb_request_failed": "warning",
        "tmdb_retrying": "debug",
        "tmdb_failed": "error",
        "tmdb_cache_stats": "debug",
        "tmdb_cache_degraded": "warning",
    }
    msg = messages.get(event, "[TMDb] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "TMDb")
    level = levels.get(event, "info")
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    elif level == "error":
        logger.error(msg)
    else:
        logger.debug(msg)


def log_fanart_event(event, logger=None, **kwargs):
    """Log value-safe Fanart.tv provider events without exposing credentials."""
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "fanart_disabled": "[Fanart.tv] Artwork fallback is unavailable because the bundled project credential is missing.",
        "fanart_cache_hit": "[Fanart.tv] Returning cached artwork response for {resource_type}:{resource_id}.",
        "fanart_negative_cache_hit": "[Fanart.tv] Skipping recently missing artwork for {resource_type}:{resource_id}; cached HTTP 404 is still valid.",
        "fanart_request_coalesced": "[Fanart.tv] Reusing the in-flight request for {resource_type}:{resource_id}.",
        "fanart_request": "[Fanart.tv] Requesting artwork for {resource_type}:{resource_id} (attempt {attempt}/{retries}).",
        "fanart_success": "[Fanart.tv] Artwork response received for {resource_type}:{resource_id}.",
        "fanart_not_found": "[Fanart.tv] No artwork resource exists for {resource_type}:{resource_id}.",
        "fanart_authorization_failed": "[Fanart.tv] Project authentication failed; disabling Fanart.tv fallback for this run.",
        "fanart_rate_limited": "[Fanart.tv] Rate limited; retrying after {retry_after}s.",
        "fanart_circuit_open": "[Fanart.tv] Provider circuit is open; continuing to Plex/best-available fallback ({retry_after:.1f}s remaining).",
        "fanart_response_too_large": "[Fanart.tv] Rejected oversized response for {resource_type}:{resource_id}.",
        "fanart_invalid_response": "[Fanart.tv] Rejected malformed response for {resource_type}:{resource_id}: {error}",
        "fanart_request_failed": "[Fanart.tv] Request failed for {resource_type}:{resource_id} on attempt {attempt}/{retries}: {error}",
        "fanart_retrying": "[Fanart.tv] Retrying in {sleep_time}s (attempt {next_attempt}/{retries}).",
        "fanart_failed": "[Fanart.tv] Provider unavailable after {retries} attempts for {resource_type}:{resource_id}; continuing fallback.",
        "fanart_cache_stats": "[Cache] Provider=Fanart.tv | Entries={entries} | Compressed={stored_mib:.1f} MiB | Disk={disk_mib:.1f} MiB | Hits={hits} | Misses={misses} | Evictions={evictions} | Recoveries={recoveries}",
        "fanart_cache_degraded": "[Cache] Provider=Fanart.tv | Status=Degraded | Fallback=Memory | Error={error}",
    }
    levels = {
        "fanart_disabled": "debug",
        "fanart_cache_hit": "debug",
        "fanart_negative_cache_hit": "debug",
        "fanart_request_coalesced": "debug",
        "fanart_request": "debug",
        "fanart_success": "debug",
        "fanart_not_found": "debug",
        "fanart_authorization_failed": "warning",
        "fanart_rate_limited": "warning",
        "fanart_circuit_open": "warning",
        "fanart_response_too_large": "warning",
        "fanart_invalid_response": "warning",
        "fanart_request_failed": "warning",
        "fanart_retrying": "debug",
        "fanart_failed": "warning",
        "fanart_cache_stats": "debug",
        "fanart_cache_degraded": "warning",
    }
    msg = _format_event_message(
        messages.get(event, "[Fanart.tv] Unknown event"),
        kwargs,
        logger,
        "Fanart.tv",
    )
    getattr(logger, levels.get(event, "info"))(msg)
        
def log_processing_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logging.getLogger()
    messages = {
        "processing_no_item": "[Processing] No item found. Skipping...",
        "processing_unsupported_type": "[Processing] Unsupported library type for {full_title}. Skipping...",
        "processing_failed_item": "[Processing] Item | {full_title} | Failed | Error={error}",
        "processing_library_items": "[Inventory] {library_name} | Available={library_items} | Selected={total_items} | Scan={scan_mode}",
        "processing_failed_metadata": "[Metadata] {media_type} | {title} ({year}) | Failed to read Plex metadata | Error={error}",
        "processing_failed_parse_yaml": "[Metadata] Kometa YAML | Failed to parse | Destination={output_path} | Error={error}",
        "processing_metadata_saved": "[Metadata] {library_name} | Saved Kometa YAML | Destination={output_path} | ChangedItems={changed_items} | NormalizedEntries={normalized_entries}",
        "processing_cache_saved": "[Processing] Cache files saved.",
        "processing_failed_write_metadata": "[Metadata] {library_name} | Failed to write Kometa YAML | Destination={output_path} | Error={error}",
        "processing_metadata_dry_run": "[Dry Run] [Metadata] {library_name} | Evaluated Kometa YAML | No file written",
        "processing_failed_library": "[Processing] Failed to process library '{library_name}': {error}",
        "processing_selection_reason": "[Processing] Selected {title} (rating key {rating_key}) from {library_name}: cause={causes}; work={work}",
        "processing_selection_summary": "[Processing] Selection summary for {library_name}: selected={selected}, unchanged/not due={skipped}, causes: {causes}",
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
        "processing_selection_summary": "info",
        "processing_ambiguous_editions": "error",
        "processing_ambiguous_editions_allowed": "warning",
    }
    msg = messages.get(event, "[Processing] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "Processing")
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
    kwargs.setdefault("provider", "TMDb")
    messages = {
        "builder_missing_tmdb_and_imdb_id": "[Identity] {media_type} | {full_title} | Missing required external ID | Expected=TMDb or IMDb",
        "builder_missing_tvdb_id_and_tmdb_id": "[Identity] {media_type} | {full_title} | Missing required external ID | Expected=TVDb or TMDb",
        "builder_missing_tvdb_id_and_imdb_id": "[Identity] {media_type} | {full_title} | Missing required external ID | Expected=TVDb or IMDb",
        "builder_no_tmdb_id": "[Identity] {media_type} | {full_title} | TMDb identity unresolved | Action=Skipped",
        "builder_invalid_tmdb_id": "[Identity] {media_type} | {full_title} | TMDb resource unavailable | Action=Skipped",
        "builder_tmdb_id_recovered": "[Identity] {media_type} | {full_title} | Recovered stale TMDb identity | Old={old_id} | New={new_id}",
        "builder_tmdb_identity_mismatch": "[Identity] {media_type} | {full_title} | Rejected TMDb identity | Reason={reason}",
        "builder_tmdb_identity_warning": "[Identity] {media_type} | {full_title} | Identity requires review | Reason={reason}",
        "builder_tmdb_identity_alias": "[Identity] {media_type} | {full_title} | Accepted alias | Reason={reason}",
        "builder_no_tmdb_season_data": "[Metadata] {media_type} | {full_title} | Season {season_number} unavailable from TMDb | Existing values preserved",
        "builder_episode_group_fallback": "[Mapping] {media_type} | {full_title} | Applied alternate episode ordering | TMDbGroup={group_id}",
        "builder_split_series_mapping": "[Mapping] {media_type} | {full_title} | Applied cross-provider season mapping | PlexSeasons={seasons}",
        "builder_split_series_show_preserved": "[Mapping] {media_type} | {full_title} | Preserved top-level metadata and artwork | Season and episode updates remain enabled",
        "builder_episode_overrides": "[Mapping] {media_type} | {full_title} | Applied episode-number overrides | Count={count}",
        "builder_episode_metadata_pending": "[Metadata] {media_type} | {full_title} | Episode metadata pending from TMDb | Count={count} | Episodes={episodes} | Existing values preserved",
        "builder_episode_order_unresolved": "[Mapping] {media_type} | {full_title} | Episode mapping unresolved | Count={count} | Episodes={episodes} | Existing values preserved",
        "builder_metadata_diagnostics": "[Metadata] {media_type} | {full_title} | Diagnostics={diagnostics}",
        "builder_no_metadata_changes": "[Metadata] Kometa | {media_type} | {full_title} | Unchanged | FieldCoverage={percent}% | MissingFields={incomplete_percent}%",
        "build_metadata_changed": "[Metadata] Kometa | {media_type} | {full_title} | Prepared update | FieldCoverage={percent}% | TMDbID={tmdb_id} | Changes={changes}",
        "builder_no_existing_metadata": "[Metadata] Kometa | {media_type} | {full_title} | Prepared new entry | TMDbID={tmdb_id}",
        "builder_plex_candidate_ready": "[Metadata] Plex | {full_title} | Prepared TMDb candidate | FieldCoverage={percent}% | MissingFields={incomplete_percent}%",
        "builder_dry_run_metadata": "[Dry Run] [Metadata] {media_type} | {full_title} | Would evaluate metadata",
        "builder_metadata_cached": "[Cache] {media_type} | {full_title} | Updated metadata state | Key={cache_key}",
        "builder_dry_run_asset": "[Dry Run] [Artwork] {media_type} | {full_title} | Would evaluate {asset_type}",
        "builder_dry_run_asset_selected": "[Dry Run] [Artwork] {media_type} | {full_title} | Selected {asset_type} | Source={provider} | {source_path}",
        "builder_artwork_language_fallback": "[Artwork] {media_type} | {full_title} | Selected unrestricted-language {asset_type} | Source={provider} | Language={language}",
        "builder_reusing_shared_asset": "[Artwork] {media_type} | {full_title} | Reused shared {asset_type} | Source={provider} | Destination={destination}",
        "builder_asset_ownership_adopted": "[Artwork] {media_type} | {full_title} | Adopted existing {asset_type} | Source={provider} | Exact source matched without rewriting",
        "builder_preserving_existing_asset": "[Artwork] {media_type} | {full_title} | Preserved {asset_type} | Destination={destination} | Reason={reason}",
        "builder_asset_destination_collision": "[Artwork] {media_type} | {full_title} | Refused {asset_type} destination collision | Destination={destination} | Owner={owner}",
        "builder_no_asset_path": "[Artwork] {media_type} | {full_title} | Missing {asset_type} destination | Detail={extra}",
        "builder_no_suitable_asset": "[Artwork] {media_type} | {full_title} | Missing {asset_type} | No provider candidate was available{extra}",
        "builder_downloading_asset": "[Artwork] {media_type} | {full_title} | Downloaded {asset_type} | Source={provider} | Size={filesize}",
        "builder_asset_download_failed": "[Artwork] {media_type} | {full_title} | Failed {asset_type} | Source={provider} | HTTP={status} | {error}",
        "builder_asset_upgraded": "[Artwork] {media_type} | {full_title} | Upgraded {asset_type} | Source={provider} | Size={filesize} | {reason}",
        "builder_force_upgrade_stale": "[Artwork] {media_type} | {full_title} | Stale {asset_type} selected for upgrade | Size={filesize} | LastUpgrade={last_upgraded} | AgeDays={stale_days}",
        "builder_already_up_to_date": "[Artwork] {media_type} | {full_title} | Unchanged {asset_type} | Size={filesize}",
        "builder_no_upgrade_needed": "[Artwork] {media_type} | {full_title} | Unchanged {asset_type} | Size={filesize}",
        "builder_stale_candidate_downgrade": "[Artwork] {media_type} | {full_title} | Preserved higher-quality {asset_type} | Reason=Replacement candidate scored lower",
        "builder_no_image_for_compare": "[Artwork] {media_type} | {full_title} | Image comparison unavailable | Detail={extra}",
        "builder_error_image_compare": "[Artwork] {media_type} | {full_title} | Image comparison failed | Detail={extra} | Error={error}",
        "builder_dry_run_asset_season": "[Dry Run] [Artwork] {media_type} | {full_title} | Would evaluate Season {season_number} {asset_type}",
        "builder_no_asset_path_season": "[Artwork] {media_type} | {full_title} | Missing Season {season_number} poster destination",
        "builder_no_season_details": "[Artwork] {media_type} | {full_title} | Season {season_number} details unavailable",
        "builder_no_suitable_asset_season": "[Artwork] {media_type} | {full_title} Season {season_number} | Missing {asset_type} | No provider candidate was available",
        "builder_downloading_asset_season": "[Artwork] {media_type} | {full_title} Season {season_number} | Downloaded {asset_type} | Source={provider} | Size={filesize}",
        "builder_asset_download_failed_season": "[Artwork] {media_type} | {full_title} Season {season_number} | Failed {asset_type} | Source={provider} | HTTP={status} | {error}",
        "builder_asset_upgraded_season": "[Artwork] {media_type} | {full_title} Season {season_number} | Upgraded {asset_type} | Source={provider} | Size={filesize} | {reason}",
        "builder_force_upgrade_stale_season": "[Artwork] {media_type} | {full_title} | Season {season_number} poster selected for stale upgrade | Size={filesize} | LastUpgrade={last_upgraded} | AgeDays={stale_days}",
        "builder_already_up_to_date_season": "[Artwork] {media_type} | {full_title} | Unchanged Season {season_number} {asset_type} | Size={filesize}",
        "builder_no_upgrade_needed_season": "[Artwork] {media_type} | {full_title} | Unchanged Season {season_number} {asset_type} | Size={filesize}",
        "builder_stale_candidate_downgrade_season": "[Artwork] {media_type} | {full_title} | Preserved higher-quality Season {season_number} {asset_type} | Reason=Replacement candidate scored lower",
        "builder_no_image_for_compare_season": "[Artwork] {media_type} | {full_title} | Season {season_number} image comparison unavailable",
        "builder_error_image_compare_season": "[Artwork] {media_type} | {full_title} | Season {season_number} image comparison failed | Error={error}",
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
        "builder_tmdb_identity_alias": "debug",
        "builder_no_tmdb_season_data": "warning",
        "builder_episode_group_fallback": "debug",
        "builder_split_series_mapping": "debug",
        "builder_split_series_show_preserved": "debug",
        "builder_episode_overrides": "debug",
        "builder_episode_metadata_pending": "debug",
        "builder_episode_order_unresolved": "warning",
        "builder_metadata_diagnostics": "debug",
        "builder_no_metadata_changes": "debug",
        "builder_no_existing_metadata": "debug",
        "build_metadata_changed": "debug",
        "builder_plex_candidate_ready": "debug",
        "builder_dry_run_metadata": "info",
        "builder_metadata_cached": "debug",
        "builder_dry_run_asset": "info",
        "builder_dry_run_asset_selected": "debug",
        "builder_artwork_language_fallback": "debug",
        "builder_reusing_shared_asset": "debug",
        "builder_asset_ownership_adopted": "debug",
        "builder_preserving_existing_asset": "warning",
        "builder_asset_destination_collision": "error",
        "builder_no_asset_path": "error",
        "builder_no_suitable_asset": "debug",
        "builder_downloading_asset": "debug",
        "builder_asset_download_failed": "debug",
        "builder_asset_upgraded": "debug",
        "builder_force_upgrade_stale": "debug",
        "builder_already_up_to_date": "debug",
        "builder_no_upgrade_needed": "debug",
        "builder_stale_candidate_downgrade": "warning",
        "builder_no_image_for_compare": "warning",
        "builder_error_image_compare": "error",
        "builder_dry_run_asset_season": "info",
        "builder_no_asset_path_season": "warning",
        "builder_no_season_details": "debug",
        "builder_no_suitable_asset_season": "debug",
        "builder_downloading_asset_season": "debug",
        "builder_asset_download_failed_season": "debug",
        "builder_asset_upgraded_season": "debug",
        "builder_force_upgrade_stale_season": "debug",
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
            reason = f"Provider score: {context.get('new_votes')} (Cached: {context.get('cached_votes')})"
        elif status_code == "UPGRADE_STRICT":
            reason = f"Provider score: {context.get('new_votes')} (Cached: {context.get('cached_votes')}, Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_THRESHOLD":
            reason = f"Provider score: {context.get('new_votes')} (Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_RELAXED":
            reason = f"Provider score: {context.get('new_votes')} (Relaxed: {context.get('vote_relaxed')})"
        elif status_code == "UPGRADE_DIMENSIONS":
            reason = f"Candidate dimensions: {context.get('new_width')}x{context.get('new_height')}, Existing: {context.get('existing_width', '?')}x{context.get('existing_height', '?')}"
        else:
            reason = ""
        kwargs["reason"] = reason
    if event == "builder_asset_upgraded_season":
        status_code = kwargs.get("status_code")
        context = kwargs.get("context", {})
        if status_code == "UPGRADE_VOTES_SEASON":
            reason = f"Provider score: {context.get('new_votes')} (Cached: {context.get('cached_votes')})"
        elif status_code == "UPGRADE_ZERO_VOTE_SEASON":
            reason = f"(Cached: {context.get('cached_votes')}) Upgrade dimensions {context.get('new_width')}x{context.get('new_height')}"
        elif status_code == "UPGRADE_STRICT_SEASON":
            reason = f"Provider score: {context.get('new_votes')} (Cached: {context.get('cached_votes')}, Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_THRESHOLD_SEASON":
            reason = f"Provider score: {context.get('new_votes')} (Threshold: {context.get('vote_threshold')})"
        elif status_code == "UPGRADE_RELAXED_SEASON":
            reason = f"Provider score: {context.get('new_votes')} (Relaxed: {context.get('vote_relaxed')})"
        elif status_code == "UPGRADE_DIMENSIONS_SEASON":
            reason = f"Candidate dimensions: {context.get('new_width')}x{context.get('new_height')}, Existing: {context.get('existing_width', '?')}x{context.get('existing_height', '?')}"
        else:
            reason = ""
        kwargs["reason"] = reason
        
    msg = messages.get(event, "[Builder] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "Builder")
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


def log_item_outcomes(
    library_name,
    full_title,
    stats,
    feature_flags,
    *,
    logger=None,
):
    """Emit consistent metadata/artwork outcomes from the finalized item result."""
    logger = logger or logging.getLogger()
    feature_flags = feature_flags or {}
    stats = stats or {}

    def emit(component, subject, action, source, target=None, detail=None):
        if action in {None, "not_due"}:
            return
        labels = {
            "downloaded": "Created" if component == "Metadata" else "Downloaded",
            "upgraded": "Updated" if component == "Metadata" else "Upgraded",
            "adopted": "Adopted",
            "skipped": "Unchanged",
            "preserved": "Preserved",
            "missing": "Missing",
            "failed": "Failed",
            "deferred": "Deferred",
        }
        outcome = labels.get(action, str(action).replace("_", " ").title())
        if subject:
            outcome = f"{str(subject).title()} {outcome.lower()}"
        message = (
            f"[{component}] {library_name} | {full_title} | "
            f"{outcome} | Source={source}"
        )
        if target:
            message += f" | Target={target}"
        if detail:
            message += f" | {detail}"
        if action == "failed":
            logger.error(message)
        elif action in {"missing", "deferred"}:
            logger.warning(message)
        elif action in {"skipped", "preserved"}:
            logger.debug(message)
        else:
            logger.info(message)

    metadata_action = stats.get("metadata_action")
    if metadata_action != "not_due":
        mode = "Plex" if feature_flags.get("plex_metadata", False) else "Kometa YAML"
        try:
            completeness = f"{float(stats.get('percent') or 0):g}%"
        except (TypeError, ValueError):
            completeness = "unknown"
        details = [f"FieldCoverage={completeness}"]
        if feature_flags.get("plex_metadata", False):
            details.append(f"APIBatches={int(stats.get('plex_metadata_writes', 0))}")
        emit("Metadata", None, metadata_action, "TMDb", mode, " | ".join(details))

    providers = stats.get("artwork_providers") or {}
    artwork_target = (
        "Plex local media"
        if feature_flags.get("mode") == "plex"
        else "Kometa assets"
    )

    def provider_label(provider_value, action):
        provider_key = str(provider_value or "")
        known = {
            "tmdb": "TMDb",
            "fanart": "Fanart.tv",
            "plex": "Plex",
        }.get(provider_key)
        if known:
            return known
        if action in {"skipped", "preserved"}:
            return "Existing"
        return "None"

    for asset, action_key in (
        ("poster", "poster_action"),
        ("background", "background_action"),
    ):
        action = stats.get(action_key)
        if action == "not_due":
            continue
        provider = provider_label(providers.get(asset), action)
        stage = (stats.get("artwork_selection_stages") or {}).get(asset)
        detail = (
            "Selection=Automatic missing-only relaxation"
            if stage == "missing_only_relaxed"
            else None
        )
        emit("Artwork", asset, action, provider, artwork_target, detail)

    season_actions = stats.get("season_poster_actions") or {}
    if season_actions:
        action_counts: dict[str, int] = {}
        provider_counts: dict[str, int] = {}
        season_providers = stats.get("season_artwork_providers") or {}
        for season_number, action in season_actions.items():
            if action == "not_due":
                continue
            action_counts[action] = action_counts.get(action, 0) + 1
            provider_value = season_providers.get(season_number)
            if provider_value is None:
                provider_value = season_providers.get(str(season_number))
            label = provider_label(provider_value, action)
            provider_counts[label] = provider_counts.get(label, 0) + 1
        if action_counts:
            action_labels = {
                "downloaded": "Downloaded",
                "upgraded": "Upgraded",
                "adopted": "Adopted",
                "skipped": "Unchanged",
                "preserved": "Preserved",
                "missing": "Missing",
                "failed": "Failed",
                "deferred": "Deferred",
            }
            action_detail = ", ".join(
                f"{action_labels.get(name, name.title())}={count}"
                for name, count in sorted(action_counts.items())
            )
            provider_detail = ", ".join(
                f"{name}={count}" for name, count in sorted(provider_counts.items())
            )
            attempt_status_labels = {
                "no_candidates": "no candidates",
                "no_candidate": "no explicit season thumb",
                "reserve": "below requirements",
                "selected": "selected",
                "selected_best_available": "selected as best available",
                "selected_missing_only_relaxed": "selected by missing-only relaxation",
            }
            missing_details = []
            season_attempts = stats.get("season_artwork_attempts") or {}
            for season_number, action in sorted(
                season_actions.items(),
                key=lambda value: (-1 if value[0] is None else int(value[0])),
            ):
                if action != "missing":
                    continue
                attempts = season_attempts.get(season_number)
                if attempts is None:
                    attempts = season_attempts.get(str(season_number), [])
                attempt_text = ", ".join(
                    f"{attempt.get('provider', 'Unknown')}:"
                    f"{attempt_status_labels.get(attempt.get('status'), attempt.get('status', 'unknown'))}"
                    for attempt in attempts
                )
                season_label = (
                    "unknown"
                    if season_number is None
                    else f"S{int(season_number):02d}"
                )
                missing_details.append(
                    f"{season_label} ({attempt_text or 'provider attempts unavailable'})"
                )
            quiet_actions = set(action_counts).issubset({"skipped", "preserved"})
            season_stages = stats.get("season_artwork_selection_stages") or {}
            relaxed_count = sum(
                (
                    season_stages.get(number)
                    if season_stages.get(number) is not None
                    else season_stages.get(str(number))
                )
                == "missing_only_relaxed"
                for number in season_actions
            )
            if relaxed_count:
                action_detail += f", AutomaticRelaxed={relaxed_count}"
            level = (
                logger.error
                if action_counts.get("failed")
                else logger.warning
                if action_counts.get("missing") or action_counts.get("deferred")
                else logger.debug
                if quiet_actions
                else logger.info
            )
            level(
                "[Artwork] %s | %s | Season posters | %s | Sources=%s | "
                "Target=%s%s",
                library_name,
                full_title,
                action_detail,
                provider_detail,
                artwork_target,
                (
                    f" | MissingSeasons={'; '.join(missing_details)}"
                    if missing_details
                    else ""
                ),
            )

def log_cleanup_event(event, logger=None, **kwargs):
    logger = kwargs.get("logger") or logger or logging.getLogger()
    messages = {
        "cleanup_start": "[Cleanup] Reconciliation | Starting | Mode={mode} | Scope={scope}",
        "cleanup_error": "[Cleanup] Reconciliation | Skipped | Reason=Plex inventory was unavailable",
        "cleanup_unsafe_scope": "[Cleanup] Reconciliation | Skipped | Reason=No fully scanned library type was available",
        "cleanup_incomplete_episode_inventory": "[Cleanup] TV inventory | Skipped | Reason=Season/episode inventory was incomplete | Titles={titles}",
        "cleanup_skipped_run_scope": "[Cleanup] Reconciliation | Skipped | Reason={reason}",
        "cleanup_failed_cache": "[Cleanup] State | Failed cache reconciliation | Error={error}",
        "cleanup_removed_cache_entry": "[Cleanup] State | {key} | Removed cache entry | Reason=Not present in complete Plex inventory",
        "cleanup_removed_orphaned_season_cache": "[Cleanup] State | {show} ({year}) Season {season} | Removed season cache entry | Reason=Not present in complete Plex inventory",
        "cleanup_skipped_plex_mode": "[Cleanup] Plex mode | State-only reconciliation | Kometa YAML and artwork are preserved",
        "cleanup_skipping_nonpreferred": "[Cleanup] Kometa YAML | {filename} | Preserved file | Reason=Outside managed cleanup filenames",
        "cleanup_removed_orphans": "[Cleanup] Kometa YAML | {filename} | Removed {orphans_in_file} title entries | Reason=Not present in complete Plex inventory",
        "cleanup_removed_orphaned_season_yaml": "[Cleanup] Kometa YAML | {show} ({year}) Season {season} | Removed season entry | Reason=Not present in complete Plex inventory",
        "cleanup_removed_orphaned_episode_yaml": "[Cleanup] Kometa YAML | {show} ({year}) S{season}E{episode} | Removed episode entry | Reason=Not present in complete Plex inventory",
        "cleanup_failed_remove_metadata": "[Cleanup] Kometa YAML | {filename} | Failed cleanup | Error={error}",
        "cleanup_skipping_valid_asset": "[Cleanup] Artwork | {path} | Preserved {description} | Reason=Not an eligible managed orphan",
        "cleanup_preserving_modified_asset": "[Cleanup] Artwork | {path} | Preserved {description} | Reason={reason}",
        "cleanup_removing_asset": "[Cleanup] Artwork | {path} | Removed {description} | Reason=Not present in complete Plex inventory",
        "cleanup_removing_empty_dir": "[Cleanup] Artwork | {parent} | Removed empty managed directory",
        "cleanup_failed_remove_asset": "[Cleanup] Artwork | {path} | Failed to remove {description} | Error={error}",
        "cleanup_consolidated_removed": "[Cleanup] {summary}",
        "cleanup_dry_run": "[Cleanup] Preview | {path} | Would remove {description} | Reason=Not present in complete Plex inventory",
    }
    levels = {
        "cleanup_start": "info",
        "cleanup_error": "error",
        "cleanup_unsafe_scope": "warning",
        "cleanup_incomplete_episode_inventory": "warning",
        "cleanup_skipped_run_scope": "info",
        "cleanup_failed_cache": "error",
        "cleanup_removed_cache_entry": "debug",
        "cleanup_removed_orphaned_season_cache": "debug",
        "cleanup_skipped_plex_mode": "info",
        "cleanup_skipping_nonpreferred": "debug",
        "cleanup_removed_orphans": "debug",
        "cleanup_removed_orphaned_season_yaml": "debug",
        "cleanup_removed_orphaned_episode_yaml": "debug",
        "cleanup_failed_remove_metadata": "error",
        "cleanup_skipping_valid_asset": "debug",
        "cleanup_preserving_modified_asset": "warning",
        "cleanup_removing_asset": "debug",
        "cleanup_removing_empty_dir": "debug",
        "cleanup_failed_remove_asset": "warning",
        "cleanup_consolidated_removed": "info",
        "cleanup_dry_run": "info",
    }
    
    if event == "cleanup_consolidated_removed" and "removed_summary" in kwargs:
        summary_lines = []
        for (title, year), types in kwargs["removed_summary"].items():
            parts = []
            if types.get("cache"):
                parts.append("cache entry")
            if types.get("yaml"):
                parts.append("Kometa YAML entry")
            for asset_type in types.get("asset", []):
                parts.append(f"managed {asset_type}")
            if parts:
                summary_lines.append(
                    f"Inventory | {title} ({year}) | Removed {', '.join(parts)} "
                    "| Reason=Not present in complete Plex inventory"
                )
        kwargs["summary"] = "\n[Cleanup] ".join(summary_lines)
    
    msg = messages.get(event, "[Cleanup] Unknown event")
    msg = _format_event_message(msg, kwargs, logger, "Cleanup")
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
            "Metadata | Target=Plex | Changed="
            f"{library_summary.get('meta_upgraded', 0)} | "
            f"APIBatches={library_summary.get('plex_metadata_writes', 0)} | "
            f"Unchanged={library_summary.get('meta_skipped', 0)} | "
            f"Failed={library_summary.get('meta_failed', 0)}"
        )
    if feature_flags.get("metadata_basic", False) or feature_flags.get(
        "metadata_enhanced", False
    ):
        return (
            "Metadata | Target=Kometa YAML | Created="
            f"{library_summary.get('meta_downloaded', 0)} | "
            f"Updated={library_summary.get('meta_upgraded', 0)} | "
            f"Unchanged={library_summary.get('meta_skipped', 0)} | "
            f"Failed={library_summary.get('meta_failed', 0)}"
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
            "[Metadata] Plex progress | %s | Checked=%d/%d (%.1f%%) | "
            "Changed=%d | APIBatches=%d | Unchanged=%d | Failed=%d",
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
        assert self.last_logged_at is not None
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
    box_width = None
    def box_line(text, width=None):
        return [f"[Summary] Library={library_name} | {text}"]

    library_type = (library_type or "unknown").strip().lower()
    if library_type not in ("movie", "tv", "show"):
        if "movie" in (library_name or "").lower():
            library_type = "movie"
        elif "tv" in (library_name or "").lower() or "show" in (library_name or "").lower():
            library_type = "tv"
        else:
            library_type = "unknown"
            
    lines = [
        f"[Summary] Library={library_name} | Inventory | Titles={total_items}"
        + (
            f" | Seasons={season_count or 0} | Episodes={episode_count or 0}"
            if library_type in ("tv", "show")
            and (season_count is not None or episode_count is not None)
            else ""
        )
    ]
    
    metadata_summary = metadata_action_summary(library_summary, feature_flags)
    if metadata_summary:
        lines.extend(box_line(metadata_summary, box_width))
    if run_metadata:
        meta_line = (
            f"Metadata coverage | MeetsThreshold={completed}/{total_items} "
            f"({percent_complete}%) | BelowThreshold={incomplete} "
            f"({percent_incomplete}%)"
        )
        lines.extend(box_line(meta_line))
       
    if feature_flags and feature_flags.get("poster", False) and (library_type in ("movie", "tv", "show")):
        lines.extend(box_line(
            f"Artwork poster | Downloaded={library_summary.get('poster_downloaded', 0)} | "
            f"Upgraded={library_summary.get('poster_upgraded', 0)} | Adopted={library_summary.get('poster_adopted', 0)} | "
            f"Unchanged={library_summary.get('poster_skipped', 0)} | NotDue={library_summary.get('poster_not_due', 0)} | "
            f"Preserved={library_summary.get('poster_preserved', 0)} | Missing={library_summary.get('poster_missing', 0)} | "
            f"Deferred={library_summary.get('poster_deferred', 0)} | Failed={library_summary.get('poster_failed', 0)}"))
    if feature_flags and feature_flags.get("background", False) and (library_type in ("movie", "tv", "show")):
        lines.extend(box_line(
            f"Artwork background | Downloaded={library_summary.get('background_downloaded', 0)} | "
            f"Upgraded={library_summary.get('background_upgraded', 0)} | Adopted={library_summary.get('background_adopted', 0)} | "
            f"Unchanged={library_summary.get('background_skipped', 0)} | NotDue={library_summary.get('background_not_due', 0)} | "
            f"Preserved={library_summary.get('background_preserved', 0)} | Missing={library_summary.get('background_missing', 0)} | "
            f"Deferred={library_summary.get('background_deferred', 0)} | Failed={library_summary.get('background_failed', 0)}"))
    if (
        feature_flags and feature_flags.get("season", False)
        and library_type in ("tv", "show")
        and (
            library_summary.get('season_poster_downloaded', 0) > 0 or
            library_summary.get('season_poster_upgraded', 0) > 0 or
            library_summary.get('season_poster_adopted', 0) > 0 or
            library_summary.get('season_poster_skipped', 0) > 0 or
            library_summary.get('season_poster_not_due', 0) > 0 or
            library_summary.get('season_poster_preserved', 0) > 0 or
            library_summary.get('season_poster_missing', 0) > 0 or
            library_summary.get('season_poster_deferred', 0) > 0 or
            library_summary.get('season_poster_failed', 0) > 0
        )
    ):
        lines.extend(box_line(
            f"Artwork season posters | Downloaded={library_summary.get('season_poster_downloaded', 0)} | "
            f"Upgraded={library_summary.get('season_poster_upgraded', 0)} | Adopted={library_summary.get('season_poster_adopted', 0)} | "
            f"Unchanged={library_summary.get('season_poster_skipped', 0)} | NotDue={library_summary.get('season_poster_not_due', 0)} | "
            f"Preserved={library_summary.get('season_poster_preserved', 0)} | Missing={library_summary.get('season_poster_missing', 0)} | "
            f"Deferred={library_summary.get('season_poster_deferred', 0)} | Failed={library_summary.get('season_poster_failed', 0)}"))

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
        lines.extend(box_line(f"Storage observed | {', '.join(asset_summaries)}{total_size}"))

    for line in lines:
        logger.info(line)


def _file_group_bytes(paths):
    total = 0
    for path in paths:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _runtime_storage_bytes():
    cache_dir = BASE_CONFIG_DIR / "cache"
    databases = {
        "StateDB": cache_dir / "meta_db.sqlite3",
        "TMDbCache": cache_dir / "tmdb_cache.sqlite3",
        "FanartCache": cache_dir / "fanart_cache.sqlite3",
    }
    result = {}
    for label, path in databases.items():
        result[label] = _file_group_bytes(
            [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
        )
    result["Logs"] = _file_group_bytes(LOGS_DIR.glob("*"))
    result["Reports"] = _file_group_bytes((BASE_CONFIG_DIR / "reports").glob("*"))
    return result


def _nearest_existing_path(path):
    candidate = Path(path)
    return candidate if candidate.exists() else None


def _storage_mounts(config):
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    candidates: list[tuple[str, Path | str]] = [("Config", BASE_CONFIG_DIR)]
    if mode == "kometa":
        candidates.append(("Kometa output", config.get("settings", {}).get("path", ".")))
    else:
        for index, mapping in enumerate(config.get("plex", {}).get("path_mappings", []), 1):
            _source, separator, destination = str(mapping).partition("=>")
            if separator and destination.strip():
                candidates.append((f"Plex media {index}", destination.strip()))
    mounts: dict[int, dict[str, Any]] = {}
    for label, raw_path in candidates:
        path = _nearest_existing_path(raw_path)
        if path is None:
            continue
        try:
            device = path.stat().st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        record = mounts.setdefault(
            device,
            {"labels": [], "path": str(path), "usage": usage},
        )
        record["labels"].append(label)
    return list(mounts.values())


def log_final_summary(
    logger, elapsed_time, metadata_summaries, library_filesize, cleanup_result, cleanup_title_orphans,
    selected_libraries, libraries, config, feature_flags=None
):
    box_width = None
    def box_line(text, width=None):
        return [f"[Summary] {text}"]

    lines = []
    storage_warnings = []
    minutes, seconds = divmod(int(elapsed_time), 60)
    run_date = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    lines.extend(
        box_line(
            f"Run | CompletedAt={run_date} | Duration={minutes}m {seconds}s | "
            f"Mode={str(config.get('settings', {}).get('mode', 'kometa')).title()}"
        )
    )
    processed_libraries = [
        lib["title"] for lib in libraries if lib["title"] in selected_libraries
    ]
    configured_libraries = config.get("plex_libraries", [])
    auto_discovery = any(
        str(value).strip().casefold() == "auto" for value in configured_libraries
    )
    scope = "Plex automatic discovery" if auto_discovery else "Explicit library selection"
    lines.extend(box_line(f"Scope | Selection={scope}"))
    lines.extend(box_line(
        f"Scope | Libraries={', '.join(processed_libraries) if processed_libraries else 'None'} "
        f"| LibraryCount={len(processed_libraries)}",
    ))

    summaries = [
        summary for summary in metadata_summaries.values() if isinstance(summary, dict)
    ]
    overall: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    for summary in summaries:
        for key, value in (summary.get("library_summary") or {}).items():
            if key == "artwork_provider_writes":
                providers.update(value or {})
            elif isinstance(value, (int, float)):
                overall[key] += int(value)
    total_titles = sum(int(summary.get("total_items", 0)) for summary in summaries)
    artwork_written = sum(
        overall[key]
        for key in (
            "poster_downloaded", "poster_upgraded",
            "background_downloaded", "background_upgraded",
            "season_poster_downloaded", "season_poster_upgraded",
        )
    )
    artwork_adopted = sum(
        overall[key]
        for key in (
            "poster_adopted", "background_adopted", "season_poster_adopted"
        )
    )
    artwork_unchanged = sum(
        overall[key]
        for key in (
            "poster_skipped", "background_skipped", "season_poster_skipped"
        )
    )
    artwork_not_due = sum(
        overall[key]
        for key in (
            "poster_not_due", "background_not_due", "season_poster_not_due"
        )
    )
    artwork_preserved = sum(
        overall[key]
        for key in ("poster_preserved", "background_preserved", "season_poster_preserved")
    )
    artwork_missing = sum(
        overall[key]
        for key in ("poster_missing", "background_missing", "season_poster_missing")
    )
    artwork_failed = sum(
        overall[key]
        for key in ("poster_failed", "background_failed", "season_poster_failed")
    )
    artwork_deferred = sum(
        overall[key]
        for key in (
            "poster_deferred", "background_deferred", "season_poster_deferred"
        )
    )
    lines.extend(box_line(
        f"Overall | Titles={total_titles} | MetadataChanged="
        f"{overall['meta_downloaded'] + overall['meta_upgraded']} | "
        f"MetadataUnchanged={overall['meta_skipped']} | "
        f"MetadataFailed={overall['meta_failed']}",
        box_width,
    ))
    lines.extend(box_line(
        f"Artwork | Written={artwork_written} | Adopted={artwork_adopted} | "
        f"Unchanged={artwork_unchanged} | NotDue={artwork_not_due} | "
        f"Preserved={artwork_preserved} | Missing={artwork_missing} | "
        f"Deferred={artwork_deferred} | Failed={artwork_failed} | "
        f"AutomaticRelaxed={overall['artwork_automatic_relaxed']}",
        box_width,
    ))
    if providers:
        labels = {"tmdb": "TMDb", "fanart": "Fanart.tv", "plex": "Plex"}
        provider_text = ", ".join(
            f"{labels.get(name, name.title())}: {count}"
            for name, count in sorted(providers.items())
        )
        lines.extend(box_line(
            f"Artwork providers | Selected={provider_text}",
            box_width,
        ))

    for lib, summary in metadata_summaries.items():
        if summary is None:
            continue
        libsum = summary.get("library_summary", {})
        asset_size = int(
            libsum.get("artwork_bytes", library_filesize.get(lib, 0)) or 0
        )
        library_type = (summary.get("library_type", "") or "unknown").strip().lower()
        if library_type not in ("movie", "tv", "show"):
            if "movie" in lib.lower():
                library_type = "movie"
            elif "tv" in lib.lower() or "show" in lib.lower():
                library_type = "tv"
            else:
                library_type = "unknown"
                
        season_count = summary.get("season_count")
        episode_count = summary.get("episode_count")
        summary_line = (
            f"Library={lib} | Inventory | Titles={summary['total_items']}"
            + (
                f" | Seasons={season_count or 0} | Episodes={episode_count or 0}"
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
                    f"Library={lib} | Incremental | LibraryItems={library_items} | "
                    f"Processed={summary['total_items']} | UnchangedSkipped={incremental_skipped}",
                    box_width,
                )
            )
        metadata_summary = metadata_action_summary(libsum, feature_flags)
        if metadata_summary:
            lines.extend(box_line(f"Library={lib} | {metadata_summary}", box_width))
        if summary.get("percent_complete") is not None:
            percent_incomplete = summary.get("percent_incomplete")
            if percent_incomplete is None:
                percent_incomplete = 100 - summary["percent_complete"]
            lines.extend(box_line(
                f"Library={lib} | Metadata coverage | MeetsThreshold="
                f"{summary['complete']}/{summary['total_items']} "
                f"({summary['percent_complete']}%) | BelowThreshold="
                f"{summary['incomplete']} ({percent_incomplete}%)", box_width))

        if feature_flags and feature_flags.get("poster", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Library={lib} | Artwork poster | Downloaded={libsum.get('poster_downloaded', 0)} | "
                f"Upgraded={libsum.get('poster_upgraded', 0)} | Adopted={libsum.get('poster_adopted', 0)} | "
                f"Unchanged={libsum.get('poster_skipped', 0)} | NotDue={libsum.get('poster_not_due', 0)} | "
                f"Preserved={libsum.get('poster_preserved', 0)} | Missing={libsum.get('poster_missing', 0)} | "
                f"Deferred={libsum.get('poster_deferred', 0)} | Failed={libsum.get('poster_failed', 0)}", box_width))

        if feature_flags and feature_flags.get("background", False) and library_type in ("movie", "tv", "show"):
            lines.extend(box_line(
                f"Library={lib} | Artwork background | Downloaded={libsum.get('background_downloaded', 0)} | "
                f"Upgraded={libsum.get('background_upgraded', 0)} | Adopted={libsum.get('background_adopted', 0)} | "
                f"Unchanged={libsum.get('background_skipped', 0)} | NotDue={libsum.get('background_not_due', 0)} | "
                f"Preserved={libsum.get('background_preserved', 0)} | Missing={libsum.get('background_missing', 0)} | "
                f"Deferred={libsum.get('background_deferred', 0)} | Failed={libsum.get('background_failed', 0)}", box_width))

        if (
            feature_flags and feature_flags.get("season", False)
            and library_type in ("tv", "show")
            and (
                libsum.get('season_poster_downloaded', 0) > 0 or
                libsum.get('season_poster_upgraded', 0) > 0 or
                libsum.get('season_poster_adopted', 0) > 0 or
                libsum.get('season_poster_skipped', 0) > 0 or
                libsum.get('season_poster_not_due', 0) > 0 or
                libsum.get('season_poster_preserved', 0) > 0 or
                libsum.get('season_poster_missing', 0) > 0 or
                libsum.get('season_poster_deferred', 0) > 0 or
                libsum.get('season_poster_failed', 0) > 0
            )
        ):
            lines.extend(box_line(
                f"Library={lib} | Artwork season posters | Downloaded={libsum.get('season_poster_downloaded', 0)} | "
                f"Upgraded={libsum.get('season_poster_upgraded', 0)} | Adopted={libsum.get('season_poster_adopted', 0)} | "
                f"Unchanged={libsum.get('season_poster_skipped', 0)} | NotDue={libsum.get('season_poster_not_due', 0)} | "
                f"Preserved={libsum.get('season_poster_preserved', 0)} | Missing={libsum.get('season_poster_missing', 0)} | "
                f"Deferred={libsum.get('season_poster_deferred', 0)} | Failed={libsum.get('season_poster_failed', 0)}", box_width))

        library_providers = libsum.get("artwork_provider_writes") or {}
        if library_providers:
            labels = {"tmdb": "TMDb", "fanart": "Fanart.tv", "plex": "Plex"}
            provider_text = ", ".join(
                f"{labels.get(name, name.title())}: {count}"
                for name, count in sorted(library_providers.items())
            )
            lines.extend(box_line(
                f"Library={lib} | Artwork providers | Selected={provider_text}",
                box_width,
            ))

        poster_bytes = int(libsum.get("poster_bytes", 0) or 0)
        background_bytes = int(libsum.get("background_bytes", 0) or 0)
        season_bytes = int(libsum.get("season_poster_bytes", 0) or 0)
        metadata_bytes = int(libsum.get("metadata_bytes", 0) or 0)
        storage_scope = str(libsum.get("storage_scope") or "processed items")
        mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
        if mode == "kometa":
            metadata_storage = f"MetadataYAML={human_readable_size(metadata_bytes)}"
            accounted = asset_size + metadata_bytes
        else:
            metadata_storage = "PlexMetadata=Server-managed (not measurable)"
            accounted = asset_size
        lines.extend(
            box_line(
                f"Library={lib} | Storage | Scope={storage_scope} | "
                f"Artwork={human_readable_size(asset_size)} | "
                f"Posters={human_readable_size(poster_bytes)} | "
                f"Backgrounds={human_readable_size(background_bytes)} | "
                f"SeasonPosters={human_readable_size(season_bytes)} | "
                f"{metadata_storage} | AccountedTotal={human_readable_size(accounted)}"
            )
        )

    runtime_storage = _runtime_storage_bytes()
    lines.extend(
        box_line(
            "Runtime storage | "
            + " | ".join(
                f"{label}={human_readable_size(size)}"
                for label, size in runtime_storage.items()
            )
            + f" | Total={human_readable_size(sum(runtime_storage.values()))}"
        )
    )
    minimum_free = max(
        0,
        int(float(config.get("runtime", {}).get("min_free_space_mb", 256)) * 1024 * 1024),
    )
    for mount in _storage_mounts(config):
        usage = mount["usage"]
        free_percent = (usage.free / usage.total * 100) if usage.total else 0.0
        lines.extend(
            box_line(
                f"Filesystem={'+'.join(mount['labels'])} | Path={mount['path']} | "
                f"Used={human_readable_size(usage.used)} | "
                f"Free={human_readable_size(usage.free)} | "
                f"Capacity={human_readable_size(usage.total)} | "
                f"FreePercent={free_percent:.1f}%"
            )
        )
        if usage.free < minimum_free:
            storage_warnings.append(
                (
                    "+".join(mount["labels"]),
                    human_readable_size(usage.free),
                    human_readable_size(minimum_free),
                )
            )

    if cleanup_result is not None and getattr(cleanup_result, "failed_reason", None):
        cleanup_mode = str(getattr(cleanup_result, "mode", "unknown")).title()
        lines.extend(
            box_line(
                f"Cleanup | Status=Failed | Mode={cleanup_mode} | "
                f"Reason={cleanup_result.failed_reason}",
                box_width,
            )
        )
        lines.extend(
            box_line(
                "Cleanup confirmed before failure | "
                f"Titles={cleanup_result.titles} | "
                f"Seasons={cleanup_result.seasons} | "
                f"Episodes={cleanup_result.episodes}",
                box_width,
            )
        )
        lines.extend(
            box_line(
                f"Cleanup records | Cache={cleanup_result.cache_entries} | "
                f"KometaYAML={cleanup_result.yaml_entries}",
                box_width,
            )
        )
        lines.extend(
            box_line(
                f"Cleanup artwork | Removed={cleanup_result.assets} | "
                f"Preserved={cleanup_result.assets_preserved} | "
                f"Unchanged={cleanup_result.assets_skipped}",
                box_width,
            )
        )
        lines.extend(
            box_line(
                f"Cleanup failures | Total={cleanup_result.failures}",
                box_width,
            )
        )
    elif cleanup_result is not None and getattr(cleanup_result, "skipped_reason", None):
        lines.extend(
            box_line(
                f"Cleanup | Status=Skipped | Reason={cleanup_result.skipped_reason}",
                box_width,
            )
        )
    elif feature_flags and feature_flags.get("cleanup", False):
        if hasattr(cleanup_result, "titles"):
            action = "Would remove" if cleanup_result.dry_run else "Removed"
            status = "Preview" if cleanup_result.dry_run else "Completed"
            cleanup_mode = str(getattr(cleanup_result, "mode", "unknown")).title()
            scope = (
                "State only; Kometa YAML and artwork preserved"
                if cleanup_mode.lower() == "plex"
                else "Generated Kometa output and state"
            )
            lines.extend(
                box_line(
                    f"Cleanup | Status={status} | Mode={cleanup_mode} | Scope={scope}",
                    box_width,
                )
            )
            lines.extend(
                box_line(
                    f"Cleanup stale inventory | Titles={cleanup_result.titles} | "
                    f"Seasons={cleanup_result.seasons} | "
                    f"Episodes={cleanup_result.episodes}",
                    box_width,
                )
            )
            lines.extend(
                box_line(
                    f"Cleanup records | Action={action} | Cache="
                    f"{cleanup_result.cache_entries} | KometaYAML="
                    f"{cleanup_result.yaml_entries}",
                    box_width,
                )
            )
            lines.extend(
                box_line(
                    f"Cleanup artwork | Action={action} | Assets={cleanup_result.assets} | "
                    f"Preserved={cleanup_result.assets_preserved} | "
                    f"Unchanged={cleanup_result.assets_skipped}",
                    box_width,
                )
            )
            lines.extend(
                box_line(
                    f"Cleanup failures | Total={cleanup_result.failures}",
                    box_width,
                )
            )
        else:
            lines.extend(
                box_line(f"Cleanup | TitlesRemoved={cleanup_result or 0}", box_width)
            )
    if config["settings"].get("dry_run", False):
        lines.extend(box_line("Dry run | Completed | FilesWritten=0", box_width))
    for line in lines:
        logger.info(line)
    for filesystem, free, required in storage_warnings:
        logger.warning(
            "[Storage] Low free space | Filesystem=%s | Free=%s | Required=%s",
            filesystem,
            free,
            required,
        )
            
def human_readable_size(size, decimal_places=2):
    for unit in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0 or unit == 'TB':
            return f"{size:.{decimal_places}f} {unit}"
        size /= 1024.0
