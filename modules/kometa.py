import shutil
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


def validate_metadata_document(document):
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
    return True


def _prune_backups(backup_dir, filename, keep):
    backups = sorted(
        backup_dir.glob(f"{filename}.*.bak"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old_backup in backups[max(0, keep):]:
        old_backup.unlink()


def write_kometa_metadata(path, document, validate_schema=True, backup_count=3):
    path = Path(path)
    if validate_schema:
        validate_metadata_document(document)

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
            validate_metadata_document(written)
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
