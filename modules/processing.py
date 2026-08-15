import asyncio, yaml
from pathlib import Path
from helper.cache import save_cache, load_cache
from helper.config import mode_check 
from helper.logging import log_processing_event, log_library_summary
from helper.plex import get_plex_metadata
from modules.builder import build_movie, build_tv

async def process_item(
    plex_item, consolidated_metadata, config, feature_flags=None, existing_yaml_data=None,  library_name="Unknown",
    existing_assets=None, session=None, ignored_fields=None, 
):
    if ignored_fields is None:
        ignored_fields = set()
    if not plex_item:
        log_processing_event("processing_no_item")
        return None

    meta = await get_plex_metadata(plex_item)
    title = meta.get("title", "Unknown")
    year = meta.get("year", "Unknown")
    full_title = f"{title} ({year})"

    if library_name == "Unknown":
        library_name = meta.get("library_name", "Unknown")
    library_type = meta.get("library_type", "unknown")

    try:
        async with asyncio.Lock():
            if library_type == "movie":
                stats = await build_movie(
                    config, consolidated_metadata,
                    existing_yaml_data=existing_yaml_data, session=session,
                    ignored_fields=ignored_fields, existing_assets=existing_assets,
                    meta=meta, feature_flags=feature_flags
                )
            elif library_type in ("show", "tv"):
                stats = await build_tv(
                    config, consolidated_metadata,
                    existing_yaml_data=existing_yaml_data, session=session,
                    ignored_fields=ignored_fields, existing_assets=existing_assets,
                    meta=meta, feature_flags=feature_flags   
                )
            else:
                log_processing_event("processing_unsupported_type", full_title=full_title)
                return None
    except Exception as e:
        log_processing_event("processing_failed_item", full_title=full_title, error=str(e))
        return None
    return stats

plex_metadata_dict = {} 
async def process_library(
    library_section, config, feature_flags=None, library_item_counts=None, library_filesize=None, metadata_summaries=None, 
    season_cache=None, episode_cache=None, movie_cache=None, session=None, ignored_fields=None
):
    global plex_metadata_dict

    library_name = library_section.title
    if ignored_fields is None:
        ignored_fields = {"runtime", "guest"}
    existing_yaml_data = {}

    if library_item_counts is not None:
        library_item_counts[library_name] = 0
    if library_filesize is not None:
        library_filesize[library_name] = 0

    poster_size = background_size = season_poster_size = total_asset_size = 0
    completed = incomplete = 0
    season_count = episode_count = 0
    meta_downloaded = meta_upgraded = meta_skipped = 0
    poster_downloaded = poster_upgraded = poster_skipped = poster_missing = poster_failed = 0
    background_downloaded = background_upgraded = background_skipped = background_missing = background_failed = 0
    season_poster_downloaded = season_poster_upgraded = season_poster_skipped = season_poster_missing = season_poster_failed = 0

    try:
        library_name = library_section.title
        items = await asyncio.to_thread(library_section.all)
        total_items = len(items)
        log_processing_event("processing_library_items", library_name=library_name, total_items=total_items)

        for item in items:
            try:
                meta = await get_plex_metadata(
                    item, 
                    _season_cache=season_cache, 
                    _episode_cache=episode_cache, 
                    _movie_cache=movie_cache
                )
                media_type = meta.get("library_type", "").lower()
                if media_type == "show":
                    media_type = "tv"
                key = (meta.get("title"), meta.get("year"), media_type)
                plex_metadata_dict[key] = meta
            except Exception as e:
                title = getattr(item, "title", None)
                year = getattr(item, "year", None)
                media_type = getattr(item, "type", None)
                if media_type == "show":
                    media_type = "tv"
                key = (title, year, media_type)
                plex_metadata_dict[key] = {}
                log_processing_event("processing_failed_metadata", title=title, year=year, media_type=media_type, error=str(e))

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
        consolidated_metadata = {"metadata": {}}
        if mode_check(config, "kometa"):
            kometa_root = config.get("settings", {}).get("path", ".")
            metadata_dir = Path(kometa_root) / "metadata"
            if not feature_flags["dry_run"]:
                metadata_dir.mkdir(parents=True, exist_ok=True)
            output_path = metadata_dir / f"{library_type}_metadata.yml"
            if output_path.exists():
                try:
                    with open(output_path, "r", encoding="utf-8") as f:
                        existing_yaml_data = yaml.safe_load(f) or {}
                except Exception as e:
                    log_processing_event("processing_failed_parse_yaml", output_path=output_path, error=str(e))
            consolidated_metadata = existing_yaml_data if existing_yaml_data else {"metadata": {}}

        existing_assets = set()    
        all_stats = []
        async def process_and_collect(item):
            nonlocal poster_size, background_size, season_poster_size, total_asset_size
            nonlocal completed, incomplete, season_count, episode_count
            nonlocal meta_downloaded, meta_upgraded, meta_skipped
            nonlocal poster_downloaded, poster_upgraded, poster_skipped, poster_missing, poster_failed
            nonlocal background_downloaded, background_upgraded, background_skipped, background_missing, background_failed
            nonlocal season_poster_downloaded, season_poster_upgraded, season_poster_skipped, season_poster_missing, season_poster_failed

            stats = await process_item(
                plex_item=item, consolidated_metadata=consolidated_metadata, config=config,
                feature_flags=feature_flags, existing_yaml_data=existing_yaml_data,
                library_name=library_name, existing_assets=existing_assets,
                session=session, ignored_fields=ignored_fields,
            )
            if stats and isinstance(stats, dict):
                all_stats.append(stats)

                action = stats.get("metadata_action")
                if action == "downloaded":
                    meta_downloaded += 1
                elif action == "upgraded":
                    meta_upgraded += 1
                elif action == "skipped":
                    meta_skipped += 1

                action = stats.get("poster_action")
                if action == "downloaded":
                    poster_downloaded += 1
                elif action == "upgraded":
                    poster_upgraded += 1
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

        item_tasks = [process_and_collect(item) for item in items]
        await asyncio.gather(*item_tasks)

        if library_filesize is not None:
            library_filesize[library_name] = total_asset_size

        if mode_check(config, "kometa") and not feature_flags["dry_run"]:
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    yaml.dump(consolidated_metadata, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                log_processing_event("processing_metadata_saved", output_path=output_path)
                save_cache(load_cache())
                log_processing_event("processing_cache_saved")
            except Exception as e:
                log_processing_event("processing_failed_write_metadata", error=str(e))
        elif mode_check(config, "kometa") and feature_flags["dry_run"]:
            log_processing_event("processing_metadata_dry_run", library_name=library_name)

        run_metadata = feature_flags["metadata_basic"] or feature_flags["metadata_enhanced"]
        percent_complete = round((completed / total_items) * 100, 2) if total_items else 0.0
        percent_incomplete = round((incomplete / total_items) * 100, 2) if total_items else 0.0

        library_summary = {
            "meta_downloaded": meta_downloaded, "meta_upgraded": meta_upgraded, "meta_skipped": meta_skipped,
            "poster_downloaded": poster_downloaded, "poster_upgraded": poster_upgraded, "poster_skipped": poster_skipped,
            "poster_failed": poster_failed, "poster_missing": poster_missing,
            "background_downloaded": background_downloaded, "background_upgraded": background_upgraded, "background_skipped": background_skipped,
            "background_failed": background_failed, "background_missing": background_missing,
            "season_poster_downloaded": season_poster_downloaded, "season_poster_upgraded": season_poster_upgraded, "season_poster_skipped": season_poster_skipped,
            "season_poster_failed": season_poster_failed, "season_poster_missing": season_poster_missing
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
                "percent_complete": percent_complete if run_metadata else None,
                "percent_incomplete": percent_incomplete if run_metadata else None,
                "library_summary": library_summary,
                "library_type": library_type,
                "season_count": season_count,
                "episode_count": episode_count,
            }
        
        return all_stats
    except Exception as e:
        log_processing_event("processing_failed_library", library_name=library_name, error=str(e))
        return []
