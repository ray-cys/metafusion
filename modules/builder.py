import asyncio
from helper.logging import log_builder_event, log_asset_status
from helper.cache import load_cache, meta_cache_async
from helper.config import get_image_upgrade_days
from helper.identity import cache_key_for_meta, legacy_cache_key, match_for_meta, metadata_key_for_meta
from helper.io import atomic_replace_file, sha256_file
from helper.plex import get_plex_country
from helper.tmdb import artwork_language_codes, resolve_tmdb_id, tmdb_api_request
from modules.utils import (
    smart_meta_update, get_meta_field, recursive_season_diff, get_best_poster, get_best_season, get_best_background,
    smart_asset_upgrade, smart_season_asset_upgrade, asset_temp_path, download_poster, get_asset_path, format_runtime
)
from modules.kometa import (
    EPISODE_BASIC_FIELDS,
    EPISODE_ENHANCED_FIELDS,
    build_episode_metadata,
)


def regional_movie_certification(release_dates, region="US"):
    regions = [str(region or "US").upper()]
    if "US" not in regions:
        regions.append("US")
    for wanted in regions:
        for country in release_dates or []:
            if country.get("iso_3166_1") != wanted:
                continue
            for release in country.get("release_dates", []):
                if release.get("certification"):
                    return release["certification"]
    return ""


def regional_tv_certification(content_ratings, region="US"):
    regions = [str(region or "US").upper()]
    if "US" not in regions:
        regions.append("US")
    for wanted in regions:
        for rating in content_ratings or []:
            if rating.get("iso_3166_1") == wanted and rating.get("rating"):
                return rating["rating"]
    return ""


def cached_source_matches(cache_key, source_path, asset_path, asset_type, season_number=None):
    """Avoid downloading a known TMDb source when its managed file still exists."""
    if not source_path or not asset_path.exists():
        return False
    cached = load_cache().get(cache_key, {})
    if not isinstance(cached, dict):
        return False
    if asset_type == "season":
        season = (cached.get("seasons") or {}).get(str(season_number), {})
        return season.get("season_source_path") == source_path
    return cached.get(f"{asset_type}_source_path") == source_path

async def build_movie(
    config, consolidated_metadata, feature_flags=None, existing_yaml_data=None, session=None, ignored_fields=None,
    existing_assets=None, meta=None, 
):
    feature_flags = feature_flags or {}
    run_metadata = feature_flags.get("metadata_basic", True)
    run_poster = feature_flags.get("poster", False)
    run_background = feature_flags.get("background", False)
    metadata_action = "skipped" if run_metadata else "not_due"
    poster_action = "skipped" if run_poster else "not_due"
    background_action = "skipped" if run_background else "not_due"
    result = {
        "poster": {"size": 0},
        "background": {"size": 0},
    }
        
    if not any((run_metadata, run_poster, run_background)):
        return {
            "percent": 100,
            "incomplete_percent": 0,
            "is_complete": True,
            "metadata_action": metadata_action,
            "poster_action": poster_action,
            "background_action": background_action,
            **result,
        }
    if ignored_fields is None:
        ignored_fields = set()
    if existing_assets is None:
        existing_assets = set()
    title = meta.get("title", "Unknown") if meta else None
    year = meta.get("year", "Unknown") if meta else None
    full_title = metadata_key_for_meta(meta)
    cache_key = cache_key_for_meta(meta)
    old_cache_key = legacy_cache_key(meta)
    movie_path = meta.get("movie_path") if meta else None
    tmdb_id = meta.get("tmdb_id") if meta else None
    imdb_id = meta.get("imdb_id") if meta else None
    tmdb_id = await resolve_tmdb_id(
        config,
        "movie",
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        session=session,
    )
    if meta is not None and tmdb_id:
        meta["tmdb_id"] = tmdb_id
    mapping_id = None

    if tmdb_id:
        mapping_id = int(tmdb_id)
    if not mapping_id and imdb_id:
        mapping_id = imdb_id
    if run_metadata and not mapping_id:
        log_builder_event("builder_missing_tmdb_and_imdb_id", media_type="Movie", full_title=full_title)
        metadata_action = "failed"
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": metadata_action,
            "poster_action": poster_action,
            "background_action": background_action,
            **result,
        }

    details_key = f"movie/{tmdb_id}"
    details = await tmdb_api_request(
        config,
        details_key,
        params={
            "append_to_response": "credits,release_dates,external_ids,images",
            "language": config.get("tmdb", {}).get("language", "en-US"),
            "region": config.get("tmdb", {}).get("region", "US"),
            "include_image_language": artwork_language_codes(config),
        },
        session=session
    )
    if not details:
        log_builder_event("builder_invalid_tmdb_id", media_type="Movie", full_title=full_title)
        metadata_action = "failed"
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": "failed" if run_metadata else metadata_action,
            "poster_action": "failed" if run_poster else poster_action,
            "background_action": "failed" if run_background else background_action,
            **result,
        }

    release_dates = get_meta_field(details, "results", [], path=["release_dates"])
    content_rating = regional_movie_certification(
        release_dates,
        config.get("tmdb", {}).get("region", "US"),
    )

    genres = [g.get("name", "") for g in get_meta_field(details, "genres", [])]
    studio = ", ".join([c.get("name", "") for c in get_meta_field(details, "production_companies", []) if c.get("name")]) or ""
    release_date = get_meta_field(details, "release_date", "")

    production_countries = get_meta_field(details, "production_countries", [])
    country_codes = [c.get("iso_3166_1", "") for c in production_countries if c.get("iso_3166_1")]
    countries = [get_plex_country(code) for code in country_codes]

    originally_available = release_date or ""
    runtime = format_runtime(get_meta_field(details, "runtime", None))

    collection_info = get_meta_field(details, "belongs_to_collection", {})
    collection_id = get_meta_field(collection_info, "id", None)
    collection_name = get_meta_field(collection_info, "name", "")
    cleaned_collection = collection_name.removesuffix(" Collection")

    director_jobs = {"Director", "Co-Director", "Assistant Director"}
    writer_jobs = {"Writer", "Screenplay", "Story", "Creator", "Co-Writer", "Author", "Adaptation"}
    producer_jobs = {"Producer", "Executive Producer", "Associate Producer", "Co-Producer", "Line Producer", "Co-Executive Producer"}
    
    credits = get_meta_field(details, "credits", {})
    crew = get_meta_field(credits, "crew", [])
    cast = get_meta_field(credits, "cast", [])
    directors = [m.get("name", "") for m in crew if m.get("job") in director_jobs]
    writers = [m.get("name", "") for m in crew if m.get("job") in writer_jobs]
    producers = [m.get("name", "") for m in crew if m.get("job") in producer_jobs]
    top_cast = [c.get("name", "") for c in cast[:10]]

    basic_fields = [
        "sort_title", "original_title", "originally_available", "content_rating",
        "studio", "runtime", "tagline", "summary", "country.sync", "genre.sync"
    ]
    enhanced_fields = [
        "cast.sync", "director.sync", "writer.sync", "producer.sync"
    ]
    fields_to_write = basic_fields + (enhanced_fields if feature_flags.get("metadata_enhanced", True) else [])

    new_metadata = {}
    for k in fields_to_write:
        if k == "sort_title":
            new_metadata[k] = title or ""
        elif k == "original_title":
            new_metadata[k] = get_meta_field(details, "original_title", title) or ""
        elif k == "originally_available":
            new_metadata[k] = originally_available or ""
        elif k == "content_rating":
            new_metadata[k] = content_rating or ""
        elif k == "studio":
            new_metadata[k] = studio or ""
        elif k == "runtime":
            new_metadata[k] = runtime if runtime is not None else ""
        elif k == "tagline":
            new_metadata[k] = get_meta_field(details, "tagline", "") or ""
        elif k == "summary":
            new_metadata[k] = get_meta_field(details, "overview", "") or ""
        elif k == "country.sync":
            new_metadata[k] = countries if countries else []
        elif k == "genre.sync":
            new_metadata[k] = genres if genres else []
        elif k == "cast.sync":
            new_metadata[k] = top_cast if top_cast else []
        elif k == "director.sync":
            new_metadata[k] = directors if directors else []
        elif k == "writer.sync":
            new_metadata[k] = writers if writers else []
        elif k == "producer.sync":
            new_metadata[k] = producers if producers else []
        else:
            new_metadata[k] = "" 

    expected_fields = fields_to_write
    if ignored_fields is None:
        ignored_fields = set()
    filtered_fields = [f for f in expected_fields if f not in ignored_fields]
    if not filtered_fields:
        percent_filled = 100
        filled = 0
    else:
        filled = sum(
            bool(new_metadata.get(f)) and new_metadata.get(f) != [] and new_metadata.get(f) != ""
            for f in filtered_fields
        )
        percent_filled = round((filled / len(filtered_fields)) * 100)
    percent = percent_filled
    is_complete = (percent >= 90)

    metadata_changed = False
    changes = []
    if run_metadata:
        if existing_yaml_data:
            existing_metadata = existing_yaml_data.get("metadata", {}).get(full_title, {})
            changes = smart_meta_update(existing_metadata, new_metadata)
            if not changes:
                log_builder_event(
                    "builder_no_metadata_changes", media_type="Movie", full_title=full_title,
                    percent=percent, incomplete_percent=100 - percent
                )
                metadata_action = "skipped"
            else:
                consolidated_metadata["metadata"][full_title] = {
                    "match": match_for_meta(meta, mapping_id),
                    **new_metadata
                }
                metadata_changed = True
                log_builder_event(
                    "build_metadata_changed", media_type="Movie", full_title=full_title,
                    percent=percent, tmdb_id=tmdb_id, changes=changes
                )
                metadata_action = "upgraded"
        else:
            consolidated_metadata["metadata"][full_title] = {
                "match": match_for_meta(meta, mapping_id),
                **new_metadata
            }
            metadata_changed = True
            changes = list(new_metadata.keys())
            log_builder_event(
                "builder_no_existing_metadata", media_type="Movie", full_title=full_title, tmdb_id=tmdb_id
            )
            metadata_action = "downloaded"

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_metadata", media_type="Movie", full_title=full_title)

        if not feature_flags.get("dry_run", False):
            if metadata_changed:
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    legacy_cache_key=old_cache_key,
                    collection_id=collection_id, collection_name=cleaned_collection
                )
                log_builder_event("builder_metadata_cached", media_type="Movie", full_title=full_title, cache_key=cache_key)
            else:
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    legacy_cache_key=old_cache_key,
                    collection_id=collection_id, collection_name=cleaned_collection, update_timestamp=False
                )

    async def process_poster():
        poster_size = 0
        nonlocal poster_action
        if not feature_flags or not feature_flags.get("poster", True):
            result["poster"]["size"] = poster_size
            poster_action = "not_due"
            return
        if not movie_path:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="poster", extra="")
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_asset", media_type="Movie", asset_type="poster", full_title=full_title)
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return
        
        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_poster(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="Movie", asset_type="poster", full_title=full_title, extra="")
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, poster_checked=True,
            )
            result["poster"]["size"] = poster_size
            poster_action = "missing"
            return   

        asset_path = get_asset_path(config, meta, asset_type="poster")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="poster", extra="")
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        if cached_source_matches(
            cache_key, best.get("file_path"), asset_path, "poster"
        ):
            poster_size = asset_path.stat().st_size
            existing_assets.add(str(asset_path.resolve()))
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, poster_checked=True,
                poster_average=best.get("vote_average", 0),
                poster_source_path=best.get("file_path"),
            )
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return

        temp_path = asset_temp_path(config, meta)
        try:
            success, status, error = await download_poster(config, best["file_path"], temp_path, session=session)
            if not success:
                log_builder_event(
                    "builder_asset_download_failed", media_type="Movie", asset_type="poster",
                    full_title=full_title, status=status, error=error
                )
                poster_action = "failed"
            if success and temp_path.exists():
                stale_days = get_image_upgrade_days(config, "movie")
                should_upgrade, status_code, context = smart_asset_upgrade(
                    config, asset_path, best, new_image_path=temp_path, asset_type="poster",
                    cache_key=cache_key, stale_days=stale_days,
                )
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    update_timestamp=False, poster_checked=True,
                    poster_average=best.get("vote_average", 0),
                    poster_source_path=best.get("file_path"),
                )
                if should_upgrade:
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace_file(temp_path, asset_path)
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "movie",
                        poster_average=best.get("vote_average", 0),
                        poster_path=str(asset_path.resolve()),
                        poster_checksum=await asyncio.to_thread(sha256_file, asset_path),
                        poster_upgraded=True,
                        update_timestamp=False,
                    )
                    if status_code == "FORCE_UPGRADE_STALE":
                        log_builder_event(
                            "builder_force_upgrade_stale", media_type="Movie", full_title=full_title, filesize=poster_size,
                            last_upgraded=context.get("last_upgraded"), stale_days=stale_days)
                        poster_action = "upgraded"
                    elif status_code == "NO_EXISTING_ASSET":
                        log_builder_event(
                            "builder_downloading_asset", media_type="Movie", asset_type="poster",
                            full_title=full_title, filesize=poster_size
                        )
                        poster_action = "downloaded"
                    else:
                        log_builder_event(
                            "builder_asset_upgraded", media_type="Movie", asset_type="Poster",
                            full_title=full_title, status_code=status_code, context=context, filesize=poster_size
                        )
                        poster_action = "upgraded"
                    existing_assets.add(str(asset_path.resolve()))
                else:
                    poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    log_asset_status(
                        status_code, media_type="Movie", asset_type="poster", full_title=full_title,
                        filesize=poster_size, error=context.get("error") if context else None, extra="", season_number=None
                    )
                    poster_action = "skipped"
                    if asset_path.exists():
                        existing_assets.add(str(asset_path.resolve()))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        result["poster"]["size"] = poster_size

    async def process_background():
        background_size = 0
        nonlocal background_action
        if not feature_flags or not feature_flags.get("background", True):
            result["background"]["size"] = background_size
            background_action = "not_due"
            return
        if not movie_path:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="background", extra="")
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_asset", media_type="Movie", asset_type="background", full_title=full_title)
            result["background"]["size"] = background_size
            background_action = "skipped"
            return
    
        images = get_meta_field(details, "backdrops", [], path=["images"])
        best = get_best_background(config, images)
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="Movie", asset_type="background", full_title=full_title, extra="")
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, background_checked=True,
            )
            result["background"]["size"] = background_size
            background_action = "missing"
            return

        asset_path = get_asset_path(config, meta, asset_type="background")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="background", extra="")
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        if cached_source_matches(
            cache_key, best.get("file_path"), asset_path, "background"
        ):
            background_size = asset_path.stat().st_size
            existing_assets.add(str(asset_path.resolve()))
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, background_checked=True,
                bg_average=best.get("vote_average", 0),
                background_source_path=best.get("file_path"),
            )
            result["background"]["size"] = background_size
            background_action = "skipped"
            return

        temp_path = asset_temp_path(config, meta)
        try:
            success, status, error = await download_poster(config, best["file_path"], temp_path, session=session)
            if not success:
                log_builder_event(
                    "builder_asset_download_failed", media_type="Movie", asset_type="background",
                    full_title=full_title, status=status, error=error
                )
                background_action = "failed"
            if success and temp_path.exists():
                stale_days = get_image_upgrade_days(config, "movie")
                should_upgrade, status_code, context = smart_asset_upgrade(
                    config, asset_path, best, new_image_path=temp_path, asset_type="background",
                    cache_key=cache_key, stale_days=stale_days,
                )
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    update_timestamp=False, background_checked=True,
                    bg_average=best.get("vote_average", 0),
                    background_source_path=best.get("file_path"),
                )
                if should_upgrade:
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace_file(temp_path, asset_path)
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    background_size = asset_path.stat().st_size if asset_path.exists() else 0
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "movie",
                        bg_average=best.get("vote_average", 0),
                        background_path=str(asset_path.resolve()),
                        background_checksum=await asyncio.to_thread(sha256_file, asset_path),
                        background_upgraded=True,
                        update_timestamp=False,
                    )
                    if status_code == "FORCE_UPGRADE_STALE":
                        log_builder_event(
                            "builder_force_upgrade_stale", media_type="Movie", full_title=full_title, filesize=background_size,
                            last_upgraded=context.get("last_upgraded"), stale_days=stale_days)
                        background_action = "upgraded"
                    elif status_code == "NO_EXISTING_ASSET":
                        log_builder_event(
                            "builder_downloading_asset", media_type="Movie", asset_type="background",
                            full_title=full_title, filesize=background_size
                        )
                        background_action = "downloaded"
                    else:
                        log_builder_event(
                        "builder_asset_upgraded", media_type="Movie", asset_type="Background",
                        full_title=full_title, status_code=status_code, context=context, filesize=background_size
                        )
                        background_action = "upgraded"
                    existing_assets.add(str(asset_path.resolve()))
                else:
                    background_size = asset_path.stat().st_size if asset_path.exists() else 0
                    log_asset_status(
                        status_code, media_type="Movie", asset_type="background", full_title=full_title,
                        filesize=background_size, error=context.get("error") if context else None, extra="", season_number=None
                    )
                    background_action = "skipped"
                    if asset_path.exists():
                        existing_assets.add(str(asset_path.resolve()))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        result["background"]["size"] = background_size

    await asyncio.gather(
        process_poster(),
        process_background(),
    )

    return {
        "percent": percent,
        "incomplete_percent": 100 - percent,
        "is_complete": is_complete,
        "metadata_action": metadata_action,
        "poster_action": poster_action,
        "background_action": background_action,
        **result
    }

async def build_tv(
    config, consolidated_metadata, feature_flags=None, existing_yaml_data=None, session=None, ignored_fields=None,
    existing_assets=None, meta=None, 
):
    feature_flags = feature_flags or {}
    run_metadata = feature_flags.get("metadata_basic", True)
    run_poster = feature_flags.get("poster", False)
    run_background = feature_flags.get("background", False)
    run_season = feature_flags.get("season", False)
    metadata_action = "skipped" if run_metadata else "not_due"
    poster_action = "skipped" if run_poster else "not_due"
    background_action = "skipped" if run_background else "not_due"
    season_poster_actions = {}
    result = {
        "poster": {"size": 0},
        "background": {"size": 0},
        "season_poster": {"size": 0},
        "season_posters": {}, 
    }
    if not any((run_metadata, run_poster, run_background, run_season)):
        return {
            "percent": 100,
            "incomplete_percent": 0,
            "is_complete": True,
            "metadata_action": metadata_action,
            "poster_action": poster_action,
            "background_action": background_action,
            "seasons": {},
            "season_poster_actions": season_poster_actions,
            **result,
        }
    if ignored_fields is None:
        ignored_fields = set()
    if existing_assets is None:
        existing_assets = set()
    title = meta.get("title", "Unknown") if meta else None
    year = meta.get("year", "Unknown") if meta else None
    full_title = metadata_key_for_meta(meta)
    cache_key = cache_key_for_meta(meta)
    old_cache_key = legacy_cache_key(meta)
    show_path = meta.get("show_path") if meta else None
    seasons_episodes = meta.get("seasons_episodes") if meta else None
    tmdb_id = meta.get("tmdb_id") if meta else None
    tvdb_id = meta.get("tvdb_id") if meta else None
    imdb_id = meta.get("imdb_id") if meta else None
    tmdb_id = await resolve_tmdb_id(
        config,
        "tv",
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        session=session,
    )
    if meta is not None and tmdb_id:
        meta["tmdb_id"] = tmdb_id
    if not feature_flags.get("dry_run", False):
        await meta_cache_async(
            cache_key,
            tmdb_id,
            title,
            year,
            "tv",
            update_timestamp=False,
            legacy_cache_key=old_cache_key,
        )
    mapping_id = None

    if tvdb_id:
        mapping_id = int(tvdb_id)
    elif imdb_id:
        mapping_id = imdb_id
    elif tmdb_id:
        external_ids = await tmdb_api_request(
            config,
            f"tv/{tmdb_id}/external_ids",
            session=session,
        )
        if external_ids:
            tvdb_id_from_tmdb = external_ids.get("tvdb_id", "")
            imdb_id_from_tmdb = external_ids.get("imdb_id", "")
            if tvdb_id_from_tmdb:
                mapping_id = tvdb_id_from_tmdb
            elif imdb_id_from_tmdb:
                mapping_id = imdb_id_from_tmdb

    if run_metadata and not mapping_id:
        log_builder_event("builder_missing_tvdb_id_and_imdb_id", media_type="TV Show", full_title=full_title)
        metadata_action = "failed"
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": metadata_action,
            "poster_action": poster_action,
            "background_action": background_action,
            "seasons": {},
            "season_poster_actions": season_poster_actions,
            **result,
        }

    details_key = f"tv/{tmdb_id}"
    details = await tmdb_api_request(
        config,
        details_key,
        params={
            "append_to_response": "credits,keywords,content_ratings,external_ids,images",
            "language": config.get("tmdb", {}).get("language", "en-US"),
            "region": config.get("tmdb", {}).get("region", "US"),
            "include_image_language": artwork_language_codes(config),
        },
        session=session
    )
    if not details:
        log_builder_event("builder_no_tmdb_id", media_type="TV Show", full_title=full_title)
        metadata_action = "failed"
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": "failed" if run_metadata else metadata_action,
            "poster_action": "failed" if run_poster else poster_action,
            "background_action": "failed" if run_background else background_action,
            "seasons": {},
            "season_poster_actions": season_poster_actions,
            **result,
        }

    content_ratings = get_meta_field(details, "results", [], path=["content_ratings"])
    content_rating = regional_tv_certification(
        content_ratings,
        config.get("tmdb", {}).get("region", "US"),
    )
    
    genres = [g.get("name", "") for g in get_meta_field(details, "genres", [])]
    studios = [n.get("name", "") for n in get_meta_field(details, "networks", []) if n.get("name")]
    studio = ", ".join(studios) if studios else ""
    originally_available = get_meta_field(details, "first_air_date", "") or ""
    country_codes = get_meta_field(details, "origin_country", [])
    countries = [get_plex_country(code) for code in country_codes]

    season_infos = get_meta_field(details, "seasons", [])
    season_details_by_number = {}

    async def get_season_details(season_number):
        if season_number in season_details_by_number:
            return season_details_by_number[season_number]
        season_details = await tmdb_api_request(
            config,
            f"tv/{tmdb_id}/season/{season_number}",
            params={
                "append_to_response": "credits,images",
                "include_image_language": artwork_language_codes(config),
            },
            session=session,
        )
        if season_details:
            season_details_by_number[season_number] = season_details
        return season_details

    seasons_data = {}
    grand_percent = 100
    is_complete = True
    if run_metadata:
        show_basic_fields = [
            "sort_title", "original_title", "originally_available", "content_rating",
            "studio", "tagline", "summary", "country.sync", "genre.sync", "seasons"
        ]
        show_fields_to_write = list(show_basic_fields)
        episode_fields_to_write = list(EPISODE_BASIC_FIELDS)
        if feature_flags.get("metadata_enhanced", True):
            episode_fields_to_write.extend(EPISODE_ENHANCED_FIELDS)

        new_metadata = {}
        for key in show_fields_to_write:
            values = {
                "sort_title": title or "",
                "original_title": details.get("original_name", title) or "",
                "originally_available": originally_available or "",
                "content_rating": content_rating or "",
                "studio": studio or "",
                "tagline": details.get("tagline", "") or "",
                "summary": details.get("overview", "") or "",
                "genre.sync": genres if genres else [],
                "country.sync": countries if countries else [],
            }
            new_metadata[key] = values.get(key, "")

        async def process_season(season_info):
            season_number = season_info.get("season_number")
            if (
                season_number is None
                or not seasons_episodes
                or season_number not in seasons_episodes
            ):
                return season_number, None
            season_details = await get_season_details(season_number)
            if not season_details:
                log_builder_event(
                    "builder_no_tmdb_season_data", media_type="TV Shows",
                    season_number=season_number, full_title=full_title
                )
                return season_number, None

            show_crew = get_meta_field(
                get_meta_field(details, "credits", {}), "crew", []
            ) or []
            season_crew = get_meta_field(
                get_meta_field(season_details, "credits", {}), "crew", []
            ) or []
            director_jobs = {"Director", "Co-Director", "Assistant Director"}
            writer_jobs = {
                "Writer", "Screenplay", "Story", "Creator", "Co-Writer",
                "Author", "Adaptation", "Novel",
            }
            episodes = {}
            for episode in get_meta_field(season_details, "episodes", []):
                episode_number = episode.get("episode_number")
                if episode_number not in seasons_episodes[season_number]:
                    continue
                crew = get_meta_field(episode, "crew", []) or season_crew or show_crew
                episodes[episode_number] = build_episode_metadata(
                    episode,
                    directors=[
                        member.get("name", "")
                        for member in crew
                        if member.get("job") in director_jobs
                    ],
                    writers=[
                        member.get("name", "")
                        for member in crew
                        if member.get("job") in writer_jobs
                    ],
                    enhanced=feature_flags.get("metadata_enhanced", True),
                )
            return season_number, {
                "originally_available": get_meta_field(
                    season_details, "air_date", ""
                ) or "",
                "episodes": episodes,
            }

        results = await asyncio.gather(*(process_season(info) for info in season_infos))
        for season_number, season_data in results:
            if season_data:
                seasons_data[season_number] = season_data

        episode_filled = 0
        episode_total = 0
        for season in seasons_data.values():
            for episode in season.get("episodes", {}).values():
                for field in episode_fields_to_write:
                    episode_total += 1
                    if episode.get(field) not in (None, "", []):
                        episode_filled += 1

        metadata_entry = {
            "match": {"title": title, "year": year, "mapping_id": mapping_id},
            **new_metadata,
            "seasons": seasons_data,
        }
        expected_fields = [field for field in show_fields_to_write if field != "seasons"]
        filtered_fields = [field for field in expected_fields if field not in ignored_fields]
        show_fields_filled = sum(
            new_metadata.get(field) not in (None, "", []) for field in filtered_fields
        )
        all_filled = show_fields_filled + episode_filled
        all_total = len(filtered_fields) + episode_total
        grand_percent = round((all_filled / all_total) * 100) if all_total else 100
        is_complete = grand_percent >= 70

        metadata_changed = False
        if existing_yaml_data:
            existing_metadata = existing_yaml_data.get("metadata", {}).get(full_title, {})
            changes = smart_meta_update(
                {key: value for key, value in existing_metadata.items() if key != "seasons"},
                {key: value for key, value in metadata_entry.items() if key != "seasons"},
            ) + recursive_season_diff(
                existing_metadata.get("seasons", {}), seasons_data
            )
            if changes:
                consolidated_metadata["metadata"][full_title] = metadata_entry
                metadata_changed = True
                metadata_action = "upgraded"
                log_builder_event(
                    "build_metadata_changed", media_type="TV Show",
                    full_title=full_title, percent=grand_percent,
                    tmdb_id=tmdb_id, changes=changes,
                )
            else:
                metadata_action = "skipped"
                log_builder_event(
                    "builder_no_metadata_changes", media_type="TV Show",
                    full_title=full_title, percent=grand_percent,
                    incomplete_percent=100 - grand_percent,
                )
        else:
            consolidated_metadata["metadata"][full_title] = metadata_entry
            metadata_changed = True
            metadata_action = "downloaded"
            log_builder_event(
                "builder_no_existing_metadata", media_type="TV Show",
                full_title=full_title, tmdb_id=tmdb_id,
            )

        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_metadata", media_type="TV Show", full_title=full_title
            )
        if metadata_changed and not feature_flags.get("dry_run", False):
            await meta_cache_async(cache_key, tmdb_id, title, year, "tv")
            log_builder_event(
                "builder_metadata_cached", media_type="TV Show",
                full_title=full_title, cache_key=cache_key,
            )

    async def process_tv_poster():
        poster_size = 0
        nonlocal poster_action
        if not feature_flags or not feature_flags.get("poster", True):
            result["poster"]["size"] = poster_size
            poster_action = "not_due"
            return
        if not show_path:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="poster", extra="")
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_asset", media_type="TV Show", asset_type="poster", full_title=full_title)
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return
            
        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_poster(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="TV Show", asset_type="poster", full_title=full_title, extra="")
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "tv",
                update_timestamp=False, poster_checked=True,
            )
            result["poster"]["size"] = poster_size
            poster_action = "missing"
            return

        asset_path = get_asset_path(config, meta, asset_type="poster")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="poster", extra="")
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        if cached_source_matches(
            cache_key, best.get("file_path"), asset_path, "poster"
        ):
            poster_size = asset_path.stat().st_size
            existing_assets.add(str(asset_path.resolve()))
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "tv",
                update_timestamp=False, poster_checked=True,
                poster_average=best.get("vote_average", 0),
                poster_source_path=best.get("file_path"),
            )
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return

        temp_path = asset_temp_path(config, meta)
        try:
            success, status, error = await download_poster(config, best["file_path"], temp_path, session=session)
            if not success:
                log_builder_event(
                    "builder_asset_download_failed", media_type="TV Show", asset_type="poster",
                    full_title=full_title, status=status, error=error
                )
                poster_action = "failed"
            if success and temp_path.exists():
                stale_days = get_image_upgrade_days(config, "series")
                should_upgrade, status_code, context = smart_asset_upgrade(
                    config, asset_path, best, new_image_path=temp_path, asset_type="poster",
                    cache_key=cache_key, stale_days=stale_days,
                )
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "tv",
                    update_timestamp=False, poster_checked=True,
                    poster_average=best.get("vote_average", 0),
                    poster_source_path=best.get("file_path"),
                )
                if should_upgrade:
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace_file(temp_path, asset_path)
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        poster_average=best.get("vote_average", 0),
                        poster_path=str(asset_path.resolve()),
                        poster_checksum=await asyncio.to_thread(sha256_file, asset_path),
                        poster_upgraded=True,
                        update_timestamp=False,
                    )
                    if status_code == "FORCE_UPGRADE_STALE":
                        log_builder_event(
                            "builder_force_upgrade_stale", media_type="TV Show", full_title=full_title, filesize=poster_size,
                            last_upgraded=context.get("last_upgraded"), stale_days=stale_days)
                        poster_action = "upgraded"
                    elif status_code == "NO_EXISTING_ASSET":
                        log_builder_event(
                            "builder_downloading_asset", media_type="TV Show", asset_type="poster",
                            full_title=full_title, filesize=poster_size
                        )
                        poster_action = "downloaded"
                    else:
                        log_builder_event(
                            "builder_asset_upgraded", media_type="TV Show", asset_type="Poster",
                            full_title=full_title, status_code=status_code, context=context, filesize=poster_size
                        )
                        poster_action = "upgraded"
                    existing_assets.add(str(asset_path.resolve()))
                else:
                    poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    log_asset_status(
                        status_code, media_type="TV Show", asset_type="poster", full_title=full_title,
                        filesize=poster_size, error=context.get("error") if context else None, extra="", season_number=None
                    )
                    poster_action = "skipped"
                    if asset_path.exists():
                        existing_assets.add(str(asset_path.resolve()))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        result["poster"]["size"] = poster_size

    async def process_tv_background():
        background_size = 0
        nonlocal background_action
        if not feature_flags or not feature_flags.get("background", True):
            result["background"]["size"] = background_size
            background_action = "not_due"
            return
        if not show_path:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="background", extra="")
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_asset", media_type="TV Show", asset_type="background", full_title=full_title)
            result["background"]["size"] = background_size
            background_action = "skipped"
            return
            
        images = get_meta_field(details, "backdrops", [], path=["images"])
        best = get_best_background(config, images)
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="TV Show", asset_type="background", full_title=full_title, extra="")
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "tv",
                update_timestamp=False, background_checked=True,
            )
            result["background"]["size"] = background_size
            background_action = "missing"
            return
    
        asset_path = get_asset_path(config, meta, asset_type="background")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="background", extra="")
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        if cached_source_matches(
            cache_key, best.get("file_path"), asset_path, "background"
        ):
            background_size = asset_path.stat().st_size
            existing_assets.add(str(asset_path.resolve()))
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "tv",
                update_timestamp=False, background_checked=True,
                bg_average=best.get("vote_average", 0),
                background_source_path=best.get("file_path"),
            )
            result["background"]["size"] = background_size
            background_action = "skipped"
            return
    
        temp_path = asset_temp_path(config, meta)
        try:
            success, status, error = await download_poster(config, best["file_path"], temp_path, session=session)
            if not success:
                log_builder_event(
                    "builder_asset_download_failed", media_type="TV Show", asset_type="background",
                    full_title=full_title, status=status, error=error
                )
                background_action = "failed"
            if success and temp_path.exists():
                stale_days = get_image_upgrade_days(config, "series")
                should_upgrade, status_code, context = smart_asset_upgrade(
                    config, asset_path, best, new_image_path=temp_path, asset_type="background",
                    cache_key=cache_key, stale_days=stale_days,
                )
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "tv",
                    update_timestamp=False, background_checked=True,
                    bg_average=best.get("vote_average", 0),
                    background_source_path=best.get("file_path"),
                )
                if should_upgrade:
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace_file(temp_path, asset_path)
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    background_size = asset_path.stat().st_size if asset_path.exists() else 0
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        bg_average=best.get("vote_average", 0),
                        background_path=str(asset_path.resolve()),
                        background_checksum=await asyncio.to_thread(sha256_file, asset_path),
                        background_upgraded=True,
                        update_timestamp=False,
                    )
                    if status_code == "FORCE_UPGRADE_STALE":
                        log_builder_event(
                            "builder_force_upgrade_stale", media_type="TV Show", full_title=full_title, filesize=background_size,
                            last_upgraded=context.get("last_upgraded"), stale_days=stale_days)
                        background_action = "upgraded"
                    elif status_code == "NO_EXISTING_ASSET":
                        log_builder_event(
                            "builder_downloading_asset", media_type="TV Show", asset_type="background",
                            full_title=full_title, filesize=background_size
                        )
                        background_action = "downloaded"
                    else:
                        log_builder_event(
                            "builder_asset_upgraded", media_type="TV Show", asset_type="Background",
                            full_title=full_title, status_code=status_code, context=context, filesize=background_size
                        )
                        background_action = "upgraded"
                    existing_assets.add(str(asset_path.resolve()))
                else:
                    background_size = asset_path.stat().st_size if asset_path.exists() else 0
                    log_asset_status(
                        status_code, media_type="TV Show", asset_type="background", full_title=full_title,
                        filesize=background_size, error=context.get("error") if context else None, extra="", season_number=None
                    )
                    background_action = "skipped"
                    if asset_path.exists():
                        existing_assets.add(str(asset_path.resolve()))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        result["background"]["size"] = background_size
    
    async def process_season_poster(season_info):
        season_poster_size = 0
        season_number = season_info.get("season_number")
        if season_number is None:
            nonlocal season_poster_actions
            season_poster_actions[season_number] = "skipped"
            return
        
        if not show_path:
            log_builder_event("builder_no_asset_path_season", media_type="TV Show", full_title=full_title, season_number=season_number)
            season_poster_actions[season_number] = "failed"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_asset_season", media_type="TV Show", season_number=season_number, asset_type="poster", full_title=full_title)
            result["season_posters"][season_number] = season_poster_size
            season_poster_actions[season_number] = "skipped"
            return
        
        season_details = await get_season_details(season_number)
        if not season_details:
            log_builder_event("builder_no_season_details", media_type="TV Show", full_title=full_title, season_number=season_number)
            season_poster_actions[season_number] = "failed"
            return

        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(season_details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_season(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            log_builder_event(
                "builder_no_suitable_asset_season", media_type="TV Show", asset_type="poster",
                full_title=full_title, season_number=season_number
            )
            season_poster_actions[season_number] = "missing"
            return

        asset_path = get_asset_path(config, meta, asset_type="season", season_number=season_number)
        if asset_path is None:
            log_builder_event("builder_no_asset_path_season", media_type="TV Show", full_title=full_title, season_number=season_number)
            season_poster_actions[season_number] = "failed"
            return

        if cached_source_matches(
            cache_key,
            best.get("file_path"),
            asset_path,
            "season",
            season_number=season_number,
        ):
            season_poster_size = asset_path.stat().st_size
            existing_assets.add(str(asset_path.resolve()))
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "tv",
                update_timestamp=False, season_number=season_number,
                season_average=best.get("vote_average", 0),
                season_source_path=best.get("file_path"),
            )
            result["season_posters"][season_number] = season_poster_size
            season_poster_actions[season_number] = "skipped"
            return

        temp_path = asset_temp_path(config, meta)
        try:
            success, status, error = await download_poster(config, best["file_path"], temp_path, session=session)
            if not success:
                log_builder_event(
                    "builder_asset_download_failed_season", media_type="TV Show", asset_type="poster",
                    full_title=full_title, season_number=season_number, status=status, error=error
                )
                season_poster_actions[season_number] = "failed"
            if success and temp_path.exists():
                stale_days = get_image_upgrade_days(config, "season")
                should_upgrade, status_code, context = smart_season_asset_upgrade(
                    config, asset_path, best, new_image_path=temp_path,
                    cache_key=cache_key, season_number=season_number,
                    stale_days=stale_days,
                )
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "tv",
                    update_timestamp=False, season_number=season_number,
                    season_average=best.get("vote_average", 0),
                    season_source_path=best.get("file_path"),
                )
                if should_upgrade:
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_replace_file(temp_path, asset_path)
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                    season_poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        season_number=season_number,
                        season_average=best.get("vote_average", 0),
                        season_path=str(asset_path.resolve()),
                        season_checksum=await asyncio.to_thread(sha256_file, asset_path),
                        season_upgraded=season_number,
                        update_timestamp=False,
                    )
                    if status_code == "FORCE_UPGRADE_STALE_SEASON":
                        log_builder_event(
                            "builder_force_upgrade_stale_season", media_type="TV Show", full_title=full_title,
                            season_number=season_number, filesize=season_poster_size, last_upgraded=context.get("last_upgraded"),
                            stale_days=stale_days)
                        season_poster_actions[season_number] = "upgraded"
                    elif status_code == "NO_EXISTING_ASSET_SEASON":
                        log_builder_event(
                            "builder_downloading_asset_season", media_type="TV Show", asset_type="poster",
                            full_title=full_title, season_number=season_number, filesize=season_poster_size
                        )
                        season_poster_actions[season_number] = "downloaded"
                    else:
                        log_builder_event(
                            "builder_asset_upgraded_season", media_type="TV Show", asset_type="poster",
                            full_title=full_title, season_number=season_number, status_code=status_code, context=context,
                            filesize=season_poster_size
                        )
                        season_poster_actions[season_number] = "upgraded" 
                    existing_assets.add(str(asset_path.resolve()))
                else:
                    season_poster_size = asset_path.stat().st_size if asset_path.exists() else 0
                    log_asset_status(
                        status_code, media_type="TV Show", asset_type="poster", full_title=full_title,
                        filesize=season_poster_size, error=context.get("error") if context else None, extra="", season_number=season_number
                    )
                    season_poster_actions[season_number] = "skipped"
                    if asset_path.exists():
                        existing_assets.add(str(asset_path.resolve()))
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
        result["season_posters"][season_number] = season_poster_size
    
    season_poster_tasks = []
    if feature_flags and feature_flags.get("season", True):
        for season_info in season_infos:
            season_number = season_info.get("season_number")
            if season_number is not None:
                season_poster_tasks.append(process_season_poster(season_info))

    await asyncio.gather(
        process_tv_poster(),
        process_tv_background(),
        *season_poster_tasks
    )
    if (
        feature_flags
        and feature_flags.get("season", True)
        and not feature_flags.get("dry_run", False)
        and "failed" not in season_poster_actions.values()
    ):
        await meta_cache_async(
            cache_key, tmdb_id, title, year, "tv",
            update_timestamp=False, season_checked=True,
        )

    return {
        "percent": grand_percent,
        "incomplete_percent": 100 - grand_percent,
        "is_complete": is_complete,
        "metadata_action": metadata_action,
        "poster_action": poster_action,
        "background_action": background_action,
        "seasons": seasons_data,
        "season_poster_actions": season_poster_actions,
        **result
    }
