import asyncio, copy, time, yaml
from collections import Counter
from pathlib import Path
from helper.cache import (
    load_cache,
    meta_cache_async,
    reset_cache_scope,
    set_cache_scope,
)
from helper.config import mode_check
from helper.incremental import child_inventory_fingerprint, plan_items
from helper.logging import (
    PlexMetadataProgress,
    log_processing_event,
    log_library_summary,
)
from helper.performance import tracker_for
from helper.plex import get_plex_metadata, plex_operation
from helper.identity import cache_key_for_meta, item_identity
from helper.io import sha256_file
from modules.builder import build_movie, build_tv
from modules.kometa import (
    normalize_metadata_order,
    remove_deprecated_metadata_fields,
    validate_metadata_document,
    write_kometa_metadata,
)
from helper.plex_metadata import apply_plex_metadata
from helper.state_db import prune_plex_metadata_library


class ItemProcessingError(RuntimeError):
    pass


class LibraryProcessingError(RuntimeError):
    pass


class AmbiguousEditionError(LibraryProcessingError):
    pass


MAX_ITEM_FAILURE_DETAILS = 10


def _output_snapshot(path):
    exists = path.exists()
    return exists, sha256_file(path) if exists else None


def _read_existing_metadata(path, validate_schema):
    with path.open("r", encoding="utf-8") as stream:
        document = yaml.safe_load(stream) or {}
    if validate_schema:
        validate_metadata_document(document)
    return document


def apply_cached_tmdb_recovery(meta, cache):
    """Reuse a verified replacement while Plex still reports the stale ID."""
    if not isinstance(meta, dict) or not meta.get("tmdb_id"):
        return False
    entry = cache.get(cache_key_for_meta(meta), {})
    if not isinstance(entry, dict):
        return False
    source_id = entry.get("tmdb_recovery_source_id")
    replacement_id = entry.get("tmdb_id")
    plex_id = meta.get("tmdb_id")
    if (
        source_id is None
        or replacement_id is None
        or str(source_id) != str(plex_id)
        or str(replacement_id) == str(plex_id)
    ):
        return False
    meta["plex_tmdb_id"] = str(plex_id)
    meta["tmdb_id"] = str(replacement_id)
    return True


def _item_failure_label(item):
    title = getattr(item, "title", None) or "Unknown"
    year = getattr(item, "year", None)
    label = f"{title} ({year})" if year is not None else str(title)
    rating_key = getattr(item, "ratingKey", None)
    if rating_key is not None:
        label += f" [rating key {rating_key}]"
    return label


def _root_error_message(error):
    cause = error
    seen = set()
    while getattr(cause, "__cause__", None) is not None and id(cause) not in seen:
        seen.add(id(cause))
        cause = cause.__cause__
    return str(cause) or cause.__class__.__name__


def format_item_failures(item_errors):
    ordered_errors = sorted(item_errors, key=lambda item: item[0].casefold())
    details = [
        f"{label}: {_root_error_message(error)}"
        for label, error in ordered_errors[:MAX_ITEM_FAILURE_DETAILS]
    ]
    remaining = len(item_errors) - len(details)
    if remaining:
        details.append(f"... and {remaining} more item(s)")
    return "; ".join(details)


def find_ambiguous_editions(metadata):
    groups = {}
    for meta in metadata:
        if (meta.get("library_type") or "").lower() != "movie":
            continue
        key = (meta.get("title"), meta.get("year"))
        groups.setdefault(key, []).append(meta)

    ambiguous = []
    for (title, year), entries in groups.items():
        if len(entries) < 2:
            continue
        edition_counts = Counter(entry.get("edition_title") for entry in entries)
        duplicate_editions = [
            "blank" if edition is None else str(edition)
            for edition, count in edition_counts.items()
            if count > 1
        ]
        if duplicate_editions:
            ambiguous.append(
                f"{title} ({year}): duplicate editions {', '.join(duplicate_editions)}"
            )
    return ambiguous


def cleanup_inventory_errors(metadata, feature_flags):
    errors = []
    asset_cleanup_enabled = any(
        feature_flags.get(name, False) for name in ("poster", "season", "background")
    )
    for meta in metadata:
        media_type = (meta.get("library_type") or "").lower()
        if media_type == "show":
            media_type = "tv"
        missing = [
            field for field in ("title", "year", "ratingKey") if not meta.get(field)
        ]
        if asset_cleanup_enabled:
            if media_type == "movie" and not meta.get("movie_path"):
                missing.append("movie_path")
            if media_type == "tv" and not meta.get("show_path"):
                missing.append("show_path")
        tv_inventory_cleanup_enabled = media_type == "tv" and any(
            feature_flags.get(name, False)
            for name in ("metadata_basic", "metadata_enhanced", "season")
        )
        if tv_inventory_cleanup_enabled:
            if not isinstance(meta.get("seasons_episodes"), dict):
                missing.append("seasons_episodes")
        if missing:
            errors.append(
                f"{meta.get('title')} ({meta.get('year')}): {', '.join(missing)}"
            )
    return errors

async def process_item(
    plex_item, consolidated_metadata, config, feature_flags=None, existing_yaml_data=None,  library_name="Unknown",
    existing_assets=None, session=None, ignored_fields=None, incremental_fingerprint=None,
    work_reasons=None,
):
    if ignored_fields is None:
        ignored_fields = set()
    if not plex_item:
        log_processing_event("processing_no_item")
        return None

    meta = await get_plex_metadata(plex_item, _plex_config=config.get("plex", {}))
    title = meta.get("title", "Unknown")
    year = meta.get("year", "Unknown")
    full_title = f"{title} ({year})"

    if library_name == "Unknown":
        library_name = meta.get("library_name", "Unknown")
    library_type = meta.get("library_type", "unknown")

    effective_flags = dict(feature_flags or {})
    if work_reasons is not None:
        work_reasons = set(work_reasons)
        effective_flags["metadata_basic"] = bool(
            effective_flags.get("metadata_basic", True) and "metadata" in work_reasons
        )
        effective_flags["metadata_enhanced"] = bool(
            effective_flags.get("metadata_enhanced", False)
            and "metadata" in work_reasons
        )
        effective_flags["poster"] = bool(
            effective_flags.get("poster", False) and "poster" in work_reasons
        )
        effective_flags["background"] = bool(
            effective_flags.get("background", False) and "background" in work_reasons
        )
        effective_flags["season"] = bool(
            effective_flags.get("season", False) and "season" in work_reasons
        )

    try:
        if library_type == "movie":
            stats = await build_movie(
                config, consolidated_metadata,
                existing_yaml_data=existing_yaml_data, session=session,
                ignored_fields=ignored_fields, existing_assets=existing_assets,
                meta=meta, feature_flags=effective_flags
            )
        elif library_type in ("show", "tv"):
            stats = await build_tv(
                config, consolidated_metadata,
                existing_yaml_data=existing_yaml_data, session=session,
                ignored_fields=ignored_fields, existing_assets=existing_assets,
                meta=meta, feature_flags=effective_flags
            )
        else:
            raise ItemProcessingError(f"Unsupported library type: {library_type}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        failure_label = _item_failure_label(plex_item)
        log_processing_event(
            "processing_failed_item", full_title=failure_label, error=str(e)
        )
        raise ItemProcessingError(f"Failed to process {failure_label}") from e
    failed_actions = []
    if isinstance(stats, dict):
        failed_actions.extend(
            stats.get(name)
            for name in ("metadata_action", "poster_action", "background_action")
            if stats.get(name) == "failed"
        )
        failed_actions.extend(
            action
            for action in stats.get("season_poster_actions", {}).values()
            if action == "failed"
        )
    if not isinstance(stats, dict):
        raise ItemProcessingError(f"Builder returned no result for {full_title}")
    plex_result = await apply_plex_metadata(
        plex_item,
        stats.pop("plex_candidate", None),
        config,
        meta,
    )
    stats["plex_metadata_writes"] = plex_result.get("writes", 0)
    if feature_flags.get("plex_metadata", False):
        stats["metadata_action"] = (
            "failed"
            if plex_result.get("failures")
            else ("upgraded" if plex_result.get("writes") else "skipped")
        )
    if plex_result.get("writes") and hasattr(plex_item, "reload"):
        await plex_operation(
            lambda: plex_item.reload(),
            config.get("runtime", {}),
            description=f"Reload Plex metadata identity for {full_title}",
        )
        updated_at = getattr(plex_item, "updatedAt", None)
        meta["updatedAt"] = (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat")
            else (str(updated_at) if updated_at is not None else None)
        )
    if feature_flags.get("plex_metadata", False) and not feature_flags.get(
        "dry_run", False
    ):
        normalized_type = "tv" if library_type == "show" else library_type
        await meta_cache_async(
            cache_key_for_meta(meta),
            meta.get("tmdb_id"),
            title,
            year,
            normalized_type,
            update_timestamp=False,
            plex_metadata_checked=True,
        )
    if plex_result.get("failures"):
        failed_actions.append("failed")
    stats["_incremental_success"] = not failed_actions
    return stats

plex_metadata_dict = {}
async def process_library(
    library_section, config, feature_flags=None, library_item_counts=None, library_filesize=None, metadata_summaries=None,
    season_cache=None, episode_cache=None, movie_cache=None, session=None, ignored_fields=None,
    full_scan=True, rating_keys=None, incremental_fingerprint=None,
    all_items=None, global_identity_counts=None, global_edition_counts=None,
    explain_selection=False,
):
    global plex_metadata_dict

    library_name = library_section.title
    if ignored_fields is None:
        ignored_fields = {"runtime", "guest"}
    existing_yaml_data = {}
    original_yaml_data = {}
    normalized_metadata_order = 0

    if library_item_counts is not None:
        library_item_counts[library_name] = 0
    if library_filesize is not None:
        library_filesize[library_name] = 0

    poster_size = background_size = season_poster_size = total_asset_size = 0
    completed = incomplete = 0
    season_count = episode_count = 0
    meta_downloaded = meta_upgraded = meta_skipped = meta_failed = 0
    plex_metadata_writes = 0
    poster_downloaded = poster_upgraded = poster_adopted = poster_skipped = poster_missing = poster_failed = 0
    background_downloaded = background_upgraded = background_adopted = background_skipped = background_missing = background_failed = 0
    season_poster_downloaded = season_poster_upgraded = season_poster_adopted = season_poster_skipped = season_poster_missing = season_poster_failed = 0

    server = getattr(library_section, "_server", None)
    scope_token = set_cache_scope(
        server_id=getattr(server, "machineIdentifier", None),
        library_uuid=(
            getattr(library_section, "uuid", None)
            or getattr(library_section, "key", None)
            or library_name
        ),
        library_name=library_name,
    )

    try:
        library_name = library_section.title
        if all_items is None:
            all_items = await plex_operation(
                lambda: list(library_section.all()),
                config.get("runtime", {}),
                description=f"List library {library_name}",
            )
        planned_items = plan_items(
            all_items,
            load_cache(),
            incremental_fingerprint,
            full_scan=full_scan,
            rating_keys=rating_keys,
            config=config,
            feature_flags=feature_flags,
            server_id=getattr(server, "machineIdentifier", None) or "unknown",
            library_uuid=(
                getattr(library_section, "uuid", None)
                or getattr(library_section, "key", None)
                or library_name
            ),
        )
        items = [planned.item for planned in planned_items]
        total_library_items = len(all_items)
        total_items = len(items)
        log_processing_event(
            "processing_library_items",
            library_name=library_name,
            total_items=total_items,
        )
        if explain_selection:
            for planned in planned_items:
                log_processing_event(
                    "processing_selection_reason",
                    library_name=library_name,
                    rating_key=getattr(planned.item, "ratingKey", "unknown"),
                    title=getattr(planned.item, "title", "Unknown"),
                    reasons=", ".join(sorted(planned.reasons)),
                )
            return []

        preloaded_metadata = []
        metadata_errors = []
        for item in items:
            try:
                meta = await get_plex_metadata(
                    item,
                    _season_cache=season_cache,
                    _episode_cache=episode_cache,
                    _movie_cache=movie_cache,
                    _runtime_config=config.get("runtime", {}),
                    _plex_config=config.get("plex", {}),
                )
                preloaded_metadata.append(meta)
            except Exception as e:
                title = getattr(item, "title", None)
                year = getattr(item, "year", None)
                media_type = getattr(item, "type", None)
                if media_type == "show":
                    media_type = "tv"
                preloaded_metadata.append({
                    "title": title,
                    "year": year,
                    "library_type": media_type,
                    "ratingKey": getattr(item, "ratingKey", None),
                })
                metadata_errors.append(f"{title} ({year}): {e}")
                log_processing_event("processing_failed_metadata", title=title, year=year, media_type=media_type, error=str(e))

        if metadata_errors:
            raise LibraryProcessingError(
                "Plex metadata inventory was incomplete: " + "; ".join(metadata_errors)
            )
        cache = load_cache()
        for meta in preloaded_metadata:
            apply_cached_tmdb_recovery(meta, cache)
        if feature_flags.get("cleanup", False):
            inventory_errors = cleanup_inventory_errors(
                preloaded_metadata, feature_flags
            )
            if inventory_errors:
                raise LibraryProcessingError(
                    "Cleanup requires complete Plex identity/path metadata: "
                    + "; ".join(inventory_errors)
                )

        edition_inventory = preloaded_metadata if full_scan else [
            {
                "title": getattr(item, "title", None),
                "year": getattr(item, "year", None),
                "library_type": (getattr(item, "type", None) or "").lower(),
                "library_name": library_name,
                "ratingKey": getattr(item, "ratingKey", None),
                "edition_title": getattr(item, "editionTitle", None)
                or getattr(item, "edition", None),
            }
            for item in all_items
        ]
        identity_groups = global_identity_counts or Counter(
            (
                ("tv" if (meta.get("library_type") or "").lower() == "show" else (meta.get("library_type") or "").lower()),
                meta.get("title"),
                meta.get("year"),
            )
            for meta in edition_inventory
        )
        edition_groups = global_edition_counts or Counter(
            (meta.get("title"), meta.get("year"), meta.get("edition_title"))
            for meta in edition_inventory
            if (meta.get("library_type") or "").lower() == "movie"
        )
        ambiguous_editions = []
        for meta in edition_inventory:
            if (meta.get("library_type") or "").lower() != "movie":
                continue
            group = (meta.get("title"), meta.get("year"))
            if (
                identity_groups.get(("movie", *group), 0) > 1
                and edition_groups.get((*group, meta.get("edition_title")), 0) > 1
            ):
                edition = meta.get("edition_title") or "blank"
                description = f"{meta.get('title')} ({meta.get('year')}): duplicate edition {edition}"
                if description not in ambiguous_editions:
                    ambiguous_editions.append(description)
        if ambiguous_editions and mode_check(config, "kometa"):
            description = "; ".join(ambiguous_editions)
            if not config.get("safety", {}).get("allow_ambiguous_editions", False):
                log_processing_event(
                    "processing_ambiguous_editions",
                    library_name=library_name,
                    description=description,
                )
                raise AmbiguousEditionError(
                    "Kometa cannot uniquely match these versions. Give every copy "
                    f"a unique Plex edition: {description}"
                )
            log_processing_event(
                "processing_ambiguous_editions_allowed",
                library_name=library_name,
                description=description,
            )
        for meta in preloaded_metadata:
            media_type = (meta.get("library_type") or "").lower()
            if media_type == "show":
                media_type = "tv"
            if media_type == "movie":
                group = (meta.get("title"), meta.get("year"))
                meta["requires_unique_key"] = identity_groups[("movie", *group)] > 1
                edition_group = (*group, meta.get("edition_title"))
                meta["edition_key_collision"] = bool(
                    meta.get("edition_title") and edition_groups[edition_group] > 1
                )
            elif media_type == "tv":
                meta["requires_unique_key"] = identity_groups[
                    ("tv", meta.get("title"), meta.get("year"))
                ] > 1
            key = (meta.get("title"), meta.get("year"), media_type, item_identity(meta))
            plex_metadata_dict[key] = meta

        library_type = getattr(library_section, "type", None)
        if library_type is not None:
            library_type = library_type.lower()
            if library_type == "movies":
                library_type = "movie"
            elif library_type in ("show", "shows"):
                library_type = "tv"
        else:
            if "movies" in library_name.lower():
                library_type = "movie"
            elif "tv shows" in library_name.lower() or "show" in library_name.lower():
                library_type = "tv"
            else:
                library_type = "unknown"

        output_path = None
        output_snapshot = None
        consolidated_metadata = {"metadata": {}}
        if mode_check(config, "kometa"):
            kometa_root = config.get("settings", {}).get("path", ".")
            metadata_dir = Path(kometa_root) / "metadata"
            if not feature_flags["dry_run"]:
                metadata_dir.mkdir(parents=True, exist_ok=True)
            output_path = metadata_dir / f"{library_type}_metadata.yml"
            output_snapshot = await asyncio.to_thread(_output_snapshot, output_path)
            if output_path.exists():
                try:
                    existing_yaml_data = await asyncio.to_thread(
                        _read_existing_metadata,
                        output_path,
                        config.get("output", {}).get("validate_schema", True),
                    )
                except Exception as e:
                    log_processing_event("processing_failed_parse_yaml", output_path=output_path, error=str(e))
                    raise LibraryProcessingError(
                        f"Unable to parse existing metadata file: {output_path}"
                    ) from e
            original_yaml_data = copy.deepcopy(existing_yaml_data)
            remove_deprecated_metadata_fields(existing_yaml_data, library_type)
            normalized_metadata_order = normalize_metadata_order(existing_yaml_data)
            consolidated_metadata = existing_yaml_data if existing_yaml_data else {"metadata": {}}

        existing_assets = set()
        all_stats = []
        pending_incremental = []

        async def process_and_collect(planned):
            nonlocal poster_size, background_size, season_poster_size, total_asset_size
            nonlocal completed, incomplete, season_count, episode_count
            nonlocal meta_downloaded, meta_upgraded, meta_skipped, meta_failed
            nonlocal plex_metadata_writes
            nonlocal poster_downloaded, poster_upgraded, poster_adopted, poster_skipped, poster_missing, poster_failed
            nonlocal background_downloaded, background_upgraded, background_adopted, background_skipped, background_missing, background_failed
            nonlocal season_poster_downloaded, season_poster_upgraded, season_poster_adopted, season_poster_skipped, season_poster_missing, season_poster_failed

            item = planned.item
            item_metadata = {"metadata": {}}
            item_started = time.monotonic()
            try:
                stats = await process_item(
                    plex_item=item, consolidated_metadata=item_metadata, config=config,
                    feature_flags=feature_flags, existing_yaml_data=existing_yaml_data,
                    library_name=library_name, existing_assets=existing_assets,
                    session=session, ignored_fields=ignored_fields,
                    incremental_fingerprint=incremental_fingerprint,
                    work_reasons=planned.reasons,
                )
            finally:
                performance = tracker_for(config)
                if performance:
                    performance.record_item(
                        library_name,
                        getattr(item, "ratingKey", None),
                        time.monotonic() - item_started,
                    )
            if stats and isinstance(stats, dict):
                generated_entries = item_metadata.get("metadata", {})
                if isinstance(generated_entries, dict):
                    consolidated_metadata.setdefault("metadata", {}).update(
                        generated_entries
                    )
                all_stats.append(stats)
                if (
                    stats.pop("_incremental_success", False)
                    and not feature_flags.get("dry_run", False)
                ):
                    pending_count = (
                        stats.get("metadata_pending_count", 0)
                        if "metadata" in planned.reasons
                        else None
                    )
                    pending_incremental.append(
                        (
                            meta_by_rating_key.get(
                                str(getattr(item, "ratingKey", ""))
                            ),
                            pending_count,
                            child_inventory_fingerprint(item),
                        )
                    )

                action = stats.get("metadata_action")
                if action == "downloaded":
                    meta_downloaded += 1
                elif action == "upgraded":
                    meta_upgraded += 1
                elif action == "skipped":
                    meta_skipped += 1
                elif action == "failed":
                    meta_failed += 1
                plex_metadata_writes += stats.get("plex_metadata_writes", 0)

                action = stats.get("poster_action")
                if action == "downloaded":
                    poster_downloaded += 1
                elif action == "upgraded":
                    poster_upgraded += 1
                elif action == "adopted":
                    poster_adopted += 1
                elif action == "skipped":
                    poster_skipped += 1
                elif action == "missing":
                    poster_missing += 1
                elif action == "failed":
                    poster_failed += 1

                action = stats.get("background_action")
                if action == "downloaded":
                    background_downloaded += 1
                elif action == "upgraded":
                    background_upgraded += 1
                elif action == "adopted":
                    background_adopted += 1
                elif action == "skipped":
                    background_skipped += 1
                elif action == "missing":
                    background_missing += 1
                elif action == "failed":
                    background_failed += 1

                season_actions = stats.get("season_poster_actions", {})
                for season_action in season_actions.values():
                    if season_action == "downloaded":
                        season_poster_downloaded += 1
                    elif season_action == "upgraded":
                        season_poster_upgraded += 1
                    elif season_action == "adopted":
                        season_poster_adopted += 1
                    elif season_action == "skipped":
                        season_poster_skipped += 1
                    elif season_action == "missing":
                        season_poster_missing += 1
                    elif season_action == "failed":
                        season_poster_failed += 1

                if feature_flags["poster"]:
                    poster_size += stats.get("poster", {}).get("size", 0)
                if feature_flags["background"]:
                    background_size += stats.get("background", {}).get("size", 0)
                if feature_flags["season"]:
                    if "season_posters" in stats:
                        season_poster_size += sum(stats["season_posters"].values())
                    else:
                        season_poster_size += stats.get("season_poster", {}).get("size", 0)
                total_asset_size = poster_size + background_size + season_poster_size

                if library_type in ("tv", "show"):
                    seasons_data = stats.get("seasons", {})
                    season_count += len(seasons_data)
                    for season in seasons_data.values():
                        episode_count += len(season.get("episodes", {}))

                if feature_flags["metadata_basic"]:
                    is_complete = stats.get("is_complete", False)
                    if is_complete:
                        completed += 1
                    else:
                        incomplete += 1

            if library_item_counts is not None and library_name != "Unknown":
                library_item_counts[library_name] = library_item_counts.get(library_name, 0) + 1

        max_concurrency = max(
            1,
            int(config.get("runtime", {}).get("max_concurrency", 8)),
        )
        meta_by_rating_key = {
            str(meta.get("ratingKey")): meta
            for meta in preloaded_metadata
            if meta.get("ratingKey") is not None
        }
        queue = asyncio.Queue()
        for planned in planned_items:
            queue.put_nowait(planned)
        item_errors = []
        processed_items = 0
        plex_progress = None
        progress_task = None
        if (
            total_items
            and mode_check(config, "plex")
            and feature_flags.get("plex_metadata", False)
        ):
            plex_progress = PlexMetadataProgress(library_name, total_items)
            plex_progress.start()

        def update_plex_progress(*, force=False):
            if plex_progress is None:
                return False
            return plex_progress.update(
                processed_items,
                changed=meta_upgraded,
                api_batches=plex_metadata_writes,
                unchanged=meta_skipped,
                failed=meta_failed + len(item_errors),
                force=force,
            )

        async def progress_heartbeat():
            check_seconds = max(
                1.0,
                min(
                    plex_progress.minimum_seconds,
                    plex_progress.heartbeat_seconds,
                ),
            )
            while processed_items < total_items:
                await asyncio.sleep(check_seconds)
                if processed_items >= total_items:
                    return
                update_plex_progress()

        async def worker():
            nonlocal processed_items
            while True:
                planned = await queue.get()
                try:
                    await process_and_collect(planned)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    item_errors.append((_item_failure_label(planned.item), error))
                finally:
                    processed_items += 1
                    update_plex_progress(force=processed_items >= total_items)
                    queue.task_done()

        workers = [
            asyncio.create_task(worker())
            for _ in range(min(max_concurrency, total_items))
        ]
        if plex_progress is not None:
            progress_task = asyncio.create_task(progress_heartbeat())
        try:
            await queue.join()
        finally:
            if progress_task is not None:
                progress_task.cancel()
            for worker_task in workers:
                worker_task.cancel()
            pending_tasks = [*workers]
            if progress_task is not None:
                pending_tasks.append(progress_task)
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        if (
            full_scan
            and not item_errors
            and mode_check(config, "plex")
            and feature_flags.get("plex_metadata", False)
            and not feature_flags.get("dry_run", False)
        ):
            prune_plex_metadata_library(
                getattr(server, "machineIdentifier", None) or "unknown",
                getattr(library_section, "uuid", None)
                or getattr(library_section, "key", None)
                or library_name,
                {
                    str(getattr(item, "ratingKey", ""))
                    for item in all_items
                    if getattr(item, "ratingKey", None) is not None
                },
            )

        if library_filesize is not None:
            library_filesize[library_name] = total_asset_size

        output_changed = (
            consolidated_metadata != original_yaml_data
            or normalized_metadata_order > 0
        )
        if (
            mode_check(config, "kometa")
            and not feature_flags["dry_run"]
            and items
            and output_changed
        ):
            try:
                output_config = config.get("output", {})
                write_kometa_metadata(
                    output_path,
                    consolidated_metadata,
                    validate_schema=output_config.get("validate_schema", True),
                    backup_count=output_config.get("backup_count", 3),
                    library_type=library_type,
                    expected_snapshot=output_snapshot,
                )
                log_processing_event("processing_metadata_saved", output_path=output_path)
            except Exception as e:
                log_processing_event("processing_failed_write_metadata", error=str(e))
                raise LibraryProcessingError(
                    f"Unable to save metadata file: {output_path}"
                ) from e
        elif mode_check(config, "kometa") and feature_flags["dry_run"]:
            log_processing_event("processing_metadata_dry_run", library_name=library_name)

        for meta, metadata_pending_count, plex_child_fingerprint in pending_incremental:
            if not meta:
                continue
            media_type = (meta.get("library_type") or "unknown").lower()
            if media_type == "show":
                media_type = "tv"
            await meta_cache_async(
                cache_key_for_meta(meta),
                meta.get("tmdb_id"),
                meta.get("title"),
                meta.get("year"),
                media_type,
                update_timestamp=False,
                rating_key=meta.get("ratingKey"),
                plex_updated_at=meta.get("updatedAt"),
                plex_child_fingerprint=plex_child_fingerprint,
                config_fingerprint=incremental_fingerprint,
                metadata_pending_count=metadata_pending_count,
            )

        run_metadata = feature_flags["metadata_basic"] or feature_flags["metadata_enhanced"]
        percent_complete = round((completed / total_items) * 100, 2) if total_items else 100.0
        percent_incomplete = round((incomplete / total_items) * 100, 2) if total_items else 0.0

        library_summary = {
            "meta_downloaded": meta_downloaded, "meta_upgraded": meta_upgraded,
            "meta_skipped": meta_skipped, "meta_failed": meta_failed + len(item_errors),
            "plex_metadata_writes": plex_metadata_writes,
            "poster_downloaded": poster_downloaded, "poster_upgraded": poster_upgraded, "poster_adopted": poster_adopted, "poster_skipped": poster_skipped,
            "poster_failed": poster_failed, "poster_missing": poster_missing,
            "background_downloaded": background_downloaded, "background_upgraded": background_upgraded, "background_adopted": background_adopted, "background_skipped": background_skipped,
            "background_failed": background_failed, "background_missing": background_missing,
            "season_poster_downloaded": season_poster_downloaded, "season_poster_upgraded": season_poster_upgraded, "season_poster_adopted": season_poster_adopted, "season_poster_skipped": season_poster_skipped,
            "season_poster_failed": season_poster_failed, "season_poster_missing": season_poster_missing,
            "incremental_skipped": total_library_items - total_items,
            "item_failures": len(item_errors),
        }

        log_library_summary(
            library_name=library_name, completed=completed, incomplete=incomplete, total_items=total_items,
            percent_complete=percent_complete, percent_incomplete=percent_incomplete,
            poster_size=poster_size, background_size=background_size,
            season_poster_size=season_poster_size, library_filesize=library_filesize,
            run_metadata=run_metadata, library_summary=library_summary, library_type=library_type,
            feature_flags=feature_flags, season_count=season_count, episode_count=episode_count
        )

        if metadata_summaries is not None:
            metadata_summaries[library_name] = {
                "complete": completed,
                "incomplete": incomplete,
                "total_items": total_items,
                "library_items": total_library_items,
                "percent_complete": percent_complete if run_metadata else None,
                "percent_incomplete": percent_incomplete if run_metadata else None,
                "library_summary": library_summary,
                "library_type": library_type,
                "season_count": season_count,
                "episode_count": episode_count,
                "status": "failed" if item_errors else "success",
            }

        if item_errors:
            raise LibraryProcessingError(
                f"{len(item_errors)} of {total_items} items failed in {library_name}; "
                f"failed items: {format_item_failures(item_errors)}; "
                "successful item output was preserved"
            ) from item_errors[0][1]

        return all_stats
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_processing_event("processing_failed_library", library_name=library_name, error=str(e))
        if isinstance(e, LibraryProcessingError):
            raise
        raise LibraryProcessingError(f"Failed to process library: {library_name}") from e
    finally:
        reset_cache_scope(scope_token)
