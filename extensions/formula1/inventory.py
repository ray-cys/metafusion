"""Plex inventory discovery and dual Formula 1 filename parsing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from helper.plex import plex_operation

YEAR_PATTERN = re.compile(r"\b\d{4}\b")
CURRENT_PATTERN = re.compile(
    r"^S(?P<season>\d{1,3})E(?P<episode>\d{1,3})\s*-\s*"
    r"(?P<event>.+?)\s*-\s*(?P<program>.+)$",
    re.IGNORECASE,
)
KOMETA_PATTERN = re.compile(
    r"^(?P<season>\d{1,3})x(?P<episode>\d{1,3})\s*-\s*"
    r"(?P<event>.+?)\s*-\s*(?P<program>.+)$",
    re.IGNORECASE,
)

PROGRAM_ALIASES = {
    "weekend warm up": ("Weekend Warm-Up", "warmup"),
    "fp1": ("Free Practice 1", "practice1"),
    "free practice 1": ("Free Practice 1", "practice1"),
    "fp2": ("Free Practice 2", "practice2"),
    "free practice 2": ("Free Practice 2", "practice2"),
    "fp3": ("Free Practice 3", "practice3"),
    "free practice 3": ("Free Practice 3", "practice3"),
    "sprint qualifying": ("Sprint Qualifying Session", "sprint_qualifying"),
    "sprint shootout session": ("Sprint Qualifying Session", "sprint_qualifying"),
    "pre sprint show": ("Pre-Sprint Buildup", "pre_sprint"),
    "pre sprint race buildup": ("Pre-Sprint Buildup", "pre_sprint"),
    "sprint": ("Sprint Session", "sprint"),
    "sprint race session": ("Sprint Session", "sprint"),
    "post sprint show": ("Post-Sprint Analysis", "post_sprint"),
    "post sprint race analysis": ("Post-Sprint Analysis", "post_sprint"),
    "pre qualifying show": ("Pre-Qualifying Buildup", "pre_qualifying"),
    "pre qualifying buildup": ("Pre-Qualifying Buildup", "pre_qualifying"),
    "qualifying": ("Qualifying Session", "qualifying"),
    "qualifying session": ("Qualifying Session", "qualifying"),
    "post qualifying show": ("Post-Qualifying Analysis", "post_qualifying"),
    "post qualifying analysis": ("Post-Qualifying Analysis", "post_qualifying"),
    "post qualyfing analysis": ("Post-Qualifying Analysis", "post_qualifying"),
    "pre race show": ("Pre-Race Buildup", "pre_race"),
    "pre race buildup": ("Pre-Race Buildup", "pre_race"),
    "race": ("Race Session", "race"),
    "race session": ("Race Session", "race"),
    "post race show": ("Post-Race Analysis", "post_race"),
    "post race analysis": ("Post-Race Analysis", "post_race"),
    "post race press conference": (
        "Post-Race Press Conference",
        "post_race_press_conference",
    ),
    "post race press conference show": (
        "Post-Race Press Conference",
        "post_race_press_conference",
    ),
    "post race conference": (
        "Post-Race Press Conference",
        "post_race_press_conference",
    ),
    "highlights": ("Highlights", "highlights"),
}

EVENT_ALIASES = {
    "australia": "Australian",
    "bahrein": "Bahrain",
    "britain": "British",
    "great britain": "British",
    "netherlands": "Dutch",
    "united states": "United States",
    "usa": "United States",
    "turkey": "Turkish",
    "turkiye": "Turkish",
}

COUNTRY_IDENTITIES = {
    "australia": {"australia", "australian"},
    "austria": {"austria", "austrian"},
    "azerbaijan": {"azerbaijan", "azerbaijani"},
    "bahrain": {"bahrain", "bahraini"},
    "belgium": {"belgium", "belgian"},
    "brazil": {"brazil", "brazilian"},
    "canada": {"canada", "canadian"},
    "china": {"china", "chinese"},
    "great britain": {"great britain", "britain", "british", "united kingdom", "uk"},
    "hungary": {"hungary", "hungarian"},
    "italy": {"italy", "italian"},
    "japan": {"japan", "japanese"},
    "mexico": {"mexico", "mexican", "mexico city"},
    "netherlands": {"netherlands", "dutch", "holland"},
    "saudi arabia": {"saudi arabia", "saudi arabian"},
    "spain": {"spain", "spanish"},
    "turkiye": {"turkiye", "turkey", "turkish"},
    "united arab emirates": {"united arab emirates", "uae", "abu dhabi"},
    "united states": {"united states", "usa", "us", "american", "miami", "las vegas"},
}


@dataclass(frozen=True)
class Formula1Episode:
    year: int
    round_number: int
    episode_number: int
    event_name: str
    program_title: str
    program_kind: str
    media_path: Path
    plex_rating_key: str
    naming_profile: str
    plex_item: object | None = field(default=None, compare=False, repr=False)

    @property
    def logical_key(self):
        return f"{self.year}:r{self.round_number:02d}:e{self.episode_number:02d}"


@dataclass
class Formula1Show:
    year: int
    title: str
    plex_rating_key: str
    episodes: list[Formula1Episode] = field(default_factory=list)
    plex_item: object | None = field(default=None, compare=False, repr=False)


@dataclass
class InventoryResult:
    shows: list[Formula1Show] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    profiles: dict[str, int] = field(default_factory=lambda: {"current": 0, "kometa": 0})


@dataclass(frozen=True)
class EventMatch:
    accepted: bool
    confidence: float
    reason: str
    alias: str
    scheduled_identity: str


def _words(value):
    value = re.sub(r"[._\\]+", " ", str(value))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_event(value):
    words = _words(value)
    words = re.sub(r"\bGP\b", "Grand Prix", words, flags=re.IGNORECASE)
    base = re.sub(r"\s+Grand Prix$", "", words, flags=re.IGNORECASE).strip()
    canonical = EVENT_ALIASES.get(base.casefold(), base)
    return f"{canonical} Grand Prix"


def canonical_program(value, aliases=None):
    words = _words(value)
    key = re.sub(r"[^a-z0-9]+", " ", words.casefold()).strip()
    combined = dict(PROGRAM_ALIASES)
    combined.update(aliases or {})
    if key in combined:
        mapped = combined[key]
        if isinstance(mapped, dict):
            return str(mapped.get("title") or words), str(mapped.get("kind") or "other")
        return tuple(mapped)
    title = " ".join(part.capitalize() for part in words.split())
    return title, "other"


def _identity(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    value = re.sub(r"\b(?:grand prix|gp|circuit|international|autodrome)\b", " ", ascii_value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _country_family(country):
    identity = _identity(country)
    for canonical, values in COUNTRY_IDENTITIES.items():
        normalized = {_identity(value) for value in values | {canonical}}
        if identity in normalized:
            return normalized
    return {identity}


def match_event_to_schedule(event_name, race, learned=None):
    """Explain a conservative schedule-derived event identity decision."""
    alias = _identity(event_name)
    scheduled = _identity(race.name)
    learned = learned or {}
    if (
        learned.get("alias") == alias
        and learned.get("scheduled_identity") == scheduled
    ):
        return EventMatch(True, 1.0, "learned binding", alias, scheduled)

    exact = {
        scheduled,
        _identity(race.country),
        _identity(race.locality),
        _identity(race.circuit),
        _identity(race.circuit_id),
        *(_country_family(race.country)),
    }
    exact.discard("")
    if alias in exact:
        return EventMatch(True, 1.0, "exact schedule identity", alias, scheduled)

    alias_tokens = set(alias.split())
    candidates = []
    for expected in exact:
        expected_tokens = set(expected.split())
        overlap = len(alias_tokens & expected_tokens) / max(
            min(len(alias_tokens), len(expected_tokens)), 1
        )
        similarity = SequenceMatcher(None, alias, expected).ratio()
        candidates.append((max(overlap, similarity), expected))
    candidates.sort(reverse=True)
    if candidates and candidates[0][0] >= 0.86:
        return EventMatch(
            True,
            round(candidates[0][0], 3),
            f"high-confidence schedule alias for {candidates[0][1]}",
            alias,
            scheduled,
        )
    score = candidates[0][0] if candidates else 0.0
    return EventMatch(False, round(score, 3), "no safe schedule identity", alias, scheduled)


def event_matches_schedule(event_name, race):
    return match_event_to_schedule(event_name, race).accepted


def parse_episode_filename(
    path,
    expected_season=None,
    expected_episode=None,
    profile="auto",
    session_aliases=None,
):
    """Parse either supported naming convention and verify Plex numbering."""
    stem = Path(path).stem
    patterns = []
    if profile in {"auto", "current"}:
        patterns.append(("current", CURRENT_PATTERN))
    if profile in {"auto", "kometa"}:
        patterns.append(("kometa", KOMETA_PATTERN))
    for naming_profile, pattern in patterns:
        match = pattern.match(stem)
        if match is None:
            continue
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        if expected_season is not None and season != int(expected_season):
            raise ValueError(
                f"filename season {season} does not match Plex season {expected_season}"
            )
        if expected_episode is not None and episode != int(expected_episode):
            raise ValueError(
                f"filename episode {episode} does not match Plex episode {expected_episode}"
            )
        title, kind = canonical_program(match.group("program"), session_aliases)
        return {
            "season": season,
            "episode": episode,
            "event": canonical_event(match.group("event")),
            "program_title": title,
            "program_kind": kind,
            "profile": naming_profile,
        }
    raise ValueError(f"unsupported Formula 1 filename: {Path(path).name}")


def _show_year(show):
    match = YEAR_PATTERN.search(str(getattr(show, "title", "")))
    if match and 1950 <= int(match.group()) <= 2200:
        return int(match.group())
    year = getattr(show, "year", None)
    if year is not None and 1950 <= int(year) <= 2200:
        return int(year)
    return None


def _episode_path(episode):
    locations = getattr(episode, "locations", None) or []
    if locations:
        return Path(locations[0])
    if hasattr(episode, "iterParts"):
        parts = list(episode.iterParts())
        if parts and getattr(parts[0], "file", None):
            return Path(parts[0].file)
    return None


async def discover_formula1_inventory(section, config, detail_logger):
    """Load the dedicated Plex library and return a stable logical inventory."""
    runtime = config.get("runtime", {})
    extension = config["formula1"]
    profile = extension.get("library", {}).get("naming_profile", "auto")
    session_aliases = extension.get("sessions", {}).get("aliases", {})
    result = InventoryResult()
    shows = await plex_operation(
        lambda: list(section.all()), runtime, description=f"List Formula 1 library {section.title}"
    )
    seen = {}
    duplicates = set()
    for show in shows:
        year = _show_year(show)
        if year is None:
            result.issues.append(f"Ignored show without a championship year: {show.title}")
            continue
        discovered_show = Formula1Show(
            year=year,
            title=str(show.title),
            plex_rating_key=str(getattr(show, "ratingKey", "")),
            plex_item=show,
        )
        seasons = await plex_operation(
            lambda item=show: list(item.seasons()),
            runtime,
            description=f"List Formula 1 seasons for {show.title}",
        )
        for season in seasons:
            round_number = int(getattr(season, "index", -1))
            if round_number == 0:
                continue
            if round_number < 0:
                result.issues.append(f"Ignored invalid season for {show.title}")
                continue
            episodes = await plex_operation(
                lambda item=season: list(item.episodes()),
                runtime,
                description=f"List Formula 1 episodes for {show.title} round {round_number}",
            )
            for episode in episodes:
                episode_number = int(getattr(episode, "index", -1))
                path = _episode_path(episode)
                if path is None:
                    result.issues.append(
                        f"Missing media path: {show.title} S{round_number:02d}E{episode_number:02d}"
                    )
                    continue
                try:
                    parsed = parse_episode_filename(
                        path,
                        expected_season=round_number,
                        expected_episode=episode_number,
                        profile=profile,
                        session_aliases=session_aliases,
                    )
                except ValueError as error:
                    result.issues.append(str(error))
                    continue
                record = Formula1Episode(
                    year=year,
                    round_number=round_number,
                    episode_number=episode_number,
                    event_name=parsed["event"],
                    program_title=parsed["program_title"],
                    program_kind=parsed["program_kind"],
                    media_path=path,
                    plex_rating_key=str(getattr(episode, "ratingKey", "")),
                    naming_profile=parsed["profile"],
                    plex_item=episode,
                )
                if record.program_kind == "other":
                    result.issues.append(
                        f"Unrecognized programme label retained for review: "
                        f"{show.title} S{round_number:02d}E{episode_number:02d} "
                        f"({record.program_title})"
                    )
                if record.logical_key in seen:
                    duplicates.add(record.logical_key)
                    result.issues.append(f"Duplicate episode identity: {record.logical_key}")
                    continue
                seen[record.logical_key] = record
                result.profiles[record.naming_profile] += 1
                discovered_show.episodes.append(record)
        if discovered_show.episodes:
            result.shows.append(discovered_show)
    if duplicates:
        for show in result.shows:
            show.episodes = [
                episode for episode in show.episodes if episode.logical_key not in duplicates
            ]
        result.shows = [show for show in result.shows if show.episodes]
        for key in duplicates:
            first = seen[key]
            result.profiles[first.naming_profile] -= 1
    detail_logger.info(
        "[Inventory] Library: %s | Shows: %d | Episodes: %d | Current names: %d | Kometa names: %d",
        section.title,
        len(result.shows),
        sum(len(show.episodes) for show in result.shows),
        result.profiles["current"],
        result.profiles["kometa"],
    )
    return result
