"""Licensed, race-aware Formula 1 cinematic-background discovery."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

from PIL import Image, ImageStat

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

PROVIDER = "wikimedia-commons-race-background"
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
ATMOSPHERE_REJECTED_IDENTITIES = {
    "award ceremony",
    "crowd only",
    "driver portrait",
    "podium",
    "press conference",
    "trophy",
}


@dataclass(frozen=True)
class RaceEnvironment:
    mode: str
    race_key: str
    event_terms: tuple[str, ...]
    circuit_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    scene_terms: tuple[str, ...]


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
    subject_type: str = "safety_car"
    match_tier: str = "season_safety_car"
    environment: str = "unknown"
    race_key: str = ""
    evidence: tuple[str, ...] = ()

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        payload = dict(value)
        payload["evidence"] = tuple(payload.get("evidence") or ())
        return cls(**payload)


def _identity_terms(value):
    return tuple(term for term in re.split(r"\s+", _normalize(value)) if len(term) > 2)


def _meaningful_terms(value):
    ignored = {
        "circuit",
        "formula",
        "grand",
        "international",
        "prix",
        "race",
        "racing",
        "the",
    }
    return tuple(term for term in _identity_terms(value) if term not in ignored)


def _solar_elevation(race):
    """Approximate solar elevation at the race start using NOAA equations."""
    if not race.race_date or not race.race_time_utc:
        return None
    if race.latitude is None or race.longitude is None:
        return None
    try:
        stamp = datetime.fromisoformat(
            f"{race.race_date}T{str(race.race_time_utc).replace('Z', '+00:00')}"
        ).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    day = stamp.timetuple().tm_yday
    minutes = stamp.hour * 60 + stamp.minute + stamp.second / 60
    gamma = 2 * math.pi / 365 * (day - 1 + (minutes / 60 - 12) / 24)
    equation = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    solar_minutes = (minutes + equation + 4 * float(race.longitude)) % 1440
    hour_angle = math.radians(solar_minutes / 4 - 180)
    latitude = math.radians(float(race.latitude))
    cosine = math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(
        declination
    ) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, cosine))))


def derive_race_environment(race):
    elevation = _solar_elevation(race)
    mode = (
        "unknown"
        if elevation is None
        else "night"
        if elevation <= -6
        else "twilight"
        if elevation <= 4
        else "day"
    )
    event_base = re.sub(r"\b(grand prix|gp)\b", "", _normalize(race.name)).strip()
    event_terms = _meaningful_terms(event_base)
    circuit_terms = _meaningful_terms(f"{race.circuit_id} {race.circuit}")
    location_terms = _meaningful_terms(f"{race.locality} {race.country}")
    profile = _normalize(race.circuit_profile or "")
    vocabulary = (
        "city",
        "coastal",
        "desert",
        "floodlit",
        "forest",
        "harbour",
        "night",
        "street",
        "twilight",
        "urban",
        "waterfront",
        "wooded",
    )
    scene_terms = tuple(term for term in vocabulary if term in profile)
    additions = {
        "night": ("night", "floodlit"),
        "twilight": ("twilight", "dusk"),
        "day": ("daylight",),
    }.get(mode, ())
    scene_terms = tuple(dict.fromkeys((*scene_terms, *additions)))
    race_key = (
        f"{int(race.year)}:{int(race.round_number):02d}:"
        f"{_normalize(race.circuit_id or race.circuit).replace(' ', '-')}:{mode}"
    )
    return RaceEnvironment(mode, race_key, event_terms, circuit_terms, location_terms, scene_terms)


def classify_image_environment(path):
    """Classify pixels so metadata alone cannot claim a night scene."""
    with Image.open(path) as source:
        grayscale = source.convert("L").resize((96, 54), Image.Resampling.BILINEAR)
        values = sorted(grayscale.get_flattened_data())
        mean = ImageStat.Stat(grayscale).mean[0]
        median = values[len(values) // 2]
    if median < 75 and mean < 115:
        return "night"
    if median < 130 and mean < 160:
        return "twilight"
    return "day"


def environment_compatible(expected, actual):
    if expected == "unknown":
        return True
    if expected in {"night", "twilight"}:
        return actual in {"night", "twilight"}
    return actual in {"day", "twilight"}


def _contains_terms(identity, terms):
    words = set(identity.split())
    meaningful = tuple(term for term in terms if len(term) > 3)
    return bool(meaningful) and all(term in words for term in meaningful[:3])


def _year_values(identity):
    return {int(value) for value in re.findall(r"\b(19\d{2}|20\d{2}|21\d{2})\b", identity)}


def _vehicle_name(title, subject_type="safety_car"):
    if subject_type == "circuit_atmosphere":
        return "Circuit atmosphere"
    value = str(title or "").removeprefix("File:")
    return value.rsplit(".", 1)[0].strip() or "Official FIA F1 Safety Car"


def parse_safety_car_candidates(payload, race_or_year, config):
    """Return licensed exact-event/circuit backgrounds, safety car first."""
    race = race_or_year if hasattr(race_or_year, "round_number") else None
    year = int(race.year if race else race_or_year)
    environment = derive_race_environment(race) if race else None
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
        f1_identity = any(marker in words for marker in (" formula 1 ", " formula one ", " f1 "))
        safety_car = any(marker in words for marker in (" safety car ", " safety cars "))
        event_match = bool(environment and _contains_terms(identity, environment.event_terms))
        circuit_match = bool(environment and _contains_terms(identity, environment.circuit_terms))
        years = _year_values(identity)
        exact_year = year in years
        recent_year = any(0 <= year - value <= 3 for value in years)
        if (
            mime not in ALLOWED_MIME_TYPES
            or page_id <= 0
            or width < minimum_width
            or height < minimum_height
            or not 1.45 <= width / max(height, 1) <= 2.30
            or (race is None and (not exact_year or not safety_car))
            or (race is not None and not (event_match or circuit_match))
            or (race is not None and not (exact_year or recent_year))
            or not f1_identity
            or any(value in identity for value in REJECTED_IDENTITIES)
            or not _licence_allowed(licence)
            or (attribution_required and not author)
            or (attribution_required and not licence_url.startswith("https://"))
        ):
            continue
        if not safety_car and (
            race is None or any(value in identity for value in ATMOSPHERE_REJECTED_IDENTITIES)
        ):
            continue
        image_url = str(image_info.get("thumburl") or image_info.get("url") or "")
        if not image_url.startswith("https://upload.wikimedia.org/"):
            continue
        subject_type = "safety_car" if safety_car else "circuit_atmosphere"
        if safety_car and exact_year and (event_match or circuit_match):
            match_tier, tier_score = "exact_event_circuit_safety_car", 300
        elif safety_car:
            match_tier, tier_score = "recent_circuit_safety_car", 200
        else:
            match_tier, tier_score = "exact_event_atmosphere", 100
        location_match = bool(
            environment and any(term in identity for term in environment.location_terms)
        )
        scene_match = bool(
            environment and any(term in identity for term in environment.scene_terms)
        )
        evidence = tuple(
            name
            for name, matched in (
                ("event", event_match),
                ("circuit", circuit_match),
                ("season", exact_year),
                ("location", location_match),
                ("environment", scene_match),
            )
            if matched
        )
        score = tier_score + min(width * height / 1_000_000, 30.0)
        score += max(0.0, 6.0 - abs(width / height - 16 / 9) * 12)
        score += 15 if exact_year else 8
        score += 8 if location_match else 0
        score += 12 if scene_match else 0
        candidates.append(
            SafetyCarCandidate(
                page_id,
                title,
                f"https://commons.wikimedia.org/?curid={page_id}",
                image_url,
                width,
                height,
                mime,
                str(image_info.get("sha1") or ""),
                author or "Unknown contributor",
                licence,
                licence_url,
                _vehicle_name(title, subject_type),
                round(score, 4),
                subject_type,
                match_tier,
                environment.mode if environment else "unknown",
                environment.race_key if environment else "",
                evidence,
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
    """Retained for compatibility diagnostics; production uses targeted search."""
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


def _race_queries(race, environment):
    year = int(race.year)
    event = str(race.name).replace('"', "")
    circuit = str(race.circuit).replace('"', "")
    scene = " ".join(environment.scene_terms[:2])
    return (
        f'"{event}" {year} "Formula 1" "safety car" {scene}'.strip(),
        f'"{circuit}" {year} "Formula 1" "safety car" {scene}'.strip(),
        f'"{circuit}" "Formula One" "safety car" {scene}'.strip(),
        f'"{event}" {year} "Formula 1" track {scene}'.strip(),
        f'"{circuit}" {year} "Formula 1" track {scene}'.strip(),
    )


async def search_safety_cars(session, state, config, race_or_year, logger):
    """Discover licensed exact-race cinematic backgrounds, safety car first."""
    race = race_or_year if hasattr(race_or_year, "round_number") else None
    year = int(race.year if race else race_or_year)
    environment = derive_race_environment(race) if race else None
    key = f"search:v2:{environment.race_key}" if environment else f"search:v1:{year}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return parse_safety_car_candidates(cached, race_or_year, config), "cache"
    queries = (
        _race_queries(race, environment)
        if race
        else (
            f'intitle:{year} intitle:"safety car" F1',
            f'intitle:{year} intitle:"safety car" "Formula One"',
            f'{year} "Formula One safety car"',
        )
    )
    try:
        pages: list[dict] = []
        seen = set()
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
        state.cache_put(PROVIDER, key, payload, config["providers"]["commons_cache_hours"])
        return parse_safety_car_candidates(payload, race_or_year, config), PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning(
            "[Provider] Wikimedia race-background search: stale cache used | Year: %s | Round: %s",
            year,
            getattr(race, "round_number", "season"),
        )
        return parse_safety_car_candidates(stale, race_or_year, config), "stale-cache"
