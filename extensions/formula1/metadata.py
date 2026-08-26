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
    if (
        race.circuit_length_km is not None
        and race.lap_count is not None
        and race.race_distance_km is not None
    ):
        fact_text = (
            f" The circuit measures {race.circuit_length_km:.3f} km; the scheduled "
            f"race runs for {race.lap_count} laps and covers "
            f"{race.race_distance_km:.3f} km."
        )
    else:
        facts = []
        if race.circuit_length_km is not None:
            facts.append(f"Circuit length: {race.circuit_length_km:.3f} km")
        if race.lap_count is not None:
            facts.append(f"Lap count: {race.lap_count}")
        if race.race_distance_km is not None:
            facts.append(f"Race distance: {race.race_distance_km:.3f} km")
        fact_text = f" {'; '.join(facts)}." if facts else ""
    history = []
    if race.circuit_history:
        history.append(race.circuit_history)
    if race.first_grand_prix_year is not None:
        history.append(
            f"The venue first hosted a Formula 1 Grand Prix in {race.first_grand_prix_year}."
        )
    if race.circuit_profile:
        history.append(f"Formula1.com circuit profile: {race.circuit_profile}.")
    history_text = f" {' '.join(history)}" if history else ""
    return (
        f"Round {race.round_number} of the {race.year} Formula 1 season at "
        f"{race.circuit} in {race.locality}, {race.country}.{fact_text}{history_text}{sprint}"
    )


def _episode_summary(episode, race):
    return (
        f"{episode.program_title} from the {race.name} at {race.circuit}, "
        f"{race.locality}, {race.country}. {_race_summary(race)}"
    )


def build_show_entry(
    show,
    races,
    poster_references,
    config,
    show_artwork=None,
    episode_poster_references=None,
):
    episode_poster_references = episode_poster_references or {}
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
        episode_reference = episode_poster_references.get(
            (episode.round_number, episode.episode_number)
        )
        if episode_reference:
            generated_episode["file_poster"] = episode_reference
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
    if show_artwork:
        generated["file_poster"] = show_artwork.get("poster")
        generated["file_background"] = show_artwork.get("background")
    return generated, set(seasons), dict(authoritative_episodes)


def write_show_metadata(
    show,
    races,
    poster_references,
    config,
    show_artwork=None,
    *,
    authoritative_seasons=None,
    authoritative_episodes=None,
    previous_title=None,
    episode_poster_references=None,
):
    destination = config["paths"]["metadata"] / f"formula1_{show.year}.yml"
    document = _read_document(destination)
    generated, seasons, episodes = build_show_entry(
        show,
        races,
        poster_references,
        config,
        show_artwork,
        episode_poster_references,
    )
    existing = document["metadata"].get(show.title, {})
    if previous_title and previous_title != show.title:
        previous = document["metadata"].get(previous_title)
        if isinstance(previous, dict) and not existing:
            existing = previous
    merged, diagnostics = merge_generated_metadata(
        existing,
        generated,
        "show",
        authoritative_seasons=(seasons if authoritative_seasons is None else authoritative_seasons),
        authoritative_episodes=(
            episodes if authoritative_episodes is None else authoritative_episodes
        ),
    )
    updated = {"metadata": dict(document["metadata"])}
    if previous_title and previous_title != show.title:
        updated["metadata"].pop(previous_title, None)
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
