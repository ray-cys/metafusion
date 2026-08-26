"""Schedule, circuit facts, and open circuit-shape provider adapter."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field

PROVIDER = "jolpica"
SHAPE_PROVIDER = "f1-circuits-svg"

CIRCUIT_SLUGS = {
    "albert_park": "melbourne-2",
    "americas": "austin-1",
    "bahrain": "bahrain-1",
    "baku": "baku-1",
    "catalunya": "catalunya-6",
    "hungaroring": "hungaroring-3",
    "interlagos": "interlagos-2",
    "jeddah": "jeddah-1",
    "las_vegas": "las-vegas-1",
    "losail": "lusail-1",
    "marina_bay": "marina-bay-4",
    "miami": "miami-1",
    "monaco": "monaco-6",
    "monza": "monza-7",
    "red_bull_ring": "spielberg-3",
    "rodriguez": "mexico-city-3",
    "shanghai": "shanghai-1",
    "silverstone": "silverstone-8",
    "spa": "spa-francorchamps-4",
    "suzuka": "suzuka-2",
    "villeneuve": "montreal-6",
    "yas_marina": "yas-marina-2",
    "zandvoort": "zandvoort-5",
}


@dataclass(frozen=True)
class RaceData:
    year: int
    round_number: int
    name: str
    circuit_id: str
    circuit: str
    locality: str
    country: str
    race_date: str | None
    sprint_date: str | None
    latitude: float | None
    longitude: float | None
    circuit_length_km: float | None = None
    lap_count: int | None = None
    race_distance_km: float | None = None
    first_grand_prix_year: int | None = None
    circuit_profile: str | None = None
    circuit_history: str | None = None
    session_dates: dict[str, str] = field(default_factory=dict)
    race_time_utc: str | None = None

    @property
    def sprint(self):
        return bool(self.sprint_date)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_schedule(payload, year):
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    parsed = []
    for race in races:
        circuit = race.get("Circuit") or {}
        location = circuit.get("Location") or {}
        try:
            round_number = int(race["round"])
        except (KeyError, TypeError, ValueError):
            continue
        session_dates = {
            str(name): str(value["date"])
            for name, value in race.items()
            if name != "Circuit" and isinstance(value, dict) and value.get("date")
        }
        parsed.append(
            RaceData(
                year=int(year),
                round_number=round_number,
                name=str(race.get("raceName") or f"Round {round_number}"),
                circuit_id=str(circuit.get("circuitId") or ""),
                circuit=str(circuit.get("circuitName") or "Unknown circuit"),
                locality=str(location.get("locality") or "Unknown locality"),
                country=str(location.get("country") or "Unknown country"),
                race_date=race.get("date"),
                sprint_date=(race.get("Sprint") or {}).get("date"),
                latitude=_number(location.get("lat")),
                longitude=_number(location.get("long")),
                session_dates=session_dates,
                race_time_utc=(str(race.get("time")) if race.get("time") else None),
            )
        )
    return sorted(parsed, key=lambda item: item.round_number)


def _valid_year(year):
    """Accept plausible championship years; provider existence is authoritative."""
    return 1950 <= int(year) <= 2200


async def _response_json(response):
    if getattr(response, "status", 0) != 200:
        raise RuntimeError(f"provider returned HTTP {getattr(response, 'status', 'unknown')}")
    payload = await response.json(content_type=None)
    if not isinstance(payload, dict):
        raise TypeError("provider returned an invalid JSON document")
    return payload


async def _get(session, url, *, retries, json_response=True):
    last_error = None
    for attempt in range(int(retries)):
        try:
            async with session.get(
                url,
                headers={"User-Agent": "MetaFusion-Formula1/1.0"},
            ) as response:
                if json_response:
                    return await _response_json(response)
                if response.status != 200:
                    raise RuntimeError(f"provider returned HTTP {response.status}")
                text = await response.text()
                if len(text) > 1_000_000:
                    raise RuntimeError("provider response exceeded the safety limit")
                return text
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            last_error = error
            if attempt + 1 < int(retries):
                await asyncio.sleep(0)
    raise RuntimeError(f"provider request failed: {last_error}") from last_error


async def load_schedule(session, state, config, year, logger):
    """Return fresh data, with an explicitly logged stale-cache fallback."""
    if not _valid_year(year):
        raise RuntimeError(f"unsupported Formula 1 season year: {year}")
    key = f"schedule:{int(year)}"
    cached = state.cache_get(PROVIDER, key)
    if cached is not None:
        return parse_schedule(cached, year), "cache"
    url = f"{config['providers']['jolpica_url']}/{int(year)}.json"
    try:
        payload = await _get(session, url, retries=config["providers"]["retries"])
        races = parse_schedule(payload, year)
        if not races:
            raise RuntimeError("schedule contained no valid races")
        state.cache_put(PROVIDER, key, payload, config["providers"]["cache_hours"])
        return races, PROVIDER
    except RuntimeError:
        stale = state.cache_get(PROVIDER, key, allow_expired=True)
        if stale is None:
            raise
        logger.warning("[Provider] Schedule: stale cache used | Year: %s", year)
        return parse_schedule(stale, year), "stale-cache"


def _extract_path(svg):
    match = re.search(r'<path\s+d="([^"]+)"', svg, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _shape_key(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def _manifest_names(payload):
    if not isinstance(payload, list):
        return []
    return [
        str(item.get("name")) for item in payload if isinstance(item, dict) and item.get("name")
    ]


async def _load_shape_manifest(session, state, config):
    cached = state.cache_get(SHAPE_PROVIDER, "manifest")
    if cached is not None:
        return list(cached.get("names", []))
    retries = config["providers"]["retries"]
    last_error = None
    for attempt in range(retries):
        try:
            async with session.get(
                config["providers"]["circuit_manifest_url"],
                headers={"User-Agent": "MetaFusion-Formula1/1.1"},
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"provider returned HTTP {response.status}")
                names = _manifest_names(await response.json(content_type=None))
                if not names:
                    raise RuntimeError("circuit manifest contained no SVG files")
                state.cache_put(
                    SHAPE_PROVIDER,
                    "manifest",
                    {"names": names},
                    config["providers"]["cache_hours"],
                )
                return names
        except (OSError, asyncio.TimeoutError, RuntimeError) as error:
            last_error = error
            if attempt + 1 < retries:
                await asyncio.sleep(0)
    stale = state.cache_get(SHAPE_PROVIDER, "manifest", allow_expired=True)
    if stale is not None:
        return list(stale.get("names", []))
    raise RuntimeError(f"circuit manifest request failed: {last_error}") from last_error


def _select_shape_slug(race, names):
    circuit_id = _shape_key(race.circuit_id)
    candidates = []
    identity = set(_shape_key(f"{race.circuit} {race.locality}").split("-"))
    for name in names:
        stem = re.sub(r"\.svg$", "", name, flags=re.IGNORECASE)
        base = re.sub(r"-\d+$", "", stem)
        if base == circuit_id:
            return stem
        tokens = set(base.split("-"))
        score = len(tokens & identity) / max(len(tokens), 1)
        if score >= 0.75:
            candidates.append((score, stem))
    candidates.sort(reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        return candidates[0][1]
    return None


async def load_circuit_path(session, state, config, race, logger):
    circuit_id = race.circuit_id if isinstance(race, RaceData) else str(race)
    slug = CIRCUIT_SLUGS.get(circuit_id)
    if not slug and isinstance(race, RaceData):
        binding_key = f"shape-binding:{circuit_id}"
        binding = state.cache_get(SHAPE_PROVIDER, binding_key, allow_expired=True)
        slug = binding.get("slug") if binding else None
        if not slug:
            try:
                slug = _select_shape_slug(race, await _load_shape_manifest(session, state, config))
            except RuntimeError as error:
                logger.warning("[Provider] Circuit manifest unavailable | Error: %s", error)
            if slug:
                state.cache_put(SHAPE_PROVIDER, binding_key, {"slug": slug}, 720)
    if not slug:
        return None, "unmapped"
    key = f"circuit:{slug}"
    cached = state.cache_get(SHAPE_PROVIDER, key)
    if cached is not None:
        return cached.get("path"), "cache"
    url = f"{config['providers']['circuit_svg_url']}/{slug}.svg"
    try:
        svg = await _get(session, url, retries=config["providers"]["retries"], json_response=False)
        path = _extract_path(svg)
        if not path:
            raise RuntimeError("circuit SVG contained no usable path")
        state.cache_put(
            SHAPE_PROVIDER,
            key,
            {"path": path},
            config["providers"]["cache_hours"],
        )
        return path, SHAPE_PROVIDER
    except RuntimeError:
        stale = state.cache_get(SHAPE_PROVIDER, key, allow_expired=True)
        if stale is None:
            logger.warning("[Provider] Circuit shape unavailable | Circuit: %s", circuit_id)
            return None, "unavailable"
        return stale.get("path"), "stale-cache"
