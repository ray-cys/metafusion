"""Official Formula 1 event-fact discovery and validation."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, replace
from typing import TypedDict

from extensions.formula1.provider import _get

PROVIDER = "formula1.com"
FACT_CACHE_VERSION = 2

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
    first_grand_prix_year: int | None = None
    circuit_profile: str | None = None
    circuit_history: str | None = None


class FactStatistics(TypedDict):
    resolved: int
    missing: int
    stale: int
    canonicalized: int
    profiles_resolved: int
    profiles_missing: int
    issues: list[str]


PROFILE_RULES = (
    ("a street circuit", (r"\bstreet circuit\b", r"\bpublic roads?\b")),
    ("a semi-permanent layout", (r"\bsemi[- ]permanent\b",)),
    ("a temporary layout", (r"\btemporary (?:facility|circuit|layout)\b",)),
    ("a permanent circuit", (r"\bpermanent circuit\b", r"\bpurpose[- ]built\b")),
    ("fast", (r"\bfast(?:er|est)?\b", r"\bhigh[- ]speed\b")),
    ("technical", (r"\btechnical\b",)),
    ("stop-start", (r"\bstop[- ]start\b",)),
    ("flowing", (r"\bflowing\b",)),
    ("narrow", (r"\bnarrow\b",)),
    ("bumpy", (r"\bbump(?:y|s)\b",)),
    ("undulating", (r"\bundulat(?:ing|ion)\b", r"\belevation change(?:s)?\b")),
    ("low-downforce", (r"\blow[- ]downforce\b",)),
    ("high-downforce", (r"\bhigh[- ]downforce\b",)),
    ("heavy-braking zones", (r"\bheavy[- ]braking\b", r"\bbig braking\b")),
    ("a wide layout", (r"\bwide layout\b", r"\bwide track\b")),
    ("long straights", (r"\blong straights?\b",)),
    ("high-speed corners", (r"\bhigh[- ]speed corners?\b",)),
    ("slow corners", (r"\bslow corners?\b", r"\blow[- ]speed corners?\b")),
    ("sweeping corners", (r"\bsweep(?:ing|ers?)\b",)),
    ("chicanes", (r"\bchicanes?\b",)),
    ("a hairpin", (r"\bhairpin\b",)),
    ("S-curves", (r"\bs['’]?[- ]?curves?\b",)),
    ("a crossover", (r"\bcrossover\b", r"\bfigure[- ]eight\b")),
    ("multiple racing lines", (r"\bmultiple racing lines?\b",)),
    ("a slippery surface", (r"\bslippery\b",)),
)


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
    return html.unescape(match.group(1)).strip() if match else None


def _visible_number(document, label):
    pattern = rf"{re.escape(label)}.*?</dt>\s*<dd[^>]*>(.*?)</dd>"
    match = re.search(pattern, document, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    number = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    return number.group(0) if number else None


def _decode_json_text(value):
    try:
        decoded = json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        decoded = value
    decoded = html.unescape(str(decoded))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", decoded)).strip()


def parse_editorial_sections(document):
    """Return Formula1.com FAQ content keyed by normalized heading."""
    normalized_document = document.replace(r'\"', '"')
    pattern = re.compile(
        r'"heading"\s*:\s*"(?P<heading>(?:\\.|[^"\\])*)"\s*,\s*'
        r'"content"\s*:\s*"(?P<content>(?:\\.|[^"\\])*)"',
        flags=re.IGNORECASE,
    )
    sections: dict[str, str] = {}
    for match in pattern.finditer(normalized_document):
        heading = _decode_json_text(match.group("heading"))
        content = _decode_json_text(match.group("content"))
        key = _slug(heading)
        if key and content:
            sections.setdefault(key, content)
    return sections


def _first_grand_prix_year(sections):
    text = sections.get("when-was-its-first-grand-prix") or sections.get(
        "when-was-the-first-grand-prix"
    )
    if not text:
        return None
    targeted = (
        r"\bfirst (?:hosted|held|staged)[^.]{0,160}\b((?:19|20)\d{2})\b",
        r"\bfirst (?:race|grand prix)[^.]{0,160}\b((?:19|20)\d{2})\b",
        r"\b((?:19|20)\d{2})\b[^.]{0,160}\bfirst (?:hosted|held|staged|grand prix)\b",
    )
    for pattern in targeted:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    years = [int(value) for value in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    return years[-1] if years else None


def _circuit_profile(sections):
    text = sections.get("what-s-the-circuit-like") or sections.get("whats-the-circuit-like")
    if not text:
        return None
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold()
    labels = [
        label
        for label, patterns in PROFILE_RULES
        if any(re.search(pattern, normalized) for pattern in patterns)
    ]
    if not labels:
        return None
    return ", ".join(labels[:8])


def _circuit_history(sections):
    text = next(
        (
            value
            for key, value in sections.items()
            if key.startswith("when-was-") and key.endswith("-built")
        ),
        None,
    )
    if not text:
        return None
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    milestone = None
    patterns = (
        (r"\b(?:officially )?opened in ((?:19|20)\d{2})\b", "opened in {}"),
        (r"\b(?:was )?built in ((?:19|20)\d{2})\b", "was built in {}"),
        (r"\bconstruction began in ((?:19|20)\d{2})\b", "saw construction begin in {}"),
        (r"^\s*in ((?:19|20)\d{2})\b", "dates to {}"),
    )
    for pattern, template in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            milestone = template.format(match.group(1))
            break
    folded = normalized.casefold()
    origin = None
    if "test track" in folded:
        origin = "originated as a manufacturer test track"
    elif "man-made" in folded and "island" in folded and "roads" in folded:
        origin = "was created from roads on a man-made island"
    elif re.search(r"\b(?:existing|public) roads?\b", folded):
        origin = "was created from existing roads"
    elif "airfield" in folded:
        origin = "was developed from an airfield"
    elif "purpose-built" in folded:
        origin = "was developed as a purpose-built venue"
    if milestone and origin:
        return f"The circuit {milestone} and {origin}."
    if milestone:
        return f"The circuit {milestone}."
    if origin:
        return f"The circuit {origin}."
    return None


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
    sections = parse_editorial_sections(document)
    return CircuitFacts(
        circuit_length_km=length,
        lap_count=lap_count,
        race_distance_km=distance,
        circuit=_structured_value(document, "circuitOfficialName"),
        locality=(
            _structured_value(document, "meetingLocation")
            or _structured_value(document, "circuitLocation")
        ),
        first_grand_prix_year=_first_grand_prix_year(sections),
        circuit_profile=_circuit_profile(sections),
        circuit_history=_circuit_history(sections),
    )


def _cached_facts(payload):
    """Return only a complete, plausible cached fact record."""
    if not isinstance(payload, dict):
        return None
    try:
        facts = CircuitFacts(**payload)
    except (TypeError, ValueError):
        return None
    length = _number(facts.circuit_length_km)
    laps = _number(facts.lap_count)
    distance = _number(facts.race_distance_km)
    if length is None or laps is None or distance is None or laps != int(laps):
        return None
    if not 2 <= length <= 10 or not 20 <= int(laps) <= 100:
        return None
    if not 150 <= distance <= 400 or abs(length * int(laps) - distance) > 15:
        return None
    return replace(
        facts,
        circuit_length_km=length,
        lap_count=int(laps),
        race_distance_km=distance,
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


def _usable_official_text(value):
    text = str(value or "").strip()
    return text if _slug(text) not in {"", "tbc", "unknown", "to-be-confirmed"} else None


def _canonical_venue(race, facts):
    """Promote a validated official venue name without reducing location precision."""
    circuit = _usable_official_text(facts.circuit) or race.circuit
    official_locality = _usable_official_text(facts.locality)
    locality = race.locality
    if official_locality and _slug(official_locality) != _slug(race.country):
        locality = official_locality
    return circuit, locality


async def _load_official_facts(session, state, config, race, event_slug):
    key = f"facts:v{FACT_CACHE_VERSION}:{race.year}:{event_slug}"
    cached = _cached_facts(state.cache_get(PROVIDER, key))
    if cached is not None:
        return cached, "cache"
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
        stale = _cached_facts(state.cache_get(PROVIDER, key, allow_expired=True))
        if stale is not None:
            return stale, "stale-cache"
        # Older valid records remain a safe outage fallback, but are never used
        # as the normal cache hit after the parser contract changes.
        legacy_key = f"facts:{race.year}:{event_slug}"
        legacy = _cached_facts(
            state.cache_get(PROVIDER, legacy_key, allow_expired=True)
        )
        if legacy is not None:
            return legacy, "legacy-stale-cache"
        raise


async def enrich_race_facts(session, state, config, races, round_numbers, logger):
    """Enrich only rounds present in Plex, preserving partial output on outages."""
    statistics: FactStatistics = {
        "resolved": 0,
        "missing": 0,
        "stale": 0,
        "canonicalized": 0,
        "profiles_resolved": 0,
        "profiles_missing": 0,
        "issues": [],
    }
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
        statistics["stale"] += int(source.endswith("stale-cache"))
        profile_available = bool(
            facts.first_grand_prix_year or facts.circuit_profile or facts.circuit_history
        )
        statistics["profiles_resolved"] += int(profile_available)
        statistics["profiles_missing"] += int(not profile_available)
        if not profile_available:
            statistics["issues"].append(
                f"Official circuit profile unavailable: {race.year} round "
                f"{race.round_number} {race.name}"
            )
        circuit, locality = _canonical_venue(race, facts)
        identity_changed = circuit != race.circuit or locality != race.locality
        statistics["canonicalized"] += int(identity_changed)
        enriched.append(
            replace(
                race,
                circuit=circuit,
                locality=locality,
                circuit_length_km=facts.circuit_length_km,
                lap_count=facts.lap_count,
                race_distance_km=facts.race_distance_km,
                first_grand_prix_year=facts.first_grand_prix_year,
                circuit_profile=facts.circuit_profile,
                circuit_history=facts.circuit_history,
            )
        )
        logger.info(
            "[Provider] Circuit facts | Year: %s | Round: %02d | Source: %s | "
            "Circuit length: %.3f km | Laps: %d | Race distance: %.3f km | "
            "Venue identity: %s | Circuit profile: %s",
            race.year,
            race.round_number,
            source,
            facts.circuit_length_km,
            facts.lap_count,
            facts.race_distance_km,
            "canonicalized" if identity_changed else "unchanged",
            "resolved" if profile_available else "unavailable",
        )
    return enriched, statistics
