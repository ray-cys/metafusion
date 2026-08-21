import copy
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml

from helper.logging import log_config_event
from helper.plex_paths import PlexPathError, parse_path_mappings
from helper.provider_mappings import validate_provider_mapping_config


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


def safe_path_mappings(val, default, key=None):
    if isinstance(val, list):
        return [str(item).strip() for item in val if str(item).strip()]
    if isinstance(val, str):
        return [item.strip() for item in val.split(";") if item.strip()]
    if key:
        log_config_event("invalid_env_var", key=key, value=val, default=default)
    return copy.deepcopy(default)


def safe_json_mapping(val, default, key=None):
    if isinstance(val, dict):
        return copy.deepcopy(val)
    try:
        parsed = json.loads(str(val))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    if key:
        log_config_event("invalid_env_var", key=key, value=val, default=default)
    return copy.deepcopy(default)

BASE_CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_FILE = BASE_CONFIG_DIR / "config.yml"
TEMPLATE_FILE = Path(__file__).parent.parent / "config" / "config_template.yml"
CACHE_DIR = BASE_CONFIG_DIR / "cache"

CONFIG_SCHEMA_FILE = Path(__file__).parent.parent / "config_schema.yml"


def _load_config_schema():
    try:
        schema = yaml.safe_load(CONFIG_SCHEMA_FILE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Unable to load configuration schema: {CONFIG_SCHEMA_FILE}") from error
    if not isinstance(schema, dict) or schema.get("schema_version") != 1:
        raise ConfigError("Unsupported or invalid configuration schema")
    return schema


_CONFIG_SCHEMA = _load_config_schema()
DEFAULT_CONFIG = copy.deepcopy(_CONFIG_SCHEMA["defaults"])
_CONVERTERS = {
    "string": None,
    "safe_int": safe_int,
    "safe_float": safe_float,
    "safe_bool": safe_bool,
    "safe_list": safe_list,
    "safe_path_mappings": safe_path_mappings,
    "safe_json_mapping": safe_json_mapping,
}
ENV_BINDINGS = tuple(
    (setting["name"], tuple(setting["path"]), _CONVERTERS[setting["converter"]])
    for setting in _CONFIG_SCHEMA["settings"]
    if "path" in setting
)
SECRET_FILE_BINDINGS = tuple(
    (setting["name"], tuple(setting["secret_file"]["path"]), setting["secret_file"]["direct_env"])
    for setting in _CONFIG_SCHEMA["settings"]
    if "secret_file" in setting
)

SECRET_PATHS = {
    ("plex", "token"),
    ("tmdb", "api_key"),
}
PLEX_METADATA_FIELDS = {
    "title",
    "originalTitle",
    "originallyAvailableAt",
    "contentRating",
    "studio",
    "tagline",
    "summary",
    "country",
    "genre",
    "director",
    "writer",
    "producer",
}


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
        (("cleanup", "run_cleanup"), "Cleanup Libraries"),
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
    metadata = config.get("metadata", {})
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    plex_metadata_enabled = bool(
        mode == "plex" and config.get("plex_metadata", {}).get("enabled", False)
    )
    metadata_available = mode != "plex" or plex_metadata_enabled
    if metadata_available and metadata.get("run_enhanced", False):
        metadata_mode = "Enhanced"
    elif metadata_available and metadata.get("run_basic", False):
        metadata_mode = "Basic"
    else:
        metadata_mode = "Disabled"
    def enabled_label(value):
        return "Enabled" if bool(value) else "Disabled"
    log_config_event(
        "feature_profile",
        mode=mode.title(),
        metadata=metadata_mode,
        plex_metadata=enabled_label(
            plex_metadata_enabled
        ),
        poster=enabled_label(config.get("assets", {}).get("run_poster", True)),
        season=enabled_label(config.get("assets", {}).get("run_season", True)),
        background=enabled_label(
            config.get("assets", {}).get("run_background", False)
        ),
        cleanup=enabled_label(config.get("cleanup", {}).get("run_cleanup", False)),
        dry_run=enabled_label(config.get("settings", {}).get("dry_run", False)),
    )

def get_feature_flags(config):
    plex_metadata = config.get("plex_metadata", {})
    direct_plex_metadata = mode_check(config, "plex") and plex_metadata.get(
        "enabled", False
    )
    feature_flags = {
        "mode": str(config.get("settings", {}).get("mode", "kometa")).lower(),
        "dry_run": config.get("settings", {}).get("dry_run", False),
        "metadata_basic": config.get("metadata", {}).get("run_basic", True)
        and (not mode_check(config, "plex") or direct_plex_metadata),
        "metadata_enhanced": config.get("metadata", {}).get("run_enhanced", True)
        and (not mode_check(config, "plex") or direct_plex_metadata),
        "plex_metadata": direct_plex_metadata,
        "poster": config.get("assets", {}).get("run_poster", True),
        "season": config.get("assets", {}).get("run_season", True),
        "background": config.get("assets", {}).get("run_background", False),
        "cleanup": config.get("cleanup", {}).get("run_cleanup", False),
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
    if parent_key in {
        "library_overrides",
        "tmdb.split_series_mappings",
        "tmdb.episode_overrides",
    }:
        return
    for key in user_cfg:
        if key not in default_cfg:
            full_key = f"{parent_key}.{key}" if parent_key else key
            log_config_event("unknown_key", key=full_key)
        elif isinstance(user_cfg[key], dict) and isinstance(default_cfg[key], dict):
            warn_unknown_keys(user_cfg[key], default_cfg[key], parent_key=f"{parent_key}.{key}" if parent_key else key)

def merge_config_dicts(default, user, sources=None, prefix=(), source="config.yml"):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(default.get(k), dict):
            merge_config_dicts(
                default[k],
                v,
                sources=sources,
                prefix=(*prefix, k),
                source=source,
            )
        else:
            default[k] = v
            if sources is not None:
                _mark_sources(v, source, sources, (*prefix, k))


def config_for_library(config, library_name):
    """Apply an advanced library's artwork cadence over global defaults."""
    effective = copy.deepcopy(config)
    effective["_library_name"] = library_name
    override = config.get("library_overrides", {}).get(library_name, {})
    if isinstance(override, dict):
        upgrades = override.get("image_upgrades")
        if isinstance(upgrades, dict):
            effective["image_upgrades"].update(upgrades)
        plex_metadata = override.get("plex_metadata")
        if isinstance(plex_metadata, dict):
            effective["plex_metadata"].update(plex_metadata)
    return effective

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
        elif converter in (safe_list, safe_path_mappings):
            return isinstance(raw_value, (str, list))
        elif converter is safe_json_mapping:
            if isinstance(raw_value, dict):
                return True
            return isinstance(json.loads(str(raw_value)), dict)
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
    plex_metadata = config.get("plex_metadata", {})
    kometa = config.get("kometa", {})
    compatibility = config.get("compatibility", {})

    errors.extend(validate_provider_mapping_config(tmdb))

    mode = str(settings.get("mode", "")).lower()
    if mode not in {"kometa", "plex"}:
        errors.append("settings.mode must be either 'kometa' or 'plex'")
    profile = str(compatibility.get("profile", "auto")).strip().lower()
    if profile not in {"auto", "kometa-2.4", "plex-api-v1"}:
        errors.append(
            "compatibility.profile must be auto, kometa-2.4, or plex-api-v1"
        )
    elif profile == "kometa-2.4" and mode != "kometa":
        errors.append("compatibility profile kometa-2.4 requires Kometa mode")
    elif profile == "plex-api-v1" and mode != "plex":
        errors.append("compatibility profile plex-api-v1 requires Plex mode")
    if mode == "kometa" and not str(settings.get("path", "")).strip():
        errors.append("settings.path is required in Kometa mode")
    if plex_metadata.get("enabled", False) and mode != "plex":
        errors.append("plex_metadata.enabled requires settings.mode 'plex'")
    tag_policy = str(kometa.get("tag_policy", "append")).lower()
    if tag_policy not in {"append", "sync"}:
        errors.append("kometa.tag_policy must be append or sync")
    policy = str(plex_metadata.get("policy", "fill_missing")).lower()
    if policy not in {"fill_missing", "managed", "overwrite"}:
        errors.append(
            "plex_metadata.policy must be fill_missing, managed, or overwrite"
        )
    if policy == "overwrite" and not plex_metadata.get("allow_overwrite", False):
        errors.append(
            "plex_metadata.allow_overwrite must be true for overwrite policy"
        )
    fields = plex_metadata.get("fields", [])
    if not isinstance(fields, list):
        errors.append("plex_metadata.fields must be a list")
    elif set(fields) - PLEX_METADATA_FIELDS:
        errors.append(
            "plex_metadata.fields contains unsupported fields: "
            + ", ".join(sorted(set(fields) - PLEX_METADATA_FIELDS))
        )
    mappings = plex.get("path_mappings", [])
    if not isinstance(mappings, list):
        errors.append("plex.path_mappings must be a list")
    else:
        try:
            parse_path_mappings(mappings)
        except PlexPathError as error:
            errors.append(str(error))

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
    if not isinstance(libraries, list):
        errors.append("plex_libraries must be a list or use auto discovery")
    elif any(str(value).strip().casefold() == "auto" for value in libraries) and len(
        [value for value in libraries if str(value).strip()]
    ) > 1:
        errors.append("plex_libraries auto cannot be combined with explicit names")

    if settings.get("schedule", False):
        run_times = settings.get("run_times")
        if not isinstance(run_times, list) or not run_times:
            errors.append("settings.run_times must contain at least one HH:MM value")
        else:
            for run_time in run_times:
                try:
                    datetime.strptime(  # noqa: DTZ007 -- validates a local wall clock
                        str(run_time), "%H:%M"
                    )
                except (TypeError, ValueError):
                    errors.append(f"Invalid schedule time: {run_time!r}; expected HH:MM")

    numeric_rules = (
        ("settings.log_max_mb", settings.get("log_max_mb"), 0, 1024),
        ("settings.log_backup_count", settings.get("log_backup_count"), 1, 365),
        (
            "settings.schedule_catch_up_max_hours",
            settings.get("schedule_catch_up_max_hours"),
            0.1,
            168,
        ),
        ("runtime.max_concurrency", runtime.get("max_concurrency"), 0, 64),
        ("runtime.request_timeout", runtime.get("request_timeout"), 1, 600),
        ("runtime.connect_timeout", runtime.get("connect_timeout"), 1, 120),
        ("runtime.plex_timeout", runtime.get("plex_timeout"), 1, 600),
        ("runtime.plex_retries", runtime.get("plex_retries"), 1, 10),
        ("runtime.plex_retry_delay", runtime.get("plex_retry_delay"), 0, 60),
        ("runtime.shutdown_timeout", runtime.get("shutdown_timeout"), 1, 300),
        ("runtime.max_image_mb", runtime.get("max_image_mb"), 1, 250),
        ("runtime.min_free_space_mb", runtime.get("min_free_space_mb"), 0, 1048576),
        (
            "incremental.full_scan_interval_hours",
            config.get("incremental", {}).get("full_scan_interval_hours"),
            1,
            8760,
        ),
        (
            "incremental.metadata_pending_recheck_hours",
            config.get("incremental", {}).get("metadata_pending_recheck_hours"),
            0.1,
            8760,
        ),
        (
            "image_upgrades.default_days",
            config.get("image_upgrades", {}).get("default_days"),
            0,
            3650,
        ),
        ("tmdb_cache.ttl_hours", config.get("tmdb_cache", {}).get("ttl_hours"), 0.1, 8760),
        ("tmdb_cache.negative_ttl_hours", config.get("tmdb_cache", {}).get("negative_ttl_hours"), 0.1, 168),
        ("tmdb_cache.max_entries", config.get("tmdb_cache", {}).get("max_entries"), 0, 100000),
        ("tmdb_cache.max_mb", config.get("tmdb_cache", {}).get("max_mb"), 0, 102400),
        ("output.backup_count", config.get("output", {}).get("backup_count"), 0, 50),
        (
            "output.report_retention",
            config.get("output", {}).get("report_retention"),
            1,
            1000,
        ),
        (
            "cleanup.quarantine_days",
            config.get("cleanup", {}).get("quarantine_days"),
            1,
            3650,
        ),
        ("plex_metadata.recheck_days", plex_metadata.get("recheck_days"), 0, 3650),
        (
            "plex_metadata.max_writes_per_run",
            plex_metadata.get("max_writes_per_run"),
            1,
            100000,
        ),
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
    library_overrides = config.get("library_overrides", {})
    if not isinstance(library_overrides, dict):
        errors.append("library_overrides must be a mapping keyed by Plex library name")
    else:
        allowed_override_keys = {"image_upgrades", "plex_metadata"}
        allowed_image_keys = {
            "default_days", "movie_days", "series_days", "season_days"
        }
        for library_name, override in library_overrides.items():
            if not isinstance(override, dict):
                errors.append(f"library_overrides.{library_name} must be a mapping")
                continue
            unexpected = set(override) - allowed_override_keys
            if unexpected:
                errors.append(
                    f"library_overrides.{library_name} contains unsupported keys: "
                    + ", ".join(sorted(unexpected))
                )
            upgrades = override.get("image_upgrades", {})
            if not isinstance(upgrades, dict):
                errors.append(
                    f"library_overrides.{library_name}.image_upgrades must be a mapping"
                )
                continue
            unexpected = set(upgrades) - allowed_image_keys
            if unexpected:
                errors.append(
                    f"library_overrides.{library_name}.image_upgrades contains unsupported keys: "
                    + ", ".join(sorted(unexpected))
                )
            for key, value in upgrades.items():
                if value is None:
                    continue
                try:
                    numeric_value = float(value)
                except (TypeError, ValueError):
                    errors.append(
                        f"library_overrides.{library_name}.image_upgrades.{key} must be numeric or null"
                    )
                    continue
                if numeric_value < 0 or numeric_value > 3650:
                    errors.append(
                        f"library_overrides.{library_name}.image_upgrades.{key} must be between 0 and 3650"
                    )
            metadata_override = override.get("plex_metadata", {})
            if not isinstance(metadata_override, dict):
                errors.append(
                    f"library_overrides.{library_name}.plex_metadata must be a mapping"
                )
            else:
                allowed_metadata_keys = {
                    "enabled", "policy", "lock_writes", "lock_merged_tags",
                    "allow_overwrite", "recheck_days", "max_writes_per_run",
                    "fields",
                }
                unexpected = set(metadata_override) - allowed_metadata_keys
                if unexpected:
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata contains unsupported keys: "
                        + ", ".join(sorted(unexpected))
                    )
                effective_metadata = {**plex_metadata, **metadata_override}
                if effective_metadata.get("enabled", False) and mode != "plex":
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata.enabled "
                        "requires settings.mode 'plex'"
                    )
                override_policy = str(
                    effective_metadata.get("policy", "fill_missing")
                ).lower()
                if override_policy not in {"fill_missing", "managed", "overwrite"}:
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata.policy "
                        "must be fill_missing, managed, or overwrite"
                    )
                if override_policy == "overwrite" and not effective_metadata.get(
                    "allow_overwrite", False
                ):
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata.allow_overwrite "
                        "must be true for overwrite policy"
                    )
                override_fields = effective_metadata.get("fields", [])
                if not isinstance(override_fields, list):
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata.fields must be a list"
                    )
                elif set(override_fields) - PLEX_METADATA_FIELDS:
                    errors.append(
                        f"library_overrides.{library_name}.plex_metadata.fields contains unsupported fields: "
                        + ", ".join(
                            sorted(set(override_fields) - PLEX_METADATA_FIELDS)
                        )
                    )
                for key, minimum, maximum in (
                    ("recheck_days", 0, 3650),
                    ("max_writes_per_run", 1, 100000),
                ):
                    try:
                        number = float(effective_metadata.get(key))
                    except (TypeError, ValueError):
                        errors.append(
                            f"library_overrides.{library_name}.plex_metadata.{key} must be numeric"
                        )
                        continue
                    if number < minimum or number > maximum:
                        errors.append(
                            f"library_overrides.{library_name}.plex_metadata.{key} "
                            f"must be between {minimum} and {maximum}"
                        )
    try:
        if float(runtime.get("request_timeout")) < float(runtime.get("connect_timeout")):
            errors.append("runtime.request_timeout must be at least runtime.connect_timeout")
    except (TypeError, ValueError):
        pass

    cleanup = config.get("cleanup", {})
    try:
        confirmations = int(cleanup.get("confirmation_scans", 2))
        if confirmations < 1 or confirmations > 100:
            errors.append("cleanup.confirmation_scans must be between 1 and 100")
    except (TypeError, ValueError):
        errors.append("cleanup.confirmation_scans must be an integer")
    try:
        grace_hours = float(cleanup.get("grace_hours", 48.0))
        if grace_hours < 0 or grace_hours > 8760:
            errors.append("cleanup.grace_hours must be between 0 and 8760")
    except (TypeError, ValueError):
        errors.append("cleanup.grace_hours must be numeric")

    metadata = config.get("metadata", {})
    assets = config.get("assets", {})
    asset_policy = str(assets.get("update_policy", "managed")).strip().lower()
    if asset_policy not in {"fill_missing", "managed", "overwrite"}:
        errors.append(
            "assets.update_policy must be one of fill_missing, managed, overwrite"
        )
    if metadata.get("run_enhanced", False) and not metadata.get("run_basic", False):
        errors.append("metadata.run_basic must be enabled for enhanced metadata")
    override_metadata_enabled = any(
        isinstance(override, dict)
        and isinstance(override.get("plex_metadata"), dict)
        and override["plex_metadata"].get("enabled", False)
        for override in config.get("library_overrides", {}).values()
    ) if isinstance(config.get("library_overrides", {}), dict) else False
    metadata_processing = metadata.get("run_basic", False) and (
        mode == "kometa"
        or plex_metadata.get("enabled", False)
        or override_metadata_enabled
    )
    any_processing = metadata_processing or any(
        assets.get(key, False) for key in ("run_poster", "run_season", "run_background")
    ) or config.get("cleanup", {}).get("run_cleanup", False)
    if not any_processing:
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


def config_source_overview(config, sources):
    """Return a redacted, human-readable summary of effective config sources."""
    selection = sources.get((), {})
    yaml_source = selection.get("yaml_source")
    source_values = tuple(
        source for source in sources.values() if isinstance(source, str)
    )
    direct_environment = {name for name, _path, _converter in ENV_BINDINGS}
    secret_environment = {name for name, _path, _direct in SECRET_FILE_BINDINGS}
    return {
        "config_file": selection.get("path") or "None",
        "selection": selection.get("strategy", "environment and defaults"),
        "yaml_values": sum(source == yaml_source for source in source_values)
        if yaml_source
        else 0,
        "environment_overrides": sum(
            source in direct_environment for source in source_values
        ),
        "secret_file_overrides": sum(
            source in secret_environment for source in source_values
        ),
        "cli_overrides": sum(source == "CLI" for source in source_values),
    }


def _discover_config_file(config_dir, environ):
    """Select one conventional YAML source without silently merging profiles."""
    config_dir = Path(config_dir)
    conventional = config_dir / "config.yml"
    if conventional.exists():
        return conventional, "conventional config.yml", None

    profiles = {
        mode: config_dir / f"{mode}.yml"
        for mode in ("kometa", "plex")
        if (config_dir / f"{mode}.yml").exists()
    }
    requested_mode = str(environ.get("RUN_MODE", "")).strip().lower()
    if len(profiles) == 1:
        profile_mode, profile_path = next(iter(profiles.items()))
        if requested_mode in {"kometa", "plex"} and requested_mode != profile_mode:
            raise ConfigError(
                f"RUN_MODE={requested_mode} conflicts with the only run-type "
                f"configuration file: {profile_path}"
            )
        return profile_path, "single run-type profile", profile_mode
    if len(profiles) == 2:
        if requested_mode in profiles:
            return profiles[requested_mode], "RUN_MODE-selected profile", requested_mode
        raise ConfigError(
            "Both /config/kometa.yml and /config/plex.yml exist. Set RUN_MODE "
            "to select one, keep only the active file, or use /config/config.yml."
        )
    return conventional, "environment and defaults", None

def mode_check(config, mode="kometa"):
    return config.get("settings", {}).get("mode", "kometa").lower() == mode.lower()


def report_retention(config):
    """Return the global per-report-type retention with a safe default."""
    try:
        return max(1, int(config.get("output", {}).get("report_retention", 10)))
    except (AttributeError, TypeError, ValueError):
        return 10

def load_config_file(
    config_file=None,
    template_file=None,
    environ=None,
    create_if_missing=True,
    return_sources=False,
):
    environ = os.environ if environ is None else environ
    if config_file is None:
        config_file, selection_strategy, profile_mode = _discover_config_file(
            BASE_CONFIG_DIR, environ
        )
    else:
        config_file = Path(config_file)
        selection_strategy = "explicit file"
        profile_mode = (
            config_file.stem
            if config_file.name in {"kometa.yml", "plex.yml"}
            else None
        )
    template_file = Path(template_file) if template_file is not None else TEMPLATE_FILE
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
    sources = dict.fromkeys(_leaf_paths(config), "default")
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
                if not isinstance(user_config, dict):
                    raise yaml.YAMLError("Configuration root must be a mapping")
                if profile_mode:
                    profile_settings = user_config.setdefault("settings", {})
                    if not isinstance(profile_settings, dict):
                        raise yaml.YAMLError("Configuration settings must be a mapping")
                    declared_mode = str(
                        profile_settings.get("mode", profile_mode)
                    ).strip().lower()
                    if declared_mode != profile_mode:
                        raise ConfigError(
                            f"Run-type configuration {config_file} declares "
                            f"settings.mode={declared_mode!r}; expected {profile_mode!r}"
                        )
                    profile_settings.setdefault("mode", profile_mode)
                warn_unknown_keys(user_config, DEFAULT_CONFIG)
                yaml_source = f"YAML: {config_file.name}"
                merge_config_dicts(
                    config,
                    user_config,
                    sources=sources,
                    source=yaml_source,
                )
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
    sources[()] = {
        "path": str(config_file) if config_file.exists() else None,
        "strategy": selection_strategy,
        "profile": profile_mode,
        "yaml_source": f"YAML: {config_file.name}" if config_file.exists() else None,
    }
    if return_sources:
        return config, sources
    return config
