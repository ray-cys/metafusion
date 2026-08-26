"""Licensed, season-specific Formula 1 safety-car artwork discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from extensions.formula1.commons import (
    ALLOWED_MIME_TYPES,
    COMMONS_PAGE_LIMIT,
    _candidate_pages,
    _commons_json,
    _licence_allowed,
    _metadata_value,
    _normalize,
    _plain_text,
)

PROVIDER = "wikimedia-commons-safety-car"
REJECTED_IDENTITIES = {
    "diecast",
    "exhibition",
    "formula e",
    "lego",
    "medical car",
    "miniature",
    "model car",
    "museum",
    "sculpture",
    "showroom",
    "simulator",
    "toy",
}


@dataclass(frozen=True)
class SafetyCarCandidate:
    page_id: int
    title: str
    page_url: str
    image_url: str
    width: int
    height: int
    mime: str
    source_sha1: str
    author: str
    licence: str
    licence_url: str
    vehicle_name: str
    score: float

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


def _vehicle_name(title):
    value = str(title or "").removeprefix("File:")
    return value.rsplit(".", 1)[0].strip() or "Official FIA F1 Safety Car"


def parse_safety_car_candidates(payload, year, config):
    """Return reusable landscape photographs explicitly identified to an F1 season."""
    candidates = []
    minimum_width = config["show_artwork"]["minimum_source_width"]
    minimum_height = config["show_artwork"]["minimum_source_height"]
    for page in _candidate_pages(payload):
        image_info = (page.get("imageinfo") or [{}])[0]
        metadata = image_info.get("extmetadata") or {}
        mime = str(image_info.get("mime") or "").casefold()
        width = int(image_info.get("width") or 0)
        height = int(image_info.get("height") or 0)
        page_id = int(page.get("pageid") or 0)
        title = _plain_text(page.get("title"))
        categories = " ".join(
            str(category.get("title") or "") for category in page.get("categories") or []
        )
        description = _metadata_value(metadata, "ImageDescription")
        identity = _normalize(f"{title} {description} {categories}")
        words = f" {identity} "
        licence = _metadata_value(metadata, "LicenseShortName") or _metadata_value(
            metadata, "UsageTerms"
        )
        author = _metadata_value(metadata, "Artist")
        licence_url = _metadata_value(metadata, "LicenseUrl")
        attribution_required = _normalize(licence).startswith("cc by ")
        formula_one_identity = any(
            marker in words for marker in (" formula 1 ", " formula one ", " f1 ")
        )
        if (
            mime not in ALLOWED_MIME_TYPES
            or page_id <= 0
            or width < minimum_width
            or height < minimum_height
            or not 1.45 <= width / max(height, 1) <= 2.30
            or f" {int(year)} " not in words
            or not any(marker in words for marker in (" safety car ", " safety cars "))
            or not formula_one_identity
            or any(value in identity for value in REJECTED_IDENTITIES)
            or not _licence_allowed(licence)
            or (attribution_required and not author)
            or (attribution_required and not licence_url.startswith("https://"))
        ):
            continue
        image_url = str(image_info.get("thumburl") or image_info.get("url") or "")
        if not image_url.startswith("https://upload.wikimedia.org/"):
            continue
        score = min(width * height / 1_000_000, 30.0)
        score += max(0.0, 6.0 - abs(width / height - 16 / 9) * 12)
        score += 3.0 if "grand prix" in identity or " gp " in words else 0.0
        score += 2.0 if "on track" in identity or "track" in identity else 0.0
        candidates.append(
            SafetyCarCandidate(
                page_id=page_id,
                title=title,
                page_url=f"https://commons.wikimedia.org/?curid={page_id}",
                image_url=image_url,
                width=width,
                height=height,
                mime=mime,
                source_sha1=str(image_info.get("sha1") or ""),
                author=author or "Unknown contributor",
                licence=licence,
                licence_url=licence_url,
                vehicle_name=_vehicle_name(title),
                score=round(score, 4),
            )
        )
    unique = {candidate.page_id: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda item: (-item.score, item.title.casefold()))


def _safety_car_search_url(config, query, *, offset=None):
    parameters = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrlimit": "30",
        "gsrsearch": query,
        "prop": "imageinfo|categories",
        "iiprop": "url|size|sha1|mime|extmetadata",
        "iiurlwidth": "2560",
        "cllimit": "max",
        "maxlag": "5",
    }
    if offset is not None:
        parameters["gsroffset"] = str(offset)
    return f"{config['providers']['commons_url']}?{urlencode(parameters)}"


def _safety_car_category_url(config, *, continuation=None):
    parameters = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "categorymembers",
        "gcmtitle": "Category:Safety cars",
        "gcmnamespace": "6",
        "gcmlimit": "max",
        "gcmtype": "file",
        "prop": "imageinfo|categories",
        "iiprop": "url|size|sha1|mime|extmetadata",
        "iiurlwidth": "2560",
        "cllimit": "max",
        "maxlag": "5",
    }
    if continuation is not None:
        parameters["gcmcontinue"] = str(continuation)
    return f"{config['providers']['commons_url']}?{urlencode(parameters)}"


async def search_safety_cars(session, state, config, year, logger):
    """Discover a bounded pool of licensed safety-car views for one season."""
    key = f"search:v1:{int(year)}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return parse_safety_car_candidates(cached, year, config), "cache"
    queries = (
        f'intitle:{int(year)} intitle:"safety car" F1',
        f'intitle:{int(year)} intitle:"safety car" "Formula One"',
        f'{int(year)} "Formula One safety car"',
    )
    try:
        pages: list[dict] = []
        seen = set()
        continuation = None
        for _page in range(COMMONS_PAGE_LIMIT):
            response = await _commons_json(
                session,
                _safety_car_category_url(config, continuation=continuation),
                config["providers"]["retries"],
            )
            for page in _candidate_pages(response):
                page_id = int(page.get("pageid") or 0)
                if page_id not in seen:
                    pages.append(page)
                    seen.add(page_id)
            continuation = (response.get("continue") or {}).get("gcmcontinue")
            if continuation is None:
                break
        for query in queries:
            offset = None
            for _page in range(COMMONS_PAGE_LIMIT):
                response = await _commons_json(
                    session,
                    _safety_car_search_url(config, query, offset=offset),
                    config["providers"]["retries"],
                )
                for page in _candidate_pages(response):
                    page_id = int(page.get("pageid") or 0)
                    if page_id not in seen:
                        pages.append(page)
                        seen.add(page_id)
                offset = (response.get("continue") or {}).get("gsroffset")
                if offset is None:
                    break
        payload = {"query": {"pages": pages}}
        state.cache_put(
            PROVIDER, key, payload, config["providers"]["commons_cache_hours"]
        )
        return parse_safety_car_candidates(payload, year, config), PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning(
            "[Provider] Wikimedia safety-car search: stale cache used | Year: %s",
            year,
        )
        return parse_safety_car_candidates(stale, year, config), "stale-cache"
