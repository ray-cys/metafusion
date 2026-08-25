"""Formula 1 extension orchestration and strict core-isolation boundary."""

import hashlib
import json
from pathlib import Path
from typing import TypedDict

from extensions.formula1.artwork import artwork_fingerprint, render_round_poster
from extensions.formula1.config import formula1_requested, load_formula1_config
from extensions.formula1.facts import enrich_race_facts
from extensions.formula1.inventory import discover_formula1_inventory
from extensions.formula1.logging import create_formula1_logger, run_identifier
from extensions.formula1.metadata import write_show_metadata
from extensions.formula1.provider import load_circuit_path, load_schedule
from extensions.formula1.state import Formula1State
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
    facts_resolved: int
    facts_missing: int
    facts_stale: int
    venues_canonicalized: int
    profiles_resolved: int
    profiles_missing: int
    issues: int
    cleanup_removed: int
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
        "facts_resolved": 0,
        "facts_missing": 0,
        "facts_stale": 0,
        "venues_canonicalized": 0,
        "profiles_resolved": 0,
        "profiles_missing": 0,
        "issues": 0,
        "cleanup_removed": 0,
        "issue_report": None,
        "log": None,
    }
    issues = []
    current_keys = set()
    state.start_run(run_id)
    try:
        detail_logger.info("[Startup] Formula 1 extension | Run: %s", run_id)
        for section in sections:
            section_type = str(
                getattr(section, "type", None) or getattr(section, "TYPE", "")
            ).casefold()
            if section_type not in {"show", "tv"}:
                raise RuntimeError(f"Formula 1 library must be a Plex TV library: {section.title}")
            inventory_config = {
                "runtime": core_config.get("runtime", {}),
                "formula1": config,
            }
            inventory = await discover_formula1_inventory(section, inventory_config, detail_logger)
            issues.extend(inventory.issues)
            summary["libraries"] += 1
            summary["shows"] += len(inventory.shows)
            for show in inventory.shows:
                races, source = await load_schedule(
                    session, state, config, show.year, detail_logger
                )
                detail_logger.info(
                    "[Provider] Schedule | Year: %d | Races: %d | Source: %s",
                    show.year,
                    len(races),
                    source,
                )
                inventory_rounds = {item.round_number for item in show.episodes}
                races, fact_statistics = await enrich_race_facts(
                    session,
                    state,
                    config,
                    races,
                    inventory_rounds,
                    detail_logger,
                )
                summary["facts_resolved"] += fact_statistics["resolved"]
                summary["facts_missing"] += fact_statistics["missing"]
                summary["facts_stale"] += fact_statistics["stale"]
                summary["venues_canonicalized"] += fact_statistics["canonicalized"]
                summary["profiles_resolved"] += fact_statistics["profiles_resolved"]
                summary["profiles_missing"] += fact_statistics["profiles_missing"]
                issues.extend(fact_statistics["issues"])
                race_by_round = {race.round_number: race for race in races}
                poster_references = {}
                for round_number in sorted({item.round_number for item in show.episodes}):
                    race = race_by_round.get(round_number)
                    if race is None:
                        issues.append(f"No schedule match: {show.title} round {round_number}")
                        continue
                    destination = (
                        config["paths"]["assets"]
                        / str(show.year)
                        / f"round-{round_number:02d}"
                        / "poster.png"
                    )
                    path_data, shape_source = await load_circuit_path(
                        session, state, config, race, detail_logger
                    )
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
                if config["metadata"].get("enabled", True):
                    destination, changed, diagnostics = write_show_metadata(
                        show, races, poster_references, config
                    )
                    summary["metadata_updated" if changed else "metadata_unchanged"] += 1
                    detail_logger.info(
                        "[Metadata] %s | Action: %s | Destination: %s | Fields: %d",
                        show.title,
                        "updated" if changed else "unchanged",
                        destination,
                        diagnostics["available"],
                    )
                for episode in show.episodes:
                    current_keys.add(episode.logical_key)
                    state.bind(
                        episode.logical_key,
                        episode.plex_rating_key,
                        episode.media_path,
                        f"{episode.event_name} - {episode.program_title}",
                        episode.naming_profile,
                    )
                summary["episodes"] += len(show.episodes)

        stale = state.reconcile_bindings(
            current_keys,
            cleanup=bool(config["cleanup"].get("enabled", False)),
            confirmation_scans=config["cleanup"]["confirmation_scans"],
            grace_hours=config["cleanup"]["grace_hours"],
        )
        active_rounds = {key.rsplit(":e", 1)[0] for key in current_keys}
        removed_rounds = set()
        for binding in stale:
            round_key = binding["logical_key"].rsplit(":e", 1)[0]
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
                "Circuit facts resolved/missing: %d/%d | Circuit profiles resolved/missing: "
                "%d/%d | Venues canonicalized: %d | Issues: %d",
                summary["libraries"],
                summary["shows"],
                summary["episodes"],
                summary["metadata_updated"],
                summary["artwork_created"],
                summary["artwork_updated"],
                summary["facts_resolved"],
                summary["facts_missing"],
                summary["profiles_resolved"],
                summary["profiles_missing"],
                summary["venues_canonicalized"],
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
