"""Strict Flickr adapter for licensed Formula 1 photographs.

Public search uses only the user-supplied API key. OAuth and the API secret are
deliberately unsupported because MetaFusion never accesses private Flickr data.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from extensions.formula1.commons import (
    ALLOWED_MIME_TYPES,
    CommonsCandidate,
    ConstructorData,
    _constructor_aliases,
    _normalize,
)
from extensions.formula1.race_background import (
    BACKGROUND_CANDIDATE_VERSION,
    RaceBackgroundCandidate,
    _active_race_match,
    _contains_terms,
    _year_values,
    derive_race_environment,
)

PROVIDER = "flickr"
LICENSE_PROVIDER = "flickr-licenses"
SEARCH_VERSION = 3
MAX_QUERIES = 11
FLICKR_REQUEST_INTERVAL_SECONDS = 0.25
BACKGROUND_GEO_RADIUS_KM = 32.0
BACKGROUND_GEO_MINIMUM_ACCURACY = 11
BACKGROUND_RACE_WINDOW_DAYS = 7
REJECTED_IDENTITIES = {
    "black and white",
    "diecast",
    "display car",
    "exhibition",
    "formula 2",
    "formula 3",
    "formula e",
    "historic",
    "lego",
    "medical car",
    "miniature",
    "monochrome",
    "museum",
    "replica",
    "roadshow",
    "safety car",
    "show car",
    "showroom",
    "simulator",
    "testing",
    "toy",
}
FORMULA_ONE_TERMS = {"f1", "formula 1", "formula one"}
CAR_TERMS = {"car", "cars", "race car", "racing car", "single seater"}
DRAMATIC_TERMS = {
    "action",
    "battle",
    "corner",
    "dusk",
    "floodlit",
    "grid",
    "motion",
    "night",
    "panning",
    "qualifying",
    "race",
    "racing",
    "rain",
    "spray",
    "sunset",
    "track",
    "under lights",
    "wet",
    "wheel to wheel",
}


class FlickrError(RuntimeError):
    """A sanitized Flickr provider failure that never exposes the API key."""


def _description(photo):
    value = photo.get("description") or ""
    return str(value.get("_content") if isinstance(value, dict) else value)


def _identity(photo):
    return _normalize(
        " ".join(
            str(value or "")
            for value in (
                photo.get("title"),
                _description(photo),
                photo.get("tags"),
                photo.get("machine_tags"),
            )
        )
    )


def _allowed_licences(payload):
    """Resolve reusable licences from Flickr's live licence registry."""
    allowed = {}
    for item in payload.get("licenses", {}).get("license", []):
        licence_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        identity = _normalize(name)
        reusable = (
            identity.startswith("cc0")
            or "public domain" in identity
            or "no known copyright restrictions" in identity
            or identity.startswith("attribution license")
            or identity.startswith("cc by ")
        )
        restricted = any(
            value in identity
            for value in ("noncommercial", "no derivatives", "sharealike", "share alike")
        )
        if licence_id and reusable and not restricted and url.startswith("https://"):
            allowed[licence_id] = {"name": name, "url": url}
    return allowed


async def _flickr_json(session, config, method, parameters):
    endpoint = config["providers"]["flickr_url"]
    params = {
        "method": method,
        "api_key": config["providers"]["flickr_api_key"],
        "format": "json",
        "nojsoncallback": "1",
        **parameters,
    }
    last_error = None
    for attempt in range(config["providers"]["retries"]):
        try:
            await asyncio.sleep(FLICKR_REQUEST_INTERVAL_SECONDS)
            async with session.get(
                endpoint,
                params=params,
                headers={
                    "User-Agent": (
                        "MetaFusion/1.2 (https://github.com/ray-cys/metafusion; "
                        "Flickr API attribution client)"
                    )
                },
            ) as response:
                if response.status == 429:
                    raise FlickrError("rate limited")
                if response.status != 200:
                    raise FlickrError(f"HTTP {response.status}")
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise FlickrError("invalid JSON response")
                if payload.get("stat") == "fail":
                    raise FlickrError(f"API error {payload.get('code', 'unknown')}")
                return payload
        except (OSError, asyncio.TimeoutError, FlickrError, TypeError, ValueError) as error:
            last_error = error
            if attempt + 1 < config["providers"]["retries"]:
                await asyncio.sleep(min(2**attempt, 4))
    # Transport exceptions may include a fully rendered request URL. Never copy
    # that text because Flickr's API key is a query parameter.
    raise FlickrError("Flickr request failed after bounded retries") from last_error


async def _licences(session, state, config, logger):
    cached = state.cache_get(LICENSE_PROVIDER, "current")
    if cached is not None:
        return _allowed_licences(cached), "cache"
    try:
        payload = await _flickr_json(
            session, config, "flickr.photos.licenses.getInfo", {}
        )
        allowed = _allowed_licences(payload)
        if not allowed:
            raise FlickrError("licence registry contained no permitted licences")
        state.cache_put(
            LICENSE_PROVIDER,
            "current",
            payload,
            config["providers"]["flickr_cache_hours"],
        )
        return allowed, PROVIDER
    except FlickrError:
        stale = state.cache_get(LICENSE_PROVIDER, "current", allow_expired=True)
        if stale is None:
            raise
        logger.warning("[Provider] Flickr licence registry: stale cache used")
        return _allowed_licences(stale), "stale-cache"


def _photo_dimensions(photo):
    for suffix in ("o", "l", "c"):
        url = str(photo.get(f"url_{suffix}") or "")
        width = int(photo.get(f"width_{suffix}") or 0)
        height = int(photo.get(f"height_{suffix}") or 0)
        if url and width and height:
            return url, width, height
    return "", 0, 0


def _safe_image_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and (
        parsed.hostname == "live.staticflickr.com"
        or bool(re.fullmatch(r"farm\d+\.staticflickr\.com", parsed.hostname or ""))
    )


def _mime(photo, image_url):
    format_name = str(photo.get("originalformat") or "").casefold()
    if format_name in {"jpg", "jpeg"} or image_url.casefold().endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if format_name == "png" or image_url.casefold().endswith(".png"):
        return "image/png"
    if format_name == "webp" or image_url.casefold().endswith(".webp"):
        return "image/webp"
    return ""


def _page_url(photo):
    owner = str(photo.get("owner") or "").strip()
    photo_id = str(photo.get("id") or "").strip()
    return f"https://www.flickr.com/photos/{owner}/{photo_id}"


def _source_identity(photo, image_url):
    return hashlib.sha256(
        f"flickr:{photo.get('id')}:{photo.get('secret')}:{image_url}".encode()
    ).hexdigest()


def _formula_one(identity):
    padded = f" {identity} "
    return any(f" {term} " in padded for term in FORMULA_ONE_TERMS)


def _car_subject(identity):
    padded = f" {identity} "
    return any(f" {term} " in padded for term in CAR_TERMS)


def _dramatic_score(identity):
    return sum(1 for term in DRAMATIC_TERMS if term in identity)


def _capture_year(photo):
    """Return Flickr's provider-supplied capture year when it is plausible."""
    match = re.match(r"\s*(19\d{2}|20\d{2}|21\d{2})\b", str(photo.get("datetaken") or ""))
    return int(match.group(1)) if match else None


def _capture_date(photo):
    """Parse Flickr's provider-supplied taken date without trusting upload time."""
    raw = str(photo.get("datetaken") or "").strip()
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _race_date(race):
    try:
        return date.fromisoformat(str(race.race_date))
    except (TypeError, ValueError):
        return None


def _coordinate(photo, name):
    try:
        return float(photo.get(name))
    except (TypeError, ValueError):
        return None


def _distance_km(first_latitude, first_longitude, second_latitude, second_longitude):
    """Return great-circle distance for independently supplied provider coordinates."""
    radius = 6371.0088
    first_latitude, second_latitude = map(math.radians, (first_latitude, second_latitude))
    latitude_delta = second_latitude - first_latitude
    longitude_delta = math.radians(second_longitude - first_longitude)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return radius * 2 * math.asin(min(1.0, math.sqrt(value)))


def _identity_match(identity, value):
    terms = tuple(
        term for term in _normalize(value).split() if len(term) > 3
    )
    return _contains_terms(identity, terms)


def _composite_background_evidence(photo, race, identity):
    """Combine independent weak context without relaxing subject or licence safety."""
    taken = _capture_date(photo)
    scheduled = _race_date(race)
    capture_season = bool(taken and taken.year == int(race.year))
    race_week = bool(
        taken
        and scheduled
        and abs((taken - scheduled).days) <= BACKGROUND_RACE_WINDOW_DAYS
    )
    locality_match = _identity_match(identity, race.locality)
    country_match = _identity_match(identity, race.country)
    latitude = _coordinate(photo, "latitude")
    longitude = _coordinate(photo, "longitude")
    try:
        accuracy = int(photo.get("accuracy") or 0)
    except (TypeError, ValueError):
        accuracy = 0
    distance = None
    if (
        latitude is not None
        and longitude is not None
        and race.latitude is not None
        and race.longitude is not None
    ):
        distance = _distance_km(
            float(race.latitude),
            float(race.longitude),
            latitude,
            longitude,
        )
    geo_near = bool(
        distance is not None
        and distance <= BACKGROUND_GEO_RADIUS_KM
        and accuracy >= BACKGROUND_GEO_MINIMUM_ACCURACY
    )
    score = (
        (2 if capture_season else 0)
        + (3 if race_week else 0)
        + (4 if geo_near else 0)
        + (2 if locality_match else 0)
        + (1 if country_match else 0)
    )
    accepted = capture_season and race_week and (geo_near or locality_match) and score >= 7
    evidence = tuple(
        name
        for name, matched in (
            ("capture-season", capture_season),
            ("race-week-capture", race_week),
            ("geotag-near-circuit", geo_near),
            ("locality", locality_match),
            ("country", country_match),
            ("composite-event-context", accepted),
        )
        if matched
    )
    return accepted, evidence, score


def _common_photo_fields(photo, licences):
    licence = licences.get(str(photo.get("license") or ""))
    image_url, width, height = _photo_dimensions(photo)
    mime = _mime(photo, image_url)
    title = str(photo.get("title") or "").strip()
    owner = str(photo.get("ownername") or photo.get("owner") or "").strip()
    if (
        not licence
        or not _safe_image_url(image_url)
        or mime not in ALLOWED_MIME_TYPES
        or not title
        or not owner
    ):
        return None
    return {
        "title": title,
        "page_url": _page_url(photo),
        "image_url": image_url,
        "width": width,
        "height": height,
        "mime": mime,
        "source_sha1": _source_identity(photo, image_url),
        "author": owner,
        "licence": licence["name"],
        "licence_url": licence["url"],
    }


def parse_flickr_team_candidates(payload, year, constructor, roster, config, licences):
    """Accept only current-year, team-specific Formula 1 action photographs."""
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
    for photo in payload.get("photos", {}).get("photo", []):
        identity = _identity(photo)
        common = _common_photo_fields(photo, licences)
        if common is None:
            continue
        width, height = common["width"], common["height"]
        if (
            width < minimum_width
            or height < minimum_height
            or not 1.45 <= width / max(height, 1) <= 2.30
            or int(year) not in _year_values(identity)
            or not _formula_one(identity)
            or not _car_subject(identity)
            or not any(alias in identity for alias in aliases)
            or any(alias in identity for alias in other_aliases)
            or any(term in identity for term in REJECTED_IDENTITIES)
        ):
            continue
        score = min(width * height / 1_000_000, 35)
        score += max(0, 8 - abs(width / height - 16 / 9) * 12)
        score += _dramatic_score(identity) * 3
        score += min(int(photo.get("views") or 0) / 10_000, 5)
        candidates.append(
            CommonsCandidate(
                page_id=int(photo["id"]),
                **common,
                constructor_id=constructor.constructor_id,
                constructor_name=constructor.name,
                score=round(score, 4),
                provider=PROVIDER,
            )
        )
    unique = {candidate.page_id: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda value: (-value.score, value.title.casefold()))


def parse_flickr_background_candidates(payload, race, config, licences, diagnostics=None):
    """Accept exact-event/circuit Formula 1 action with atmosphere evidence."""
    environment = derive_race_environment(race)
    minimum_width = config["show_artwork"]["fallback_background_source_width"]
    minimum_height = config["show_artwork"]["fallback_background_source_height"]
    preferred_width = config["show_artwork"]["minimum_background_source_width"]
    preferred_height = config["show_artwork"]["minimum_background_source_height"]
    candidates = []
    for photo in payload.get("photos", {}).get("photo", []):
        identity = _identity(photo)
        common = _common_photo_fields(photo, licences)
        reason = None
        if common is None:
            reason = "unsafe-url-identity-or-licence"
        else:
            width, height = common["width"], common["height"]
            identity_years = _year_values(identity)
            capture_year = _capture_year(photo)
            years = identity_years | ({capture_year} if capture_year is not None else set())
            event_match = _contains_terms(identity, environment.event_terms)
            circuit_match = _contains_terms(identity, environment.circuit_terms)
            exact_year = int(race.year) in years
            recent_year = any(0 <= int(race.year) - year <= 3 for year in years)
            active = _active_race_match(identity) or _dramatic_score(identity) >= 2
            composite_match, composite_evidence, composite_score = (
                _composite_background_evidence(photo, race, identity)
            )
            strict_match = (exact_year and (event_match or circuit_match)) or (
                recent_year and circuit_match
            )
            if width < minimum_width or height < minimum_height:
                reason = "undersized"
            elif not 1.45 <= width / max(height, 1) <= 2.30:
                reason = "incompatible-aspect-ratio"
            elif not _formula_one(identity) or not _car_subject(identity) or not active:
                reason = "not-formula-one-race-action"
            elif any(term in identity for term in REJECTED_IDENTITIES):
                reason = "rejected-subject-or-series"
            elif not (strict_match or composite_match):
                reason = "insufficient-event-context"
        if reason:
            if diagnostics is not None:
                diagnostics.append({"title": str(photo.get("title") or "unknown"), "reason": reason})
            continue
        if exact_year and (event_match or circuit_match):
            tier, tier_score = "exact_event_action_race_car", 540
        elif composite_match:
            tier, tier_score = "composite_event_action_race_car", 515
        else:
            tier, tier_score = "recent_circuit_action_race_car", 500
        scene_match = any(term in identity for term in environment.scene_terms)
        evidence = tuple(
            name
            for name, matched in (
                ("event", event_match),
                ("circuit", circuit_match),
                ("active-race", active),
                ("season", int(race.year) in identity_years),
                ("capture-season", capture_year == int(race.year)),
                ("recent-season", recent_year and not exact_year),
                ("environment", scene_match),
                ("dramatic-action", _dramatic_score(identity) >= 2),
            )
            if matched
        )
        evidence = tuple(dict.fromkeys((*evidence, *composite_evidence)))
        evidence = (*evidence, "4k-source" if width >= preferred_width and height >= preferred_height else "fallback-resolution-source")
        score = tier_score + min(width * height / 1_000_000, 35)
        score += _dramatic_score(identity) * 5 + (15 if scene_match else 0)
        score += composite_score
        candidates.append(
            RaceBackgroundCandidate(
                page_id=int(photo["id"]),
                **common,
                vehicle_name="Formula 1 race action",
                score=round(score, 4),
                subject_type="race_car",
                match_tier=tier,
                environment=environment.mode,
                race_key=environment.race_key,
                evidence=evidence,
                eligibility_version=BACKGROUND_CANDIDATE_VERSION,
                provider=PROVIDER,
            )
        )
    unique = {candidate.page_id: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda value: (-value.score, value.title.casefold()))


def _team_queries(year, constructor):
    team = constructor.name.replace("F1 Team", "").strip()
    return tuple(
        dict.fromkeys(
            (
                f'Formula 1 {year} {team} race car on track',
                f'F1 {year} {team} racing action',
                f'{team} Formula One {year} qualifying',
                f'{team} F1 {year} panning race car',
                f'{team} {year} Grand Prix car',
            )
        )
    )


def _background_queries(race):
    environment = derive_race_environment(race)
    scene = " ".join(environment.scene_terms[:2])
    event = str(race.name).replace('"', "")
    circuit = str(race.circuit).replace('"', "")
    locality = str(race.locality).replace('"', "")
    year = int(race.year)
    return tuple(
        dict.fromkeys(
            (
                f'Formula 1 {year} {event} race cars action {scene}',
                f'F1 {year} {event} wheel to wheel racing',
                f'Formula One {year} {circuit} race action {scene}',
                f'F1 {year} {circuit} qualifying cars',
                f'Formula 1 {year} {locality} race cars track atmosphere',
                f'{event} {year} F1 panning motion',
                f'{circuit} Formula 1 racing rain spray night lights',
                f'{event} Formula One track action',
                f'{circuit} F1 race cars',
            )
        )
    )[:MAX_QUERIES]


def _background_searches(race):
    """Add bounded race-week geo discovery without making geo a sole identity proof."""
    searches: list[str | dict[str, str]] = list(_background_queries(race))
    scheduled = _race_date(race)
    if scheduled is None or race.latitude is None or race.longitude is None:
        return tuple(searches)
    window_start = scheduled - timedelta(days=BACKGROUND_RACE_WINDOW_DAYS)
    window_end = scheduled + timedelta(days=BACKGROUND_RACE_WINDOW_DAYS)
    shared = {
        "lat": str(float(race.latitude)),
        "lon": str(float(race.longitude)),
        "radius": str(BACKGROUND_GEO_RADIUS_KM),
        "radius_units": "km",
        "min_taken_date": f"{window_start.isoformat()} 00:00:00",
        "max_taken_date": f"{window_end.isoformat()} 23:59:59",
    }
    searches.extend(
        (
            {**shared, "text": "Formula 1 race car racing action"},
            {**shared, "text": "F1 cars qualifying race track"},
        )
    )
    return tuple(searches[:MAX_QUERIES])


async def _search(
    session,
    state,
    config,
    key,
    queries,
    licences,
    parser,
    logger,
    *,
    refresh=False,
):
    cached = None if refresh else state.cache_get(PROVIDER, key)
    if cached is not None:
        return parser(cached), "cache"
    payload: dict[str, dict[str, list[dict]]] = {"photos": {"photo": []}}
    seen = set()
    try:
        for query in queries:
            search_parameters = (
                {"text": query} if isinstance(query, str) else dict(query)
            )
            response = await _flickr_json(
                session,
                config,
                "flickr.photos.search",
                {
                    **search_parameters,
                    "license": ",".join(sorted(licences)),
                    "sort": "interestingness-desc",
                    "safe_search": "1",
                    "content_types": "0",
                    "media": "photos",
                    "per_page": "100",
                    "page": "1",
                    "extras": (
                        "description,license,date_taken,owner_name,original_format,"
                        "o_dims,views,geo,tags,machine_tags,url_o,url_l,url_c"
                    ),
                },
            )
            for photo in response.get("photos", {}).get("photo", []):
                photo_id = str(photo.get("id") or "")
                if photo_id and photo_id not in seen:
                    seen.add(photo_id)
                    payload["photos"]["photo"].append(photo)
        state.cache_put(PROVIDER, key, payload, config["providers"]["flickr_cache_hours"])
        return parser(payload), PROVIDER
    except FlickrError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning("[Provider] Flickr search: stale cache used")
        return parser(stale), "stale-cache"


async def search_flickr_team_photos(
    session, state, config, year, constructor: ConstructorData, roster, logger
):
    if not config["providers"].get("flickr_enabled"):
        return [], "disabled"
    licences, licence_source = await _licences(session, state, config, logger)
    key = f"team:v{SEARCH_VERSION}:{int(year)}:{constructor.constructor_id}"
    parser = lambda payload: parse_flickr_team_candidates(
        payload, year, constructor, roster, config, licences
    )
    candidates, source = await _search(
        session, state, config, key, _team_queries(year, constructor), licences, parser, logger
    )
    return candidates, f"{source};licences={licence_source}"


async def search_flickr_backgrounds(
    session, state, config, race, logger, *, refresh=False
):
    if not config["providers"].get("flickr_enabled"):
        return [], "disabled"
    licences, licence_source = await _licences(session, state, config, logger)
    environment = derive_race_environment(race)
    key = f"background:v{SEARCH_VERSION}:{environment.race_key}"
    diagnostics: list[dict[str, str]] = []
    parser = lambda payload: parse_flickr_background_candidates(
        payload, race, config, licences, diagnostics
    )
    candidates, source = await _search(
        session,
        state,
        config,
        key,
        _background_searches(race),
        licences,
        parser,
        logger,
        refresh=refresh,
    )
    if diagnostics:
        counts = Counter(item["reason"] for item in diagnostics)
        logger.debug(
            "[Provider] Flickr background candidates | Year: %s | Round: %s | "
            "Accepted: %s | Rejected: %s | Reasons: %s",
            race.year,
            race.round_number,
            len(candidates),
            len(diagnostics),
            ", ".join(f"{name}={count}" for name, count in sorted(counts.items())),
        )
    return candidates, f"{source};licences={licence_source}"
