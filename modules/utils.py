import asyncio, hashlib, uuid, re, datetime, os
from io import BytesIO
from pathlib import Path
from helper.config import mode_check
from helper.cache import load_cache
from helper.io import atomic_write_bytes, sha256_file
from helper.tmdb import tmdb_api_request

def smart_meta_update(existing_metadata, new_metadata, exclude_fields=None):
    if exclude_fields is None:
        exclude_fields = {"last_updated", "cache_key", "poster_average", "season_average", "background_average"}
    changed_fields = []
    for key, new_value in new_metadata.items():
        if key in exclude_fields:
            continue
        existing_value = existing_metadata.get(key)
        if isinstance(new_value, list):
            def normalize_list(lst):
                return sorted([
                    str(item).strip()
                    for item in lst if item not in (None, "", [])
                ])
            normalized_existing = normalize_list(existing_value if isinstance(existing_value, list) else [])
            normalized_new = normalize_list(new_value)
            if normalized_existing != normalized_new:
                changed_fields.append(key)
        elif isinstance(new_value, dict):
            if not isinstance(existing_value, dict):
                changed_fields.append(key)
            else:
                nested_changes = smart_meta_update(
                    existing_value,
                    new_value,
                    exclude_fields=exclude_fields,
                )
                if nested_changes:
                    changed_fields.append(key)
        else:
            if str(existing_value or "").strip() != str(new_value or "").strip():
                changed_fields.append(key)
    return changed_fields

def get_meta_field(data, field, default=None, path=None):
    try:
        if path:
            for key in path:
                data = data.get(key, {})
        return data.get(field, default)
    except Exception:
        return default

def recursive_season_diff(old, new, path=""):
    changes = []
    def strip_indices(p):
        return re.sub(r"\[\d+\]", "", p)
    if isinstance(old, dict) and isinstance(new, dict):
        all_keys = set(old.keys()) | set(new.keys())
        for k in all_keys:
            new_path = f"{path}['{k}']" if path else f"['{k}']"
            if k not in old or k not in new:
                changes.append(strip_indices(new_path))
            else:
                changes.extend(recursive_season_diff(old[k], new[k], new_path))
    elif isinstance(old, list) and isinstance(new, list):
        min_len = min(len(old), len(new))
        for i in range(min_len):
            new_path = f"{path}[{i}]"
            changes.extend(recursive_season_diff(old[i], new[i], new_path))
        if len(old) != len(new):
            changes.append(strip_indices(path))
    else:
        if old != new:
            changes.append(strip_indices(path))
    return list(set(changes))

def get_best_poster(
    config, images, preferred_language="en", fallback=None, prefer_vote=None, max_width=None,
    max_height=None, relaxed_vote=None, min_width=None, min_height=None,
):
    if not images:
        return None
    if fallback is None:
        fallback = config["tmdb"].get("fallback", [])
    if isinstance(fallback, str):
        fallback = [fallback]
    else:
        fallback = list(fallback or [])
    for val in (None, ""):
        if val not in fallback:
            fallback.append(val)
    language_priority = [preferred_language] + fallback
    poster_sel = config["poster_set"]
    default_sel = poster_sel

    prefer_vote = prefer_vote if prefer_vote is not None else poster_sel.get("prefer_vote", default_sel.get("prefer_vote", 0))
    max_width = max_width if max_width is not None else poster_sel.get("max_width", default_sel.get("max_width", 0))
    max_height = max_height if max_height is not None else poster_sel.get("max_height", default_sel.get("max_height", 0))
    relaxed_vote = relaxed_vote if relaxed_vote is not None else poster_sel.get("vote_relaxed", default_sel.get("vote_relaxed", 0))
    min_width = min_width if min_width is not None else poster_sel.get("min_width", default_sel.get("min_width", 0))
    min_height = min_height if min_height is not None else poster_sel.get("min_height", default_sel.get("min_height", 0))
    
    for lang in language_priority:
        language_filtered = [img for img in images if img.get("iso_639_1") == lang]
        if not language_filtered:
            continue
        filtered = [
            img for img in language_filtered
            if img.get("vote_average", 0) >= prefer_vote and
               img.get("width", 0) >= max_width and
               img.get("height", 0) >= max_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
            return best
        filtered = [
            img for img in language_filtered
            if img.get("vote_average", 0) >= relaxed_vote and
               img.get("width", 0) >= min_width and
               img.get("height", 0) >= min_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
            return best
        filtered = [
            img for img in language_filtered
            if img.get("width", 0) >= min_width and img.get("height", 0) >= min_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: x["width"] * x["height"])
            return best

    if images:
        best = max(images, key=lambda x: x.get("width", 0) * x.get("height", 0))
        return best
    return None

def get_best_season(
    config, images, preferred_language="en", fallback=None, prefer_vote=None, max_width=None,
    max_height=None, relaxed_vote=None, min_width=None, min_height=None,
):
    if not images:
        return None
    if fallback is None:
        fallback = config["tmdb"].get("fallback", [])
    if isinstance(fallback, str):
        fallback = [fallback]
    else:
        fallback = list(fallback or [])
    for val in (None, ""):
        if val not in fallback:
            fallback.append(val)
    language_priority = [preferred_language] + fallback
    season_sel = config["season_set"]
    default_sel = season_sel

    prefer_vote = prefer_vote if prefer_vote is not None else season_sel.get("prefer_vote", default_sel.get("prefer_vote", 0))
    max_width = max_width if max_width is not None else season_sel.get("max_width", default_sel.get("max_width", 0))
    max_height = max_height if max_height is not None else season_sel.get("max_height", default_sel.get("max_height", 0))
    relaxed_vote = relaxed_vote if relaxed_vote is not None else season_sel.get("vote_relaxed", default_sel.get("vote_relaxed", 0))
    min_width = min_width if min_width is not None else season_sel.get("min_width", default_sel.get("min_width", 0))
    min_height = min_height if min_height is not None else season_sel.get("min_height", default_sel.get("min_height", 0))

    for lang in language_priority:
        language_filtered = [img for img in images if img.get("iso_639_1") == lang]
        if not language_filtered:
            continue
        filtered = [
            img for img in language_filtered
            if img.get("vote_average", 0) >= prefer_vote and
               img.get("width", 0) >= max_width and
               img.get("height", 0) >= max_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
            return best
        filtered = [
            img for img in language_filtered
            if img.get("vote_average", 0) >= relaxed_vote and
               img.get("width", 0) >= min_width and
               img.get("height", 0) >= min_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
            return best
        filtered = [
            img for img in language_filtered
            if img.get("width", 0) >= min_width and img.get("height", 0) >= min_height
        ]
        if filtered:
            best = max(filtered, key=lambda x: x["width"] * x["height"])
            return best

    if images:
        best = max(images, key=lambda x: x.get("width", 0) * x.get("height", 0))
        return best
    return None

def get_best_background(
    config, images, prefer_vote=None, max_width=None, max_height=None, relaxed_vote=None,
    min_width=None, min_height=None
):
    if not images:
        return None
    
    bg_sel = config["background_set"]
    default_sel = bg_sel
        
    prefer_vote = prefer_vote if prefer_vote is not None else bg_sel.get("prefer_vote", default_sel.get("prefer_vote", 0))
    max_width = max_width if max_width is not None else bg_sel.get("max_width", default_sel.get("max_width", 0))
    max_height = max_height if max_height is not None else bg_sel.get("max_height", default_sel.get("max_height", 0))
    relaxed_vote = relaxed_vote if relaxed_vote is not None else bg_sel.get("vote_relaxed", default_sel.get("vote_relaxed", 0))
    min_width = min_width if min_width is not None else bg_sel.get("min_width", default_sel.get("min_width", 0))
    min_height = min_height if min_height is not None else bg_sel.get("min_height", default_sel.get("min_height", 0))
    
    filtered = [
        img for img in images
        if img.get("vote_average", 0) >= prefer_vote and
           img.get("width", 0) >= max_width and
           img.get("height", 0) >= max_height
    ]
    if filtered:
        best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
        return best 
    filtered = [
        img for img in images
        if img.get("vote_average", 0) >= relaxed_vote and
           img.get("width", 0) >= min_width and
           img.get("height", 0) >= min_height
    ]
    if filtered:
        best = max(filtered, key=lambda x: (x["vote_average"], x["width"] * x["height"]))
        return best    
    filtered = [
        img for img in images
        if img.get("width", 0) >= min_width and img.get("height", 0) >= min_height
    ]
    if filtered:
        best = max(filtered, key=lambda x: x["width"] * x["height"])
        return best

    if images:
        best = max(images, key=lambda x: x.get("width", 0) * x.get("height", 0))
        return best
    return None

def stale_image(last_upgraded, days=30):
    try:
        interval = datetime.timedelta(days=float(days))
    except (TypeError, ValueError, OverflowError):
        return True
    if interval <= datetime.timedelta(0):
        return False
    if not last_upgraded:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(str(last_upgraded).replace("Z", "+00:00"))
        now = datetime.datetime.now(last_dt.tzinfo) if last_dt.tzinfo else datetime.datetime.now()
        return now - last_dt >= interval
    except (TypeError, ValueError):
        return True


def _normalized_asset_path(path):
    return str(Path(path).absolute()) if path else None


def asset_write_allowed(config, cache_key, asset_path, asset_type, season_number=None):
    """Return whether an existing artwork file is still owned by MetaFusion."""
    if not asset_path.exists():
        return True, "missing"

    policy = str(config.get("assets", {}).get("update_policy", "managed")).strip().lower()
    if policy == "overwrite":
        return True, "overwrite"
    if policy == "fill_missing":
        return False, "fill_missing"

    cached = load_cache().get(cache_key, {})
    if not isinstance(cached, dict):
        return False, "unmanaged"
    if asset_type == "season":
        asset_record = (cached.get("seasons") or {}).get(str(season_number), {})
        expected_checksum = asset_record.get("season_checksum")
        expected_path = asset_record.get("season_path")
    else:
        expected_checksum = cached.get(f"{asset_type}_checksum")
        expected_path = cached.get(f"{asset_type}_path")
    if not expected_checksum:
        return False, "unmanaged"
    if expected_path and _normalized_asset_path(expected_path) != _normalized_asset_path(asset_path):
        return False, "unmanaged"
    try:
        current_checksum = sha256_file(asset_path)
    except OSError:
        return False, "unverifiable"
    if current_checksum != expected_checksum:
        return False, "modified"
    return True, "managed"


def claim_asset_destination(registry, cache_key, asset_path):
    """Reserve a destination synchronously so concurrent builders cannot collide."""
    normalized = _normalized_asset_path(asset_path)
    existing_owner = registry.get(normalized)
    if existing_owner not in (None, cache_key):
        return False, existing_owner
    registry[normalized] = cache_key
    return True, cache_key


def smart_asset_upgrade(
    config, asset_path, new_image_data, new_image_path=None, cache_key=None,
    asset_type="poster", stale_days=30
):
    from PIL import Image

    new_width = new_image_data.get("width", 0)
    new_height = new_image_data.get("height", 0)
    new_votes = new_image_data.get("vote_average", 0)
    if asset_type == "background":
        vote_relaxed = config["background_set"].get("vote_relaxed", 3.5)
        vote_threshold = config["background_set"].get("vote_threshold", 5.0)
        cache_key_name = "bg_average"
        last_upgraded_key = "background_last_upgraded"
    elif asset_type == "poster":
        vote_relaxed = config["poster_set"].get("vote_relaxed", 3.5)
        vote_threshold = config["poster_set"].get("vote_threshold", 5.0)
        cache_key_name = "poster_average"
        last_upgraded_key = "poster_last_upgraded"

    cached_votes = 0
    last_upgraded = None
    if cache_key:
        cache = load_cache()
        cached = cache.get(cache_key, {})
        cached_votes = cached.get(cache_key_name, 0)
        last_upgraded = cached.get(last_upgraded_key)

    context = {
        "new_width": new_width,
        "new_height": new_height,
        "new_votes": new_votes,
        "cached_votes": cached_votes,
        "vote_threshold": vote_threshold,
        "vote_relaxed": vote_relaxed,
        "asset_path_exists": asset_path.exists(),
        "new_image_path_exists": new_image_path.exists() if new_image_path else False,
        "last_upgraded": last_upgraded
    }

    if not asset_path.exists():
        return True, "NO_EXISTING_ASSET", context

    if new_image_path and new_image_path.exists():
        try:
            with open(asset_path, "rb") as f:
                existing_checksum = hashlib.md5(f.read()).hexdigest()
            with open(new_image_path, "rb") as f:
                new_checksum = hashlib.md5(f.read()).hexdigest()
            context["existing_checksum"] = existing_checksum
            context["new_checksum"] = new_checksum
            if existing_checksum == new_checksum:
                return False, "ALREADY_UP_TO_DATE", context
        except Exception as e:
            context["error"] = str(e)
            return False, "ERROR_IMAGE_COMPARE", context
    else:
        return False, "NO_IMAGE_FOR_COMPARE", context

    try:
        with Image.open(asset_path) as img:
            existing_width, existing_height = img.size
        context["existing_width"] = existing_width
        context["existing_height"] = existing_height
    except Exception as e:
        context["error"] = str(e)
        return False, "ERROR_IMAGE_COMPARE", context

    if stale_image(last_upgraded, stale_days):
        if (
            new_width >= existing_width
            and new_height >= existing_height
            and new_votes >= cached_votes
        ):
            return True, "FORCE_UPGRADE_STALE", context
        return False, "STALE_CANDIDATE_DOWNGRADE", context

    if cached_votes < vote_threshold and new_votes >= vote_threshold:
        return True, "UPGRADE_THRESHOLD", context
    if cached_votes < vote_threshold and vote_relaxed <= new_votes < vote_threshold:
        return True, "UPGRADE_RELAXED", context
    if cached_votes > 0 and new_votes > cached_votes:
        return True, "UPGRADE_VOTES", context
    if cached_votes >= vote_threshold and new_votes >= vote_threshold and new_votes > cached_votes:
        return True, "UPGRADE_STRICT", context

    if new_width > existing_width or new_height > existing_height:
        return True, "UPGRADE_DIMENSIONS", context

    return False, "NO_UPGRADE_NEEDED", context

def smart_season_asset_upgrade(
    config, asset_path, new_image_data, new_image_path=None, cache_key=None, 
    season_number=None, stale_days=30
):
    from PIL import Image

    new_width = new_image_data.get("width", 0)
    new_height = new_image_data.get("height", 0)
    new_votes = new_image_data.get("vote_average", 0)

    vote_relaxed = config["season_set"].get("vote_relaxed", 0.5)
    vote_threshold = config["season_set"].get("vote_threshold", 3.0)
    cache_key_name = "season_average"

    cached_votes = 0
    last_upgraded = None
    if cache_key:
        cache = load_cache()
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and season_number is not None:
            seasons = cached.get("seasons", {})
            season_entry = seasons.get(str(season_number), {})
            cached_votes = season_entry.get(cache_key_name, 0)
            last_upgraded = season_entry.get("season_last_upgraded")

    context = {
        "new_width": new_width,
        "new_height": new_height,
        "new_votes": new_votes,
        "cached_votes": cached_votes,
        "vote_threshold": vote_threshold,
        "vote_relaxed": vote_relaxed,
        "asset_path_exists": asset_path.exists(),
        "new_image_path_exists": new_image_path.exists() if new_image_path else False,
        "last_upgraded": last_upgraded
    }

    if not asset_path.exists():
        return True, "NO_EXISTING_ASSET_SEASON", context

    if new_image_path and new_image_path.exists():
        try:
            with open(asset_path, "rb") as f:
                existing_checksum = hashlib.md5(f.read()).hexdigest()
            with open(new_image_path, "rb") as f:
                new_checksum = hashlib.md5(f.read()).hexdigest()
            context["existing_checksum"] = existing_checksum
            context["new_checksum"] = new_checksum
            if existing_checksum == new_checksum:
                return False, "ALREADY_UP_TO_DATE_SEASON", context
        except Exception as e:
            context["error"] = str(e)
            return False, "ERROR_IMAGE_COMPARE_SEASON", context
    else:
        return False, "NO_IMAGE_FOR_COMPARE_SEASON", context

    try:
        with Image.open(asset_path) as img:
            existing_width, existing_height = img.size
        context["existing_width"] = existing_width
        context["existing_height"] = existing_height
    except Exception as e:
        context["error"] = str(e)
        return False, "ERROR_IMAGE_COMPARE_SEASON", context

    if stale_image(last_upgraded, stale_days):
        if (
            new_width >= existing_width
            and new_height >= existing_height
            and new_votes >= cached_votes
        ):
            return True, "FORCE_UPGRADE_STALE_SEASON", context
        return False, "STALE_CANDIDATE_DOWNGRADE_SEASON", context

    if cached_votes == 0:
        if new_votes > 0 and (new_width > existing_width or new_height > existing_height or new_votes > cached_votes):
            return True, "UPGRADE_ZERO_VOTE_SEASON", context
        if new_votes == 0 and (new_width > existing_width or new_height > existing_height):
            return True, "UPGRADE_VOTES_SEASON", context
    if new_votes < cached_votes:
        if new_width > existing_width or new_height > existing_height:
            return True, "UPGRADE_VOTES_SEASON", context
    if vote_relaxed <= new_votes < vote_threshold:
        if new_votes > cached_votes:
            return True, "UPGRADE_RELAXED_SEASON", context
    if new_votes >= vote_threshold:
        return True, "UPGRADE_THRESHOLD_SEASON", context
    if new_width > existing_width or new_height > existing_height:
        return True, "UPGRADE_DIMENSIONS_SEASON", context

    return False, "NO_UPGRADE_NEEDED_SEASON", context

async def download_poster(config, image_path, save_path, session=None, retries=3):
    url = f"https://image.tmdb.org/t/p/original{image_path or ''}"
    if session is None:
        return False, None, "HTTP session failed"
    try:
        response_content = await tmdb_api_request(
            config,
            url,
            raw=True,
            cache=False,
            session=session,
            retries=retries,
        )
        if not response_content:
            return False, None, "Empty or rejected response from TMDb"
        result, error = await save_poster(response_content, save_path)
        if result is True or result == "ALREADY_UP_TO_DATE":
            return True, 200, error
        return False, None, error or "File not saved after download"
    except Exception as error:
        return False, getattr(error, "status", None), str(error)

def get_asset_path(config, meta, asset_type="poster", season_number=None):
    mode = config.get("settings", {}).get("mode", "kometa")
    library_type = meta.get("library_type")
    show_path = meta.get("show_path")
    movie_path = meta.get("movie_path")

    if mode == "plex":
        def writable_directory(value):
            if not value:
                return None
            directory = Path(value)
            if not directory.is_dir() or not os.access(directory, os.W_OK):
                return None
            return directory

        if asset_type == "poster":
            if library_type == "movie":
                directory = writable_directory(meta.get("movie_dir"))
                return directory / "poster.jpg" if directory else None
            elif library_type in ("show", "tv"):
                directory = writable_directory(meta.get("show_dir"))
                return directory / "poster.jpg" if directory else None
        elif asset_type == "background":
            if library_type == "movie":
                directory = writable_directory(meta.get("movie_dir"))
                return directory / "fanart.jpg" if directory else None
            elif library_type in ("show", "tv"):
                directory = writable_directory(meta.get("show_dir"))
                return directory / "fanart.jpg" if directory else None
        elif asset_type == "season" and season_number is not None:
            season_dir = (meta.get("season_dirs") or {}).get(season_number)
            if season_dir is None:
                season_dir = (meta.get("season_dirs") or {}).get(str(season_number))
            if not season_dir:
                return None
            filename = (
                "season-specials-poster.jpg"
                if int(season_number) == 0
                else f"Season{int(season_number):02}.jpg"
            )
            directory = writable_directory(season_dir)
            return directory / filename if directory else None
    else:
        kometa_root = config.get("settings", {}).get("path", ".")
        assets_path = Path(kometa_root) / "assets" / library_type
        assets_path.mkdir(parents=True, exist_ok=True)
        if asset_type == "poster":
            if library_type == "movie":
                return assets_path / movie_path / "poster.jpg"
            elif library_type in ("show", "tv"):
                return assets_path / show_path / "poster.jpg"
        elif asset_type == "background":
            if library_type == "movie":
                return assets_path / movie_path / "fanart.jpg"
            elif library_type in ("show", "tv"):
                return assets_path / show_path / "fanart.jpg"
        elif asset_type == "season" and season_number is not None:
            return assets_path / show_path / f"Season{season_number:02}.jpg"
    return None

def asset_temp_path(config, meta, extension="jpg"):
    if mode_check(config, "kometa"):
        kometa_root = config.get("settings", {}).get("path", ".")
        library_type = meta.get("library_type", "movie")
        assets_path = Path(kometa_root) / "assets" / library_type
    else:
        library_type = meta.get("library_type", "movie")
        if library_type == "movie":
            assets_path = Path(meta["movie_dir"])
        elif library_type in ("show", "tv"):
            assets_path = Path(meta["show_dir"])
        else:
            assets_path = Path(".")
    if mode_check(config, "kometa"):
        assets_path.mkdir(parents=True, exist_ok=True)
    temp_filename = f"temp_{uuid.uuid4().hex}.{extension}"
    return assets_path / temp_filename

async def save_poster(image_content, save_path):
    try:
        from PIL import Image

        def validate_image():
            with Image.open(BytesIO(image_content)) as image:
                image.verify()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, validate_image)
        new_checksum = hashlib.md5(image_content).hexdigest()
        if save_path.exists():
            with open(save_path, "rb") as existing_file:
                existing_checksum = hashlib.md5(existing_file.read()).hexdigest()
            if existing_checksum == new_checksum:
                return "ALREADY_UP_TO_DATE", None
        await loop.run_in_executor(None, atomic_write_bytes, save_path, image_content)
        return True, None
    except Exception as e:
        return False, str(e)

def format_runtime(minutes):
    if minutes is None or minutes == "":
        return ""
    try:
        minutes = int(minutes)
        hours = minutes // 60
        mins = minutes % 60
        if hours > 0:
            return f"{hours} hr{'s' if hours > 1 else ''} {mins} min{'s' if mins != 1 else ''}"
        else:
            return f"{mins} min{'s' if mins != 1 else ''}"
    except (ValueError, TypeError):
        return str(minutes)
