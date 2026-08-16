import copy
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from helper.logging import log_config_event


class ConfigError(RuntimeError):
    pass

def safe_int(val, default, key=None):
    try:
        return int(val)
    except (TypeError, ValueError):
        if key:
            log_config_event("invalid_env_var", key=key, value=val, default=default)
        return default

def safe_float(val, default, key=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        if key:
            log_config_event("invalid_env_var", key=key, value=val, default=default)
        return default

def safe_bool(val, default, key=None):
    if isinstance(val, bool):
        return val
    normalized = str(val).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    if key:
        log_config_event("invalid_env_var", key=key, value=val, default=default)
    return default

def safe_list(val, default, key=None):
    if isinstance(val, list):
        return [str(item).strip() for item in val if str(item).strip()]
    if isinstance(val, str):
        return [item.strip() for item in val.split(",") if item.strip()]
    if key:
        log_config_event("invalid_env_var", key=key, value=val, default=default)
    return copy.deepcopy(default)

BASE_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_FILE = BASE_CONFIG_DIR / "config.yml"
TEMPLATE_FILE = Path(__file__).parent.parent / "config_template.yml"
CACHE_DIR = BASE_CONFIG_DIR / "cache"

DEFAULT_CONFIG = {
    "metafusion_run": False,
    "settings": {
        "schedule": True,
        "run_times": ["06:00", "18:30"],
        "dry_run": False,
        "log_level": "INFO",
        "mode": "kometa",
        "path": "/kometa/",
    },
    "plex": {
        "url": "http://10.0.0.1:32400",
        "token": "PLEX_TOKEN",
    },
    "plex_libraries": ["Movies", "TV Shows"],
    "tmdb": {
        "api_key": "TMDB_API_KEY",
        "language": "en",
        "fallback": ["zh", "ja"],
        "region": "US",
    },
    "metadata": {
        "run_basic": True,
        "run_enhanced": True,
    },
    "assets": {
        "run_poster": True,
        "run_season": True,
        "run_background": False,
    },
    "cleanup": {
        "run_process": False,
    },
    "runtime": {
        "max_concurrency": 8,
        "request_timeout": 30.0,
        "connect_timeout": 10.0,
        "plex_timeout": 10.0,
        "plex_retries": 3,
        "plex_retry_delay": 1.0,
        "shutdown_timeout": 15.0,
        "max_image_mb": 25,
    },
    "incremental": {
        "enabled": True,
        "full_scan_interval_hours": 168.0,
    },
    "image_upgrades": {
        "default_days": 30.0,
        "movie_days": None,
        "series_days": None,
        "season_days": None,
    },
    "tmdb_cache": {
        "enabled": True,
        "ttl_hours": 24.0,
        "max_entries": 5000,
    },
    "output": {
        "validate_schema": True,
        "backup_count": 3,
    },
    "safety": {
        "allow_ambiguous_editions": False,
    },
    "poster_set": {
        "max_width": 2000,
        "max_height": 3000,
        "min_width": 1000,
        "min_height": 1500,
        "prefer_vote": 5.0,
        "vote_relaxed": 3.5,
        "vote_threshold": 5.0,
    },
    "season_set": {
        "max_width": 2000,
        "max_height": 3000,
        "min_width": 1000,
        "min_height": 1500,
        "prefer_vote": 5.0,
        "vote_relaxed": 0.5,
        "vote_threshold": 3.0,
    },
    "background_set": {
        "max_width": 3840,
        "max_height": 2160,
        "min_width": 1920,
        "min_height": 1080,
        "prefer_vote": 5.0,
        "vote_relaxed": 3.5,
        "vote_threshold": 5.0,
    },
}

ENV_BINDINGS = (
    ("METAFUSION_RUN", ("metafusion_run",), safe_bool),
    ("RUN_SCHEDULE", ("settings", "schedule"), safe_bool),
    ("RUN_TIMES", ("settings", "run_times"), safe_list),
    ("DRY_RUN", ("settings", "dry_run"), safe_bool),
    ("LOG_LEVEL", ("settings", "log_level"), None),
    ("RUN_MODE", ("settings", "mode"), None),
    ("KOMETA_PATH", ("settings", "path"), None),
    ("PLEX_URL", ("plex", "url"), None),
    ("PLEX_TOKEN", ("plex", "token"), None),
    ("PLEX_LIBRARIES", ("plex_libraries",), safe_list),
    ("TMDB_API_KEY", ("tmdb", "api_key"), None),
    ("TMDB_LANGUAGE", ("tmdb", "language"), None),
    ("TMDB_LANGUAGE_FALLBACK", ("tmdb", "fallback"), safe_list),
    ("TMDB_REGION", ("tmdb", "region"), None),
    ("RUN_BASIC", ("metadata", "run_basic"), safe_bool),
    ("RUN_ENHANCED", ("metadata", "run_enhanced"), safe_bool),
    ("RUN_POSTER", ("assets", "run_poster"), safe_bool),
    ("RUN_SEASON", ("assets", "run_season"), safe_bool),
    ("RUN_BACKGROUND", ("assets", "run_background"), safe_bool),
    ("RUN_PROCESS", ("cleanup", "run_process"), safe_bool),
    ("RUN_CLEANUP", ("cleanup", "run_process"), safe_bool),
    ("MAX_CONCURRENCY", ("runtime", "max_concurrency"), safe_int),
    ("REQUEST_TIMEOUT", ("runtime", "request_timeout"), safe_float),
    ("CONNECT_TIMEOUT", ("runtime", "connect_timeout"), safe_float),
    ("PLEX_TIMEOUT", ("runtime", "plex_timeout"), safe_float),
    ("PLEX_RETRIES", ("runtime", "plex_retries"), safe_int),
    ("PLEX_RETRY_DELAY", ("runtime", "plex_retry_delay"), safe_float),
    ("SHUTDOWN_TIMEOUT", ("runtime", "shutdown_timeout"), safe_float),
    ("MAX_IMAGE_MB", ("runtime", "max_image_mb"), safe_int),
    ("INCREMENTAL", ("incremental", "enabled"), safe_bool),
    ("FULL_SCAN_INTERVAL_HOURS", ("incremental", "full_scan_interval_hours"), safe_float),
    ("IMAGE_UPGRADE_DAYS", ("image_upgrades", "default_days"), safe_float),
    ("MOVIE_IMAGE_UPGRADE_DAYS", ("image_upgrades", "movie_days"), safe_float),
    ("SERIES_IMAGE_UPGRADE_DAYS", ("image_upgrades", "series_days"), safe_float),
    ("SEASON_IMAGE_UPGRADE_DAYS", ("image_upgrades", "season_days"), safe_float),
    ("TMDB_CACHE_ENABLED", ("tmdb_cache", "enabled"), safe_bool),
    ("TMDB_CACHE_TTL_HOURS", ("tmdb_cache", "ttl_hours"), safe_float),
    ("TMDB_CACHE_MAX_ENTRIES", ("tmdb_cache", "max_entries"), safe_int),
    ("VALIDATE_OUTPUT", ("output", "validate_schema"), safe_bool),
    ("OUTPUT_BACKUP_COUNT", ("output", "backup_count"), safe_int),
    ("ALLOW_AMBIGUOUS_EDITIONS", ("safety", "allow_ambiguous_editions"), safe_bool),
    ("POSTER_MAX_WIDTH", ("poster_set", "max_width"), safe_int),
    ("POSTER_MAX_HEIGHT", ("poster_set", "max_height"), safe_int),
    ("POSTER_MIN_WIDTH", ("poster_set", "min_width"), safe_int),
    ("POSTER_MIN_HEIGHT", ("poster_set", "min_height"), safe_int),
    ("POSTER_PREFER_VOTE", ("poster_set", "prefer_vote"), safe_float),
    ("POSTER_VOTE_RELAXED", ("poster_set", "vote_relaxed"), safe_float),
    ("POSTER_VOTE_THRESHOLD", ("poster_set", "vote_threshold"), safe_float),
    ("SEASON_MAX_WIDTH", ("season_set", "max_width"), safe_int),
    ("SEASON_MAX_HEIGHT", ("season_set", "max_height"), safe_int),
    ("SEASON_MIN_WIDTH", ("season_set", "min_width"), safe_int),
    ("SEASON_MIN_HEIGHT", ("season_set", "min_height"), safe_int),
    ("SEASON_PREFER_VOTE", ("season_set", "prefer_vote"), safe_float),
    ("SEASON_VOTE_RELAXED", ("season_set", "vote_relaxed"), safe_float),
    ("SEASON_VOTE_THRESHOLD", ("season_set", "vote_threshold"), safe_float),
    ("BG_MAX_WIDTH", ("background_set", "max_width"), safe_int),
    ("BG_MAX_HEIGHT", ("background_set", "max_height"), safe_int),
    ("BG_MIN_WIDTH", ("background_set", "min_width"), safe_int),
    ("BG_MIN_HEIGHT", ("background_set", "min_height"), safe_int),
    ("BG_PREFER_VOTE", ("background_set", "prefer_vote"), safe_float),
    ("BG_VOTE_RELAXED", ("background_set", "vote_relaxed"), safe_float),
    ("BG_VOTE_THRESHOLD", ("background_set", "vote_threshold"), safe_float),
)

SECRET_FILE_BINDINGS = (
    ("PLEX_TOKEN_FILE", ("plex", "token"), "PLEX_TOKEN"),
    ("TMDB_API_KEY_FILE", ("tmdb", "api_key"), "TMDB_API_KEY"),
)

SECRET_PATHS = {("plex", "token"), ("tmdb", "api_key")}


def _leaf_paths(value, prefix=()):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _leaf_paths(nested, (*prefix, key))
    else:
        yield prefix


def _mark_sources(value, source, sources, prefix=()):
    for path in _leaf_paths(value, prefix):
        sources[path] = source

def get_disabled_features(config, logger):
    features = [
        (("metadata", "run_basic"), "Metadata Extraction"),
        (("metadata", "run_enhanced"), "Enhanced Metadata Extraction"),
        (("assets", "run_poster"), "Poster Assets Download"),
        (("assets", "run_season"), "Season Assets Download"),
        (("assets", "run_background"), "Background Assets Download"),
        (("cleanup", "run_process"), "Cleanup Libraries"),
    ]
    for key_tuple, feature in features:
        sub_config = config
        for k in key_tuple:
            sub_config = sub_config.get(k, None)
            if sub_config is None:
                break
        enabled = bool(sub_config)
        event = "feature_enabled" if enabled else "feature_disabled"
        log_config_event(event, feature=feature)

def get_feature_flags(config):
    feature_flags = {
        "dry_run": config.get("settings", {}).get("dry_run", False),
        "metadata_basic": config.get("metadata", {}).get("run_basic", True),
        "metadata_enhanced": config.get("metadata", {}).get("run_enhanced", True),
        "poster": config.get("assets", {}).get("run_poster", True),
        "season": config.get("assets", {}).get("run_season", True),
        "background": config.get("assets", {}).get("run_background", False),
        "cleanup": config.get("cleanup", {}).get("run_process", False),
    }
    return feature_flags


def get_image_upgrade_days(config, media_type):
    """Return the effective forced artwork refresh interval for a media type."""
    upgrades = config.get("image_upgrades", {})
    key = {
        "movie": "movie_days",
        "series": "series_days",
        "tv": "series_days",
        "show": "series_days",
        "season": "season_days",
    }.get(str(media_type).lower())
    value = upgrades.get(key) if key else None
    if value is None:
        value = upgrades.get("default_days", 30.0)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 30.0

def warn_unknown_keys(user_cfg, default_cfg, parent_key=""):
    for key in user_cfg:
        if key not in default_cfg:
            full_key = f"{parent_key}.{key}" if parent_key else key
            log_config_event("unknown_key", key=full_key)
        elif isinstance(user_cfg[key], dict) and isinstance(default_cfg[key], dict):
            warn_unknown_keys(user_cfg[key], default_cfg[key], parent_key=f"{parent_key}.{key}" if parent_key else key)

def merge_config_dicts(default, user, sources=None, prefix=()):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(default.get(k), dict):
            merge_config_dicts(default[k], v, sources=sources, prefix=(*prefix, k))
        else:
            default[k] = v
            if sources is not None:
                _mark_sources(v, "config.yml", sources, (*prefix, k))

def _set_path(config, path, value):
    parent = config
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


def _valid_env_conversion(converter, raw_value):
    try:
        if converter is safe_int:
            int(raw_value)
        elif converter is safe_float:
            float(raw_value)
        elif converter is safe_bool:
            return isinstance(raw_value, bool) or str(raw_value).strip().lower() in {
                "true",
                "false",
                "1",
                "0",
                "yes",
                "no",
                "on",
                "off",
            }
        elif converter is safe_list:
            return isinstance(raw_value, (str, list))
        return True
    except (TypeError, ValueError):
        return False


def apply_secret_file_overrides(config, environ=None, sources=None):
    environ = os.environ if environ is None else environ
    for file_env, path, direct_env in SECRET_FILE_BINDINGS:
        file_value = environ.get(file_env)
        direct_value = environ.get(direct_env)
        if direct_value is not None and str(direct_value).strip():
            continue
        if file_value is None or not str(file_value).strip():
            continue
        secret_path = Path(str(file_value).strip())
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigError(f"Unable to read {file_env}: {secret_path}") from error
        if not secret:
            raise ConfigError(f"Secret file configured by {file_env} is empty")
        _set_path(config, path, secret)
        if sources is not None:
            sources[path] = file_env
    return config


def apply_env_overrides(config, environ=None, sources=None):
    environ = os.environ if environ is None else environ
    for env_name, path, converter in ENV_BINDINGS:
        if (
            env_name == "RUN_PROCESS"
            and environ.get("RUN_CLEANUP") is not None
            and str(environ.get("RUN_CLEANUP")).strip()
        ):
            continue
        if env_name not in environ:
            continue
        raw_value = environ[env_name]
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            continue
        parent = config
        for key in path[:-1]:
            parent = parent[key]
        key = path[-1]
        if converter is None:
            parent[key] = raw_value
        else:
            if not _valid_env_conversion(converter, raw_value):
                config.setdefault("_config_errors", []).append(
                    f"Invalid value for environment variable {env_name}"
                )
            parent[key] = converter(raw_value, parent[key], key=env_name)
        if sources is not None:
            sources[path] = env_name
    return config


def validate_config(config):
    errors = list(config.get("_config_errors", []))
    settings = config.get("settings", {})
    plex = config.get("plex", {})
    tmdb = config.get("tmdb", {})
    runtime = config.get("runtime", {})

    mode = str(settings.get("mode", "")).lower()
    if mode not in {"kometa", "plex"}:
        errors.append("settings.mode must be either 'kometa' or 'plex'")
    if mode == "kometa" and not str(settings.get("path", "")).strip():
        errors.append("settings.path is required in Kometa mode")

    plex_url = str(plex.get("url", "")).strip()
    parsed_url = urlparse(plex_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        errors.append("plex.url must be a complete http:// or https:// URL")
    elif parsed_url.username or parsed_url.password:
        errors.append("plex.url must not contain embedded credentials")

    plex_token = str(plex.get("token", "")).strip()
    if not plex_token or plex_token in {"PLEX_TOKEN", "YOUR_PLEX_TOKEN", "replace-with-plex-token"}:
        errors.append("Plex token is missing or still uses a placeholder")
    tmdb_key = str(tmdb.get("api_key", "")).strip()
    if not tmdb_key or tmdb_key in {"TMDB_API_KEY", "YOUR_TMDB_API_KEY", "replace-with-tmdb-api-key"}:
        errors.append("TMDb API key is missing or still uses a placeholder")

    libraries = config.get("plex_libraries")
    if not isinstance(libraries, list) or not any(str(value).strip() for value in libraries):
        errors.append("plex_libraries must contain at least one library name")

    if settings.get("schedule", False):
        run_times = settings.get("run_times")
        if not isinstance(run_times, list) or not run_times:
            errors.append("settings.run_times must contain at least one HH:MM value")
        else:
            for run_time in run_times:
                try:
                    datetime.strptime(str(run_time), "%H:%M")
                except (TypeError, ValueError):
                    errors.append(f"Invalid schedule time: {run_time!r}; expected HH:MM")

    numeric_rules = (
        ("runtime.max_concurrency", runtime.get("max_concurrency"), 1, 64),
        ("runtime.request_timeout", runtime.get("request_timeout"), 1, 600),
        ("runtime.connect_timeout", runtime.get("connect_timeout"), 1, 120),
        ("runtime.plex_timeout", runtime.get("plex_timeout"), 1, 600),
        ("runtime.plex_retries", runtime.get("plex_retries"), 1, 10),
        ("runtime.plex_retry_delay", runtime.get("plex_retry_delay"), 0, 60),
        ("runtime.shutdown_timeout", runtime.get("shutdown_timeout"), 1, 300),
        ("runtime.max_image_mb", runtime.get("max_image_mb"), 1, 250),
        (
            "incremental.full_scan_interval_hours",
            config.get("incremental", {}).get("full_scan_interval_hours"),
            1,
            8760,
        ),
        (
            "image_upgrades.default_days",
            config.get("image_upgrades", {}).get("default_days"),
            0,
            3650,
        ),
        ("tmdb_cache.ttl_hours", config.get("tmdb_cache", {}).get("ttl_hours"), 0.1, 8760),
        ("tmdb_cache.max_entries", config.get("tmdb_cache", {}).get("max_entries"), 1, 100000),
        ("output.backup_count", config.get("output", {}).get("backup_count"), 0, 50),
    )
    for name, value, minimum, maximum in numeric_rules:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            errors.append(f"{name} must be numeric")
            continue
        if numeric_value < minimum or numeric_value > maximum:
            errors.append(f"{name} must be between {minimum} and {maximum}")

    for key in ("movie_days", "series_days", "season_days"):
        value = config.get("image_upgrades", {}).get(key)
        if value is None:
            continue
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            errors.append(f"image_upgrades.{key} must be numeric or null")
            continue
        if numeric_value < 0 or numeric_value > 3650:
            errors.append(f"image_upgrades.{key} must be between 0 and 3650")
    try:
        if float(runtime.get("request_timeout")) < float(runtime.get("connect_timeout")):
            errors.append("runtime.request_timeout must be at least runtime.connect_timeout")
    except (TypeError, ValueError):
        pass

    metadata = config.get("metadata", {})
    assets = config.get("assets", {})
    dependent_features = metadata.get("run_enhanced", False) or any(
        assets.get(key, False) for key in ("run_poster", "run_season", "run_background")
    )
    if dependent_features and not metadata.get("run_basic", False):
        errors.append("metadata.run_basic must be enabled for enhanced metadata or artwork")
    if not metadata.get("run_basic", False) and not config.get("cleanup", {}).get("run_process", False):
        errors.append("No processing feature is enabled")

    for section_name in ("poster_set", "season_set", "background_set"):
        section = config.get(section_name, {})
        for dimension in ("width", "height"):
            try:
                minimum = int(section.get(f"min_{dimension}"))
                maximum = int(section.get(f"max_{dimension}"))
                if minimum <= 0 or maximum <= 0 or minimum > maximum:
                    errors.append(
                        f"{section_name}.min_{dimension} must be positive and no greater than max_{dimension}"
                    )
            except (TypeError, ValueError):
                errors.append(f"{section_name} {dimension} limits must be integers")
    return errors


def config_source_report(config, sources):
    report = []
    for path in sorted(_leaf_paths(config)):
        if path and str(path[0]).startswith("_"):
            continue
        dotted = ".".join(path)
        source = sources.get(path, "default")
        value = config
        for key in path:
            value = value[key]
        if path in SECRET_PATHS:
            state = "set" if value and str(value).strip() else "missing"
            report.append(f"{dotted}: {state} ({source})")
        else:
            report.append(f"{dotted}: {value!r} ({source})")
    return report

def mode_check(config, mode="kometa"):
    return config.get("settings", {}).get("mode", "kometa").lower() == mode.lower()

def load_config_file(
    config_file=None,
    template_file=None,
    environ=None,
    create_if_missing=True,
    return_sources=False,
):
    config_file = Path(config_file) if config_file is not None else CONFIG_FILE
    template_file = Path(template_file) if template_file is not None else TEMPLATE_FILE
    environ = os.environ if environ is None else environ
    has_config_env = any(
        env_name in environ
        and environ[env_name] is not None
        and str(environ[env_name]).strip()
        for env_name, _, _ in ENV_BINDINGS
    ) or any(
        file_env in environ
        and environ[file_env] is not None
        and str(environ[file_env]).strip()
        for file_env, _, _ in SECRET_FILE_BINDINGS
    )

    if not config_file.exists() and not has_config_env and create_if_missing:
        if template_file.exists():
            config_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(template_file, config_file)
            log_config_event("yaml_not_found", config_file=config_file)
        else:
            log_config_event("yaml_missing", config_file=config_file)

    config = copy.deepcopy(DEFAULT_CONFIG)
    sources = {path: "default" for path in _leaf_paths(config)}
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
                if not isinstance(user_config, dict):
                    raise yaml.YAMLError("Configuration root must be a mapping")
                warn_unknown_keys(user_config, DEFAULT_CONFIG)
                merge_config_dicts(config, user_config, sources=sources)
                log_config_event("config_loaded", config_file=config_file)
        except yaml.YAMLError:
            config.setdefault("_config_errors", []).append(
                f"Unable to parse configuration YAML: {config_file}"
            )
            log_config_event("yaml_parse_error", config_file=config_file)
        except OSError as error:
            raise ConfigError(f"Unable to read configuration file: {config_file}") from error
    else:
        log_config_event("config_missing", config_file=config_file)

    apply_secret_file_overrides(config, environ=environ, sources=sources)
    apply_env_overrides(config, environ=environ, sources=sources)
    if return_sources:
        return config, sources
    return config
