import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from helper.cache import load_cache, mark_cache_dirty
from helper.identity import cache_key_for_meta, metadata_key_for_meta
from helper.io import sha256_file
from helper.logging import log_cleanup_event
from helper.quarantine import purge_expired_quarantine, quarantine_managed_asset
from helper.state_db import (
    cancel_cleanup_candidate,
    complete_cleanup_candidate,
    load_cleanup_candidates,
    load_item_exceptions,
    observe_cleanup_candidate,
    record_cleanup_history,
)
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
    candidates_pending: int = 0
    pending_by_type: dict = field(default_factory=dict)
    ownership_deferred: int = 0
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
    partial_scopes=None,
):
    mode = config.get("settings", {}).get("mode", "kometa")
    dry_run = feature_flags.get("dry_run", False)
    result = CleanupResult(dry_run=dry_run, mode=mode)

    safe_library_types = {
        normalize_library_type(value) for value in (safe_library_types or set())
    }
    safe_library_types &= {"movie", "tv"}
    partial_scopes = partial_scopes or {}
    safe_library_types.update(partial_scopes)
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
    cleanup_config = config.get("cleanup", {})
    confirmations_required = max(
        1, int(cleanup_config.get("confirmation_scans", 2))
    )
    grace_hours = max(0.0, float(cleanup_config.get("grace_hours", 48.0)))
    if not dry_run and not config.get("_held_inventory_scopes") and not config.get("_partial_inventory_run"):
        try:
            purged = purge_expired_quarantine(config, source="automated")
            purge_count = sum(
                record.get("status") in {"purged", "missing"}
                for record in purged
            )
            if purge_count:
                log_cleanup_event("cleanup_quarantine_purged", count=purge_count)
        except Exception as error:
            log_cleanup_event(
                "cleanup_quarantine_purge_deferred",
                error=str(error),
            )
    observation_id = datetime.now(timezone.utc).isoformat()
    cleanup_exceptions = {
        str(record.get("rating_key"))
        for record in load_item_exceptions()
        if record.get("output_type") in {"all", "cleanup"}
    }

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
            and not (partial_scopes.get("tv") and str(meta.get("ratingKey")) in partial_scopes["tv"].protected_keys)
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
                        "cache_key": key,
                        "checksum": raw_entry.get(f"{asset_type}_checksum"),
                        "identity": identity,
                        "asset_type": asset_type,
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
                    "cache_key": key,
                    "checksum": season_entry.get("season_checksum"),
                    "identity": identity,
                    "asset_type": "season",
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

    def cached_rating_key(key, entry):
        stored = entry.get("rating_key")
        prefix, separator, rating_key = str(key).partition(":plex:")
        if not stored and separator and prefix in {"movie", "tv"}:
            stored = rating_key
        return str(stored) if stored is not None else None

    def candidate_record(key, entry=None, meta=None, reason=None):
        entry = entry if isinstance(entry, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        identity = _cache_identity(key, entry)
        return {
            "server_id": meta.get("server_id") or entry.get("server_id"),
            "library_uuid": meta.get("library_uuid") or entry.get("library_uuid"),
            "library_name": meta.get("library_name") or entry.get("library_name"),
            "rating_key": meta.get("ratingKey") or cached_rating_key(key, entry),
            "cache_key": key,
            "media_type": normalize_library_type(
                meta.get("library_type") or cached_media_type(key, entry)
            ),
            "title": meta.get("title") or entry.get("title") or (identity or (None,))[0],
            "year": meta.get("year") or entry.get("year") or ((identity or (None, None))[1]),
            "tmdb_id": meta.get("tmdb_id") or entry.get("tmdb_id"),
            "imdb_id": meta.get("imdb_id") or entry.get("imdb_id"),
            "tvdb_id": meta.get("tvdb_id") or entry.get("tvdb_id"),
            "edition": meta.get("edition_title") or entry.get("edition"),
            "reason": reason,
        }

    def record_asset_cleanup(path, records, status):
        for owner in records:
            key = owner["cache_key"]
            subject = candidate_record(key, cache.get(key))
            subject["season_number"] = owner.get("season")
            config.setdefault("_native_observations", []).append((
                subject, "cleanup/" + str(path), status, status == "removed", {"destination": str(path)},
            ))

    def observe(key, record, scope, *, season=None, episode=None):
        decision = observe_cleanup_candidate(
            key,
            record,
            scope,
            season_number=season,
            episode_number=episode,
            confirmations_required=confirmations_required,
            grace_hours=grace_hours,
            observation_id=observation_id,
            writable=not dry_run,
        )
        if not decision.get("eligible"):
            result.candidates_pending += 1
            kind = record.get("media_type")
            result.pending_by_type[kind] = result.pending_by_type.get(kind, 0) + 1
        return decision

    if not dry_run:
        inventory_by_cache_key = {
            cache_key_for_meta(meta): meta for meta in safe_metadata
        }
        for candidate in load_cleanup_candidates():
            cache_key = candidate.get("cache_key")
            meta = inventory_by_cache_key.get(cache_key)
            if not meta:
                continue
            scope = str(candidate.get("scope") or "")
            inventory = meta.get("seasons_episodes") or {}
            season = candidate.get("season_number")
            episode = candidate.get("episode_number")
            present = scope in {"title", "metadata"}
            if scope in {"season", "metadata_season"}:
                present = str(season) in {str(value) for value in inventory}
            elif scope == "metadata_episode":
                season_key = next(
                    (value for value in inventory if str(value) == str(season)), None
                )
                present = season_key is not None and str(episode) in {
                    str(value) for value in inventory.get(season_key, [])
                }
            if present:
                cancel_cleanup_candidate(
                    candidate["candidate_key"], reason="media returned to Plex"
                )

    cache_keys_to_remove = []
    eligible_title_keys = set()
    eligible_season_keys = set()
    title_candidate_records = {}

    def authorized(key, entry):
        scope = partial_scopes.get(cached_media_type(key, entry))
        if not scope:
            return True
        return (
            isinstance(entry, dict)
            and (entry.get("server_id"), entry.get("library_uuid")) in scope.owners
            and cached_rating_key(key, entry) not in scope.protected_keys
            and _cache_identity(key, entry) not in scope.protected_titles
        )

    disabled_outputs = {}
    enabled_outputs = {"poster": run_poster, "background": run_background, "season": run_season}
    if mode != "plex" or cleanup_config.get("plex_remove_managed_artwork", False):
        for records in managed_assets.values():
            for owner in records:
                output = owner["asset_type"]
                if owner["checksum"] and not enabled_outputs[output]:
                    key = owner["cache_key"]
                    disabled_outputs.setdefault((key, None), set()).add(output)
                    disabled_outputs.setdefault((key, owner["season"]), set()).add(output)

    held_titles = set()
    for key in list(cache):
        entry = cache.get(key)
        if cached_media_type(key, entry) not in safe_library_types or key in valid_cache_keys or not authorized(key, entry):
            continue
        record = candidate_record(key, entry, reason="media is absent from the authoritative Plex inventory")
        if str(record.get("rating_key")) in cleanup_exceptions:
            continue
        disabled = disabled_outputs.get((key, None))
        if disabled:
            record["reason"] += "; ownership retained for disabled " + ", ".join(sorted(disabled))
        decision = observe(f"title:{key}", record, "title")
        if decision.get("eligible"):
            cache_keys_to_remove.append(key)
            eligible_title_keys.add(key)
            title_candidate_records[key] = record
            if disabled:
                held_titles.add(key)
                result.ownership_deferred += 1
    for meta in safe_metadata:
        if normalize_library_type(meta.get("library_type")) != "tv":
            continue
        if not (run_metadata or run_season):
            continue
        cache_key = cache_key_for_meta(meta)
        cache_entry = cache.get(cache_key)
        if not isinstance(cache_entry, dict) or not authorized(cache_key, cache_entry):
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
            record = candidate_record(
                cache_key,
                cache_entry,
                meta,
                reason="season is absent from the authoritative Plex inventory",
            )
            candidate_key = f"season:{cache_key}:{season_number}"
            if str(record.get("rating_key")) in cleanup_exceptions:
                continue
            disabled = disabled_outputs.get((cache_key, str(season_number)))
            if disabled:
                record["reason"] += "; ownership retained for disabled season artwork"
            decision = observe(
                candidate_key, record, "season", season=season_number
            )
            if not decision.get("eligible"):
                continue
            eligible_season_keys.add((cache_key, str(season_number)))
            if disabled:
                result.ownership_deferred += 1
                continue
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
                        "candidate_key": candidate_key,
                        "record": record,
                    }
                )

    def _apply_cache_changes():
        if dry_run:
            for key in set(cache_keys_to_remove) - held_titles:
                record_title(_cache_identity(key, cache.get(key)), removal_type="cache", title_removed=True)
                result.cache_entries += 1
                log_cleanup_event("cleanup_dry_run", description="cache entry", path=key)
            return
        changed = False
        for key in cache_keys_to_remove:
            if key in cache and key not in held_titles:
                identity = _cache_identity(key, cache.get(key))
                del cache[key]
                result.cache_entries += 1
                record_title(identity, removal_type="cache", title_removed=True)
                changed = True
                log_cleanup_event("cleanup_removed_cache_entry", key=key)
                record = title_candidate_records.get(key) or candidate_record(
                    key, None, reason="confirmed stale title"
                )
                record_cleanup_history(
                    "automated", "remove", "completed", record,
                    output_type="state", reason="confirmed absent from Plex",
                )
                complete_cleanup_candidate(f"title:{key}")
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
            record_cleanup_history(
                "automated", "remove", "completed", removal.get("record"),
                output_type="season_state", season_number=removal["season"],
                reason="confirmed absent from Plex",
            )
            complete_cleanup_candidate(removal["candidate_key"])
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
        if cleanup_config.get("plex_remove_managed_artwork", False):
            plex_roots = []
            for mapping in config.get("plex", {}).get("path_mappings", []):
                _source, separator, destination = str(mapping).partition("=>")
                if separator and destination.strip():
                    plex_roots.append(
                        Path(destination.strip()).resolve(strict=False)
                    )
            for raw_path, records in managed_assets.items():
                path = Path(raw_path)
                eligible_records = [
                    record for record in records
                    if record.get("cache_key") in eligible_title_keys
                    or (
                        record.get("season") is not None
                        and (record.get("cache_key"), str(record.get("season")))
                        in eligible_season_keys
                    )
                ]
                if len(eligible_records) != len(records) or not path.exists():
                    continue
                resolved_path = path.resolve(strict=False)
                primary_key = eligible_records[0]["cache_key"]
                scope = partial_scopes.get(cached_media_type(primary_key, cache[primary_key]))
                if scope and any(resolved_path.is_relative_to(root.resolve(strict=False)) for root in scope.protected_dirs):
                    record_asset_cleanup(path, eligible_records, "protected_output")
                    result.assets_preserved += 1
                    continue
                if not plex_roots or not any(
                    resolved_path.is_relative_to(root) for root in plex_roots
                ):
                    record_asset_cleanup(path, eligible_records, "protected_output")
                    result.assets_preserved += 1
                    continue
                enabled = (
                    (path.name == "poster.jpg" and run_poster)
                    or (path.name == "fanart.jpg" and run_background)
                    or (any(record.get("season") is not None for record in records) and run_season)
                )
                if not enabled:
                    continue
                checksums = {
                    record.get("checksum") for record in eligible_records
                    if record.get("checksum")
                }
                if path.is_symlink() or not checksums:
                    record_asset_cleanup(path, eligible_records, "protected_output")
                    result.assets_preserved += 1
                    continue
                try:
                    actual_checksum = await asyncio.to_thread(sha256_file, path)
                except OSError:
                    record_asset_cleanup(path, eligible_records, "protected_output")
                    result.assets_preserved += 1
                    continue
                if actual_checksum not in checksums:
                    record_asset_cleanup(path, eligible_records, "protected_output")
                    result.assets_preserved += 1
                    continue
                if dry_run:
                    log_cleanup_event(
                        "cleanup_dry_run_artwork",
                        description="Plex managed artwork",
                        path=path,
                    )
                else:
                    primary = eligible_records[0]
                    state_record = title_candidate_records.get(
                        primary.get("cache_key")
                    ) or candidate_record(
                        primary.get("cache_key"), cache.get(primary.get("cache_key"))
                    )
                    await asyncio.to_thread(
                        quarantine_managed_asset,
                        config,
                        path,
                        state_record,
                        output_type=(
                            "season" if primary.get("season") is not None else path.stem
                        ),
                        season_number=primary.get("season"),
                        checksum=actual_checksum,
                    )
                    record_asset_cleanup(path, eligible_records, "removed")
                    log_cleanup_event(
                        "cleanup_removing_asset",
                        description="Plex managed artwork",
                        path=path,
                    )
                removed_assets.add(_path_key(path))
        else:
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

    if run_metadata and mode != "plex":
        owners_by_identity = {}
        for key, entry in cache.items():
            owners_by_identity.setdefault((cached_media_type(key, entry), _cache_identity(key, entry)), []).append(key)
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

                cleaned_metadata = dict(metadata_entries)
                yaml_changed = False
                orphaned_titles = set(metadata_entries) - valid_yaml_keys.get(
                    file_media_type, set()
                )
                confirmed_orphaned_titles = set()
                for yaml_key in orphaned_titles:
                    identity = extract_title_year(yaml_key)
                    scope = partial_scopes.get(file_media_type)
                    owners = owners_by_identity.get((file_media_type, identity), [])
                    if scope and (
                        len(owners) != 1 or owners[0] in valid_cache_keys
                        or not authorized(owners[0], cache[owners[0]])
                    ):
                        continue
                    matching_key = owners[0] if owners else f"{file_media_type}:{yaml_key}"
                    record = candidate_record(
                        matching_key,
                        cache.get(matching_key),
                        reason="Kometa metadata title is absent from Plex",
                    )
                    if str(record.get("rating_key")) in cleanup_exceptions:
                        continue
                    candidate_key = f"yaml-title:{file_media_type}:{yaml_key}"
                    if not observe(candidate_key, record, "metadata").get("eligible"):
                        if scope:
                            held_titles.add(matching_key)
                        continue
                    confirmed_orphaned_titles.add(yaml_key)
                    cleaned_metadata.pop(yaml_key, None)
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
                            pending_yaml_records.append((identity, True, candidate_key, record))
                if confirmed_orphaned_titles and not dry_run:
                    yaml_changed = True

                for yaml_key, yaml_entry in cleaned_metadata.items():
                    if not isinstance(yaml_entry, dict):
                        continue
                    plex_meta = metadata_by_yaml_key.get(
                        (file_media_type, yaml_key)
                    )
                    if plex_meta and file_media_type in partial_scopes:
                        key = cache_key_for_meta(plex_meta)
                        if not authorized(key, cache.get(key)):
                            continue
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
                        cache_key = cache_key_for_meta(plex_meta)
                        record = candidate_record(
                            cache_key,
                            cache.get(cache_key),
                            plex_meta,
                            reason="Kometa metadata season is absent from Plex",
                        )
                        if str(record.get("rating_key")) in cleanup_exceptions:
                            continue
                        candidate_key = f"yaml-season:{cache_key}:{season_number}"
                        if not observe(
                            candidate_key, record, "metadata_season", season=season_number
                        ).get("eligible"):
                            continue
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
                            pending_yaml_records.append((identity, False, candidate_key, record))
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
                            cache_key = cache_key_for_meta(plex_meta)
                            record = candidate_record(
                                cache_key,
                                cache.get(cache_key),
                                plex_meta,
                                reason="Kometa metadata episode is absent from Plex",
                            )
                            candidate_key = (
                                f"yaml-episode:{cache_key}:{season_number}:{episode_number}"
                            )
                            if str(record.get("rating_key")) in cleanup_exceptions:
                                continue
                            if not observe(
                                candidate_key,
                                record,
                                "metadata_episode",
                                season=season_number,
                                episode=episode_number,
                            ).get("eligible"):
                                continue
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
                                pending_yaml_records.append((identity, False, candidate_key, record))
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
                    for identity, title_removed, candidate_key, record in pending_yaml_records:
                        record_title(
                            identity,
                            removal_type="yaml",
                            title_removed=title_removed,
                        )
                        record_cleanup_history(
                            "automated", "remove", "completed", record,
                            output_type="metadata", destination=metadata_file,
                            reason="confirmed absent from Plex",
                        )
                        complete_cleanup_candidate(candidate_key)
                    for identity, season_number in pending_seasons:
                        if identity:
                            removed_seasons.add((*identity, season_number))
                    for identity, season_number, episode_number in pending_episodes:
                        if identity:
                            removed_episodes.add(
                                (*identity, season_number, episode_number)
                            )
                    if confirmed_orphaned_titles:
                        log_cleanup_event(
                            "cleanup_removed_orphans",
                            orphans_in_file=len(confirmed_orphaned_titles),
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
            asset_library_type = normalize_library_type(
                path.relative_to(asset_path).parts[0]
            )
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
            eligible_records = [
                record for record in records
                if record.get("cache_key") in eligible_title_keys
                or (
                    record.get("season") is not None
                    and (record.get("cache_key"), str(record.get("season")))
                    in eligible_season_keys
                )
            ]
            if len(eligible_records) != len(records):
                result.assets_skipped += 1
                return
            if (
                path.parent.name in valid_asset_dirs.get(asset_library_type, set())
                and not allow_valid_title
            ):
                result.assets_skipped += 1
                return
            scope = partial_scopes.get(asset_library_type)
            if scope and any(root.name == path.parent.name for root in scope.protected_dirs):
                result.assets_skipped += 1
                return
            if path.is_symlink():
                record_asset_cleanup(path, eligible_records, "protected_output")
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
                record_asset_cleanup(path, eligible_records, "protected_output")
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
                record_asset_cleanup(path, eligible_records, "protected_output")
                result.assets_preserved += 1
                log_cleanup_event(
                    "cleanup_preserving_modified_asset",
                    description=description,
                    path=path,
                    reason=f"the checksum could not be verified: {error}",
                )
                return
            if current_checksum not in expected_checksums:
                record_asset_cleanup(path, eligible_records, "protected_output")
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
                    "cleanup_dry_run_artwork", description=description, path=path
                )
            else:
                try:
                    primary = eligible_records[0]
                    state_record = candidate_record(
                        primary.get("cache_key"), cache.get(primary.get("cache_key"))
                    )
                    await asyncio.to_thread(
                        quarantine_managed_asset,
                        config,
                        path,
                        state_record,
                        output_type=description.replace(" ", "_"),
                        season_number=primary.get("season"),
                        checksum=current_checksum,
                    )
                    record_asset_cleanup(path, eligible_records, "removed")
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
