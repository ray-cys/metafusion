import asyncio, yaml
from pathlib import Path
from helper.logging import log_cleanup_event
from helper.cache import load_cache, save_cache

def safe_int(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None
    
async def cleanup_title_orphans(
    config, feature_flags, asset_path=None, existing_assets=None, preloaded_plex_metadata=None
):
    mode = config.get("settings", {}).get("mode", "kometa")
    log_cleanup_event("cleanup_start")
    orphans_removed = 0
    global_valid_cache_keys = set()
    global_existing_titles = set()
    removed_summary = {}

    if preloaded_plex_metadata is None:
        log_cleanup_event("cleanup_error")
        return orphans_removed

    for (title, year, media_type), meta in preloaded_plex_metadata.items():
        if title and year:
            if media_type in ["show", "tv"]:
                global_valid_cache_keys.add(f"tv:{title}:{year}")
            elif media_type == "movie":
                global_valid_cache_keys.add(f"movie:{title}:{year}")
            global_existing_titles.add(f"{title} ({year})")

    cache = load_cache()
    managed_asset_paths = set()
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        for path_key in ("poster_path", "background_path"):
            if entry.get(path_key):
                managed_asset_paths.add(str(Path(entry[path_key]).resolve()))
        for season_entry in (entry.get("seasons") or {}).values():
            if isinstance(season_entry, dict) and season_entry.get("season_path"):
                managed_asset_paths.add(str(Path(season_entry["season_path"]).resolve()))
    cache_keys_to_remove = [
        key for key in list(cache.keys())
        if key not in global_valid_cache_keys
    ]
    for key in cache_keys_to_remove:
        title, year = None, None
        if key.startswith("movie:") or key.startswith("tv:"):
            try:
                _, rest = key.split(":", 1)
                title, year = rest.rsplit(":", 1) if ":" in rest else rest.rsplit(",", 1)
                title = title.strip()
                year = year.strip()
            except Exception:
                pass
        if feature_flags.get("dry_run", False):
            log_cleanup_event("cleanup_dry_run", description="cache", path=key)
        else:
            log_cleanup_event("cleanup_removed_cache_entry", key=key)
            del cache[key]
            orphans_removed += 1
            if title and year and safe_int(year) is not None:
                removed_summary.setdefault((title, safe_int(year)), {"cache": False, "asset": [], "yaml": False})
                removed_summary[(title, safe_int(year))]["cache"] = True
    
    for (title, year, media_type), meta in preloaded_plex_metadata.items():
        if media_type in ["show", "tv"] and title and year:
            cache_key = f"tv:{title}:{year}"
            if cache_key in cache:
                valid_seasons = set(str(s) for s in (meta.get("seasons_episodes") or {}).keys())
                cached_seasons = set(str(s) for s in (cache[cache_key].get("seasons") or {}).keys())
                orphaned_seasons = cached_seasons - valid_seasons
                for season_num in orphaned_seasons:
                    if feature_flags.get("dry_run", False):
                        log_cleanup_event("cleanup_dry_run", description="season", path=f"{cache_key} season {season_num}")
                    else:
                        del cache[cache_key]["seasons"][season_num]
                        log_cleanup_event("cleanup_removed_orphaned_season_cache", show=title, year=year, season=season_num)
                        orphans_removed += 1
                        if title and year and safe_int(year) is not None:
                            removed_summary.setdefault((title, safe_int(year)), {"cache": False, "asset": [], "yaml": False})
                            removed_summary[(title, safe_int(year))]["cache"] = True
    if not feature_flags.get("dry_run", False):
        save_cache(cache)

    if mode == "plex":
        log_cleanup_event("cleanup_skipped_plex_mode")
        if removed_summary:
            log_cleanup_event("cleanup_consolidated_removed", removed_summary=removed_summary)
        unique_titles_removed = set(t for t, v in removed_summary.items() if any(v.values()))
        log_cleanup_event("cleanup_total_removed", orphans_removed=len(unique_titles_removed))
        return len(unique_titles_removed)
    
    library_types = {"movie", "tv", "show"} 
    preferred_filenames = {f"{lt}_metadata.yml" for lt in library_types}
    metadata_dir = Path(config.get("settings", {}).get("path", ".")) / "metadata"
    def extract_title_year(orphan_title):
        if " (" in orphan_title and orphan_title.endswith(")"):
            t, y = orphan_title.rsplit(" (", 1)
            y = y.rstrip(")")
        else:
            t, y = orphan_title, None
        return t, y

    run_metadata_basic = feature_flags.get("metadata_basic", True)
    run_metadata_enhanced = feature_flags.get("metadata_enhanced", True)
    run_poster = feature_flags.get("poster", True)
    run_season = feature_flags.get("season", True)
    run_background = feature_flags.get("background", True)

    if run_metadata_basic or run_metadata_enhanced:
        for metadata_file in metadata_dir.glob("*.yml"):
            if metadata_file.name not in preferred_filenames:
                log_cleanup_event("cleanup_skipping_nonpreferred", filename=metadata_file.name)
                continue
            try:
                with open(metadata_file, "r", encoding="utf-8") as f:
                    metadata_content = yaml.safe_load(f) or {}

                metadata_entries = metadata_content.get("metadata", {})
                cleaned_metadata = {k: v for k, v in metadata_entries.items() if k in global_existing_titles}

                for k, v in cleaned_metadata.items():
                    t, y = extract_title_year(k)
                    if t and y and "seasons" in v:
                        plex_meta = preloaded_plex_metadata.get((t, int(y), "tv")) or preloaded_plex_metadata.get((t, int(y), "show"))
                        if plex_meta:
                            valid_seasons = set(str(s) for s in (plex_meta.get("seasons_episodes") or {}).keys())
                            cached_seasons = set(str(s) for s in (v.get("seasons") or {}).keys())
                            orphaned_seasons = cached_seasons - valid_seasons
                            for season_num in orphaned_seasons:
                                if feature_flags.get("dry_run", False):
                                    log_cleanup_event("cleanup_dry_run", description="season", path=f"{k} season {season_num}")
                                else:
                                    del v["seasons"][season_num]
                                    log_cleanup_event("cleanup_removed_orphaned_season_yaml", show=t, year=y, season=season_num)
                                    orphans_removed += 1
                                    if t and y and safe_int(y) is not None:
                                        removed_summary.setdefault((t, safe_int(y)), {"cache": False, "asset": [], "yaml": False})
                                        removed_summary[(t, safe_int(y))]["yaml"] = True

                orphans_in_file = len(metadata_entries) - len(cleaned_metadata)
                if orphans_in_file > 0:
                    if feature_flags.get("dry_run", False):
                        log_cleanup_event("cleanup_dry_run", description=cleaned_metadata, path=metadata_file)
                    else:
                        metadata_content["metadata"] = cleaned_metadata
                        with open(metadata_file, "w", encoding="utf-8") as f:
                            yaml.dump(metadata_content, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                        log_cleanup_event("cleanup_removed_orphans", orphans_in_file=orphans_in_file, filename=metadata_file.name)
                        for orphan_title in set(metadata_entries) - set(cleaned_metadata):
                            t, y = extract_title_year(orphan_title)
                            if t and y and safe_int(y) is not None:
                                removed_summary.setdefault((t, safe_int(y)), {"cache": False, "asset": [], "yaml": False})
                                removed_summary[(t, safe_int(y))]["yaml"] = True
                    orphans_removed += orphans_in_file

                if not feature_flags.get("dry_run", False):
                    metadata_content["metadata"] = cleaned_metadata
                    with open(metadata_file, "w", encoding="utf-8") as f:
                        yaml.dump(metadata_content, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                        
            except Exception as e:
                log_cleanup_event("cleanup_failed_remove_metadata", filename=metadata_file, error=str(e))

    if asset_path:
        valid_asset_dirs = set()
        for (title, year, media_type), meta in preloaded_plex_metadata.items():
            if media_type == "movie":
                dir_name = meta.get("movie_path")
                if dir_name:
                    valid_asset_dirs.add(Path(dir_name).name)
            elif media_type in ["show", "tv"]:
                dir_name = meta.get("show_path")
                if dir_name:
                    valid_asset_dirs.add(Path(dir_name).name)

        deleted_dirs = set()
        async def remove_asset_title(path, description):
            nonlocal orphans_removed
            title, year = None, None
            try:
                parent = path.parent
                if " (" in parent.name and parent.name.endswith(")"):
                    title, year = parent.name.rsplit(" (", 1)
                    year = year.rstrip(")")
            except Exception:
                pass
            resolved_path = str(path.resolve())
            if path.parent.name in valid_asset_dirs:
                return
            if existing_assets is not None and resolved_path in existing_assets:
                log_cleanup_event("cleanup_skipping_valid_asset", description=description, path=path)
                return
            if resolved_path not in managed_asset_paths:
                log_cleanup_event("cleanup_skipping_valid_asset", description=f"unmanaged {description}", path=path)
                return
            if feature_flags.get("dry_run", False):
                log_cleanup_event("cleanup_dry_run", description=description, path=path)
            else:
                log_cleanup_event("cleanup_removing_asset", description=description, path=path)
                try:
                    if path.exists():
                        await asyncio.to_thread(path.unlink)
                        orphans_removed += 1
                        deleted_dirs.add(str(path.parent.resolve()))
                        if title and year and safe_int(year) is not None:
                            removed_summary.setdefault((title, safe_int(year)), {"cache": False, "asset": [], "yaml": False})
                            removed_summary[(title, safe_int(year))]["asset"].append(description)
                    else:
                        log_cleanup_event("cleanup_failed_remove_asset", description=description, path=path, error="File does not exist")
                except Exception as e:
                    log_cleanup_event("cleanup_failed_remove_asset", description=description, path=path, error=str(e))

        orphaned_posters = [p for p in Path(asset_path).rglob("poster.jpg")]
        orphaned_season_posters = [p for p in Path(asset_path).rglob("Season*.jpg")]
        orphaned_backgrounds = [p for p in Path(asset_path).rglob("fanart.jpg")]

        tasks = []
        if run_poster:
            tasks.extend(remove_asset_title(p, "poster") for p in orphaned_posters)
        if run_season:
            tasks.extend(remove_asset_title(p, "season poster") for p in orphaned_season_posters)
        if run_background:
            tasks.extend(remove_asset_title(p, "background") for p in orphaned_backgrounds)
        await asyncio.gather(*tasks)

        for dir_path_str in deleted_dirs:
            dir_path = Path(dir_path_str)
            try:
                if dir_path.exists() and dir_path.is_dir() and not any(dir_path.iterdir()):
                    if feature_flags.get("dry_run", False):
                        log_cleanup_event("cleanup_dry_run", description="directory", path=dir_path)
                    else:
                        log_cleanup_event("cleanup_removing_empty_dir", parent=dir_path)
                        await asyncio.to_thread(dir_path.rmdir)
            except Exception as e:
                log_cleanup_event("cleanup_failed_remove_asset", description="directory", path=dir_path, error=str(e))

    if removed_summary:
        log_cleanup_event("cleanup_consolidated_removed", removed_summary=removed_summary)

    unique_titles_removed = set(t for t, v in removed_summary.items() if any(v.values()))
    log_cleanup_event("cleanup_total_removed", orphans_removed=len(unique_titles_removed))
    return len(unique_titles_removed)
