"""Formula 1 extension orchestration and strict core-isolation boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict

from extensions.formula1.artwork import (
    artwork_fingerprint,
    render_round_poster,
    validate_branding,
)
from extensions.formula1.config import formula1_requested, load_formula1_config
from extensions.formula1.facts import enrich_race_facts
from extensions.formula1.inventory import (
    discover_formula1_inventory,
    match_event_to_schedule,
)
from extensions.formula1.logging import create_formula1_logger, run_identifier
from extensions.formula1.metadata import build_show_entry, write_show_metadata
from extensions.formula1.provider import load_circuit_path, load_schedule
from extensions.formula1.show_artwork import (
    reconcile_episode_round_artwork,
    run_show_artwork_rotation,
    write_attribution_reports,
)
from extensions.formula1.state import Formula1State
from extensions.formula1.verification import (
    queue_application_verification,
    verify_due_applications,
)
from helper.io import atomic_write_text


class Formula1Summary(TypedDict):
    libraries: int
    shows: int
    episodes: int
    metadata_updated: int
    metadata_unchanged: int
    artwork_created: int
    artwork_updated: int
    artwork_adopted: int
    artwork_preserved: int
    artwork_unchanged: int
    show_artwork_rotated: int
    show_artwork_restored: int
    show_artwork_unchanged: int
    show_artwork_preserved: int
    show_artwork_missing: int
    show_artwork_rerendered: int
    show_artwork_pruned: int
    source_cache_pruned: int
    episode_artwork_created: int
    episode_artwork_updated: int
    episode_artwork_preserved: int
    episode_artwork_unchanged: int
    facts_resolved: int
    facts_missing: int
    facts_stale: int
    venues_canonicalized: int
    profiles_resolved: int
    profiles_missing: int
    issues: int
    cleanup_removed: int
    event_mismatches: int
    schedules_pending: int
    event_identities_learned: int
    branding_warnings: int
    verification_applied: int
    verification_partial: int
    verification_report: str | None
    issue_report: str | None
    log: str | None


def partition_formula1_sections(sections, core_config, environ=None, *, base_config_dir=None):
    """Remove the dedicated F1 library from core TMDb processing when enabled."""
    if not formula1_requested(core_config, environ):
        return list(sections), []
    environment = environ or {}
    name = environment.get("FORMULA1_LIBRARY")
    if not name and base_config_dir is not None:
        extension = load_formula1_config(
            core_config,
            base_config_dir,
            dry_run=bool(core_config.get("settings", {}).get("dry_run", False)),
        )
        name = extension["library"]["name"]
    name = str(name or "Formula 1")
    name = name.strip().casefold()
    formula1 = [section for section in sections if str(section.title).casefold() == name]
    regular = [section for section in sections if section not in formula1]
    return regular, formula1


def _checksum(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _asset_reference(config, year, round_number):
    root = str(config["artwork"].get("asset_reference_root", "/config/assets/formula1/rounds"))
    root = "/" + root.lstrip("/")
    return f"{root.rstrip('/')}/{year}/round-{round_number:02d}/poster.png"


def _episode_asset_reference(config, episode):
    root = str(config["artwork"].get("asset_reference_root", "/config/assets/formula1/rounds"))
    root = "/" + root.lstrip("/")
    return (
        f"{root.rstrip('/')}/{episode.year}/round-{episode.round_number:02d}/"
        f"episodes/episode-{episode.episode_number:02d}.png"
    )


def _write_issues(config, run_id, issues):
    if config["dry_run"] or not issues:
        return None
    directory = config["paths"]["reports"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"formula1-issues-{run_id}.json"
    atomic_write_text(path, json.dumps({"issues": issues}, indent=2) + "\n")
    return path


def _managed_artwork_action(state, logical_key, destination, fingerprint):
    previous = state.artwork(logical_key)
    destination = Path(destination)
    if not destination.exists():
        return "create"
    checksum = _checksum(destination)
    if previous is None:
        return "adopt"
    if checksum != previous["checksum"]:
        return "preserve-manual"
    if previous["fingerprint"] == fingerprint:
        return "unchanged"
    return "update"


def _authoritative_children(keys, year):
    seasons = set()
    episodes: dict[int, set[int]] = {}
    prefix = f"{int(year)}:r"
    for key in keys:
        if not key.startswith(prefix) or ":e" not in key:
            continue
        round_value, episode_value = key[len(prefix) :].split(":e", 1)
        if not round_value.isdigit() or not episode_value.isdigit():
            continue
        round_number, episode_number = int(round_value), int(episode_value)
        seasons.add(round_number)
        episodes.setdefault(round_number, set()).add(episode_number)
    return seasons, episodes


async def _prepare_plans(sections, core_config, session, state, config, logger, summary, issues):
    inventory_config = {"runtime": core_config.get("runtime", {}), "formula1": config}
    shows = []
    for section in sections:
        section_type = str(
            getattr(section, "type", None) or getattr(section, "TYPE", "")
        ).casefold()
        if section_type not in {"show", "tv"}:
            raise RuntimeError(f"Formula 1 library must be a Plex TV library: {section.title}")
        inventory = await discover_formula1_inventory(section, inventory_config, logger)
        issues.extend(inventory.issues)
        shows.extend(inventory.shows)
        summary["libraries"] += 1
    grouped: dict[int, list] = {}
    for show in shows:
        grouped.setdefault(show.year, []).append(show)
    duplicates = {year: values for year, values in grouped.items() if len(values) > 1}
    if duplicates:
        detail = "; ".join(
            f"{year}: "
            + ", ".join(
                f"{show.title} (Plex {show.plex_rating_key or 'unknown'})" for show in values
            )
            for year, values in sorted(duplicates.items())
        )
        raise RuntimeError(
            f"One Plex show per Formula 1 championship year is required: {detail}"
        )
    plans = []
    deferred_keys: set[str] = set()
    for show in shows:
        try:
            races, source = await load_schedule(session, state, config, show.year, logger)
        except RuntimeError as error:
            summary["schedules_pending"] += 1
            deferred_keys.update(
                key
                for key in state.bindings()
                if key.startswith(f"{show.year}:r")
            )
            issues.append(
                f"Schedule pending: {show.title} ({show.year}); existing output was "
                f"preserved and the year will retry automatically: {error}"
            )
            logger.warning(
                "[Provider] Schedule pending | Year: %d | Existing output: preserved | Error: %s",
                show.year,
                error,
            )
            continue
        logger.info(
            "[Provider] Schedule | Year: %d | Races: %d | Source: %s",
            show.year,
            len(races),
            source,
        )
        races, statistics = await enrich_race_facts(
            session,
            state,
            config,
            races,
            {item.round_number for item in show.episodes},
            logger,
        )
        for source_key, summary_key in (
            ("resolved", "facts_resolved"),
            ("missing", "facts_missing"),
            ("stale", "facts_stale"),
            ("canonicalized", "venues_canonicalized"),
            ("profiles_resolved", "profiles_resolved"),
            ("profiles_missing", "profiles_missing"),
        ):
            summary[summary_key] += statistics[source_key]
        issues.extend(statistics["issues"])
        race_by_round = {race.round_number: race for race in races}
        accepted = []
        for episode in show.episodes:
            race = race_by_round.get(episode.round_number)
            if race is None:
                issues.append(f"No schedule match: {show.title} round {episode.round_number}")
            else:
                initial = match_event_to_schedule(episode.event_name, race)
                learned = state.event_identity(
                    show.year, episode.round_number, initial.alias
                )
                decision = match_event_to_schedule(episode.event_name, race, learned)
            if race is not None and not decision.accepted:
                summary["event_mismatches"] += 1
                issues.append(
                    f"Filename event rejected: {show.title} S{episode.round_number:02d}"
                    f"E{episode.episode_number:02d} says {episode.event_name}; "
                    f"scheduled round is {race.name}; confidence={decision.confidence:.3f}; "
                    f"reason={decision.reason}"
                )
            elif race is not None:
                accepted.append(episode)
                if learned is None and not config["dry_run"]:
                    state.bind_event_identity(
                        show.year,
                        episode.round_number,
                        decision.alias,
                        decision.scheduled_identity,
                        decision.confidence,
                        decision.reason,
                    )
                    summary["event_identities_learned"] += 1
        show.episodes = accepted
        if accepted:
            plans.append((show, races, race_by_round))
    summary["shows"] = len(plans)
    return plans, shows, deferred_keys


async def run_formula1_extension(
    sections,
    core_config,
    session,
    core_logger,
    *,
    base_config_dir,
):
    """Process only the isolated Formula 1 library and return a core summary."""
    if not sections:
        return None
    dry_run = bool(core_config.get("settings", {}).get("dry_run", False))
    config = load_formula1_config(core_config, base_config_dir, dry_run=dry_run)
    run_id = run_identifier()
    detail_logger, log_path = create_formula1_logger(config, run_id)
    state = Formula1State(":memory:" if dry_run else config["paths"]["database"])
    summary: Formula1Summary = {
        "libraries": 0,
        "shows": 0,
        "episodes": 0,
        "metadata_updated": 0,
        "metadata_unchanged": 0,
        "artwork_created": 0,
        "artwork_updated": 0,
        "artwork_adopted": 0,
        "artwork_preserved": 0,
        "artwork_unchanged": 0,
        "show_artwork_rotated": 0,
        "show_artwork_restored": 0,
        "show_artwork_unchanged": 0,
        "show_artwork_preserved": 0,
        "show_artwork_missing": 0,
        "show_artwork_rerendered": 0,
        "show_artwork_pruned": 0,
        "source_cache_pruned": 0,
        "episode_artwork_created": 0,
        "episode_artwork_updated": 0,
        "episode_artwork_preserved": 0,
        "episode_artwork_unchanged": 0,
        "facts_resolved": 0,
        "facts_missing": 0,
        "facts_stale": 0,
        "venues_canonicalized": 0,
        "profiles_resolved": 0,
        "profiles_missing": 0,
        "issues": 0,
        "cleanup_removed": 0,
        "event_mismatches": 0,
        "schedules_pending": 0,
        "event_identities_learned": 0,
        "branding_warnings": 0,
        "verification_applied": 0,
        "verification_partial": 0,
        "verification_report": None,
        "issue_report": None,
        "log": None,
    }
    issues: list[str] = []
    current_keys = set()
    state.start_run(run_id)
    try:
        detail_logger.info("[Startup] Formula 1 extension | Run: %s", run_id)
        branding_warnings = validate_branding(config)
        summary["branding_warnings"] = len(branding_warnings)
        for warning in branding_warnings:
            detail_logger.warning("[Branding] %s", warning)
        plans, discovered_shows, deferred_keys = await _prepare_plans(
            sections, core_config, session, state, config, detail_logger, summary, issues
        )
        shows = [plan[0] for plan in plans]
        current_keys = {
            episode.logical_key for show in shows for episode in show.episodes
        } | deferred_keys
        verification_records, verification_report = await verify_due_applications(
            state, discovered_shows, config, core_config, session, run_id, detail_logger
        )
        summary["verification_applied"] = sum(
            record.get("status") == "applied" for record in verification_records
        )
        summary["verification_partial"] = (
            len(verification_records) - summary["verification_applied"]
        )
        summary["verification_report"] = (
            str(verification_report) if verification_report else None
        )
        existing_keys = set(state.bindings())
        stale = state.reconcile_bindings(
            current_keys,
            cleanup=bool(config["cleanup"].get("enabled", False)),
            confirmation_scans=config["cleanup"]["confirmation_scans"],
            grace_hours=config["cleanup"]["grace_hours"],
        )
        stale_keys = {binding["logical_key"] for binding in stale}
        retained_keys = current_keys | (existing_keys - stale_keys)
        for show, races, race_by_round in plans:
            if show is not None:
                poster_references = {}
                circuit_paths = {}
                artwork_changed = False
                for round_number in sorted({item.round_number for item in show.episodes}):
                    race = race_by_round[round_number]
                    destination = (
                        config["paths"]["assets"]
                        / str(show.year)
                        / f"round-{round_number:02d}"
                        / "poster.png"
                    )
                    path_data, shape_source = await load_circuit_path(
                        session, state, config, race, detail_logger
                    )
                    circuit_paths[round_number] = path_data
                    fingerprint = artwork_fingerprint(race, path_data, config)
                    logical_key = f"{show.year}:r{round_number:02d}"
                    action = _managed_artwork_action(state, logical_key, destination, fingerprint)
                    if action in {"create", "update"} and config["artwork"].get("enabled", True):
                        if not dry_run:
                            checksum = render_round_poster(race, path_data, config, destination)
                            state.save_artwork(logical_key, destination, fingerprint, checksum)
                        if action == "create":
                            summary["artwork_created"] += 1
                        else:
                            summary["artwork_updated"] += 1
                        artwork_changed = True
                    elif action == "adopt":
                        if not dry_run:
                            state.save_artwork(
                                logical_key, destination, fingerprint, _checksum(destination)
                            )
                        summary["artwork_adopted"] += 1
                    elif action == "preserve-manual":
                        summary["artwork_preserved"] += 1
                    else:
                        summary["artwork_unchanged"] += 1
                    if destination.exists() or action in {"create", "update"}:
                        poster_references[round_number] = _asset_reference(
                            config, show.year, round_number
                        )
                    detail_logger.info(
                        "[Artwork] %s | Round: %02d | Action: %s | Shape source: %s",
                        show.title,
                        round_number,
                        action,
                        shape_source,
                    )
                show_artwork = {}
                episode_poster_references = {}
                detected_rounds = sorted({item.round_number for item in show.episodes})
                if (
                    config["show_artwork"].get("enabled", True)
                    and config["metadata"].get("enabled", True)
                    and detected_rounds
                ):
                    trigger_round = detected_rounds[-1]
                    trigger_race = race_by_round.get(trigger_round)
                    if trigger_race is not None:
                        rotation = await run_show_artwork_rotation(
                            session,
                            state,
                            config,
                            show,
                            trigger_race,
                            circuit_paths.get(trigger_round),
                            detail_logger,
                        )
                        if rotation.poster_reference:
                            show_artwork["poster"] = rotation.poster_reference
                        if rotation.background_reference:
                            show_artwork["background"] = rotation.background_reference
                        episode_actions = {
                            (trigger_round, episode_number): action
                            for episode_number, action in rotation.episode_actions.items()
                        }
                        for episode_number, reference in rotation.episode_references.items():
                            episode_poster_references[(trigger_round, episode_number)] = reference
                        episode_teams = {
                            trigger_round: rotation.constructor or "none"
                        }
                        trigger_binding = state.episode_round_source(
                            show.year, trigger_round
                        )
                        for round_number in detected_rounds:
                            expected_episodes = sum(
                                item.round_number == round_number for item in show.episodes
                            )
                            if (
                                round_number == trigger_round
                                and len(rotation.episode_actions) >= expected_episodes
                                and (
                                    trigger_binding is not None
                                    or (
                                        rotation.photo_path
                                        and rotation.source_identity
                                    )
                                )
                            ):
                                continue
                            race = race_by_round[round_number]
                            round_artwork = await reconcile_episode_round_artwork(
                                session,
                                state,
                                config,
                                show,
                                race,
                                circuit_paths.get(round_number),
                                detail_logger,
                            )
                            if round_artwork.issue:
                                issues.append(
                                    f"Episode artwork: {show.title} round "
                                    f"{round_number:02d}: {round_artwork.issue}"
                                )
                            episode_teams[round_number] = (
                                round_artwork.constructor or "none"
                            )
                            references = round_artwork.references
                            actions = round_artwork.actions
                            if references:
                                episode_poster_references.update(
                                    (
                                        ((round_number, episode_number), reference)
                                        for episode_number, reference in references.items()
                                    )
                                )
                            if actions:
                                episode_actions.update(
                                    ((round_number, episode_number), value)
                                    for episode_number, value in actions.items()
                                )
                        for (round_number, episode_number), action in episode_actions.items():
                            if action == "create":
                                summary["episode_artwork_created"] += 1
                            elif action == "update":
                                summary["episode_artwork_updated"] += 1
                            elif action == "preserve-manual":
                                summary["episode_artwork_preserved"] += 1
                            else:
                                summary["episode_artwork_unchanged"] += 1
                            detail_logger.info(
                                "[Episode Artwork] %s | Round: %02d | Episode: %02d | "
                                "Action: %s | Team: %s | Source: Wikimedia Commons",
                                show.title,
                                round_number,
                                episode_number,
                                action,
                                episode_teams.get(round_number, "none"),
                            )
                        artwork_changed |= any(
                            action in {"create", "update"}
                            for action in episode_actions.values()
                        )
                        if rotation.action in {"rotated", "rotate-planned"}:
                            summary["show_artwork_rotated"] += 1
                        elif rotation.action in {"restored", "restore-planned"}:
                            summary["show_artwork_restored"] += 1
                        elif rotation.action in {"rerendered", "rerender-planned"}:
                            summary["show_artwork_rerendered"] += 1
                        elif rotation.action == "unchanged":
                            summary["show_artwork_unchanged"] += 1
                        elif rotation.action in {"preserved", "preserve-manual"}:
                            summary["show_artwork_preserved"] += 1
                        else:
                            summary["show_artwork_missing"] += 1
                        summary["show_artwork_pruned"] += rotation.pairs_pruned
                        summary["source_cache_pruned"] += rotation.cache_pruned
                        if rotation.issue:
                            issues.append(f"Show artwork: {show.title}: {rotation.issue}")
                        write_attribution_reports(state, config)
                        artwork_changed |= rotation.action in {
                            "rotated",
                            "restored",
                            "rerendered",
                            "rotate-planned",
                            "restore-planned",
                            "rerender-planned",
                        }
                        detail_logger.info(
                            "[Show Artwork] %s | Trigger round: %02d | Action: %s | "
                            "Team: %s | Source: Wikimedia Commons",
                            show.title,
                            rotation.trigger_round,
                            rotation.action,
                            rotation.constructor or "none",
                        )
                for episode in show.episodes:
                    destination = (
                        config["paths"]["assets"]
                        / str(episode.year)
                        / f"round-{episode.round_number:02d}"
                        / "episodes"
                        / f"episode-{episode.episode_number:02d}.png"
                    )
                    if destination.is_file():
                        episode_poster_references.setdefault(
                            (episode.round_number, episode.episode_number),
                            _episode_asset_reference(config, episode),
                        )
                if config["metadata"].get("enabled", True):
                    authoritative_seasons, authoritative_episodes = _authoritative_children(
                        retained_keys, show.year
                    )
                    previous_show = state.show_binding(show.year)
                    previous_title = (
                        previous_show["title"]
                        if previous_show
                        and str(previous_show["plex_rating_key"])
                        == str(show.plex_rating_key)
                        else None
                    )
                    destination, changed, diagnostics = write_show_metadata(
                        show,
                        races,
                        poster_references,
                        config,
                        show_artwork,
                        authoritative_seasons=authoritative_seasons,
                        authoritative_episodes=authoritative_episodes,
                        previous_title=previous_title,
                        episode_poster_references=episode_poster_references,
                    )
                    summary["metadata_updated" if changed else "metadata_unchanged"] += 1
                    detail_logger.info(
                        "[Metadata] %s | Action: %s | Destination: %s | Fields: %d",
                        show.title,
                        "updated" if changed else "unchanged",
                        destination,
                        diagnostics["available"],
                    )
                    if changed or artwork_changed:
                        expected_entry = build_show_entry(
                            show,
                            races,
                            poster_references,
                            config,
                            show_artwork,
                            episode_poster_references,
                        )[0]
                        expected_artwork = [
                            {
                                "child_key": f"season:{round_number}",
                                "asset_type": "poster",
                                "destination": str(
                                    config["paths"]["assets"]
                                    / str(show.year)
                                    / f"round-{round_number:02d}"
                                    / "poster.png"
                                ),
                            }
                            for round_number in poster_references
                        ]
                        current_rotation = state.show_rotation(f"show:{show.year}")
                        expected_artwork.extend(
                            {
                                "child_key": f"episode:{round_number}:{episode_number}",
                                "asset_type": "poster",
                                "destination": str(
                                    config["paths"]["assets"]
                                    / str(show.year)
                                    / f"round-{round_number:02d}"
                                    / "episodes"
                                    / f"episode-{episode_number:02d}.png"
                                ),
                            }
                            for round_number, episode_number in episode_poster_references
                        )
                        if current_rotation:
                            expected_artwork.extend(
                                [
                                    {
                                        "child_key": "",
                                        "asset_type": "poster",
                                        "destination": current_rotation["poster_destination"],
                                    },
                                    {
                                        "child_key": "",
                                        "asset_type": "background",
                                        "destination": current_rotation[
                                            "background_destination"
                                        ],
                                    },
                                ]
                            )
                        queue_application_verification(
                            state, show, expected_entry, expected_artwork, config
                        )
                    if not dry_run:
                        state.bind_show(show.year, show.plex_rating_key, show.title)
                for episode in show.episodes:
                    if not dry_run:
                        state.bind(
                            episode.logical_key,
                            episode.plex_rating_key,
                            episode.media_path,
                            f"{episode.event_name} - {episode.program_title}",
                            episode.naming_profile,
                        )
                summary["episodes"] += len(show.episodes)

        active_rounds = {key.rsplit(":e", 1)[0] for key in retained_keys}
        removed_rounds = set()
        for binding in stale:
            round_key = binding["logical_key"].rsplit(":e", 1)[0]
            episode_artwork = state.artwork(binding["logical_key"])
            if episode_artwork:
                episode_destination = Path(episode_artwork["destination"])
                if (
                    episode_destination.exists()
                    and _checksum(episode_destination) == episode_artwork["checksum"]
                ):
                    if not dry_run:
                        episode_destination.unlink()
                    summary["cleanup_removed"] += 1
            artwork = state.artwork(round_key)
            if round_key not in active_rounds and artwork and round_key not in removed_rounds:
                destination = Path(artwork["destination"])
                if destination.exists() and _checksum(destination) == artwork["checksum"]:
                    if not dry_run:
                        destination.unlink()
                    summary["cleanup_removed"] += 1
                if not dry_run:
                    state.remove_artwork(round_key)
                removed_rounds.add(round_key)
            if not dry_run:
                state.remove_binding(binding["logical_key"], binding)
        summary["issues"] = len(issues)
        report = _write_issues(config, run_id, issues)
        summary["issue_report"] = str(report) if report else None
        state.finish_run(run_id, "success", summary)
        detail_logger.info("[Summary] %s", json.dumps(summary, sort_keys=True))
        if config["logging"]["console"] != "off":
            core_logger.info(
                "[Formula 1] Summary | Libraries: %d | Shows: %d | Episodes: %d | "
                "Metadata updated: %d | Artwork created/updated: %d/%d | "
                "Show artwork rotated/rerendered/preserved/missing: %d/%d/%d/%d | "
                "Episode artwork created/updated/preserved/unchanged: %d/%d/%d/%d | "
                "Circuit facts resolved/missing: %d/%d | Circuit profiles resolved/missing: "
                "%d/%d | Event mismatches: %d | Verification applied/partial: %d/%d | "
                "Pending schedules: %d | Event identities learned: %d | Issues: %d",
                summary["libraries"],
                summary["shows"],
                summary["episodes"],
                summary["metadata_updated"],
                summary["artwork_created"],
                summary["artwork_updated"],
                summary["show_artwork_rotated"],
                summary["show_artwork_rerendered"],
                summary["show_artwork_preserved"],
                summary["show_artwork_missing"],
                summary["episode_artwork_created"],
                summary["episode_artwork_updated"],
                summary["episode_artwork_preserved"],
                summary["episode_artwork_unchanged"],
                summary["facts_resolved"],
                summary["facts_missing"],
                summary["profiles_resolved"],
                summary["profiles_missing"],
                summary["event_mismatches"],
                summary["verification_applied"],
                summary["verification_partial"],
                summary["schedules_pending"],
                summary["event_identities_learned"],
                summary["issues"],
            )
        summary["log"] = str(log_path) if log_path else None
        return summary
    except Exception as error:
        summary["issues"] = len(issues) + 1
        state.finish_run(run_id, "failed", {**summary, "error": str(error)})
        detail_logger.exception("[Failure] Formula 1 extension failed")
        raise
    finally:
        state.close()
