"""Official Formula 1 event-fact discovery and validation."""

import html
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import TypedDict

from extensions.formula1.provider import _get

PROVIDER = "formula1.com"

EVENT_SLUGS = {
    "albert_park": "australia",
    "americas": "united-states",
    "bahrain": "bahrain",
    "baku": "azerbaijan",
    "catalunya": "barcelona-catalunya",
    "hungaroring": "hungary",
    "interlagos": "brazil",
    "jeddah": "saudi-arabia",
    "las_vegas": "las-vegas",
    "losail": "qatar",
    "marina_bay": "singapore",
    "miami": "miami",
    "monaco": "monaco",
    "monza": "italy",
    "red_bull_ring": "austria",
    "rodriguez": "mexico",
    "shanghai": "china",
    "silverstone": "great-britain",
    "spa": "belgium",
    "suzuka": "japan",
    "villeneuve": "canada",
    "yas_marina": "united-arab-emirates",
    "zandvoort": "netherlands",
}

COUNTRY_ALIASES = {
    "uk": "great-britain",
    "united-kingdom": "great-britain",
    "usa": "united-states",
    "us": "united-states",
    "uae": "united-arab-emirates",
}


@dataclass(frozen=True)
class CircuitFacts:
    circuit_length_km: float
    lap_count: int
    race_distance_km: float
    circuit: str | None = None
    locality: str | None = None


class FactStatistics(TypedDict):
    resolved: int
    missing: int
    stale: int
    issues: list[str]


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def parse_event_slugs(document, year):
    pattern = rf'href=["\']/en/racing/{int(year)}/([^"\'/?#]+)'
    values = re.findall(pattern, document, flags=re.IGNORECASE)
    return list(dict.fromkeys(value for value in values if not value.startswith("pre-season")))


def _structured_value(document, key):
    pattern = rf'(?:\\?["\']){re.escape(key)}(?:\\?["\'])\s*:\s*(?:\\?["\'])([^\\"\']+)'
    match = re.search(pattern, document, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _visible_number(document, label):
    pattern = rf"{re.escape(label)}.*?</dt>\s*<dd[^>]*>(.*?)</dd>"
    match = re.search(pattern, document, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    number = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    return number.group(0) if number else None


def parse_official_facts(document):
    """Extract and cross-check scheduled race facts from an event page."""
    length = _number(_structured_value(document, "trackLength"))
    laps = _number(_structured_value(document, "scheduledLapCount"))
    distance = _number(_structured_value(document, "scheduledDistance"))
    length = length if length is not None else _number(_visible_number(document, "Circuit Length"))
    laps = laps if laps is not None else _number(_visible_number(document, "Number of Laps"))
    distance = (
        distance if distance is not None else _number(_visible_number(document, "Race Distance"))
    )
    if length is None or laps is None or distance is None:
        raise RuntimeError("official event page omitted one or more required circuit facts")
    lap_count = int(laps)
    if not 2 <= length <= 10 or laps != lap_count or not 20 <= lap_count <= 100:
        raise RuntimeError("official event page returned implausible circuit facts")
    if not 150 <= distance <= 400 or abs(length * lap_count - distance) > 15:
        raise RuntimeError("official event page returned inconsistent circuit facts")
    return CircuitFacts(
        circuit_length_km=length,
        lap_count=lap_count,
        race_distance_km=distance,
        circuit=_structured_value(document, "circuitOfficialName"),
        locality=(
            _structured_value(document, "circuitLocation")
            or _structured_value(document, "meetingLocation")
        ),
    )


async def _load_event_slugs(session, state, config, year, logger):
    key = f"events:{int(year)}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return list(cached.get("slugs", [])), "cache"
    url = f"{config['providers']['formula1_url']}/{int(year)}"
    try:
        document = await _get(
            session,
            url,
            retries=config["providers"]["retries"],
            json_response=False,
        )
        slugs = parse_event_slugs(document, year)
        if not slugs:
            raise RuntimeError("official calendar contained no event links")
        state.cache_put(
            PROVIDER,
            key,
            {"slugs": slugs},
            config["providers"]["cache_hours"],
        )
        return slugs, PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is not None:
            logger.warning("[Provider] Official calendar: stale cache used | Year: %s", year)
            return list(stale.get("slugs", [])), "stale-cache"
        logger.warning("[Provider] Official calendar unavailable | Year: %s", year)
        return [], "unavailable"


def _select_event_slug(race, available):
    available = list(dict.fromkeys(available))
    known = EVENT_SLUGS.get(race.circuit_id)
    if known and (not available or known in available):
        return known
    country = COUNTRY_ALIASES.get(_slug(race.country), _slug(race.country))
    exact = {
        country,
        _slug(race.locality),
        _slug(race.circuit_id),
        re.sub(r"-(?:grand-prix|gp)$", "", _slug(race.name)),
    }
    matches = [candidate for candidate in available if candidate in exact]
    if len(matches) == 1:
        return matches[0]
    identity_tokens = set(
        _slug(" ".join((race.name, race.circuit, race.locality, race.country))).split("-")
    ) - {"grand", "prix", "circuit", "international", "autodrome"}
    scored = []
    for candidate in available:
        tokens = set(candidate.split("-"))
        score = len(tokens & identity_tokens) / max(len(tokens), 1)
        if score >= 0.75:
            scored.append((score, candidate))
    scored.sort(reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


def _identity_matches(race, facts):
    """Reject a redirected or misbound page when it exposes venue identity."""
    if facts.locality and _slug(facts.locality) == _slug(race.locality):
        return True
    if facts.circuit:
        official = set(_slug(facts.circuit).split("-")) - {"circuit", "international"}
        expected = set(_slug(race.circuit).split("-")) - {"circuit", "international"}
        if official and len(official & expected) / len(official) >= 0.5:
            return True
    return not facts.locality and not facts.circuit


async def _load_official_facts(session, state, config, race, event_slug):
    key = f"facts:{race.year}:{event_slug}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return CircuitFacts(**cached), "cache"
    url = f"{config['providers']['formula1_url']}/{race.year}/{event_slug}"
    try:
        document = await _get(
            session,
            url,
            retries=config["providers"]["retries"],
            json_response=False,
        )
        facts = parse_official_facts(document)
        if not _identity_matches(race, facts):
            raise RuntimeError("official event page identity did not match the scheduled venue")
        state.cache_put(
            PROVIDER,
            key,
            asdict(facts),
            config["providers"]["cache_hours"],
        )
        return facts, PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        return CircuitFacts(**stale), "stale-cache"


async def enrich_race_facts(session, state, config, races, round_numbers, logger):
    """Enrich only rounds present in Plex, preserving partial output on outages."""
    statistics: FactStatistics = {"resolved": 0, "missing": 0, "stale": 0, "issues": []}
    if not races:
        return [], statistics
    event_slugs, _calendar_source = await _load_event_slugs(
        session, state, config, races[0].year, logger
    )
    selected = set(round_numbers)
    enriched = []
    for race in races:
        if race.round_number not in selected:
            enriched.append(race)
            continue
        event_slug = _select_event_slug(race, event_slugs)
        if not event_slug:
            statistics["missing"] += 1
            statistics["issues"].append(
                f"Official fact identity unavailable: {race.year} round {race.round_number} {race.name}"
            )
            enriched.append(race)
            continue
        try:
            facts, source = await _load_official_facts(session, state, config, race, event_slug)
        except RuntimeError as error:
            statistics["missing"] += 1
            statistics["issues"].append(
                f"Official circuit facts unavailable: {race.year} round {race.round_number} "
                f"{race.name} ({error})"
            )
            logger.warning(
                "[Provider] Official circuit facts unavailable | Year: %s | Round: %02d",
                race.year,
                race.round_number,
            )
            enriched.append(race)
            continue
        statistics["resolved"] += 1
        statistics["stale"] += int(source == "stale-cache")
        enriched.append(
            replace(
                race,
                circuit_length_km=facts.circuit_length_km,
                lap_count=facts.lap_count,
                race_distance_km=facts.race_distance_km,
            )
        )
        logger.info(
            "[Provider] Circuit facts | Year: %s | Round: %02d | Source: %s",
            race.year,
            race.round_number,
            source,
        )
    return enriched, statistics
