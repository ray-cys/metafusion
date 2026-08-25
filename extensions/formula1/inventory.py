"""Plex inventory discovery and dual Formula 1 filename parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from helper.plex import plex_operation

YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
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
    ignored_testing: int = 0
    profiles: dict[str, int] = field(default_factory=lambda: {"current": 0, "kometa": 0})


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


def canonical_program(value):
    words = _words(value)
    key = re.sub(r"[^a-z0-9]+", " ", words.casefold()).strip()
    if key in PROGRAM_ALIASES:
        return PROGRAM_ALIASES[key]
    title = " ".join(part.capitalize() for part in words.split())
    return title, "other"


def event_matches_schedule(event_name, race):
    """Require the filename event to agree with the authoritative scheduled round."""
    expected = {
        canonical_event(race.name).casefold(),
        canonical_event(race.country).casefold(),
    }
    return canonical_event(event_name).casefold() in expected


def parse_episode_filename(path, expected_season=None, expected_episode=None, profile="auto"):
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
        title, kind = canonical_program(match.group("program"))
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
    if match:
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
    include_testing = bool(extension.get("testing", {}).get("include", False))
    profile = extension.get("library", {}).get("naming_profile", "auto")
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
            if round_number == 0 and not include_testing:
                result.ignored_testing += 1
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
