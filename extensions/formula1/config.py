"""Strict, isolated configuration for the Formula 1 extension."""

import copy
import os
import tempfile
from pathlib import Path

import yaml

from helper.io import atomic_replace_file

TRUE_VALUES = {"1", "true", "yes", "on"}
TEMPLATE_SOURCE = Path(__file__).with_name("formula1_template.yml")
FILE_MODE = 0o664


class Formula1ConfigError(RuntimeError):
    """Raised when the extension's private configuration is unsafe or invalid."""


def formula1_requested(config, environ=None):
    """Return true only for an explicit opt-in while Kometa mode is active."""
    environ = os.environ if environ is None else environ
    enabled = str(environ.get("FORMULA1_ENABLED", "")).strip().casefold()
    mode = str(config.get("settings", {}).get("mode", "kometa")).casefold()
    return mode == "kometa" and enabled in TRUE_VALUES


def _merge(base, overrides):
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _read_yaml(path):
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise Formula1ConfigError(f"Unable to read Formula 1 configuration: {path}") from error
    if not isinstance(value, dict):
        raise Formula1ConfigError("Formula 1 configuration root must be a mapping")
    return value


def _bounded_int(value, name, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise Formula1ConfigError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise Formula1ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value, name, minimum, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise Formula1ConfigError(f"{name} must be numeric") from error
    if not minimum <= parsed <= maximum:
        raise Formula1ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _resolve_under(root, value, name):
    root = Path(root).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise Formula1ConfigError(f"{name} must stay under {root}")
    return candidate


def sync_formula1_template(root):
    """Copy the packaged value-free template without replacing active config."""
    root = Path(root)
    destination = root / "formula1_template.yml"
    root.mkdir(parents=True, exist_ok=True)
    packaged = TEMPLATE_SOURCE.read_bytes()
    if destination.exists() and destination.read_bytes() == packaged:
        os.chmod(destination, FILE_MODE)
        return False
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=root, prefix=".formula1_template.", suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(packaged)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace_file(temporary, destination, new_file_mode=FILE_MODE)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True


def load_formula1_config(core_config, base_config_dir, *, dry_run=False):
    """Load private defaults and optional user overrides without touching core config."""
    root = Path(base_config_dir) / "formula1"
    defaults = _read_yaml(TEMPLATE_SOURCE)
    active = root / "formula1.yml"
    overrides = _read_yaml(active) if active.exists() else {}
    config = _merge(defaults, overrides)
    if not dry_run:
        sync_formula1_template(root)

    library = config.setdefault("library", {})
    library["name"] = str(library.get("name", "Formula 1")).strip()
    if not library["name"]:
        raise Formula1ConfigError("library.name must not be empty")
    profile = str(library.get("naming_profile", "auto")).casefold()
    if profile not in {"auto", "current", "kometa"}:
        raise Formula1ConfigError("library.naming_profile must be auto, current, or kometa")
    library["naming_profile"] = profile

    artwork = config.setdefault("artwork", {})
    artwork["width"] = _bounded_int(artwork.get("width", 1000), "artwork.width", 600, 3000)
    artwork["height"] = _bounded_int(artwork.get("height", 1500), "artwork.height", 900, 4500)
    if artwork["height"] * 2 != artwork["width"] * 3:
        raise Formula1ConfigError("artwork dimensions must use a 2:3 aspect ratio")
    if str(artwork.get("policy", "managed")).casefold() != "managed":
        raise Formula1ConfigError("artwork.policy currently supports only managed")

    show_artwork = config.setdefault("show_artwork", {})
    show_artwork["enabled"] = bool(show_artwork.get("enabled", True))
    show_artwork["poster_width"] = _bounded_int(
        show_artwork.get("poster_width", 1000), "show_artwork.poster_width", 600, 3000
    )
    show_artwork["poster_height"] = _bounded_int(
        show_artwork.get("poster_height", 1500), "show_artwork.poster_height", 900, 4500
    )
    if show_artwork["poster_height"] * 2 != show_artwork["poster_width"] * 3:
        raise Formula1ConfigError("show artwork poster dimensions must use a 2:3 aspect ratio")
    show_artwork["background_width"] = _bounded_int(
        show_artwork.get("background_width", 1920),
        "show_artwork.background_width",
        1280,
        3840,
    )
    show_artwork["background_height"] = _bounded_int(
        show_artwork.get("background_height", 1080),
        "show_artwork.background_height",
        720,
        2160,
    )
    if show_artwork["background_width"] * 9 != show_artwork["background_height"] * 16:
        raise Formula1ConfigError("show artwork background dimensions must use a 16:9 aspect ratio")
    if str(show_artwork.get("trigger", "plex_new_race")).casefold() != "plex_new_race":
        raise Formula1ConfigError("show_artwork.trigger currently supports only plex_new_race")
    show_artwork["trigger"] = "plex_new_race"
    if str(show_artwork.get("policy", "managed")).casefold() != "managed":
        raise Formula1ConfigError("show_artwork.policy currently supports only managed")
    show_artwork["policy"] = "managed"
    show_artwork["minimum_source_width"] = _bounded_int(
        show_artwork.get("minimum_source_width", 1600),
        "show_artwork.minimum_source_width",
        800,
        5000,
    )
    show_artwork["minimum_source_height"] = _bounded_int(
        show_artwork.get("minimum_source_height", 900),
        "show_artwork.minimum_source_height",
        450,
        3000,
    )

    providers = config.setdefault("providers", {})
    providers["cache_hours"] = _bounded_float(
        providers.get("cache_hours", 24), "providers.cache_hours", 1, 720
    )
    providers["retries"] = _bounded_int(providers.get("retries", 3), "providers.retries", 1, 5)
    provider_url = str(providers.get("jolpica_url", "")).strip().rstrip("/")
    if not provider_url.startswith("https://"):
        raise Formula1ConfigError("providers.jolpica_url must use HTTPS")
    providers["jolpica_url"] = provider_url
    circuit_url = str(providers.get("circuit_svg_url", "")).strip().rstrip("/")
    if not circuit_url.startswith("https://"):
        raise Formula1ConfigError("providers.circuit_svg_url must use HTTPS")
    providers["circuit_svg_url"] = circuit_url
    formula1_url = str(providers.get("formula1_url", "")).strip().rstrip("/")
    if not formula1_url.startswith("https://"):
        raise Formula1ConfigError("providers.formula1_url must use HTTPS")
    providers["formula1_url"] = formula1_url
    manifest_url = str(providers.get("circuit_manifest_url", "")).strip()
    if not manifest_url.startswith("https://"):
        raise Formula1ConfigError("providers.circuit_manifest_url must use HTTPS")
    providers["circuit_manifest_url"] = manifest_url
    commons_url = str(providers.get("commons_url", "")).strip().rstrip("/")
    if not commons_url.startswith("https://commons.wikimedia.org/"):
        raise Formula1ConfigError("providers.commons_url must use Wikimedia Commons HTTPS")
    providers["commons_url"] = commons_url
    providers["commons_cache_hours"] = _bounded_float(
        providers.get("commons_cache_hours", 24),
        "providers.commons_cache_hours",
        1,
        168,
    )

    cleanup = config.setdefault("cleanup", {})
    cleanup["confirmation_scans"] = _bounded_int(
        cleanup.get("confirmation_scans", 2), "cleanup.confirmation_scans", 1, 20
    )
    cleanup["grace_hours"] = _bounded_float(
        cleanup.get("grace_hours", 48), "cleanup.grace_hours", 0, 8760
    )

    logging_config = config.setdefault("logging", {})
    level = str(logging_config.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise Formula1ConfigError("logging.level must be DEBUG, INFO, WARNING, or ERROR")
    console_value = logging_config.get("console", "summary")
    console = "off" if console_value is False else str(console_value).casefold()
    if console not in {"off", "summary", "full"}:
        raise Formula1ConfigError("logging.console must be off, summary, or full")
    logging_config.update(
        level=level,
        console=console,
        retention=_bounded_int(logging_config.get("retention", 20), "logging.retention", 1, 365),
    )

    kometa_root = Path(core_config.get("settings", {}).get("path", "/kometa")).resolve()
    config["paths"] = {
        "root": root,
        "database": root / "cache" / "formula1.sqlite3",
        "logs": root / "logs",
        "reports": root / "reports",
        "metadata": _resolve_under(kometa_root, "metadata", "metadata output"),
        "assets": _resolve_under(kometa_root, "assets/formula1/rounds", "artwork output"),
        "show_assets": _resolve_under(
            kometa_root, "assets/formula1/shows", "show artwork output"
        ),
        "show_image_cache": root / "cache" / "show-artwork",
        "branding": root / "branding",
    }
    config["dry_run"] = bool(dry_run)
    return config
