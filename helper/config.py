import copy
import os
import shutil
from pathlib import Path

import yaml

from helper.logging import log_config_event

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

def warn_unknown_keys(user_cfg, default_cfg, parent_key=""):
    for key in user_cfg:
        if key not in default_cfg:
            full_key = f"{parent_key}.{key}" if parent_key else key
            log_config_event("unknown_key", key=full_key)
        elif isinstance(user_cfg[key], dict) and isinstance(default_cfg[key], dict):
            warn_unknown_keys(user_cfg[key], default_cfg[key], parent_key=f"{parent_key}.{key}" if parent_key else key)

def merge_config_dicts(default, user):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(default.get(k), dict):
            merge_config_dicts(default[k], v)
        else:
            default[k] = v

def apply_env_overrides(config, environ=None):
    environ = os.environ if environ is None else environ
    for env_name, path, converter in ENV_BINDINGS:
        if env_name not in environ:
            continue
        parent = config
        for key in path[:-1]:
            parent = parent[key]
        key = path[-1]
        raw_value = environ[env_name]
        if converter is None:
            parent[key] = raw_value
        else:
            parent[key] = converter(raw_value, parent[key], key=env_name)
    return config

def mode_check(config, mode="kometa"):
    return config.get("settings", {}).get("mode", "kometa").lower() == mode.lower()

def load_config_file(config_file=None, template_file=None, environ=None):
    config_file = Path(config_file) if config_file is not None else CONFIG_FILE
    template_file = Path(template_file) if template_file is not None else TEMPLATE_FILE
    environ = os.environ if environ is None else environ
    has_config_env = any(env_name in environ for env_name, _, _ in ENV_BINDINGS)

    if not config_file.exists() and not has_config_env:
        if template_file.exists():
            config_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(template_file, config_file)
            log_config_event("yaml_not_found", config_file=config_file)
        else:
            log_config_event("yaml_missing", config_file=config_file)

    config = copy.deepcopy(DEFAULT_CONFIG)
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            try:
                user_config = yaml.safe_load(f) or {}
                if not isinstance(user_config, dict):
                    raise yaml.YAMLError("Configuration root must be a mapping")
                warn_unknown_keys(user_config, DEFAULT_CONFIG)
                merge_config_dicts(config, user_config)
                log_config_event("config_loaded", config_file=config_file)
            except yaml.YAMLError:
                log_config_event("yaml_parse_error", config_file=config_file)
    else:
        log_config_event("config_missing", config_file=config_file)

    return apply_env_overrides(config, environ=environ)
