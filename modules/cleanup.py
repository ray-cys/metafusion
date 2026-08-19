import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from helper.cache import load_cache, mark_cache_dirty
from helper.identity import cache_key_for_meta, metadata_key_for_meta
from helper.io import sha256_file
from helper.logging import log_cleanup_event
from modules.kometa import write_kometa_metadata


class CleanupError(RuntimeError):
    def __init__(self, message, *, result=None):
        super().__init__(message)
        self.result = result


@dataclass
class CleanupResult:
    titles: int = 0
    seasons: int = 0
    episodes: int = 0
    assets: int = 0
    cache_entries: int = 0
    yaml_entries: int = 0
    assets_preserved: int = 0
    assets_skipped: int = 0
    failures: int = 0
    dry_run: bool = False
    skipped_reason: Optional[str] = None
    failed_reason: Optional[str] = None
    mode: str = "kometa"


def normalize_library_type(value):
    media_type = (value or "").lower()
    if media_type == "movies":
        return "movie"
    if media_type in {"show", "shows"}:
        return "tv"
    return media_type


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _path_key(path):
    return str(Path(path).absolute())


def _directory_is_empty(path):
    directory = Path(path)
    return directory.is_dir() and not any(directory.iterdir())


def _cache_identity(key, entry):
    entry = entry if isinstance(entry, dict) else {}
    title = entry.get("title")
    year = safe_int(entry.get("year"))
    try:
        _, remainder = str(key).split(":", 1)
        parsed_title, parsed_year = remainder.rsplit(":", 1)
        title = title or parsed_title.strip()
        year = year if year is not None else safe_int(parsed_year.strip())
    except (ValueError, AttributeError):
        pass
    return (title, year) if title and year is not None else None


async def cleanup_title_orphans(
    config,
    feature_flags,
    asset_path=None,
    existing_assets=None,
    preloaded_plex_metadata=None,
    safe_library_types=None,
):
    mode = config.get("settings", {}).get("mode", "kometa")
    dry_run = feature_flags.get("dry_run", False)
    result = CleanupResult(dry_run=dry_run, mode=mode)

    safe_library_types = {
        normalize_library_type(value) for value in (safe_library_types or set())
    }
    safe_library_types &= {"movie", "tv"}
    log_cleanup_event(
        "cleanup_start",
        mode=mode.title(),
        scope=", ".join(sorted(safe_library_types)) or "none",
    )
    if preloaded_plex_metadata is None:
        result.skipped_reason = "Plex metadata was unavailable"
        log_cleanup_event("cleanup_error")
        return result
    if not safe_library_types:
        result.skipped_reason = "no fully scanned library type was available"
        log_cleanup_event("cleanup_unsafe_scope")
        return result

    run_metadata = feature_flags.get("metadata_basic", True) or feature_flags.get(
        "metadata_enhanced", True
    )
    run_poster = feature_flags.get("poster", True)
    run_season = feature_flags.get("season", True)
    run_background = feature_flags.get("background", True)

    safe_metadata = [
        meta
        for meta in preloaded_plex_metadata.values()
        if normalize_library_type(meta.get("library_type")) in safe_library_types
    ]
    if "tv" in safe_library_types and (run_metadata or run_season):
        incomplete_shows = [
            metadata_key_for_meta(meta)
            for meta in safe_metadata
            if normalize_library_type(meta.get("library_type")) == "tv"
            and not isinstance(meta.get("seasons_episodes"), dict)
        ]
        if incomplete_shows:
            result.skipped_reason = "Plex season/episode inventory was incomplete"
            log_cleanup_event(
                "cleanup_incomplete_episode_inventory",
                titles=", ".join(incomplete_shows),
            )
            return result

    valid_cache_keys = set()
    valid_yaml_keys = {"movie": set(), "tv": set()}
    metadata_by_yaml_key = {}
    for meta in safe_metadata:
        title = meta.get("title")
        year = meta.get("year")
        if title and year is not None:
            valid_cache_keys.add(cache_key_for_meta(meta))
            yaml_key = metadata_key_for_meta(meta)
            media_type = normalize_library_type(meta.get("library_type"))
            valid_yaml_keys[media_type].add(yaml_key)
            metadata_by_yaml_key[(media_type, yaml_key)] = meta

    try:
        cache = load_cache()
    except Exception as error:
        result.failures += 1
        result.failed_reason = f"state cache could not be loaded: {error}"
        raise CleanupError(
            "Failed to load cleanup state cache", result=result
        ) from error
    managed_assets = {}
    for key, raw_entry in cache.items():
        if not isinstance(raw_entry, dict):
            continue
        identity = _cache_identity(key, raw_entry)
        for asset_type in ("poster", "background"):
            path = raw_entry.get(f"{asset_type}_path")
            if path:
                managed_assets.setdefault(_path_key(path), []).append(
                    {
                        "checksum": raw_entry.get(f"{asset_type}_checksum"),
                        "identity": identity,
                        "season": None,
                    }
                )
        for season_number, season_entry in (raw_entry.get("seasons") or {}).items():
            if not isinstance(season_entry, dict) or not season_entry.get("season_path"):
                continue
            managed_assets.setdefault(
                _path_key(season_entry["season_path"]), []
            ).append(
                {
                    "checksum": season_entry.get("season_checksum"),
                    "identity": identity,
                    "season": str(season_number),
                }
            )

    removed_titles = set()
    removed_seasons = set()
    removed_episodes = set()
    removed_assets = set()
    orphaned_season_asset_paths = set()
    removed_summary = {}
    pending_season_cache_removals = []
    confirmed_outcomes_logged = False

    def record_title(
        identity, removal_type=None, asset_type=None, title_removed=False
    ):
        if identity is None:
            return
        if title_removed:
            removed_titles.add(identity)
        details = removed_summary.setdefault(
            identity, {"cache": False, "asset": [], "yaml": False}
        )
        if removal_type:
            details[removal_type] = True
        if asset_type and asset_type not in details["asset"]:
            details["asset"].append(asset_type)

    def cached_media_type(key, entry):
        if isinstance(entry, dict) and entry.get("media_type"):
            return normalize_library_type(entry.get("media_type"))
        return normalize_library_type(str(key).split(":", 1)[0])

    def refresh_result_counts():
        result.titles = len(removed_titles)
        result.seasons = len(removed_seasons)
        result.episodes = len(removed_episodes)
        result.assets = len(removed_assets)

    def log_confirmed_outcomes():
        nonlocal confirmed_outcomes_logged
        if removed_summary and not dry_run and not confirmed_outcomes_logged:
            log_cleanup_event(
                "cleanup_consolidated_removed", removed_summary=removed_summary
            )
            confirmed_outcomes_logged = True

    def cleanup_failure(message, reason, *, report_confirmed=True):
        result.failures += 1
        result.failed_reason = reason
        refresh_result_counts()
        if report_confirmed:
            log_confirmed_outcomes()
        return CleanupError(message, result=result)

    cache_keys_to_remove = [
        key
        for key in list(cache)
        if cached_media_type(key, cache.get(key)) in safe_library_types
        and key not in valid_cache_keys
    ]
    for key in cache_keys_to_remove:
        identity = _cache_identity(key, cache.get(key))
        if dry_run:
            record_title(identity, removal_type="cache", title_removed=True)
            result.cache_entries += 1
            log_cleanup_event("cleanup_dry_run", description="cache entry", path=key)

    for meta in safe_metadata:
        if normalize_library_type(meta.get("library_type")) != "tv":
            continue
        if not (run_metadata or run_season):
            continue
        cache_key = cache_key_for_meta(meta)
        cache_entry = cache.get(cache_key)
        if not isinstance(cache_entry, dict):
            continue
        valid_seasons = {
            str(number) for number in (meta.get("seasons_episodes") or {})
        }
        cached_season_keys = {
            str(number): number for number in (cache_entry.get("seasons") or {})
        }
        identity = (meta.get("title"), safe_int(meta.get("year")))
        if not identity[0] or identity[1] is None:
            identity = None
        for season_number in set(cached_season_keys) - valid_seasons:
            season_entry = cache_entry["seasons"][cached_season_keys[season_number]]
            if isinstance(season_entry, dict) and season_entry.get("season_path"):
                orphaned_season_asset_paths.add(_path_key(season_entry["season_path"]))
            if dry_run:
                if identity:
                    removed_seasons.add((*identity, season_number))
                    record_title(identity, removal_type="cache")
                result.cache_entries += 1
                log_cleanup_event(
                    "cleanup_dry_run",
                    description="season cache entry",
                    path=f"{cache_key} season {season_number}",
                )
            else:
                pending_season_cache_removals.append(
                    {
                        "cache_key": cache_key,
                        "season_key": cached_season_keys[season_number],
                        "show": meta.get("title"),
                        "year": meta.get("year"),
                        "season": season_number,
                    }
                )

    def _apply_cache_changes():
        if dry_run:
            return
        changed = False
        for key in cache_keys_to_remove:
            if key in cache:
                identity = _cache_identity(key, cache.get(key))
                del cache[key]
                result.cache_entries += 1
                record_title(identity, removal_type="cache", title_removed=True)
                changed = True
                log_cleanup_event("cleanup_removed_cache_entry", key=key)
        for removal in pending_season_cache_removals:
            cache_entry = cache.get(removal["cache_key"])
            seasons = cache_entry.get("seasons") if isinstance(cache_entry, dict) else None
            if not isinstance(seasons, dict) or removal["season_key"] not in seasons:
                continue
            del seasons[removal["season_key"]]
            cache[removal["cache_key"]] = cache_entry
            identity = (removal["show"], safe_int(removal["year"]))
            if identity[0] and identity[1] is not None:
                removed_seasons.add((*identity, str(removal["season"])))
                record_title(identity, removal_type="cache")
            result.cache_entries += 1
            changed = True
            log_cleanup_event(
                "cleanup_removed_orphaned_season_cache",
                show=removal["show"],
                year=removal["year"],
                season=removal["season"],
            )
        if changed:
            mark_cache_dirty()

    def apply_cache_changes():
        try:
            _apply_cache_changes()
        except Exception as error:
            log_cleanup_event("cleanup_failed_cache", error=str(error))
            raise cleanup_failure(
                "Failed to update cleanup state cache",
                "state cache changes could not be committed",
            ) from error

    def finish():
        refresh_result_counts()
        log_confirmed_outcomes()
        return result

    if mode == "plex":
        log_cleanup_event("cleanup_skipped_plex_mode")
        apply_cache_changes()
        return finish()

    preferred_filenames = {
        f"{library_type}_metadata.yml" for library_type in safe_library_types
    }
    metadata_dir = Path(config.get("settings", {}).get("path", ".")) / "metadata"

    def extract_title_year(orphan_title):
        if " (" in orphan_title and orphan_title.endswith(")"):
            title, year = orphan_title.rsplit(" (", 1)
            return title, safe_int(year.rstrip(")"))
        return orphan_title, None

    if run_metadata:
        metadata_documents = []
        for metadata_file in metadata_dir.glob("*.yml"):
            if metadata_file.name not in preferred_filenames:
                log_cleanup_event(
                    "cleanup_skipping_nonpreferred", filename=metadata_file.name
                )
                continue
            try:
                source_bytes = metadata_file.read_bytes()
                metadata_content = yaml.safe_load(source_bytes.decode("utf-8")) or {}
                metadata_entries = metadata_content.get("metadata", {})
                if not isinstance(metadata_entries, dict):
                    raise TypeError("metadata must be a mapping")
                metadata_documents.append(
                    (
                        metadata_file,
                        metadata_content,
                        metadata_entries,
                        (True, hashlib.sha256(source_bytes).hexdigest()),
                    )
                )
            except Exception as error:
                log_cleanup_event(
                    "cleanup_failed_remove_metadata",
                    filename=metadata_file,
                    error=str(error),
                )
                raise cleanup_failure(
                    f"Failed to clean metadata file: {metadata_file}",
                    f"Kometa YAML could not be read: {metadata_file.name}",
                ) from error

        for (
            metadata_file,
            metadata_content,
            metadata_entries,
            output_snapshot,
        ) in metadata_documents:
            try:
                pending_yaml_records = []
                pending_seasons = []
                pending_episodes = []
                pending_yaml_events = []
                pending_yaml_entries = 0
                file_media_type = normalize_library_type(
                    metadata_file.name.split("_", 1)[0]
                )

                cleaned_metadata = {
                    key: value
                    for key, value in metadata_entries.items()
                    if key in valid_yaml_keys.get(file_media_type, set())
                }
                yaml_changed = False
                orphaned_titles = set(metadata_entries) - set(cleaned_metadata)
                for yaml_key in orphaned_titles:
                    identity = extract_title_year(yaml_key)
                    if dry_run:
                        if identity[1] is not None:
                            record_title(
                                identity, removal_type="yaml", title_removed=True
                            )
                        result.yaml_entries += 1
                        log_cleanup_event(
                            "cleanup_dry_run",
                            description="metadata title",
                            path=f"{metadata_file.name}: {yaml_key}",
                        )
                    else:
                        pending_yaml_entries += 1
                        if identity[1] is not None:
                            pending_yaml_records.append((identity, True))
                if orphaned_titles and not dry_run:
                    yaml_changed = True

                for yaml_key, yaml_entry in cleaned_metadata.items():
                    if not isinstance(yaml_entry, dict):
                        continue
                    plex_meta = metadata_by_yaml_key.get(
                        (file_media_type, yaml_key)
                    )
                    if not plex_meta or normalize_library_type(
                        plex_meta.get("library_type")
                    ) != "tv":
                        continue
                    identity = (
                        plex_meta.get("title"),
                        safe_int(plex_meta.get("year")),
                    )
                    if not identity[0] or identity[1] is None:
                        identity = None
                    inventory = plex_meta.get("seasons_episodes") or {}
                    yaml_seasons = yaml_entry.get("seasons") or {}
                    if not isinstance(yaml_seasons, dict):
                        continue
                    yaml_season_keys = {
                        str(number): number for number in yaml_seasons
                    }
                    valid_seasons = {str(number) for number in inventory}
                    for season_number in set(yaml_season_keys) - valid_seasons:
                        if dry_run:
                            if identity:
                                removed_seasons.add((*identity, season_number))
                                record_title(identity, removal_type="yaml")
                            result.yaml_entries += 1
                            log_cleanup_event(
                                "cleanup_dry_run",
                                description="season metadata",
                                path=f"{yaml_key} season {season_number}",
                            )
                        else:
                            pending_yaml_entries += 1
                            pending_seasons.append((identity, season_number))
                            pending_yaml_records.append((identity, False))
                            pending_yaml_events.append(
                                (
                                    "cleanup_removed_orphaned_season_yaml",
                                    {
                                        "show": identity[0] if identity else yaml_key,
                                        "year": identity[1] if identity else "unknown",
                                        "season": season_number,
                                    },
                                )
                            )
                            del yaml_seasons[yaml_season_keys[season_number]]
                            yaml_changed = True

                    inventory_keys = {str(number): number for number in inventory}
                    for season_number in set(yaml_season_keys) & valid_seasons:
                        season_entry = yaml_seasons.get(
                            yaml_season_keys[season_number]
                        )
                        if not isinstance(season_entry, dict):
                            continue
                        episodes = season_entry.get("episodes")
                        if not isinstance(episodes, dict):
                            continue
                        episode_keys = {
                            str(number): number for number in episodes
                        }
                        valid_episodes = {
                            str(number)
                            for number in inventory[inventory_keys[season_number]]
                        }
                        for episode_number in set(episode_keys) - valid_episodes:
                            if dry_run:
                                if identity:
                                    removed_episodes.add(
                                        (*identity, season_number, episode_number)
                                    )
                                    record_title(identity, removal_type="yaml")
                                result.yaml_entries += 1
                                log_cleanup_event(
                                    "cleanup_dry_run",
                                    description="episode metadata",
                                    path=(
                                        f"{yaml_key} season {season_number} "
                                        f"episode {episode_number}"
                                    ),
                                )
                            else:
                                pending_yaml_entries += 1
                                pending_episodes.append(
                                    (identity, season_number, episode_number)
                                )
                                pending_yaml_records.append((identity, False))
                                pending_yaml_events.append(
                                    (
                                        "cleanup_removed_orphaned_episode_yaml",
                                        {
                                            "show": (
                                                identity[0] if identity else yaml_key
                                            ),
                                            "year": (
                                                identity[1]
                                                if identity
                                                else "unknown"
                                            ),
                                            "season": season_number,
                                            "episode": episode_number,
                                        },
                                    )
                                )
                                del episodes[episode_keys[episode_number]]
                                yaml_changed = True

                if not dry_run and yaml_changed:
                    metadata_content["metadata"] = cleaned_metadata
                    output_config = config.get("output", {})
                    write_kometa_metadata(
                        metadata_file,
                        metadata_content,
                        validate_schema=output_config.get("validate_schema", True),
                        backup_count=output_config.get("backup_count", 3),
                        library_type=file_media_type,
                        expected_snapshot=output_snapshot,
                    )
                    result.yaml_entries += pending_yaml_entries
                    for identity, title_removed in pending_yaml_records:
                        record_title(
                            identity,
                            removal_type="yaml",
                            title_removed=title_removed,
                        )
                    for identity, season_number in pending_seasons:
                        if identity:
                            removed_seasons.add((*identity, season_number))
                    for identity, season_number, episode_number in pending_episodes:
                        if identity:
                            removed_episodes.add(
                                (*identity, season_number, episode_number)
                            )
                    if orphaned_titles:
                        log_cleanup_event(
                            "cleanup_removed_orphans",
                            orphans_in_file=len(orphaned_titles),
                            filename=metadata_file.name,
                        )
                    for event, event_kwargs in pending_yaml_events:
                        log_cleanup_event(event, **event_kwargs)
            except Exception as error:
                if isinstance(error, CleanupError):
                    raise
                log_cleanup_event(
                    "cleanup_failed_remove_metadata",
                    filename=metadata_file,
                    error=str(error),
                )
                raise cleanup_failure(
                    f"Failed to clean metadata file: {metadata_file}",
                    f"Kometa YAML could not be updated: {metadata_file.name}",
                ) from error

    if asset_path:
        valid_asset_dirs = {"movie": set(), "tv": set()}
        for meta in safe_metadata:
            media_type = normalize_library_type(meta.get("library_type"))
            source_path = (
                meta.get("movie_path")
                if media_type == "movie"
                else meta.get("show_path")
            )
            if source_path:
                valid_asset_dirs[media_type].add(Path(source_path).name)

        existing_asset_keys = {_path_key(path) for path in (existing_assets or set())}
        deleted_dirs = set()

        async def remove_asset(path, description, allow_valid_title=False):
            path_key = _path_key(path)
            records = managed_assets.get(path_key, [])
            try:
                asset_library_type = normalize_library_type(
                    path.relative_to(asset_path).parts[0]
                )
            except (ValueError, IndexError):
                asset_library_type = ""
            if path_key in existing_asset_keys:
                result.assets_skipped += 1
                log_cleanup_event(
                    "cleanup_skipping_valid_asset", description=description, path=path
                )
                return
            if not records:
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_skipping_valid_asset",
                    description=f"unmanaged {description}",
                    path=path,
                )
                return
            if (
                path.parent.name in valid_asset_dirs.get(asset_library_type, set())
                and not allow_valid_title
            ):
                result.assets_skipped += 1
                return
            if path.is_symlink():
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_preserving_modified_asset",
                    description=description,
                    path=path,
                    reason="the path is now a symbolic link",
                )
                return
            expected_checksums = {
                record.get("checksum")
                for record in records
                if record.get("checksum")
            }
            if not expected_checksums:
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_preserving_modified_asset",
                    description=description,
                    path=path,
                    reason="legacy cache ownership has no checksum",
                )
                return
            try:
                current_checksum = await asyncio.to_thread(sha256_file, path)
            except OSError as error:
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_preserving_modified_asset",
                    description=description,
                    path=path,
                    reason=f"the checksum could not be verified: {error}",
                )
                return
            if current_checksum not in expected_checksums:
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_preserving_modified_asset",
                    description=description,
                    path=path,
                    reason="the file was changed after MetaFusion created it",
                )
                return

            if dry_run:
                log_cleanup_event(
                    "cleanup_dry_run", description=description, path=path
                )
            else:
                try:
                    await asyncio.to_thread(path.unlink)
                    deleted_dirs.add(_path_key(path.parent))
                    log_cleanup_event(
                        "cleanup_removing_asset", description=description, path=path
                    )
                except Exception as error:
                    log_cleanup_event(
                        "cleanup_failed_remove_asset",
                        description=description,
                        path=path,
                        error=str(error),
                    )
                    raise cleanup_failure(
                        f"Failed to remove managed asset: {path}",
                        f"managed {description} could not be removed: {path}",
                        report_confirmed=False,
                    ) from error
            removed_assets.add(path_key)
            for record in records:
                record_title(
                    record.get("identity"),
                    asset_type=description,
                    title_removed=not allow_valid_title,
                )

        safe_asset_roots = [
            Path(asset_path) / library_type for library_type in safe_library_types
        ]
        posters = [
            path
            for root in safe_asset_roots
            if root.exists()
            for path in root.rglob("poster.jpg")
        ]
        season_posters = [
            path
            for root in safe_asset_roots
            if root.exists()
            for path in root.rglob("Season*.jpg")
        ]
        backgrounds = [
            path
            for root in safe_asset_roots
            if root.exists()
            for path in root.rglob("fanart.jpg")
        ]

        tasks = []
        if run_poster:
            tasks.extend(remove_asset(path, "poster") for path in posters)
        if run_season:
            tasks.extend(
                remove_asset(
                    path,
                    "season poster",
                    allow_valid_title=_path_key(path) in orphaned_season_asset_paths,
                )
                for path in season_posters
            )
        if run_background:
            tasks.extend(remove_asset(path, "background") for path in backgrounds)
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = next(
            (
                outcome
                for outcome in task_results
                if isinstance(outcome, asyncio.CancelledError)
            ),
            None,
        )
        if cancelled is not None:
            raise cancelled
        task_errors = [
            outcome for outcome in task_results if isinstance(outcome, Exception)
        ]
        if task_errors:
            refresh_result_counts()
            log_confirmed_outcomes()
            first_error = task_errors[0]
            if isinstance(first_error, CleanupError):
                first_error.result = result
                raise first_error
            raise cleanup_failure(
                "Failed to reconcile managed artwork",
                f"managed artwork reconciliation failed: {first_error}",
            ) from first_error

        for directory_path in deleted_dirs:
            directory = Path(directory_path)
            try:
                if await asyncio.to_thread(_directory_is_empty, directory):
                    await asyncio.to_thread(directory.rmdir)
                    log_cleanup_event("cleanup_removing_empty_dir", parent=directory)
            except Exception as error:
                log_cleanup_event(
                    "cleanup_failed_remove_asset",
                    description="directory",
                    path=directory,
                    error=str(error),
                )
                raise cleanup_failure(
                    f"Failed to remove empty asset directory: {directory}",
                    f"empty managed asset directory could not be removed: {directory}",
                ) from error

    apply_cache_changes()
    return finish()
