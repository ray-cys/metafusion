"""Licensed, race-aware Formula 1 cinematic-background discovery."""

from __future__ import annotations

import math
import re
from collections import Counter
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
BACKGROUND_CANDIDATE_VERSION = 8
CATEGORY_DEPTH_LIMIT = 2
CATEGORY_FETCH_LIMIT = 28
ACTION_BACKGROUND_TIERS = (
    "exact_event_action_race_car",
    "composite_event_action_race_car",
    "recent_circuit_action_race_car",
    "historical_circuit_action_race_car",
)
TEAM_CAR_FALLBACK_TIER = "current_season_team_car_fallback"
ELIGIBLE_BACKGROUND_TIERS = (*ACTION_BACKGROUND_TIERS, TEAM_CAR_FALLBACK_TIER)
REJECTED_IDENTITIES = {
    "black and white",
    "diecast",
    "exhibition",
    "formula e",
    "formula 2",
    "formula 3",
    "f1 academy",
    "lego",
    "medical car",
    "miniature",
    "monochrome",
    "mock up",
    "mock-up",
    "model car",
    "museum",
    "replica",
    "road show",
    "roadshow",
    "safety car",
    "sculpture",
    "showcar",
    "showcars",
    "showroom",
    "shopping mall",
    "simulator",
    "store display",
    "support race",
    "support races",
    "grayscale",
    "greyscale",
    "toy",
}
RACE_CAR_SUBJECT_IDENTITIES = {
    "f1 car",
    "formula 1 car",
    "formula 1 racing car",
    "formula one car",
    "formula one racing car",
    "race car",
    "racing car",
    "single seater",
    "single-seater",
}
ACTIVE_RACE_IDENTITIES = {
    "during the race",
    "final race",
    "formation lap",
    "free practice",
    "night race",
    "on circuit",
    "on track",
    "qualifying",
    "race action",
    "racing at",
    "racing for",
    "sprint race",
    "starting grid",
    "track action",
}
ACTIVE_RACE_PATTERNS = (
    re.compile(r"\bfp[123]\b"),
    re.compile(r"\bracing\b"),
    re.compile(r"\brace(?:d|s|ing)?\s+(?:at|during|on|through)\b"),
    re.compile(r"\bturn\s+\d{1,2}\b"),
)
STATIC_RACE_CAR_IDENTITIES = {
    "car launch",
    "display car",
    "garage display",
    "in garage",
    "in the garage",
    "parked car",
    "paddock display",
    "pre season test",
    "pre-season test",
    "promotional display",
    "show car",
    "static display",
    "testing session",
}
SCENE_CONTEXT_IDENTITIES = {
    "barrier",
    "city lights",
    "fence",
    "fencing",
    "floodlight",
    "floodlit",
    "grandstand",
    "night race",
    "reflection",
    "street circuit",
    "track lights",
    "trackside",
    "under lights",
    "urban circuit",
    "wet track",
}
MULTI_CAR_IDENTITIES = {
    "cars racing",
    "field of cars",
    "formula one cars",
    "multiple cars",
    "race pack",
    "racing cars",
}
TIGHT_CROP_IDENTITIES = {
    "car detail",
    "close up",
    "close-up",
    "cockpit detail",
    "front wing detail",
    "rear wing detail",
}
FORMULA_ONE_IDENTITIES = (
    " formula 1 ",
    " formula one ",
    " formel 1 ",
    " formule 1 ",
    " f1 ",
)


@dataclass(frozen=True)
class RaceEnvironment:
    mode: str
    race_key: str
    event_terms: tuple[str, ...]
    circuit_terms: tuple[str, ...]
    location_terms: tuple[str, ...]
    scene_terms: tuple[str, ...]


@dataclass(frozen=True)
class RaceBackgroundCandidate:
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
    subject_type: str = "race_car"
    match_tier: str = "exact_event_action_race_car"
    environment: str = "unknown"
    race_key: str = ""
    evidence: tuple[str, ...] = ()
    eligibility_version: int = BACKGROUND_CANDIDATE_VERSION
    provider: str = PROVIDER

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        payload = dict(value)
        payload["evidence"] = tuple(payload.get("evidence") or ())
        payload.setdefault("eligibility_version", 1)
        payload.setdefault("provider", PROVIDER)
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


def image_has_meaningful_colour(path):
    """Reject grayscale or weakly tinted photographs after pixel decoding."""
    with Image.open(path) as source:
        sample = source.convert("RGB")
        sample.thumbnail((160, 90), Image.Resampling.BILINEAR)
        pixels = list(sample.get_flattened_data())
    chroma = [max(pixel) - min(pixel) for pixel in pixels]
    coloured_ratio = sum(value >= 12 for value in chroma) / max(1, len(chroma))
    mean_chroma = sum(chroma) / max(1, len(chroma))
    return coloured_ratio >= 0.08 and mean_chroma >= 6.0


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


def _formula_one_chassis_category(value):
    """Recognize future chassis categories without maintaining a team-name list."""
    category = str(value or "").removeprefix("Category:")
    if " of " not in category.casefold():
        return False
    prefix = category.split(" of ", 1)[0]
    tokens = re.findall(r"\b[A-Z][A-Z0-9-]{1,10}\b", prefix)
    ignored = {"FP1", "FP2", "FP3", "GP", "Q1", "Q2", "Q3"}
    return any(
        token not in ignored
        and any(character.isalpha() for character in token)
        and any(character.isdigit() for character in token)
        for token in tokens
    )


def _active_race_match(identity):
    return any(marker in identity for marker in ACTIVE_RACE_IDENTITIES) or any(
        pattern.search(identity) for pattern in ACTIVE_RACE_PATTERNS
    )


def _event_category_match(categories, environment):
    if environment is None:
        return False
    for category in categories:
        identity = _normalize(category)
        if not (
            "grand prix" in identity
            or "formula one" in identity
            or "formula 1" in identity
        ):
            continue
        if _contains_terms(identity, environment.event_terms) or _contains_terms(
            identity, environment.circuit_terms
        ):
            return True
    return False


def _vehicle_name(title):
    value = str(title or "").removeprefix("File:")
    return value.rsplit(".", 1)[0].strip() or "Formula 1 race car"


def _candidate_rejection_reason(
    *,
    mime,
    page_id,
    width,
    height,
    identity_allowed,
    provider_identity,
    identity,
    licence,
    attribution_required,
    author,
    licence_url,
    image_url,
    minimum_width,
    minimum_height,
):
    if mime not in ALLOWED_MIME_TYPES:
        return "unsupported-media-type"
    if page_id <= 0:
        return "missing-page-identity"
    if width < minimum_width or height < minimum_height:
        return "undersized"
    if not 1.45 <= width / max(height, 1) <= 2.30:
        return "incompatible-aspect-ratio"
    if not identity_allowed:
        return "event-circuit-location-mismatch"
    if not provider_identity:
        return "not-formula-one-race-action"
    if any(value in identity for value in REJECTED_IDENTITIES):
        return "rejected-subject-or-series"
    if not _licence_allowed(licence):
        return "incompatible-or-unknown-licence"
    if attribution_required and not author:
        return "missing-required-author"
    if attribution_required and not licence_url.startswith("https://"):
        return "missing-required-licence-url"
    if not image_url.startswith("https://upload.wikimedia.org/"):
        return "unsafe-image-url"
    return None


def parse_race_background_candidates(payload, race_or_year, config, diagnostics=None):
    """Return licensed Formula 1 race-action photographs for the target circuit."""
    race = race_or_year if hasattr(race_or_year, "round_number") else None
    year = int(race.year if race else race_or_year)
    environment = derive_race_environment(race) if race else None
    candidates = []
    minimum_width = config["show_artwork"]["fallback_background_source_width"]
    minimum_height = config["show_artwork"]["fallback_background_source_height"]
    preferred_width = config["show_artwork"]["minimum_background_source_width"]
    preferred_height = config["show_artwork"]["minimum_background_source_height"]
    for page in _candidate_pages(payload):
        image_info = (page.get("imageinfo") or [{}])[0]
        metadata = image_info.get("extmetadata") or {}
        mime = str(image_info.get("mime") or "").casefold()
        width = int(image_info.get("width") or 0)
        height = int(image_info.get("height") or 0)
        page_id = int(page.get("pageid") or 0)
        title = _plain_text(page.get("title"))
        category_values = [
            str(category.get("title") or "") for category in page.get("categories") or []
        ]
        categories = " ".join(category_values)
        category_context = " ".join(
            str(category) for category in page.get("_metafusion_category_context") or []
        )
        description = _metadata_value(metadata, "ImageDescription")
        subject_identity = _normalize(f"{title} {description} {categories}")
        identity = _normalize(f"{subject_identity} {category_context}")
        category_identity = _normalize(categories)
        location_identity = _normalize(f"{category_identity} {category_context}")
        subject_words = f" {subject_identity} "
        licence = _metadata_value(metadata, "LicenseShortName") or _metadata_value(
            metadata, "UsageTerms"
        )
        author = _metadata_value(metadata, "Artist")
        licence_url = _metadata_value(metadata, "LicenseUrl")
        attribution_required = _normalize(licence).startswith("cc by ")
        event_category = _event_category_match(category_values, environment)
        chassis_category = any(
            _formula_one_chassis_category(category) for category in category_values
        )
        f1_identity = any(marker in subject_words for marker in FORMULA_ONE_IDENTITIES) or (
            event_category and chassis_category
        )
        event_match = bool(environment and _contains_terms(identity, environment.event_terms))
        circuit_match = bool(environment and _contains_terms(identity, environment.circuit_terms))
        location_match = bool(
            environment and any(term in identity for term in environment.location_terms)
        )
        category_match = bool(
            environment
            and (
                _contains_terms(location_identity, environment.event_terms)
                or _contains_terms(location_identity, environment.circuit_terms)
            )
        )
        race_car_subject = any(
            marker in subject_identity for marker in RACE_CAR_SUBJECT_IDENTITIES
        ) or chassis_category
        race_car = f1_identity and race_car_subject
        tight_crop = any(
            marker in subject_identity for marker in TIGHT_CROP_IDENTITIES
        )
        scene_context = any(
            marker in subject_identity for marker in SCENE_CONTEXT_IDENTITIES
        )
        active_race = (
            race_car
            and _active_race_match(subject_identity)
            and not tight_crop
        )
        multi_car = any(marker in subject_identity for marker in MULTI_CAR_IDENTITIES)
        static_race_car = any(
            marker in subject_identity for marker in STATIC_RACE_CAR_IDENTITIES
        )
        years = _year_values(identity)
        exact_year = year in years
        recent_year = any(0 <= year - value <= 3 for value in years)
        future_year = any(value > year for value in years)
        historical_year = any(value < year for value in years)
        if race is None:
            identity_allowed = exact_year and active_race
        else:
            identity_allowed = active_race and (
                (exact_year and (event_match or circuit_match))
                or (circuit_match and not future_year)
            )
        provider_identity = f1_identity and race_car and active_race
        if static_race_car:
            identity_allowed = False
        image_url = str(image_info.get("thumburl") or image_info.get("url") or "")
        rejection = _candidate_rejection_reason(
            mime=mime,
            page_id=page_id,
            width=width,
            height=height,
            identity_allowed=identity_allowed,
            provider_identity=provider_identity,
            identity=subject_identity,
            licence=licence,
            attribution_required=attribution_required,
            author=author,
            licence_url=licence_url,
            image_url=image_url,
            minimum_width=minimum_width,
            minimum_height=minimum_height,
        )
        if rejection:
            if diagnostics is not None:
                diagnostics.append({"title": title or "unknown", "reason": rejection})
            continue
        if active_race and exact_year and (event_match or circuit_match):
            match_tier, tier_score = "exact_event_action_race_car", 520
        elif active_race and recent_year:
            match_tier, tier_score = "recent_circuit_action_race_car", 490
        else:
            match_tier, tier_score = "historical_circuit_action_race_car", 460
        scene_match = bool(
            environment and any(term in identity for term in environment.scene_terms)
        )
        evidence = tuple(
            name
            for name, matched in (
                ("event", event_match),
                ("circuit", circuit_match),
                ("commons-category", category_match),
                ("race-car-subject", race_car_subject),
                ("formula-one-event-category", event_category),
                ("formula-one-chassis-category", chassis_category),
                ("active-race", active_race),
                ("scene-context", scene_context),
                ("multiple-cars", multi_car),
                ("tight-crop", tight_crop),
                ("season", exact_year),
                ("recent-season", recent_year and not exact_year),
                (
                    "historical-exact-circuit-race-car",
                    race_car and circuit_match and historical_year and not recent_year,
                ),
                ("location", location_match),
                ("environment", scene_match),
            )
            if matched
        )
        evidence = (
            *evidence,
            "4k-source"
            if width >= preferred_width and height >= preferred_height
            else "fallback-resolution-source",
        )
        score = tier_score + min(width * height / 1_000_000, 30.0)
        score += max(0.0, 6.0 - abs(width / height - 16 / 9) * 12)
        past_years = [value for value in years if value <= year]
        age = year - max(past_years) if past_years else 20
        score += 20 if exact_year else max(0, 18 - min(age, 18))
        score += 8 if location_match else 0
        score += 12 if scene_match else 0
        score += 24 if scene_context else 0
        score += 16 if multi_car else 0
        score -= 40 if tight_crop else 0
        candidates.append(
            RaceBackgroundCandidate(
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
                _vehicle_name(title),
                round(score, 4),
                "race_car",
                match_tier,
                environment.mode if environment else "unknown",
                environment.race_key if environment else "",
                evidence,
            )
        )
    unique = {candidate.page_id: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda item: (-item.score, item.title.casefold()))


def _race_background_search_url(config, query, *, offset=None):
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
        "iiurlwidth": "3840",
        "cllimit": "max",
        "maxlag": "5",
    }
    if offset is not None:
        parameters["gsroffset"] = str(offset)
    return f"{config['providers']['commons_url']}?{urlencode(parameters)}"


def _race_background_category_url(
    config,
    category="Category:Formula One cars",
    *,
    continuation=None,
):
    """Build a bounded category-members request for files and subcategories."""
    parameters = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "categorymembers",
        "gcmtitle": category,
        "gcmnamespace": "6|14",
        "gcmlimit": "max",
        "prop": "imageinfo|categories",
        "iiprop": "url|size|sha1|mime|extmetadata",
        "iiurlwidth": "3840",
        "cllimit": "max",
        "maxlag": "5",
    }
    if continuation is not None:
        parameters["gcmcontinue"] = str(continuation)
    return f"{config['providers']['commons_url']}?{urlencode(parameters)}"


def _race_category_seeds(race):
    year = int(race.year)
    event = str(race.name).strip()
    circuit = str(race.circuit).strip()
    return tuple(
        dict.fromkeys(
            (
                f"Category:{year} {event}",
                f"Category:{circuit}",
                f"Category:{event}",
                f"Category:Automobile races at {circuit}",
            )
        )
    )


def _category_is_rejected(title):
    identity = _normalize(title)
    return any(value in identity for value in REJECTED_IDENTITIES)


def _category_priority(title, target_year):
    years = _year_values(_normalize(title))
    non_future = [value for value in years if value <= target_year]
    if target_year in years:
        return 0
    if non_future:
        return min(50, target_year - max(non_future)) + 1
    if years:
        return 100 + min(years) - target_year
    return 60


def _page_with_category_context(page, categories):
    value = dict(page)
    value["categories"] = [dict(item) for item in page.get("categories") or []]
    value["_metafusion_category_context"] = list(dict.fromkeys(categories))
    return value


async def _category_pages(session, config, race):
    """Traverse a small event/circuit graph and preserve ancestry as evidence."""
    queue: list[tuple[str, int, tuple[str, ...]]] = [
        (title, 0, ()) for title in _race_category_seeds(race)
    ]
    visited: set[str] = set()
    pages: list[dict] = []
    while queue and len(visited) < CATEGORY_FETCH_LIMIT:
        category, depth, ancestry = queue.pop(0)
        normalized = _normalize(category)
        if normalized in visited or _category_is_rejected(category):
            continue
        visited.add(normalized)
        continuation = None
        children: list[tuple[str, int, tuple[str, ...]]] = []
        context = (*ancestry, category)
        for _page in range(COMMONS_PAGE_LIMIT):
            response = await _commons_json(
                session,
                _race_background_category_url(
                    config,
                    category,
                    continuation=continuation,
                ),
                config["providers"]["retries"],
            )
            for page in _candidate_pages(response):
                namespace = page.get("ns")
                if page.get("imageinfo") or namespace == 6:
                    pages.append(_page_with_category_context(page, context))
                elif namespace == 14 and depth < CATEGORY_DEPTH_LIMIT:
                    title = str(page.get("title") or "")
                    if title and not _category_is_rejected(title):
                        children.append((title, depth + 1, context))
            continuation = (response.get("continue") or {}).get("gcmcontinue")
            if continuation is None:
                break
        children.sort(key=lambda item: (_category_priority(item[0], int(race.year)), item[0]))
        queue.extend(children)
    return pages


def _race_queries(race, environment):
    year = int(race.year)
    event = str(race.name).replace('"', "")
    circuit = str(race.circuit).replace('"', "")
    scene = " ".join(environment.scene_terms[:2])
    return (
        f'"{event}" {year} "Formula 1" "race car" {scene}'.strip(),
        f'"{circuit}" {year} "Formula 1" "race car" {scene}'.strip(),
        f'"{event}" {year} F1 qualifying {scene}'.strip(),
        f'"{event}" {year} F1 racing {scene}'.strip(),
        f'"{circuit}" {year} F1 racing {scene}'.strip(),
        f'"{event}" "Formula One" racing {scene}'.strip(),
        f'"{circuit}" "Formula One" racing {scene}'.strip(),
        f'"{event}" F1 "night race"'.strip(),
        f'"{circuit}" F1 "track action" {scene}'.strip(),
    )


async def search_race_backgrounds(session, state, config, race_or_year, logger):
    """Discover licensed exact-race cinematic race-car backgrounds."""
    race = race_or_year if hasattr(race_or_year, "round_number") else None
    year = int(race.year if race else race_or_year)
    environment = derive_race_environment(race) if race else None
    key = f"search:v9:{environment.race_key}" if environment else f"search:v7:{year}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return parse_race_background_candidates(cached, race_or_year, config), "cache"
    queries = (
        _race_queries(race, environment)
        if race
        else (
            f'intitle:{year} "Formula 1" "race car"',
            f'intitle:{year} "Formula One cars"',
            f'{year} "Formula One race car"',
        )
    )
    try:
        pages: list[dict] = []
        page_indexes: dict[int, int] = {}

        def remember(page):
            page_id = int(page.get("pageid") or 0)
            if page_id <= 0:
                return
            if page_id not in page_indexes:
                page_indexes[page_id] = len(pages)
                pages.append(page)
                return
            existing = pages[page_indexes[page_id]]
            existing_categories = {
                str(item.get("title") or ""): item
                for item in existing.get("categories") or []
            }
            for item in page.get("categories") or []:
                existing_categories.setdefault(str(item.get("title") or ""), item)
            existing["categories"] = list(existing_categories.values())
            existing["_metafusion_category_context"] = list(
                dict.fromkeys(
                    [
                        *(existing.get("_metafusion_category_context") or []),
                        *(page.get("_metafusion_category_context") or []),
                    ]
                )
            )

        if race is not None:
            for page in await _category_pages(session, config, race):
                remember(page)
        for query in queries:
            offset = None
            for _page in range(COMMONS_PAGE_LIMIT):
                response = await _commons_json(
                    session,
                    _race_background_search_url(config, query, offset=offset),
                    config["providers"]["retries"],
                )
                for page in _candidate_pages(response):
                    remember(page)
                offset = (response.get("continue") or {}).get("gsroffset")
                if offset is None:
                    break
        payload = {"query": {"pages": pages}}
        state.cache_put(PROVIDER, key, payload, config["providers"]["commons_cache_hours"])
        diagnostics: list[dict[str, str]] = []
        candidates = parse_race_background_candidates(
            payload,
            race_or_year,
            config,
            diagnostics,
        )
        if diagnostics:
            counts = Counter(item["reason"] for item in diagnostics)
            logger.debug(
                "[Provider] Wikimedia race-background candidates | Year: %s | Round: %s | "
                "Accepted: %s | Rejected: %s | Reasons: %s",
                year,
                getattr(race, "round_number", "season"),
                len(candidates),
                len(diagnostics),
                ", ".join(f"{name}={count}" for name, count in sorted(counts.items())),
            )
            for item in diagnostics[:20]:
                logger.debug(
                    "[Provider] Wikimedia race-background rejected | File: %s | Reason: %s",
                    item["title"],
                    item["reason"],
                )
        return candidates, PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning(
            "[Provider] Wikimedia race-background search: stale cache used | Year: %s | Round: %s",
            year,
            getattr(race, "round_number", "season"),
        )
        return parse_race_background_candidates(stale, race_or_year, config), "stale-cache"
