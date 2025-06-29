import asyncio
from pathlib import Path
from ruamel.yaml import YAML
from helper.logging import log_processing_event, log_library_summary
from helper.cache import save_cache, load_cache
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
                    library_name=library_name, meta=meta, feature_flags=feature_flags
                )
            elif library_type in ("show", "tv"):
                stats = await build_tv(
                    config, consolidated_metadata,
                    existing_yaml_data=existing_yaml_data, session=session,
                    ignored_fields=ignored_fields, existing_assets=existing_assets,
                    library_name=library_name, meta=meta, feature_flags=feature_flags   
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
    plex_metadata_dict.clear()
    
    library_name = library_section.title
    if ignored_fields is None:
        ignored_fields = {"collection", "guest"}
    existing_yaml_data = {}
    
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                yaml = YAML()
                existing_yaml_data = yaml.load(f) or {}
        except Exception as e:
            log_processing_event("processing_failed_parse_yaml", output_path=output_path, error=str(e))

    consolidated_metadata = existing_yaml_data if existing_yaml_data else {"metadata": {}}
    existing_assets = set()

    poster_size = 0
    background_size = 0
    season_poster_size = 0
    total_asset_size = 0
    completed = 0
    incomplete = 0
    
    try:
        library_name = library_section.title
        items = await asyncio.to_thread(library_section.all)
        total_items = len(items)
        log_processing_event("processing_library_items", library_name=library_name, total_items=total_items)

        for item in items:
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

    if plex_metadata_dict:
        first_meta = next(iter(plex_metadata_dict.values()))
        library_type = first_meta.get("library_type", "unknown").lower()
        if library_type == "show":
            library_type = "tv"
    else:
        library_type = "unknown"
    output_path = Path(config["metadata"]["directory"]) / f"{library_type}_metadata.yml"

        if library_item_counts is not None:
            library_item_counts[library_name] = 0
        if library_filesize is not None:
            library_filesize[library_name] = 0

        all_stats = []
        async def process_and_collect(item):
            stats = await process_item(
                plex_item=item, consolidated_metadata=consolidated_metadata, config=config,
                feature_flags=feature_flags, existing_yaml_data=existing_yaml_data,
                library_name=library_name, existing_assets=existing_assets,
                session=session, ignored_fields=ignored_fields,
            )
            if stats and isinstance(stats, dict):
                all_stats.append(stats)
                if feature_flags["poster"]:
                    nonlocal poster_size
                    poster_size += stats.get("poster", {}).get("size", 0)
                if feature_flags["background"]:
                    nonlocal background_size
                    background_size += stats.get("background", {}).get("size", 0)
                if feature_flags["season"]:
                    nonlocal season_poster_size
                    if "season_posters" in stats:
                        season_poster_size += sum(stats["season_posters"].values())
                    else:
                        season_poster_size += stats.get("season_poster", {}).get("size", 0)
                nonlocal total_asset_size
                total_asset_size = (
                    poster_size + background_size + season_poster_size
                )
                if feature_flags["metadata_basic"]:
                    percent = stats.get("percent", 0)
                    if percent >= 100:
                        nonlocal completed
                        completed += 1
                    else:
                        nonlocal incomplete
                        incomplete += 1
            if library_item_counts is not None and library_name != "Unknown":
                library_item_counts[library_name] = library_item_counts.get(library_name, 0) + 1

        item_tasks = [process_and_collect(item) for item in items]
        await asyncio.gather(*item_tasks)

        if library_filesize is not None:
            library_filesize[library_name] = total_asset_size

        if not feature_flags["dry_run"]:
            try:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, "w", encoding="utf-8") as f:
                    yaml = YAML()
                    yaml.default_flow_style = False
                    yaml.allow_unicode = True
                    yaml.dump(consolidated_metadata, f)
                log_processing_event("processing_metadata_saved", output_path=output_path)
                save_cache(load_cache())
                log_processing_event("processing_cache_saved")

            except Exception as e:
                log_processing_event("processing_failed_write_metadata", error=str(e))
        else:
            log_processing_event("processing_metadata_dry_run", library_name=library_name)

        run_metadata = feature_flags["metadata_basic"] or feature_flags["metadata_enhanced"]
        percent_complete = round((completed / total_items) * 100, 2) if total_items else 0.0

        log_library_summary(
            library_name=library_name, completed=completed, incomplete=incomplete, total_items=total_items,
            percent_complete=percent_complete, poster_size=poster_size, background_size=background_size,
            season_poster_size=season_poster_size, library_filesize=library_filesize,
            run_metadata=run_metadata
        )

        if metadata_summaries is not None:
            metadata_summaries[library_name] = {
                "complete": completed,
                "incomplete": incomplete,
                "total_items": total_items,
                "percent_complete": percent_complete if run_metadata else None,
            }
        
        return all_stats
    except Exception as e:
        log_processing_event("processing_failed_library", library_name=library_name, error=str(e))
        return None, set(), 0