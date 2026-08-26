"""Wikimedia Commons adapter for licensed, current-season Formula 1 car images."""

import asyncio
import hashlib
import html
import io
import re
import unicodedata
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from PIL import Image, ImageFilter, ImageStat

from extensions.formula1.provider import _get
from helper.io import atomic_write_bytes

PROVIDER = "wikimedia-commons"
ROSTER_PROVIDER = "jolpica"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
COMMONS_REQUEST_INTERVAL_SECONDS = 1.0
COMMONS_PAGE_LIMIT = 3
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
REJECTED_WORDS = {
    "diecast",
    "historic",
    "helmet",
    "lego",
    "model car",
    "safety car",
    "sculpture",
    "simulator",
    "toy",
}
KNOWN_ALIASES = {
    "rb": {"racing bulls", "visa cash app racing bulls", "rb f1 team"},
    "red_bull": {"red bull", "red bull racing"},
}
SEARCH_ALIASES = {
    "rb": "Racing Bulls",
    "red_bull": "Red Bull",
}


class _CommonsRateLimit(RuntimeError):
    def __init__(self, delay):
        self.delay = float(delay)
        super().__init__(f"rate limited; retry after {self.delay:g} seconds")


@dataclass(frozen=True)
class ConstructorData:
    constructor_id: str
    name: str


@dataclass(frozen=True)
class CommonsCandidate:
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
    constructor_id: str
    constructor_name: str
    score: float

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        return cls(**value)


def _normalize(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def _plain_text(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _metadata_value(metadata, name):
    value = (metadata.get(name) or {}).get("value")
    return _plain_text(value)


def _constructor_aliases(constructor):
    aliases = set(KNOWN_ALIASES.get(constructor.constructor_id, set()))
    for value in (constructor.constructor_id.replace("_", " "), constructor.name):
        normalized = _normalize(value)
        aliases.add(normalized)
        aliases.add(
            re.sub(r"\b(?:formula one|formula 1|f1|racing) team\b", "", normalized).strip()
        )
    return {alias for alias in aliases if len(alias) >= 4}


def _licence_allowed(value):
    licence = _normalize(value)
    if licence.startswith("public domain") or licence.startswith("cc0"):
        return True
    return licence.startswith("cc by ") and all(
        restriction not in licence for restriction in ("share alike", " sa", " noncommercial", " nc")
    )


def parse_constructor_payload(payload):
    values = (
        payload.get("MRData", {})
        .get("ConstructorTable", {})
        .get("Constructors", [])
    )
    constructors = []
    for value in values:
        constructor_id = str(value.get("constructorId") or "").strip()
        name = str(value.get("name") or "").strip()
        if constructor_id and name:
            constructors.append(ConstructorData(constructor_id, name))
    return sorted(constructors, key=lambda item: item.constructor_id)


def _candidate_pages(payload):
    pages = payload.get("query", {}).get("pages", [])
    if isinstance(pages, dict):
        return list(pages.values())
    return pages if isinstance(pages, list) else []


def parse_commons_candidates(payload, year, constructor, roster, config):
    """Return strictly identified, reusable landscape photographs in score order."""
    candidates = []
    aliases = _constructor_aliases(constructor)
    other_aliases = {
        alias
        for other in roster
        if other.constructor_id != constructor.constructor_id
        for alias in _constructor_aliases(other)
    }
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
        title_identity = _normalize(title)
        title_words = f" {title_identity} "
        motorsport_title = any(
            marker in title_words
            for marker in (" gp ", " grand prix ", " formula one ", " formula 1 ")
        )
        licence = _metadata_value(metadata, "LicenseShortName") or _metadata_value(
            metadata, "UsageTerms"
        )
        author = _metadata_value(metadata, "Artist")
        licence_url = _metadata_value(metadata, "LicenseUrl")
        attribution_required = _normalize(licence).startswith("cc by ")
        if (
            mime not in ALLOWED_MIME_TYPES
            or page_id <= 0
            or width < minimum_width
            or height < minimum_height
            or not 1.45 <= width / max(height, 1) <= 2.30
            or str(year) not in identity
            or not any(alias in title_identity for alias in aliases)
            or not motorsport_title
            or any(word in identity for word in REJECTED_WORDS)
            or not _licence_allowed(licence)
            or (attribution_required and not author)
            or (attribution_required and not licence_url.startswith("https://"))
        ):
            continue
        if any(alias in title_identity for alias in other_aliases):
            continue
        image_url = str(image_info.get("thumburl") or image_info.get("url") or "")
        if not image_url.startswith("https://upload.wikimedia.org/"):
            continue
        score = min(width * height / 1_000_000, 30.0)
        score += max(0.0, 5.0 - abs(width / height - 16 / 9) * 10)
        score += 3.0 if "qualifying" in identity else 0.0
        score += 2.0 if "grand prix" in identity or " gp " in f" {identity} " else 0.0
        candidates.append(
            CommonsCandidate(
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
                constructor_id=constructor.constructor_id,
                constructor_name=constructor.name,
                score=round(score, 4),
            )
        )
    return sorted(candidates, key=lambda item: (-item.score, item.title.casefold()))


async def load_constructors(session, state, config, year, logger):
    key = f"constructors:{int(year)}"
    cached = state.cache_get(ROSTER_PROVIDER, key)
    if cached is not None:
        return parse_constructor_payload(cached), "cache"
    url = f"{config['providers']['jolpica_url']}/{int(year)}/constructors.json"
    try:
        payload = await _get(session, url, retries=config["providers"]["retries"])
        constructors = parse_constructor_payload(payload)
        if not constructors:
            raise RuntimeError("constructor roster contained no valid teams")
        state.cache_put(
            ROSTER_PROVIDER, key, payload, config["providers"]["cache_hours"]
        )
        return constructors, ROSTER_PROVIDER
    except RuntimeError:
        stale = state.cache_get(ROSTER_PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning("[Provider] Constructor roster: stale cache used | Year: %s", year)
        return parse_constructor_payload(stale), "stale-cache"


def _commons_search_url(
    config, year, constructor, *, broad=False, identity=None, offset=None
):
    aliases = sorted(_constructor_aliases(constructor), key=lambda value: (len(value), value))
    identity = identity or SEARCH_ALIASES.get(constructor.constructor_id) or (
        aliases[0] if aliases else constructor.name
    )
    query = f'intitle:{int(year)} intitle:"{identity}"'
    if not broad:
        query += " intitle:GP"
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


async def _commons_json(session, url, retries):
    last_error = None
    for attempt in range(int(retries)):
        try:
            await asyncio.sleep(COMMONS_REQUEST_INTERVAL_SECONDS)
            async with session.get(
                url,
                headers={
                    "User-Agent": (
                        "MetaFusion/1.2 "
                        "(https://github.com/ray-cys/metafusion; Wikimedia attribution client)"
                    )
                },
            ) as response:
                if response.status == 429:
                    retry_after = min(float(response.headers.get("Retry-After") or 1), 5.0)
                    raise _CommonsRateLimit(retry_after)
                if response.status != 200:
                    raise RuntimeError(f"provider returned HTTP {response.status}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise TypeError("provider returned an invalid JSON document")
                if payload.get("error"):
                    raise RuntimeError(f"provider returned API error: {payload['error']}")
                return payload
        except (OSError, asyncio.TimeoutError, RuntimeError, TypeError, ValueError) as error:
            last_error = error
            if attempt + 1 < int(retries):
                delay = error.delay if isinstance(error, _CommonsRateLimit) else 1.0
                await asyncio.sleep(delay)
    raise RuntimeError(f"Commons API request failed: {last_error}") from last_error


async def search_commons(session, state, config, year, constructor, roster, logger):
    key = f"search:v2:{int(year)}:{constructor.constructor_id}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return parse_commons_candidates(cached, year, constructor, roster, config), "cache"
    try:
        pages: list[dict] = []
        payload = {"query": {"pages": pages}}
        identities = list(
            dict.fromkeys(
                value
                for value in (
                    SEARCH_ALIASES.get(constructor.constructor_id),
                    constructor.name,
                    constructor.constructor_id.replace("_", " "),
                )
                if value
            )
        )
        for identity in identities:
            for broad in (False, True):
                offset = None
                for _page in range(COMMONS_PAGE_LIMIT):
                    response = await _commons_json(
                        session,
                        _commons_search_url(
                            config,
                            year,
                            constructor,
                            broad=broad,
                            identity=identity,
                            offset=offset,
                        ),
                        config["providers"]["retries"],
                    )
                    pages.extend(_candidate_pages(response))
                    if parse_commons_candidates(payload, year, constructor, roster, config):
                        break
                    offset = (response.get("continue") or {}).get("gsroffset")
                    if offset is None:
                        break
                if parse_commons_candidates(payload, year, constructor, roster, config):
                    break
            if parse_commons_candidates(payload, year, constructor, roster, config):
                break
        state.cache_put(
            PROVIDER, key, payload, config["providers"]["commons_cache_hours"]
        )
        return parse_commons_candidates(payload, year, constructor, roster, config), PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning(
            "[Provider] Wikimedia search: stale cache used | Year: %s | Team: %s",
            year,
            constructor.name,
        )
        return parse_commons_candidates(stale, year, constructor, roster, config), "stale-cache"


def _validate_image(data, config):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise RuntimeError("Commons image exceeded the size safety limit")
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            width, height = source.size
            if width * height > 60_000_000:
                raise RuntimeError("Commons image exceeded the pixel safety limit")
            image = source.convert("RGB")
    except (OSError, ValueError) as error:
        raise RuntimeError("Commons image could not be decoded") from error
    width, height = image.size
    if (
        width < config["show_artwork"]["minimum_source_width"]
        or height < config["show_artwork"]["minimum_source_height"]
        or not 1.45 <= width / max(height, 1) <= 2.30
    ):
        raise RuntimeError("Commons image failed dimensions or aspect-ratio validation")
    extrema = image.resize((128, 72)).getextrema()
    if max(high - low for low, high in extrema) < 16:
        raise RuntimeError("Commons image was blank or near-blank")
    edges = image.resize((320, 180)).convert("L").filter(ImageFilter.FIND_EDGES)
    if ImageStat.Stat(edges).stddev[0] < 6:
        raise RuntimeError("Commons image failed sharpness validation")
    return width, height


async def _download_bytes(session, url, retries):
    last_error = None
    for attempt in range(int(retries)):
        try:
            async with session.get(
                url,
                headers={
                    "User-Agent": (
                        "MetaFusion/1.2 "
                        "(https://github.com/ray-cys/metafusion; Wikimedia attribution client)"
                    )
                },
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"provider returned HTTP {response.status}")
                content_length = int(response.headers.get("Content-Length") or 0)
                if content_length > MAX_IMAGE_BYTES:
                    raise RuntimeError("Commons image exceeded the size safety limit")
                data = await response.read()
                if len(data) > MAX_IMAGE_BYTES:
                    raise RuntimeError("Commons image exceeded the size safety limit")
                return data
        except (OSError, asyncio.TimeoutError, RuntimeError, ValueError) as error:
            last_error = error
            if attempt + 1 < int(retries):
                await asyncio.sleep(0)
    raise RuntimeError(f"Commons image download failed: {last_error}") from last_error


async def acquire_candidate_image(session, config, candidate):
    """Download once, validate actual pixels, and return the private cache path."""
    digest = re.sub(r"[^a-z0-9]", "", candidate.source_sha1.casefold())
    if len(digest) < 8:
        digest = hashlib.sha256(candidate.image_url.encode()).hexdigest()
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[
        candidate.mime
    ]
    destination = config["paths"]["show_image_cache"] / f"{digest}{extension}"
    if destination.exists():
        _validate_image(destination.read_bytes(), config)
        return destination, "cache"
    data = await _download_bytes(
        session, candidate.image_url, retries=config["providers"]["retries"]
    )
    _validate_image(data, config)
    if not config["dry_run"]:
        atomic_write_bytes(destination, data)
    return destination, PROVIDER
