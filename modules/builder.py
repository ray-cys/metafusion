import asyncio

from helper.asset_registry import AssetDestinationRegistry, normalize_destination
from helper.logging import log_builder_event, log_asset_status
from helper.cache import load_cache, meta_cache_async
from helper.config import get_image_upgrade_days
from helper.identity import cache_key_for_meta, match_for_meta, metadata_key_for_meta
from helper.io import atomic_replace_file, sha256_file
from helper.plex import get_plex_country
from helper.tmdb import (
    artwork_language_codes,
    resolve_episode_group_mapping,
    resolve_tmdb_id,
    tmdb_api_request,
    tmdb_identity_consistent,
    tmdb_unfiltered_images,
)
from modules.utils import (
    get_meta_field, recursive_season_diff, get_best_poster, get_best_season, get_best_background,
    smart_asset_upgrade, smart_season_asset_upgrade, asset_temp_path, download_poster, get_asset_path,
    asset_write_allowed,
)
from modules.kometa import (
    EPISODE_BASIC_FIELDS,
    build_episode_metadata,
    kometa_tag_key,
    merge_generated_metadata,
)


class AssetDestinationCollisionError(RuntimeError):
    pass


def _record_artwork_gap(
    config,
    category,
    media_type,
    full_title,
    asset_type="metadata",
    detail=None,
):
    gaps = config.get("_artwork_gaps")
    if gaps is None:
        return
    gaps.append(
        {
            "library": config.get("_library_name"),
            "category": category,
            "media_type": media_type,
            "title": full_title,
            "asset_type": asset_type,
            "detail": detail,
        }
    )


def _normalized_destination(path):
    return normalize_destination(path)


def _asset_registry(config):
    registry = config.get("_asset_destination_registry")
    if isinstance(registry, AssetDestinationRegistry):
        return registry
    converted = AssetDestinationRegistry()
    for destination, owner in (registry or {}).items():
        converted.add_persisted(
            {"cache_key": owner, "destination": destination}
        )
    config["_asset_destination_registry"] = converted
    return converted


def _media_asset_lock(config, meta, feature_flags):
    """Serialize artwork work by a destination resolved for the active mode."""
    feature_flags = feature_flags or {}
    if (
        feature_flags.get("dry_run", False)
        or not any(
            feature_flags.get(name, False)
            for name in ("poster", "background", "season")
        )
        or not meta
    ):
        return None
    destination = None
    if feature_flags.get("poster", False):
        destination = get_asset_path(config, meta, asset_type="poster")
    elif feature_flags.get("background", False):
        destination = get_asset_path(config, meta, asset_type="background")
    elif feature_flags.get("season", False):
        seasons = sorted(
            int(number) for number in (meta.get("seasons_episodes") or {})
        )
        if seasons:
            destination = get_asset_path(
                config, meta, asset_type="season", season_number=seasons[0]
            )
    if destination is None:
        return None
    return _asset_registry(config).lock_for(destination)


def _managed_shared_asset_checksum(
    config, cache_key, tmdb_id, source_path, asset_path, asset_type
):
    """Return a checksum from the job's indexed, verified shared ownership."""
    return _asset_registry(config).shared_checksum(
        cache_key,
        asset_path,
        media_type="movie",
        tmdb_id=tmdb_id,
        asset_type=asset_type,
        source_path=source_path,
    )


def regional_movie_certification(release_dates, region="US"):
    regions = [str(region or "US").upper()]
    if "US" not in regions:
        regions.append("US")
    for wanted in regions:
        for country in release_dates or []:
            if country.get("iso_3166_1") != wanted:
                continue
            releases = sorted(
                country.get("release_dates", []),
                key=lambda release: {
                    3: 0,  # Theatrical
                    4: 1,  # Digital
                    5: 2,  # Physical
                    6: 3,  # TV
                    2: 4,  # Limited theatrical
                    1: 5,  # Premiere
                }.get(release.get("type"), 99),
            )
            for release in releases:
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


def protected_asset_destination(
    config,
    cache_key,
    asset_path,
    asset_type,
    *,
    media_type,
    full_title,
    season_number=None,
    tmdb_id=None,
    source_path=None,
    shared_managed=False,
):
    registry = _asset_registry(config)
    normalized_media_type = str(media_type).lower()
    if normalized_media_type == "tv show":
        normalized_media_type = "tv"
    claim_status, owner = registry.claim(
        cache_key,
        asset_path,
        media_type=normalized_media_type,
        tmdb_id=tmdb_id,
        asset_type=asset_type,
        source_path=source_path,
        season_number=season_number,
    )
    if claim_status == "collision":
        log_builder_event(
            "builder_asset_destination_collision",
            media_type=media_type,
            asset_type=asset_type,
            full_title=full_title,
            destination=asset_path,
            owner=owner,
        )
        raise AssetDestinationCollisionError(
            f"Artwork destination {asset_path} is already claimed by {owner}"
        )
    if (
        claim_status in {"self", "shared"}
        and shared_managed
        and asset_path.exists()
    ):
        return False, "shared"
    if claim_status == "shared" and asset_path.exists():
        log_builder_event(
            "builder_preserving_existing_asset",
            media_type=media_type,
            asset_type=asset_type,
            full_title=full_title,
            destination=asset_path,
            reason="shared_unverified",
        )
        return False, "shared_unverified"
    allowed, reason = asset_write_allowed(
        config,
        cache_key,
        asset_path,
        asset_type,
        season_number=season_number,
    )
    if not allowed:
        log_builder_event(
            "builder_preserving_existing_asset",
            media_type=media_type,
            asset_type=asset_type,
            full_title=full_title,
            destination=asset_path,
            reason=reason,
        )
    elif reason == "managed" and cached_source_matches(
        cache_key,
        source_path,
        asset_path,
        asset_type,
        season_number=season_number,
    ):
        registry.mark_verified(
            cache_key,
            asset_path,
            media_type=normalized_media_type,
            tmdb_id=tmdb_id,
            asset_type=asset_type,
            source_path=source_path,
            season_number=season_number,
        )
    return allowed, reason


def _mark_asset_verified(
    config,
    cache_key,
    asset_path,
    *,
    media_type,
    tmdb_id,
    asset_type,
    source_path,
    season_number=None,
    checksum=None,
):
    return _asset_registry(config).mark_verified(
        cache_key,
        asset_path,
        media_type=media_type,
        tmdb_id=tmdb_id,
        asset_type=asset_type,
        source_path=source_path,
        season_number=season_number,
        checksum=checksum,
    )


def _tag_value(metadata, field):
    return metadata.get(field, metadata.get(f"{field}.sync", []))


def _crew_names(crew, jobs):
    """Return ordered, case-insensitively deduplicated names for exact jobs."""
    names = []
    seen = set()
    for member in crew or []:
        if member.get("job") not in jobs:
            continue
        name = str(member.get("name") or "").strip()
        normalized = name.casefold()
        if not name or normalized in seen:
            continue
        seen.add(normalized)
        names.append(name)
    return names


def movie_plex_candidate(metadata):
    return {
        "root": {
            "fields": {
                "originalTitle": metadata.get("original_title"),
                "originallyAvailableAt": metadata.get("originally_available"),
                "contentRating": metadata.get("content_rating"),
                "studio": metadata.get("studio"),
                "tagline": metadata.get("tagline"),
                "summary": metadata.get("summary"),
            },
            "tags": {
                "country": _tag_value(metadata, "country"),
                "genre": _tag_value(metadata, "genre"),
                "director": _tag_value(metadata, "director"),
                "writer": _tag_value(metadata, "writer"),
                "producer": _tag_value(metadata, "producer"),
            },
        }
    }


def tv_plex_candidate(metadata, seasons, countries=None):
    return {
        "root": {
            "fields": {
                "originalTitle": metadata.get("original_title"),
                "originallyAvailableAt": metadata.get("originally_available"),
                "contentRating": metadata.get("content_rating"),
                "studio": metadata.get("studio"),
                "tagline": metadata.get("tagline"),
                "summary": metadata.get("summary"),
            },
            "tags": {
                "country": countries or [],
                "genre": _tag_value(metadata, "genre"),
            },
        },
        "seasons": seasons,
    }


async def tmdb_details_with_recovery(
    config,
    media_type,
    tmdb_id,
    *,
    imdb_id=None,
    tvdb_id=None,
    title=None,
    year=None,
    params=None,
    session=None,
):
    """Fetch details and safely replace a stale Plex-supplied TMDb ID."""
    normalized_type = "tv" if str(media_type).lower() in {"tv", "show"} else "movie"

    async def fetch(candidate_id):
        return await tmdb_api_request(
            config,
            f"{normalized_type}/{candidate_id}",
            params=params,
            session=session,
        )

    details = await fetch(tmdb_id)
    if details:
        return str(tmdb_id), details, None

    replacement_id = await resolve_tmdb_id(
        config,
        normalized_type,
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        title=title,
        year=year,
        session=session,
        excluded_ids={tmdb_id},
    )
    if not replacement_id:
        return str(tmdb_id), None, None
    replacement_details = await fetch(replacement_id)
    if not replacement_details:
        return str(tmdb_id), None, None
    return str(replacement_id), replacement_details, str(tmdb_id)

async def build_movie(
    config, consolidated_metadata, feature_flags=None, existing_yaml_data=None, session=None, ignored_fields=None,
    existing_assets=None, meta=None,
):
    lock = _media_asset_lock(config, meta, feature_flags)
    if lock is None:
        return await _build_movie(
            config,
            consolidated_metadata,
            feature_flags=feature_flags,
            existing_yaml_data=existing_yaml_data,
            session=session,
            ignored_fields=ignored_fields,
            existing_assets=existing_assets,
            meta=meta,
        )
    async with lock:
        return await _build_movie(
            config,
            consolidated_metadata,
            feature_flags=feature_flags,
            existing_yaml_data=existing_yaml_data,
            session=session,
            ignored_fields=ignored_fields,
            existing_assets=existing_assets,
            meta=meta,
        )


async def _build_movie(
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
    movie_path = meta.get("movie_path") if meta else None
    tmdb_id = meta.get("tmdb_id") if meta else None
    imdb_id = meta.get("imdb_id") if meta else None
    tmdb_id = await resolve_tmdb_id(
        config,
        "movie",
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        title=title,
        year=year,
        session=session,
    )
    if meta is not None and tmdb_id:
        meta["tmdb_id"] = tmdb_id
    if not tmdb_id:
        log_builder_event(
            "builder_no_tmdb_id", media_type="Movie", full_title=full_title
        )
        _record_artwork_gap(
            config, "tmdb_missing", "Movie", full_title,
            detail="No TMDb identity could be resolved",
        )
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": "failed" if run_metadata else metadata_action,
            "poster_action": "failed" if run_poster else poster_action,
            "background_action": "failed" if run_background else background_action,
            **result,
        }
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

    tmdb_id, details, recovered_from_tmdb_id = await tmdb_details_with_recovery(
        config,
        "movie",
        tmdb_id,
        imdb_id=imdb_id,
        title=title,
        year=year,
        params={
            "append_to_response": "credits,release_dates,external_ids,images",
            "language": config.get("tmdb", {}).get("language", "en-US"),
            "region": config.get("tmdb", {}).get("region", "US"),
            "include_image_language": artwork_language_codes(config),
        },
        session=session,
    )
    if not details:
        log_builder_event("builder_invalid_tmdb_id", media_type="Movie", full_title=full_title)
        _record_artwork_gap(
            config, "tmdb_failure", "Movie", full_title,
            detail="TMDb details were unavailable",
        )
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
    if recovered_from_tmdb_id:
        recovery_source_id = (
            meta.get("plex_tmdb_id") or recovered_from_tmdb_id
            if meta is not None
            else recovered_from_tmdb_id
        )
        if meta is not None:
            meta["plex_tmdb_id"] = str(recovery_source_id)
            meta["tmdb_id"] = tmdb_id
        mapping_id = int(tmdb_id)
        log_builder_event(
            "builder_tmdb_id_recovered",
            media_type="Movie",
            full_title=full_title,
            old_id=recovered_from_tmdb_id,
            new_id=tmdb_id,
        )
    identity_ok, identity_reason = tmdb_identity_consistent(
        "movie", title, year, details
    )
    if not identity_ok:
        log_builder_event(
            "builder_tmdb_identity_mismatch",
            media_type="Movie",
            full_title=full_title,
            reason=identity_reason,
        )
        _record_artwork_gap(
            config, "identity_rejected", "Movie", full_title,
            detail=identity_reason,
        )
        return {
            "percent": 0,
            "incomplete_percent": 100,
            "is_complete": False,
            "metadata_action": "failed" if run_metadata else metadata_action,
            "poster_action": "failed" if run_poster else poster_action,
            "background_action": "failed" if run_background else background_action,
            **result,
        }
    if identity_reason != "matched":
        log_builder_event(
            "builder_tmdb_identity_warning",
            media_type="Movie",
            full_title=full_title,
            reason=identity_reason,
        )
    if recovered_from_tmdb_id and not feature_flags.get("dry_run", False):
        await meta_cache_async(
            cache_key,
            tmdb_id,
            title,
            year,
            "movie",
            update_timestamp=False,
            tmdb_recovery_source_id=recovery_source_id,
        )

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
    director_jobs = {"Director", "Co-Director"}
    writer_jobs = {"Writer", "Screenplay", "Story", "Creator", "Co-Writer", "Author", "Adaptation"}
    producer_jobs = {"Producer", "Executive Producer", "Associate Producer", "Co-Producer", "Line Producer", "Co-Executive Producer"}
    
    credits = get_meta_field(details, "credits", {})
    crew = get_meta_field(credits, "crew", [])
    directors = _crew_names(crew, director_jobs)
    writers = _crew_names(crew, writer_jobs)
    producers = _crew_names(crew, producer_jobs)

    tag_policy = config.get("kometa", {}).get("tag_policy", "append")
    country_key = kometa_tag_key("country", tag_policy)
    genre_key = kometa_tag_key("genre", tag_policy)
    director_key = kometa_tag_key("director", tag_policy)
    writer_key = kometa_tag_key("writer", tag_policy)
    producer_key = kometa_tag_key("producer", tag_policy)

    basic_fields = [
        "sort_title", "original_title", "originally_available", "content_rating",
        "studio", "tagline", "summary", country_key, genre_key,
    ]
    enhanced_fields = [director_key, writer_key, producer_key]
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
        elif k == "tagline":
            new_metadata[k] = get_meta_field(details, "tagline", "") or ""
        elif k == "summary":
            new_metadata[k] = get_meta_field(details, "overview", "") or ""
        elif k == country_key:
            new_metadata[k] = countries if countries else []
        elif k == genre_key:
            new_metadata[k] = genres if genres else []
        elif k == director_key:
            new_metadata[k] = directors if directors else []
        elif k == writer_key:
            new_metadata[k] = writers if writers else []
        elif k == producer_key:
            new_metadata[k] = producers if producers else []
        else:
            new_metadata[k] = "" 
    plex_candidate = movie_plex_candidate(new_metadata)

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
        existing_metadata = (existing_yaml_data or {}).get("metadata", {}).get(
            full_title, {}
        )
        generated_entry = {
            "match": match_for_meta(meta, mapping_id),
            **new_metadata,
        }
        merged_entry, diagnostics = merge_generated_metadata(
            existing_metadata, generated_entry, "movie"
        )
        changes = recursive_season_diff(existing_metadata, merged_entry)
        if changes:
            consolidated_metadata["metadata"][full_title] = merged_entry
            metadata_changed = True
            if existing_metadata:
                log_builder_event(
                    "build_metadata_changed", media_type="Movie", full_title=full_title,
                    percent=percent, tmdb_id=tmdb_id, changes=changes
                )
                metadata_action = "upgraded"
            else:
                log_builder_event(
                    "builder_no_existing_metadata", media_type="Movie",
                    full_title=full_title, tmdb_id=tmdb_id
                )
                metadata_action = "downloaded"
        else:
            log_builder_event(
                "builder_no_metadata_changes", media_type="Movie", full_title=full_title,
                percent=percent, incomplete_percent=100 - percent
            )
            metadata_action = "skipped"
        log_builder_event(
            "builder_metadata_diagnostics",
            media_type="Movie",
            full_title=full_title,
            diagnostics=diagnostics,
        )

        if feature_flags.get("dry_run", False):
            log_builder_event("builder_dry_run_metadata", media_type="Movie", full_title=full_title)

        if not feature_flags.get("dry_run", False):
            if metadata_changed:
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                )
                log_builder_event("builder_metadata_cached", media_type="Movie", full_title=full_title, cache_key=cache_key)
            else:
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    update_timestamp=False
                )

    unfiltered_images_task = None

    async def all_language_images():
        nonlocal unfiltered_images_task
        if not config.get("tmdb", {}).get("artwork_allow_any_language", True):
            return {}
        if unfiltered_images_task is None:
            unfiltered_images_task = asyncio.create_task(
                tmdb_unfiltered_images(
                    config, "movie", tmdb_id, session=session
                )
            )
        return await unfiltered_images_task or {}

    async def process_poster():
        poster_size = 0
        nonlocal poster_action
        if not feature_flags or not feature_flags.get("poster", True):
            result["poster"]["size"] = poster_size
            poster_action = "not_due"
            return
        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_poster(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            unfiltered = await all_language_images()
            best = get_best_poster(
                config,
                unfiltered.get("posters", []),
                preferred_language=preferred_language,
                fallback=fallback,
            )
            if best:
                log_builder_event(
                    "builder_artwork_language_fallback", media_type="Movie",
                    asset_type="poster", full_title=full_title,
                    language=best.get("iso_639_1") or "untagged",
                )
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="Movie", asset_type="poster", full_title=full_title, extra="")
            _record_artwork_gap(
                config, "artwork_missing", "Movie", full_title, "poster"
            )
            if not feature_flags.get("dry_run", False):
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    update_timestamp=False, poster_checked=True,
                )
            result["poster"]["size"] = poster_size
            poster_action = "missing"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_asset_selected", media_type="Movie",
                asset_type="poster", full_title=full_title,
                source_path=best.get("file_path"),
            )
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return

        if not movie_path:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="poster", extra="")
            _record_artwork_gap(
                config, "path_invalid", "Movie", full_title, "poster",
                "Movie output directory was not resolved",
            )
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        asset_path = get_asset_path(config, meta, asset_type="poster")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="poster", extra="")
            _record_artwork_gap(
                config, "path_invalid", "Movie", full_title, "poster",
                "Poster destination is unavailable or not writable",
            )
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        source_path = best.get("file_path")
        shared_checksum = _managed_shared_asset_checksum(
            config, cache_key, tmdb_id, source_path, asset_path, "poster"
        )
        if shared_checksum:
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, poster_checked=True,
                poster_average=best.get("vote_average", 0),
                poster_source_path=source_path,
                poster_path=str(asset_path.resolve()),
                poster_checksum=shared_checksum,
            )
        allowed, protection_status = protected_asset_destination(
            config, cache_key, asset_path, "poster",
            media_type="Movie", full_title=full_title,
            tmdb_id=tmdb_id,
            source_path=source_path,
            shared_managed=bool(shared_checksum),
        )
        if not allowed:
            poster_size = asset_path.stat().st_size if asset_path.exists() else 0
            if asset_path.exists():
                existing_assets.add(str(asset_path.resolve()))
            if protection_status == "shared":
                log_builder_event(
                    "builder_reusing_shared_asset",
                    media_type="Movie",
                    asset_type="poster",
                    full_title=full_title,
                    destination=asset_path,
                )
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
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
                _record_artwork_gap(
                    config, "tmdb_failure", "Movie", full_title, "poster",
                    "Artwork download failed",
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
                    asset_checksum = await asyncio.to_thread(sha256_file, asset_path)
                    _mark_asset_verified(
                        config, cache_key, asset_path,
                        media_type="movie", tmdb_id=tmdb_id,
                        asset_type="poster", source_path=best.get("file_path"),
                        checksum=asset_checksum,
                    )
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "movie",
                        poster_average=best.get("vote_average", 0),
                        poster_path=str(asset_path.resolve()),
                        poster_checksum=asset_checksum,
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
        images = get_meta_field(details, "backdrops", [], path=["images"])
        best = get_best_background(config, images)
        if not best:
            unfiltered = await all_language_images()
            best = get_best_background(config, unfiltered.get("backdrops", []))
            if best:
                log_builder_event(
                    "builder_artwork_language_fallback", media_type="Movie",
                    asset_type="background", full_title=full_title,
                    language=best.get("iso_639_1") or "untagged",
                )
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="Movie", asset_type="background", full_title=full_title, extra="")
            _record_artwork_gap(
                config, "artwork_missing", "Movie", full_title, "background"
            )
            if not feature_flags.get("dry_run", False):
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "movie",
                    update_timestamp=False, background_checked=True,
                )
            result["background"]["size"] = background_size
            background_action = "missing"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_asset_selected", media_type="Movie",
                asset_type="background", full_title=full_title,
                source_path=best.get("file_path"),
            )
            result["background"]["size"] = background_size
            background_action = "skipped"
            return

        if not movie_path:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="background", extra="")
            _record_artwork_gap(
                config, "path_invalid", "Movie", full_title, "background",
                "Movie output directory was not resolved",
            )
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        asset_path = get_asset_path(config, meta, asset_type="background")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="Movie", full_title=full_title, asset_type="background", extra="")
            _record_artwork_gap(
                config, "path_invalid", "Movie", full_title, "background",
                "Background destination is unavailable or not writable",
            )
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        source_path = best.get("file_path")
        shared_checksum = _managed_shared_asset_checksum(
            config, cache_key, tmdb_id, source_path, asset_path, "background"
        )
        if shared_checksum:
            await meta_cache_async(
                cache_key, tmdb_id, title, year, "movie",
                update_timestamp=False, background_checked=True,
                bg_average=best.get("vote_average", 0),
                background_source_path=source_path,
                background_path=str(asset_path.resolve()),
                background_checksum=shared_checksum,
            )
        allowed, protection_status = protected_asset_destination(
            config, cache_key, asset_path, "background",
            media_type="Movie", full_title=full_title,
            tmdb_id=tmdb_id,
            source_path=source_path,
            shared_managed=bool(shared_checksum),
        )
        if not allowed:
            background_size = asset_path.stat().st_size if asset_path.exists() else 0
            if asset_path.exists():
                existing_assets.add(str(asset_path.resolve()))
            if protection_status == "shared":
                log_builder_event(
                    "builder_reusing_shared_asset",
                    media_type="Movie",
                    asset_type="background",
                    full_title=full_title,
                    destination=asset_path,
                )
            result["background"]["size"] = background_size
            background_action = "skipped"
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
                _record_artwork_gap(
                    config, "tmdb_failure", "Movie", full_title, "background",
                    "Artwork download failed",
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
                    asset_checksum = await asyncio.to_thread(sha256_file, asset_path)
                    _mark_asset_verified(
                        config, cache_key, asset_path,
                        media_type="movie", tmdb_id=tmdb_id,
                        asset_type="background", source_path=best.get("file_path"),
                        checksum=asset_checksum,
                    )
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "movie",
                        bg_average=best.get("vote_average", 0),
                        background_path=str(asset_path.resolve()),
                        background_checksum=asset_checksum,
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
        "plex_candidate": plex_candidate if run_metadata else None,
        **result
    }

async def build_tv(
    config, consolidated_metadata, feature_flags=None, existing_yaml_data=None,
    session=None, ignored_fields=None, existing_assets=None, meta=None,
):
    lock = _media_asset_lock(config, meta, feature_flags)
    if lock is None:
        return await _build_tv(
            config,
            consolidated_metadata,
            feature_flags=feature_flags,
            existing_yaml_data=existing_yaml_data,
            session=session,
            ignored_fields=ignored_fields,
            existing_assets=existing_assets,
            meta=meta,
        )
    async with lock:
        return await _build_tv(
            config,
            consolidated_metadata,
            feature_flags=feature_flags,
            existing_yaml_data=existing_yaml_data,
            session=session,
            ignored_fields=ignored_fields,
            existing_assets=existing_assets,
            meta=meta,
        )


async def _build_tv(
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
        title=title,
        year=year,
        session=session,
    )
    if meta is not None and tmdb_id:
        meta["tmdb_id"] = tmdb_id
    if not tmdb_id:
        log_builder_event(
            "builder_no_tmdb_id", media_type="TV Show", full_title=full_title
        )
        _record_artwork_gap(
            config, "tmdb_missing", "TV Show", full_title,
            detail="No TMDb identity could be resolved",
        )
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
    tmdb_id, details, recovered_from_tmdb_id = await tmdb_details_with_recovery(
        config,
        "tv",
        tmdb_id,
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        title=title,
        year=year,
        params={
            "append_to_response": "credits,keywords,content_ratings,external_ids,images",
            "language": config.get("tmdb", {}).get("language", "en-US"),
            "region": config.get("tmdb", {}).get("region", "US"),
            "include_image_language": artwork_language_codes(config),
        },
        session=session,
    )
    if not details:
        log_builder_event("builder_invalid_tmdb_id", media_type="TV Show", full_title=full_title)
        _record_artwork_gap(
            config, "tmdb_failure", "TV Show", full_title,
            detail="TMDb details were unavailable",
        )
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
    recovery_source_id = None
    if recovered_from_tmdb_id:
        recovery_source_id = (
            meta.get("plex_tmdb_id") or recovered_from_tmdb_id
            if meta is not None
            else recovered_from_tmdb_id
        )
        if meta is not None:
            meta["plex_tmdb_id"] = str(recovery_source_id)
            meta["tmdb_id"] = tmdb_id
        log_builder_event(
            "builder_tmdb_id_recovered",
            media_type="TV Show",
            full_title=full_title,
            old_id=recovered_from_tmdb_id,
            new_id=tmdb_id,
        )
    identity_ok, identity_reason = tmdb_identity_consistent(
        "tv", title, year, details
    )
    if not identity_ok:
        log_builder_event(
            "builder_tmdb_identity_mismatch",
            media_type="TV Show",
            full_title=full_title,
            reason=identity_reason,
        )
        _record_artwork_gap(
            config, "identity_rejected", "TV Show", full_title,
            detail=identity_reason,
        )
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
    if identity_reason != "matched":
        log_builder_event(
            "builder_tmdb_identity_warning",
            media_type="TV Show",
            full_title=full_title,
            reason=identity_reason,
        )

    if not feature_flags.get("dry_run", False):
        cache_fields = {}
        if recovery_source_id is not None:
            cache_fields["tmdb_recovery_source_id"] = recovery_source_id
        await meta_cache_async(
            cache_key,
            tmdb_id,
            title,
            year,
            "tv",
            update_timestamp=False,
            **cache_fields,
        )

    mapping_id = None
    if tvdb_id:
        mapping_id = int(tvdb_id)
    elif imdb_id:
        mapping_id = imdb_id
    else:
        external_ids = details.get("external_ids") or await tmdb_api_request(
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

    content_ratings = get_meta_field(details, "results", [], path=["content_ratings"])
    content_rating = regional_tv_certification(
        content_ratings,
        config.get("tmdb", {}).get("region", "US"),
    )
    
    genres = [g.get("name", "") for g in get_meta_field(details, "genres", [])]
    studios = [n.get("name", "") for n in get_meta_field(details, "networks", []) if n.get("name")]
    if not studios:
        studios = [
            company.get("name", "")
            for company in get_meta_field(details, "production_companies", [])
            if company.get("name")
        ]
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
    plex_seasons = {}
    grand_percent = 100
    is_complete = True
    metadata_fetch_failed = False
    if run_metadata:
        inventory = {
            int(season): {int(episode) for episode in episodes}
            for season, episodes in (seasons_episodes or {}).items()
        }
        tag_policy = config.get("kometa", {}).get("tag_policy", "append")
        genre_key = kometa_tag_key("genre", tag_policy)
        director_key = kometa_tag_key("director", tag_policy)
        writer_key = kometa_tag_key("writer", tag_policy)
        show_basic_fields = [
            "sort_title", "original_title", "originally_available", "content_rating",
            "studio", "tagline", "summary", genre_key, "seasons",
        ]
        show_fields_to_write = list(show_basic_fields)
        episode_fields_to_write = list(EPISODE_BASIC_FIELDS)
        if feature_flags.get("metadata_enhanced", True):
            episode_fields_to_write.extend((director_key, writer_key))

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
                genre_key: genres if genres else [],
            }
            new_metadata[key] = values.get(key, "")

        director_jobs = {"Director", "Co-Director"}
        writer_jobs = {
            "Writer", "Screenplay", "Story", "Creator", "Co-Writer",
            "Author", "Adaptation", "Novel",
        }

        def episode_metadata(episode):
            crew = get_meta_field(episode, "crew", []) or []
            generated = build_episode_metadata(
                episode,
                directors=_crew_names(crew, director_jobs),
                writers=_crew_names(crew, writer_jobs),
                enhanced=feature_flags.get("metadata_enhanced", True),
            )
            if feature_flags.get("metadata_enhanced", True):
                generated[director_key] = generated.pop("director", [])
                generated[writer_key] = generated.pop("writer", [])
            return generated

        def plex_season_candidate(season_metadata):
            return {
                "fields": {
                    "title": season_metadata.get("title"),
                    "summary": season_metadata.get("summary"),
                },
                "episodes": {
                    episode_number: {
                        "fields": {
                            "title": episode.get("title"),
                            "summary": episode.get("summary"),
                            "originallyAvailableAt": episode.get(
                                "originally_available"
                            ),
                        },
                        "tags": {
                            field: values
                            for field in ("director", "writer")
                            if (values := _tag_value(episode, field))
                        },
                    }
                    for episode_number, episode in season_metadata.get(
                        "episodes", {}
                    ).items()
                },
            }

        async def process_season(season_info):
            season_number = season_info.get("season_number")
            if (
                season_number is None
                or not inventory
                or season_number not in inventory
            ):
                return season_number, None, None, False
            season_details = await get_season_details(season_number)
            if not season_details:
                log_builder_event(
                    "builder_no_tmdb_season_data", media_type="TV Shows",
                    season_number=season_number, full_title=full_title
                )
                _record_artwork_gap(
                    config, "tmdb_failure", "TV Show", full_title,
                    f"season {season_number}", "TMDb season details were unavailable",
                )
                return season_number, None, None, True

            episodes = {}
            for episode in get_meta_field(season_details, "episodes", []):
                episode_number = int(episode.get("episode_number"))
                if episode_number not in inventory[season_number]:
                    continue
                episodes[episode_number] = episode_metadata(episode)
            season_metadata = {
                "title": get_meta_field(season_details, "name", "") or "",
                "summary": get_meta_field(season_details, "overview", "") or "",
                "episodes": episodes,
            }
            return (
                season_number,
                season_metadata,
                plex_season_candidate(season_metadata),
                False,
            )

        results = await asyncio.gather(*(process_season(info) for info in season_infos))
        failed_seasons = set()
        for season_number, season_data, plex_season, failed in results:
            if failed:
                failed_seasons.add(int(season_number))
            if season_data:
                seasons_data[season_number] = season_data
            if plex_season:
                plex_seasons[season_number] = plex_season

        wanted_pairs = {
            (season, episode)
            for season, episodes in inventory.items()
            for episode in episodes
        }
        generated_pairs = {
            (int(season), int(episode))
            for season, season_data in seasons_data.items()
            for episode in season_data.get("episodes", {})
        }
        missing_pairs = wanted_pairs - generated_pairs
        if missing_pairs:
            group_mapping = await resolve_episode_group_mapping(
                config,
                tmdb_id,
                inventory,
                episode_ordering=meta.get("episode_ordering"),
                session=session,
            )
            if group_mapping:
                seasons_data = {}
                plex_seasons = {}
                for (season_number, episode_number), episode in sorted(
                    group_mapping["episodes"].items()
                ):
                    context = group_mapping["seasons"].get(season_number, {})
                    season_metadata = seasons_data.setdefault(
                        season_number,
                        {
                            "title": context.get("title") or "",
                            "summary": context.get("summary") or "",
                            "episodes": {},
                        },
                    )
                    season_metadata["episodes"][episode_number] = episode_metadata(
                        episode
                    )
                plex_seasons = {
                    season_number: plex_season_candidate(season_metadata)
                    for season_number, season_metadata in seasons_data.items()
                }
                failed_seasons.clear()
                missing_pairs.clear()
                log_builder_event(
                    "builder_episode_group_fallback", media_type="TV Show",
                    full_title=full_title,
                    group_id=group_mapping["group_id"],
                )
            else:
                metadata_fetch_failed = True
                log_builder_event(
                    "builder_episode_order_unresolved", media_type="TV Show",
                    full_title=full_title, count=len(missing_pairs),
                )
        if failed_seasons:
            metadata_fetch_failed = True

        episode_filled = 0
        episode_total = 0
        for season in seasons_data.values():
            for episode in season.get("episodes", {}).values():
                for field in episode_fields_to_write:
                    episode_total += 1
                    if episode.get(field) not in (None, "", []):
                        episode_filled += 1

        generated_entry = {
            "match": match_for_meta(meta, mapping_id),
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
        existing_metadata = (existing_yaml_data or {}).get("metadata", {}).get(
            full_title, {}
        )
        merged_entry, diagnostics = merge_generated_metadata(
            existing_metadata,
            generated_entry,
            "show",
            authoritative_seasons=inventory,
            authoritative_episodes=inventory,
        )
        changes = recursive_season_diff(existing_metadata, merged_entry)
        if changes:
            consolidated_metadata["metadata"][full_title] = merged_entry
            metadata_changed = True
            if existing_metadata:
                metadata_action = "upgraded"
                log_builder_event(
                    "build_metadata_changed", media_type="TV Show",
                    full_title=full_title, percent=grand_percent,
                    tmdb_id=tmdb_id, changes=changes,
                )
            else:
                log_builder_event(
                    "builder_no_existing_metadata", media_type="TV Show",
                    full_title=full_title, tmdb_id=tmdb_id,
                )
                metadata_action = "downloaded"
        else:
            metadata_action = "skipped"
            log_builder_event(
                "builder_no_metadata_changes", media_type="TV Show",
                full_title=full_title, percent=grand_percent,
                incomplete_percent=100 - grand_percent,
            )
        diagnostics["fetch_failed"] = len(failed_seasons) + len(missing_pairs)
        log_builder_event(
            "builder_metadata_diagnostics", media_type="TV Show",
            full_title=full_title, diagnostics=diagnostics,
        )
        if metadata_fetch_failed:
            metadata_action = "failed"
            is_complete = False

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

    unfiltered_images_task = None
    season_unfiltered_tasks = {}

    async def all_language_images(season_number=None):
        nonlocal unfiltered_images_task
        if not config.get("tmdb", {}).get("artwork_allow_any_language", True):
            return {}
        if season_number is None:
            if unfiltered_images_task is None:
                unfiltered_images_task = asyncio.create_task(
                    tmdb_unfiltered_images(config, "tv", tmdb_id, session=session)
                )
            return await unfiltered_images_task or {}
        if season_number not in season_unfiltered_tasks:
            season_unfiltered_tasks[season_number] = asyncio.create_task(
                tmdb_unfiltered_images(
                    config,
                    "tv",
                    tmdb_id,
                    season_number=season_number,
                    session=session,
                )
            )
        return await season_unfiltered_tasks[season_number] or {}

    async def process_tv_poster():
        poster_size = 0
        nonlocal poster_action
        if not feature_flags or not feature_flags.get("poster", True):
            result["poster"]["size"] = poster_size
            poster_action = "not_due"
            return
        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_poster(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            unfiltered = await all_language_images()
            best = get_best_poster(
                config,
                unfiltered.get("posters", []),
                preferred_language=preferred_language,
                fallback=fallback,
            )
            if best:
                log_builder_event(
                    "builder_artwork_language_fallback", media_type="TV Show",
                    asset_type="poster", full_title=full_title,
                    language=best.get("iso_639_1") or "untagged",
                )
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="TV Show", asset_type="poster", full_title=full_title, extra="")
            _record_artwork_gap(
                config, "artwork_missing", "TV Show", full_title, "poster"
            )
            if not feature_flags.get("dry_run", False):
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "tv",
                    update_timestamp=False, poster_checked=True,
                )
            result["poster"]["size"] = poster_size
            poster_action = "missing"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_asset_selected", media_type="TV Show",
                asset_type="poster", full_title=full_title,
                source_path=best.get("file_path"),
            )
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
            return

        if not show_path:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="poster", extra="")
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title, "poster",
                "Show output directory was not resolved",
            )
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        asset_path = get_asset_path(config, meta, asset_type="poster")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="poster", extra="")
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title, "poster",
                "Poster destination is unavailable or not writable",
            )
            result["poster"]["size"] = poster_size
            poster_action = "failed"
            return

        allowed, _protection_status = protected_asset_destination(
            config, cache_key, asset_path, "poster",
            media_type="TV Show", full_title=full_title,
            tmdb_id=tmdb_id,
            source_path=best.get("file_path"),
        )
        if not allowed:
            poster_size = asset_path.stat().st_size if asset_path.exists() else 0
            if asset_path.exists():
                existing_assets.add(str(asset_path.resolve()))
            result["poster"]["size"] = poster_size
            poster_action = "skipped"
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
                _record_artwork_gap(
                    config, "tmdb_failure", "TV Show", full_title, "poster",
                    "Artwork download failed",
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
                    asset_checksum = await asyncio.to_thread(sha256_file, asset_path)
                    _mark_asset_verified(
                        config, cache_key, asset_path,
                        media_type="tv", tmdb_id=tmdb_id,
                        asset_type="poster", source_path=best.get("file_path"),
                        checksum=asset_checksum,
                    )
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        poster_average=best.get("vote_average", 0),
                        poster_path=str(asset_path.resolve()),
                        poster_checksum=asset_checksum,
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
        images = get_meta_field(details, "backdrops", [], path=["images"])
        best = get_best_background(config, images)
        if not best:
            unfiltered = await all_language_images()
            best = get_best_background(config, unfiltered.get("backdrops", []))
            if best:
                log_builder_event(
                    "builder_artwork_language_fallback", media_type="TV Show",
                    asset_type="background", full_title=full_title,
                    language=best.get("iso_639_1") or "untagged",
                )
        if not best:
            log_builder_event("builder_no_suitable_asset", media_type="TV Show", asset_type="background", full_title=full_title, extra="")
            _record_artwork_gap(
                config, "artwork_missing", "TV Show", full_title, "background"
            )
            if not feature_flags.get("dry_run", False):
                await meta_cache_async(
                    cache_key, tmdb_id, title, year, "tv",
                    update_timestamp=False, background_checked=True,
                )
            result["background"]["size"] = background_size
            background_action = "missing"
            return


        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_asset_selected", media_type="TV Show",
                asset_type="background", full_title=full_title,
                source_path=best.get("file_path"),
            )
            result["background"]["size"] = background_size
            background_action = "skipped"
            return

        if not show_path:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="background", extra="")
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title, "background",
                "Show output directory was not resolved",
            )
            result["background"]["size"] = background_size
            background_action = "failed"
            return
    
        asset_path = get_asset_path(config, meta, asset_type="background")
        if asset_path is None:
            log_builder_event("builder_no_asset_path", media_type="TV Show", full_title=full_title, asset_type="background", extra="")
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title, "background",
                "Background destination is unavailable or not writable",
            )
            result["background"]["size"] = background_size
            background_action = "failed"
            return

        allowed, _protection_status = protected_asset_destination(
            config, cache_key, asset_path, "background",
            media_type="TV Show", full_title=full_title,
            tmdb_id=tmdb_id,
            source_path=best.get("file_path"),
        )
        if not allowed:
            background_size = asset_path.stat().st_size if asset_path.exists() else 0
            if asset_path.exists():
                existing_assets.add(str(asset_path.resolve()))
            result["background"]["size"] = background_size
            background_action = "skipped"
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
                _record_artwork_gap(
                    config, "tmdb_failure", "TV Show", full_title, "background",
                    "Artwork download failed",
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
                    asset_checksum = await asyncio.to_thread(sha256_file, asset_path)
                    _mark_asset_verified(
                        config, cache_key, asset_path,
                        media_type="tv", tmdb_id=tmdb_id,
                        asset_type="background", source_path=best.get("file_path"),
                        checksum=asset_checksum,
                    )
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        bg_average=best.get("vote_average", 0),
                        background_path=str(asset_path.resolve()),
                        background_checksum=asset_checksum,
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
        
        season_details = await get_season_details(season_number)
        if not season_details:
            log_builder_event("builder_no_season_details", media_type="TV Show", full_title=full_title, season_number=season_number)
            _record_artwork_gap(
                config, "tmdb_failure", "TV Show", full_title,
                f"season {season_number} poster",
                "TMDb season details were unavailable",
            )
            season_poster_actions[season_number] = "failed"
            return

        preferred_language = config["tmdb"].get("language", "en").split("-")[0]
        images = get_meta_field(season_details, "posters", [], path=["images"])
        fallback = config["tmdb"].get("fallback", [])
        best = get_best_season(config, images, preferred_language=preferred_language, fallback=fallback)
        if not best:
            unfiltered = await all_language_images(season_number=season_number)
            best = get_best_season(
                config,
                unfiltered.get("posters", []),
                preferred_language=preferred_language,
                fallback=fallback,
            )
            if best:
                log_builder_event(
                    "builder_artwork_language_fallback", media_type="TV Show",
                    asset_type="season poster", full_title=full_title,
                    language=best.get("iso_639_1") or "untagged",
                )
        if not best:
            log_builder_event(
                "builder_no_suitable_asset_season", media_type="TV Show", asset_type="poster",
                full_title=full_title, season_number=season_number
            )
            _record_artwork_gap(
                config, "artwork_missing", "TV Show", full_title,
                f"season {season_number} poster",
            )
            season_poster_actions[season_number] = "missing"
            return

        if feature_flags.get("dry_run", False):
            log_builder_event(
                "builder_dry_run_asset_selected", media_type="TV Show",
                asset_type=f"season {season_number} poster", full_title=full_title,
                source_path=best.get("file_path"),
            )
            result["season_posters"][season_number] = season_poster_size
            season_poster_actions[season_number] = "skipped"
            return

        if not show_path:
            log_builder_event("builder_no_asset_path_season", media_type="TV Show", full_title=full_title, season_number=season_number)
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title,
                f"season {season_number} poster", "Show output directory was not resolved",
            )
            season_poster_actions[season_number] = "failed"
            return

        asset_path = get_asset_path(config, meta, asset_type="season", season_number=season_number)
        if asset_path is None:
            log_builder_event("builder_no_asset_path_season", media_type="TV Show", full_title=full_title, season_number=season_number)
            _record_artwork_gap(
                config, "path_invalid", "TV Show", full_title,
                f"season {season_number} poster",
                "Season destination is unavailable or not writable",
            )
            season_poster_actions[season_number] = "failed"
            return

        allowed, _protection_status = protected_asset_destination(
            config, cache_key, asset_path, "season",
            media_type="TV Show", full_title=full_title, season_number=season_number,
            tmdb_id=tmdb_id,
            source_path=best.get("file_path"),
        )
        if not allowed:
            season_poster_size = asset_path.stat().st_size if asset_path.exists() else 0
            if asset_path.exists():
                existing_assets.add(str(asset_path.resolve()))
            result["season_posters"][season_number] = season_poster_size
            season_poster_actions[season_number] = "skipped"
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
                _record_artwork_gap(
                    config, "tmdb_failure", "TV Show", full_title,
                    f"season {season_number} poster", "Artwork download failed",
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
                    asset_checksum = await asyncio.to_thread(sha256_file, asset_path)
                    _mark_asset_verified(
                        config, cache_key, asset_path,
                        media_type="tv", tmdb_id=tmdb_id,
                        asset_type="season", source_path=best.get("file_path"),
                        season_number=season_number, checksum=asset_checksum,
                    )
                    await meta_cache_async(
                        cache_key, tmdb_id, title, year, "tv",
                        season_number=season_number,
                        season_average=best.get("vote_average", 0),
                        season_path=str(asset_path.resolve()),
                        season_checksum=asset_checksum,
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
        "plex_candidate": (
            tv_plex_candidate(new_metadata, plex_seasons, countries=countries)
            if run_metadata else None
        ),
        **result
    }
