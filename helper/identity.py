import hashlib
import re


def fallback_cache_key(meta):
    media_type = (meta.get("library_type") or "unknown").lower()
    if media_type == "show":
        media_type = "tv"
    return f"{media_type}:{meta.get('title')}:{meta.get('year')}"


def item_identity(meta):
    rating_key = meta.get("ratingKey")
    if rating_key is not None:
        return f"plex:{rating_key}"

    media_path = meta.get("movie_dir") or meta.get("show_dir")
    if media_path:
        digest = hashlib.sha256(str(media_path).encode("utf-8")).hexdigest()[:16]
        return f"path:{digest}"

    edition_title = meta.get("edition_title")
    if edition_title:
        normalized = re.sub(r"[^a-z0-9]+", "-", edition_title.lower()).strip("-")
        return f"edition:{normalized}"

    return fallback_cache_key(meta)


def cache_key_for_meta(meta):
    media_type = (meta.get("library_type") or "unknown").lower()
    if media_type == "show":
        media_type = "tv"
    identity = item_identity(meta)
    if identity == fallback_cache_key(meta):
        return identity
    return f"{media_type}:{identity}"


def metadata_key_for_meta(meta):
    base = f"{meta.get('title')} ({meta.get('year')})"
    media_type = (meta.get("library_type") or "").lower()
    if media_type != "movie":
        if meta.get("requires_unique_key"):
            library_name = meta.get("library_name") or "Library"
            return f"{base} [{library_name} - {item_identity(meta)}]"
        return base

    edition_title = meta.get("edition_title")
    needs_unique_key = bool(meta.get("requires_unique_key"))
    edition_collision = bool(meta.get("edition_key_collision"))

    if edition_title:
        suffix = edition_title
        if edition_collision:
            suffix = f"{suffix} - {item_identity(meta)}"
        return f"{base} [{suffix}]"
    if needs_unique_key:
        return f"{base} [No Edition - {item_identity(meta)}]"
    return base


def match_for_meta(meta, mapping_id):
    match = {
        "title": meta.get("title"),
        "year": meta.get("year"),
        "mapping_id": mapping_id,
    }
    if (meta.get("library_type") or "").lower() == "movie":
        if meta.get("edition_title"):
            match["edition"] = meta["edition_title"]
        elif meta.get("requires_unique_key"):
            match["blank_edition"] = True
    return match
