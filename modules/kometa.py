import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml

from helper.io import atomic_write_yaml

EPISODE_BASIC_FIELDS = (
    "title",
    "sort_title",
    "originally_available",
    "summary",
)
EPISODE_ENHANCED_FIELDS = (
    "director",
    "writer",
)

KOMETA_TAG_FIELDS = {
    "movie": {"collection", "country", "director", "genre", "label", "producer", "writer"},
    "show": {"collection", "genre", "label"},
    "season": {"collection", "label"},
    "episode": {"collection", "director", "label", "writer"},
}

KOMETA_GENERATED_FIELDS = {
    "movie": {
        "sort_title",
        "original_title",
        "originally_available",
        "content_rating",
        "studio",
        "tagline",
        "summary",
    },
    "show": {
        "sort_title",
        "original_title",
        "originally_available",
        "content_rating",
        "studio",
        "tagline",
        "summary",
        "seasons",
    },
    "season": {"title", "summary", "episodes"},
    "episode": set(EPISODE_BASIC_FIELDS) | set(EPISODE_ENHANCED_FIELDS),
}

# These keys were emitted by older MetaFusion builds but are not valid metadata
# edits for the corresponding Kometa item type.
KOMETA_DEPRECATED_FIELDS = {
    "movie": {"cast", "cast.sync", "runtime"},
    "show": {"cast", "cast.sync", "country", "country.sync", "runtime"},
    "season": {"cast", "cast.sync", "originally_available", "runtime"},
    "episode": {"cast", "cast.sync", "runtime"},
}


def kometa_tag_key(field, policy="append"):
    """Return the documented Kometa tag key for the selected update policy."""
    return f"{field}.sync" if str(policy).lower() == "sync" else field


def _has_source_value(value):
    return value not in (None, "", [])


def _numeric_key(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _allowed_generated_key(item_type, key):
    if key in KOMETA_GENERATED_FIELDS[item_type]:
        return True
    base = key.removesuffix(".sync")
    return base in KOMETA_TAG_FIELDS[item_type]


def _match_first(entry):
    """Return a metadata entry with Kometa's selector before edit fields."""
    if not isinstance(entry, dict) or "match" not in entry:
        return entry
    return {
        "match": entry["match"],
        **{key: value for key, value in entry.items() if key != "match"},
    }


def normalize_metadata_order(document):
    """Move each top-level Kometa match block before its metadata edits.

    YAML mappings do not require an order, but emitting Kometa's documented
    layout makes generated files predictable and easier to audit. The return
    value lets callers detect order-only changes because dict equality ignores
    insertion order.
    """
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if not isinstance(metadata, dict):
        return 0
    changed = 0
    for item_name, entry in list(metadata.items()):
        if (
            isinstance(entry, dict)
            and "match" in entry
            and next(iter(entry), None) != "match"
        ):
            metadata[item_name] = _match_first(entry)
            changed += 1
    return changed


def validate_generated_metadata(entry, item_type):
    """Reject unsupported fields produced by MetaFusion before they reach YAML."""
    if item_type not in KOMETA_GENERATED_FIELDS:
        raise KometaSchemaError(f"unsupported generated metadata type: {item_type}")
    if not isinstance(entry, dict):
        raise KometaSchemaError(f"generated {item_type} metadata must be a mapping")
    for key, value in entry.items():
        if key == "match":
            continue
        if not _allowed_generated_key(item_type, key):
            raise KometaSchemaError(
                f"unsupported generated {item_type} metadata field: {key}"
            )
        if item_type == "show" and key == "seasons":
            for season in (value or {}).values():
                validate_generated_metadata(season, "season")
        elif item_type == "season" and key == "episodes":
            for episode in (value or {}).values():
                validate_generated_metadata(episode, "episode")
    return True


def _remove_deprecated(entry, item_type, diagnostics):
    for key in KOMETA_DEPRECATED_FIELDS[item_type]:
        if key in entry:
            entry.pop(key, None)
            diagnostics["deprecated_removed"] += 1


def _merge_fields(existing, generated, item_type, diagnostics):
    merged = deepcopy(existing) if isinstance(existing, dict) else {}
    _remove_deprecated(merged, item_type, diagnostics)
    for key, value in generated.items():
        if key in {"match", "seasons", "episodes"}:
            continue
        if not _has_source_value(value):
            if _has_source_value(merged.get(key)):
                diagnostics["existing_preserved"] += 1
            else:
                diagnostics["source_missing"] += 1
            continue
        base = key.removesuffix(".sync")
        if base in KOMETA_TAG_FIELDS[item_type]:
            # Avoid leaving both append and sync variants after a policy change.
            alternate = base if key.endswith(".sync") else f"{base}.sync"
            merged.pop(alternate, None)
        merged[key] = deepcopy(value)
        diagnostics["available"] += 1
    return merged


def merge_generated_metadata(
    existing,
    generated,
    item_type,
    authoritative_seasons=None,
    authoritative_episodes=None,
):
    """Merge MetaFusion-owned values without discarding manual Kometa fields.

    Missing TMDb values preserve existing YAML. Numeric seasons and episodes are
    removed only when Plex's inventory authoritatively says they no longer exist.
    """
    validate_generated_metadata(generated, item_type)
    diagnostics = {
        "available": 0,
        "source_missing": 0,
        "existing_preserved": 0,
        "deprecated_removed": 0,
        "inventory_removed": 0,
    }
    merged = _merge_fields(existing, generated, item_type, diagnostics)
    if "match" in generated:
        merged["match"] = deepcopy(generated["match"])
    merged = _match_first(merged)

    if item_type != "show":
        return merged, diagnostics

    seasons = deepcopy(merged.get("seasons", {}))
    if not isinstance(seasons, dict):
        seasons = {}
    authoritative_seasons = (
        {int(number) for number in authoritative_seasons}
        if authoritative_seasons is not None
        else None
    )
    if authoritative_seasons is not None:
        for key in list(seasons):
            number = _numeric_key(key)
            if number is not None and number not in authoritative_seasons:
                seasons.pop(key, None)
                diagnostics["inventory_removed"] += 1

    for key, season in list(seasons.items()):
        number = _numeric_key(key)
        if number is None or not isinstance(season, dict):
            continue
        _remove_deprecated(season, "season", diagnostics)
        episodes = season.get("episodes")
        if not isinstance(episodes, dict):
            continue
        valid_episodes = None
        if authoritative_episodes is not None:
            valid_episodes = {
                int(value)
                for value in authoritative_episodes.get(
                    number, authoritative_episodes.get(str(number), [])
                )
            }
        for episode_key, episode in list(episodes.items()):
            episode_number = _numeric_key(episode_key)
            if (
                valid_episodes is not None
                and episode_number is not None
                and episode_number not in valid_episodes
            ):
                episodes.pop(episode_key, None)
                diagnostics["inventory_removed"] += 1
            elif isinstance(episode, dict):
                _remove_deprecated(episode, "episode", diagnostics)

    existing_by_number = {
        _numeric_key(key): key for key in seasons if _numeric_key(key) is not None
    }
    for season_number, season_patch in (generated.get("seasons") or {}).items():
        number = int(season_number)
        existing_key = existing_by_number.get(number, season_number)
        season_existing = seasons.pop(existing_key, {})
        season_merged = _merge_fields(
            season_existing, season_patch, "season", diagnostics
        )
        episodes = deepcopy(season_merged.get("episodes", {}))
        if not isinstance(episodes, dict):
            episodes = {}
        valid_episodes = None
        if authoritative_episodes is not None:
            valid_episodes = {
                int(value)
                for value in authoritative_episodes.get(
                    number, authoritative_episodes.get(str(number), [])
                )
            }
            for key in list(episodes):
                episode_number = _numeric_key(key)
                if episode_number is not None and episode_number not in valid_episodes:
                    episodes.pop(key, None)
                    diagnostics["inventory_removed"] += 1
        existing_episode_keys = {
            _numeric_key(key): key
            for key in episodes
            if _numeric_key(key) is not None
        }
        for episode_number, episode_patch in (season_patch.get("episodes") or {}).items():
            episode_number = int(episode_number)
            existing_episode_key = existing_episode_keys.get(
                episode_number, episode_number
            )
            episode_existing = episodes.pop(existing_episode_key, {})
            episodes[episode_number] = _merge_fields(
                episode_existing, episode_patch, "episode", diagnostics
            )
        if episodes or "episodes" in season_patch:
            season_merged["episodes"] = episodes
        seasons[number] = season_merged
    if seasons or "seasons" in generated:
        merged["seasons"] = seasons
    return merged, diagnostics


def remove_deprecated_metadata_fields(document, library_type):
    """Remove only fields previously generated by MetaFusion that Kometa rejects."""
    normalized_type = (
        "show" if str(library_type).lower() in {"tv", "show", "shows"} else "movie"
    )
    removed = 0
    for entry in (document.get("metadata", {}) if isinstance(document, dict) else {}).values():
        if not isinstance(entry, dict):
            continue
        for key in KOMETA_DEPRECATED_FIELDS[normalized_type]:
            if key in entry:
                entry.pop(key, None)
                removed += 1
        if normalized_type != "show":
            continue
        for season in (entry.get("seasons", {}) or {}).values():
            if not isinstance(season, dict):
                continue
            for key in KOMETA_DEPRECATED_FIELDS["season"]:
                if key in season:
                    season.pop(key, None)
                    removed += 1
            for episode in (season.get("episodes", {}) or {}).values():
                if not isinstance(episode, dict):
                    continue
                for key in KOMETA_DEPRECATED_FIELDS["episode"]:
                    if key in episode:
                        episode.pop(key, None)
                        removed += 1
    return removed


def build_episode_metadata(episode, directors=None, writers=None, enhanced=True):
    name = episode.get("name") or ""
    metadata = {
        "title": name,
        "sort_title": name,
        "originally_available": episode.get("air_date") or "",
        "summary": episode.get("overview") or "",
    }
    if enhanced:
        metadata["director"] = [name for name in (directors or []) if name]
        metadata["writer"] = [name for name in (writers or []) if name]
    return metadata


class KometaSchemaError(ValueError):
    pass


def _numeric_mapping_key(value):
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, str) and value.isdigit()


def validate_metadata_document(document, library_type=None):
    """Validate the stable structural contract documented for Kometa metadata files."""
    if not isinstance(document, dict):
        raise KometaSchemaError("metadata document root must be a mapping")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise KometaSchemaError("metadata document must contain a metadata mapping")

    for item_name, item in metadata.items():
        if not isinstance(item_name, str) or not item_name.strip():
            raise KometaSchemaError("metadata item names must be non-empty strings")
        if not isinstance(item, dict):
            raise KometaSchemaError(f"metadata item {item_name!r} must be a mapping")
        match = item.get("match")
        if match is not None and not isinstance(match, dict):
            raise KometaSchemaError(f"match for {item_name!r} must be a mapping")
        if isinstance(match, dict) and not any(
            key in match for key in ("mapping_id", "title", "edition", "blank_edition")
        ):
            raise KometaSchemaError(
                f"match for {item_name!r} must include a supported matching field"
            )

        normalized_type = (
            "show" if str(library_type or "").lower() in {"tv", "show", "shows"}
            else "movie" if str(library_type or "").lower() in {"movie", "movies"}
            else None
        )
        if normalized_type:
            invalid = KOMETA_DEPRECATED_FIELDS[normalized_type] & set(item)
            if invalid:
                raise KometaSchemaError(
                    f"unsupported {normalized_type} metadata fields for {item_name!r}: "
                    + ", ".join(sorted(invalid))
                )

        seasons = item.get("seasons")
        if seasons is None:
            continue
        if not isinstance(seasons, dict):
            raise KometaSchemaError(f"seasons for {item_name!r} must be a mapping")
        for season_number, season in seasons.items():
            if not _numeric_mapping_key(season_number):
                raise KometaSchemaError(
                    f"season key {season_number!r} for {item_name!r} must be numeric"
                )
            if not isinstance(season, dict):
                raise KometaSchemaError(
                    f"season {season_number!r} for {item_name!r} must be a mapping"
                )
            if normalized_type == "show":
                invalid = KOMETA_DEPRECATED_FIELDS["season"] & set(season)
                if invalid:
                    raise KometaSchemaError(
                        f"unsupported season metadata fields for {item_name!r}: "
                        + ", ".join(sorted(invalid))
                    )
            episodes = season.get("episodes")
            if episodes is None:
                continue
            if not isinstance(episodes, dict):
                raise KometaSchemaError(
                    f"episodes in season {season_number!r} for {item_name!r} must be a mapping"
                )
            for episode_number, episode in episodes.items():
                if not _numeric_mapping_key(episode_number) or not isinstance(episode, dict):
                    raise KometaSchemaError(
                        f"episode {episode_number!r} in {item_name!r} must be a numeric mapping"
                    )
                if normalized_type == "show":
                    invalid = KOMETA_DEPRECATED_FIELDS["episode"] & set(episode)
                    if invalid:
                        raise KometaSchemaError(
                            f"unsupported episode metadata fields for {item_name!r}: "
                            + ", ".join(sorted(invalid))
                        )
    return True


def _prune_backups(backup_dir, filename, keep):
    backups = sorted(
        backup_dir.glob(f"{filename}.*.bak"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[max(0, keep):]:
        old_backup.unlink()


def write_kometa_metadata(
    path,
    document,
    validate_schema=True,
    backup_count=3,
    library_type=None,
    expected_snapshot=None,
):
    path = Path(path)
    document = deepcopy(document)
    normalize_metadata_order(document)
    if validate_schema:
        validate_metadata_document(document, library_type=library_type)

    if expected_snapshot is not None:
        expected_exists, expected_digest = expected_snapshot
        current_exists = path.exists()
        current_digest = None
        if current_exists:
            from helper.io import sha256_file

            current_digest = sha256_file(path)
        if (current_exists, current_digest) != (expected_exists, expected_digest):
            raise RuntimeError(
                f"Kometa metadata changed while MetaFusion was processing: {path}"
            )

    backup = None
    backup_count = max(0, int(backup_count))
    if path.exists() and backup_count:
        backup_dir = path.parent / ".metafusion-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"{path.name}.{timestamp}.bak"
        shutil.copy2(path, backup)

    replaced = False
    try:
        atomic_write_yaml(path, document)
        replaced = True
        if validate_schema:
            written = yaml.safe_load(path.read_text(encoding="utf-8"))
            validate_metadata_document(written, library_type=library_type)
    except Exception:
        if replaced:
            if backup and backup.exists():
                shutil.copy2(backup, path)
            elif path.exists():
                path.unlink()
        raise
    finally:
        if backup:
            _prune_backups(backup.parent, path.name, backup_count)
    return backup
