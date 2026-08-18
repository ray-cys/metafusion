import copy
import re

PROVIDER_KEY = re.compile(r"^(tmdb|tvdb|imdb):(.+)$", re.IGNORECASE)
EPISODE_KEY = re.compile(r"^S(\d+)E(\d+)$", re.IGNORECASE)


BUILTIN_SPLIT_SERIES_MAPPINGS = {
    "tvdb:345246": {
        "show_policy": "preserve",
        "seasons": {
            1: {"tmdb_id": "72844", "season_number": 1},
            2: {"tmdb_id": "109958", "season_number": 1},
        },
    },
    "tvdb:389492": {
        "show_policy": "preserve",
        "seasons": {
            1: {"tmdb_id": "113988", "season_number": 1},
            2: {"tmdb_id": "225634", "season_number": 1},
            3: {"tmdb_id": "286801", "season_number": 1},
        },
    },
}


def provider_identity_keys(tmdb_id=None, tvdb_id=None, imdb_id=None):
    keys = []
    for provider, value in (
        ("tvdb", tvdb_id),
        ("tmdb", tmdb_id),
        ("imdb", imdb_id),
    ):
        if value is not None and str(value).strip():
            keys.append(f"{provider}:{str(value).strip().lower()}")
    return keys


def _normalized_split_mapping(raw, default_show_policy="preserve"):
    if not isinstance(raw, dict):
        return None
    seasons = {}
    for plex_season, source in (raw.get("seasons") or {}).items():
        if not isinstance(source, dict) or source.get("tmdb_id") is None:
            continue
        try:
            plex_number = int(plex_season)
            tmdb_season = int(
                source.get("season_number", source.get("season", plex_number))
            )
        except (TypeError, ValueError):
            continue
        seasons[plex_number] = {
            "tmdb_id": str(source["tmdb_id"]),
            "season_number": tmdb_season,
        }
    if not seasons:
        return None
    policy = str(raw.get("show_policy") or default_show_policy).lower()
    if policy not in {"preserve", "primary"}:
        policy = default_show_policy
    return {"show_policy": policy, "seasons": seasons}


def _split_mapping_catalog(config):
    default_policy = str(
        config.get("tmdb", {}).get("split_series_show_policy", "preserve")
    ).lower()
    if default_policy not in {"preserve", "primary"}:
        default_policy = "preserve"
    catalog = copy.deepcopy(BUILTIN_SPLIT_SERIES_MAPPINGS)
    configured = config.get("tmdb", {}).get("split_series_mappings", {})
    if not isinstance(configured, dict):
        configured = {}
    for raw_key, raw_mapping in configured.items():
        key = str(raw_key).strip().lower()
        if not PROVIDER_KEY.match(key) or not isinstance(raw_mapping, dict):
            continue
        existing = catalog.get(key, {})
        merged = copy.deepcopy(existing)
        merged.update(
            {
                name: value
                for name, value in raw_mapping.items()
                if name != "seasons"
            }
        )
        merged["seasons"] = {
            **(existing.get("seasons") or {}),
            **(raw_mapping.get("seasons") or {}),
        }
        catalog[key] = merged
    return {
        key: normalized
        for key, value in catalog.items()
        if (normalized := _normalized_split_mapping(value, default_policy))
    }


def resolve_split_series_mapping(
    config, *, tmdb_id=None, tvdb_id=None, imdb_id=None
):
    """Resolve a split-series mapping by any stable provider identity."""
    catalog = _split_mapping_catalog(config)
    keys = provider_identity_keys(
        tmdb_id=tmdb_id, tvdb_id=tvdb_id, imdb_id=imdb_id
    )
    for key in keys:
        if key in catalog:
            return {"identity": key, **copy.deepcopy(catalog[key])}

    # Plex agents do not always expose TVDB. A primary TMDb installment can
    # still identify a unique configured split-series mapping.
    if tmdb_id is not None and not tvdb_id:
        wanted = str(tmdb_id)
        candidates = [
            (key, mapping)
            for key, mapping in catalog.items()
            if wanted
            in {
                str(source.get("tmdb_id"))
                for source in mapping.get("seasons", {}).values()
            }
        ]
        if len(candidates) == 1:
            key, mapping = candidates[0]
            return {"identity": key, **copy.deepcopy(mapping)}
    return None


def split_series_season_sources(
    config=None, *, tmdb_id=None, tvdb_id=None, imdb_id=None
):
    mapping = resolve_split_series_mapping(
        config or {}, tmdb_id=tmdb_id, tvdb_id=tvdb_id, imdb_id=imdb_id
    )
    return copy.deepcopy(mapping.get("seasons", {})) if mapping else {}


def _episode_pair(value):
    if isinstance(value, str):
        match = EPISODE_KEY.match(value.strip())
        if not match:
            return None
        pair = int(match.group(1)), int(match.group(2))
        return pair if pair[0] >= 0 and pair[1] >= 1 else None
    if isinstance(value, dict):
        season = value.get("season_number", value.get("season"))
        episode = value.get("episode_number", value.get("episode"))
        try:
            pair = int(season), int(episode)
            return pair if pair[0] >= 0 and pair[1] >= 1 else None
        except (TypeError, ValueError):
            return None
    return None


def resolve_episode_overrides(
    config, *, tmdb_id=None, tvdb_id=None, imdb_id=None
):
    """Return Plex episode -> TMDb episode overrides for one show."""
    configured = config.get("tmdb", {}).get("episode_overrides", {})
    if not isinstance(configured, dict):
        return {}
    identities = provider_identity_keys(
        tmdb_id=tmdb_id, tvdb_id=tvdb_id, imdb_id=imdb_id
    )
    split_mapping = resolve_split_series_mapping(
        config, tmdb_id=tmdb_id, tvdb_id=tvdb_id, imdb_id=imdb_id
    )
    if split_mapping and split_mapping.get("identity") not in identities:
        identities.append(split_mapping["identity"])
    normalized_config = {
        str(key).strip().lower(): value for key, value in configured.items()
    }
    raw_overrides = None
    for identity in identities:
        if identity in normalized_config:
            raw_overrides = normalized_config[identity]
            break
    if not isinstance(raw_overrides, dict):
        return {}
    normalized = {}
    for source, target in raw_overrides.items():
        source_pair = _episode_pair(source)
        target_pair = _episode_pair(target)
        if source_pair and target_pair:
            normalized[source_pair] = target_pair
    return normalized


def validate_provider_mapping_config(tmdb_config):
    errors = []
    policy = str(tmdb_config.get("split_series_show_policy", "preserve")).lower()
    if policy not in {"preserve", "primary"}:
        errors.append("tmdb.split_series_show_policy must be preserve or primary")

    mappings = tmdb_config.get("split_series_mappings", {})
    if not isinstance(mappings, dict):
        errors.append("tmdb.split_series_mappings must be a mapping")
    else:
        for identity, mapping in mappings.items():
            prefix = f"tmdb.split_series_mappings.{identity}"
            if not PROVIDER_KEY.match(str(identity).strip()):
                errors.append(f"{prefix} must use a tmdb:, tvdb:, or imdb: key")
                continue
            if not isinstance(mapping, dict):
                errors.append(f"{prefix} must be a mapping")
                continue
            unexpected = set(mapping) - {"show_policy", "seasons"}
            if unexpected:
                errors.append(
                    f"{prefix} contains unsupported keys: "
                    + ", ".join(sorted(unexpected))
                )
            mapping_policy = str(mapping.get("show_policy", policy)).lower()
            if mapping_policy not in {"preserve", "primary"}:
                errors.append(f"{prefix}.show_policy must be preserve or primary")
            seasons = mapping.get("seasons")
            if not isinstance(seasons, dict) or not seasons:
                errors.append(f"{prefix}.seasons must be a non-empty mapping")
                continue
            for plex_season, source in seasons.items():
                season_prefix = f"{prefix}.seasons.{plex_season}"
                try:
                    if int(plex_season) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{season_prefix} must use a non-negative season")
                if not isinstance(source, dict):
                    errors.append(f"{season_prefix} must be a mapping")
                    continue
                if set(source) - {"tmdb_id", "season", "season_number"}:
                    errors.append(f"{season_prefix} contains unsupported keys")
                if (
                    "season" in source
                    and "season_number" in source
                    and str(source["season"]) != str(source["season_number"])
                ):
                    errors.append(
                        f"{season_prefix}.season and season_number conflict"
                    )
                try:
                    if int(source.get("tmdb_id")) <= 0:
                        raise ValueError
                    target_season = source.get(
                        "season_number", source.get("season", plex_season)
                    )
                    if int(target_season) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(
                        f"{season_prefix} requires positive tmdb_id and "
                        "non-negative season_number"
                    )

    overrides = tmdb_config.get("episode_overrides", {})
    if not isinstance(overrides, dict):
        errors.append("tmdb.episode_overrides must be a mapping")
    else:
        for identity, mapping in overrides.items():
            prefix = f"tmdb.episode_overrides.{identity}"
            if not PROVIDER_KEY.match(str(identity).strip()):
                errors.append(f"{prefix} must use a tmdb:, tvdb:, or imdb: key")
                continue
            if not isinstance(mapping, dict):
                errors.append(f"{prefix} must be a mapping")
                continue
            targets = {}
            for source, target in mapping.items():
                source_pair = _episode_pair(source)
                target_pair = _episode_pair(target)
                if source_pair is None or target_pair is None:
                    errors.append(
                        f"{prefix}.{source} must map SxxExx to SxxExx or an "
                        "equivalent season/episode mapping"
                    )
                    continue
                previous_source = targets.setdefault(target_pair, source_pair)
                if previous_source != source_pair:
                    errors.append(
                        f"{prefix} maps multiple Plex episodes to "
                        f"S{target_pair[0]:02d}E{target_pair[1]:02d}"
                    )
    return errors
