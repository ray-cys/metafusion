"""Kometa Formula 1 metadata document generation and safe merging."""

from collections import defaultdict
from pathlib import Path

import yaml

from modules.kometa import merge_generated_metadata, write_kometa_metadata

PROGRAM_DATE_FIELDS = {
    "warmup": "FirstPractice",
    "practice1": "FirstPractice",
    "practice2": "SecondPractice",
    "practice3": "ThirdPractice",
    "sprint_qualifying": "SprintQualifying",
    "sprint": "Sprint",
    "post_sprint": "Sprint",
    "pre_sprint": "Sprint",
    "pre_qualifying": "Qualifying",
    "qualifying": "Qualifying",
    "post_qualifying": "Qualifying",
}


def _read_document(path):
    path = Path(path)
    if not path.exists():
        return {"metadata": {}}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError(f"Unable to read existing Formula 1 metadata: {path}") from error
    if not isinstance(document, dict) or not isinstance(document.get("metadata", {}), dict):
        raise TypeError(f"Invalid existing Formula 1 metadata document: {path}")
    document.setdefault("metadata", {})
    return document


def _race_summary(race):
    sprint = " This is a Sprint weekend." if race.sprint else ""
    circuit_length = (
        f"{race.circuit_length_km:.3f} km" if race.circuit_length_km else "to be confirmed"
    )
    lap_count = str(race.lap_count) if race.lap_count else "to be confirmed"
    race_distance = (
        f"{race.race_distance_km:.3f} km" if race.race_distance_km else "to be confirmed"
    )
    return (
        f"Round {race.round_number} of the {race.year} Formula 1 season at "
        f"{race.circuit} in {race.locality}, {race.country}. "
        f"Circuit length: {circuit_length}. Race distance: {race_distance}. "
        f"Lap count: {lap_count}.{sprint}"
    )


def _episode_summary(episode, race):
    return (
        f"{episode.program_title} from the {race.name} at {race.circuit}, "
        f"{race.locality}, {race.country}. {_race_summary(race)}"
    )


def build_show_entry(show, races, poster_references, config):
    by_round = {race.round_number: race for race in races}
    seasons: dict[int, dict] = {}
    authoritative_episodes: defaultdict[int, set[int]] = defaultdict(set)
    for episode in show.episodes:
        race = by_round.get(episode.round_number)
        if race is None:
            continue
        session_field = PROGRAM_DATE_FIELDS.get(episode.program_kind)
        available = race.session_dates.get(session_field) if session_field else None
        available = available or race.race_date
        generated_episode = {
            "title": episode.program_title,
            "originally_available": available,
            "summary": _episode_summary(episode, race),
        }
        season = seasons.setdefault(
            episode.round_number,
            {
                "title": race.name,
                "summary": _race_summary(race),
                "file_poster": poster_references.get(episode.round_number),
                "episodes": {},
            },
        )
        season["episodes"][episode.episode_number] = generated_episode
        authoritative_episodes[episode.round_number].add(episode.episode_number)
    metadata_config = config["metadata"]
    generated = {
        "f1_season": show.year,
        "round_prefix": bool(metadata_config.get("round_prefix", True)),
        "shorten_gp": bool(metadata_config.get("shorten_gp", False)),
        "content_rating": metadata_config.get("content_rating"),
        "studio": metadata_config.get("studio"),
        "summary": f"The {show.year} FIA Formula One World Championship.",
        "seasons": seasons,
    }
    return generated, set(seasons), dict(authoritative_episodes)


def write_show_metadata(show, races, poster_references, config):
    destination = config["paths"]["metadata"] / f"formula1_{show.year}.yml"
    document = _read_document(destination)
    generated, seasons, episodes = build_show_entry(show, races, poster_references, config)
    existing = document["metadata"].get(show.title, {})
    merged, diagnostics = merge_generated_metadata(
        existing,
        generated,
        "show",
        authoritative_seasons=seasons,
        authoritative_episodes=episodes,
    )
    updated = {"metadata": dict(document["metadata"])}
    updated["metadata"][show.title] = merged
    changed = updated != document
    if changed and not config["dry_run"]:
        write_kometa_metadata(
            destination,
            updated,
            library_type="tv",
            backup_count=3,
        )
    return destination, changed, diagnostics
