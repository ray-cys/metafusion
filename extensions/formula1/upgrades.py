"""Explicit, audited Flickr upgrades for existing Formula 1 artwork bindings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from extensions.formula1.commons import CommonsCandidate, acquire_candidate_image, load_constructors
from extensions.formula1.flickr import search_flickr_backgrounds, search_flickr_team_photos
from extensions.formula1.race_background import RaceBackgroundCandidate
from extensions.formula1.show_artwork import (
    _acquire_background_image,
    _asset_integrity,
    _perceptual_hash,
    _save_round_source,
    _show_render_fingerprint,
    _show_render_fingerprints,
    reconcile_episode_posters,
    render_show_background,
    render_show_poster,
    write_attribution_reports,
)
from helper.io import atomic_write_json, atomic_write_text

POLICY_VERSION = 1


@dataclass(frozen=True)
class Formula1UpgradeResult:
    records: tuple[dict, ...] = ()
    changed: bool = False
    episode_actions: dict[tuple[int, int], str] = field(default_factory=dict)


def _image_quality(path):
    with Image.open(path) as source:
        width, height = source.size
        sample = source.convert("L")
        sample.thumbnail((640, 360), Image.Resampling.LANCZOS)
        edges = sample.filter(ImageFilter.FIND_EDGES)
        sharpness = float(ImageStat.Stat(edges).stddev[0])
    return {
        "width": width,
        "height": height,
        "pixels": width * height,
        "sharpness": round(sharpness, 4),
    }


def _quality_decision(old_path, new_path, *, specificity_gain=False):
    new = _image_quality(new_path)
    if old_path is None or not Path(old_path).is_file():
        return (
            False,
            "existing source unavailable; quality gain cannot be verified",
            {"old": None, "new": new},
        )
    old = _image_quality(old_path)
    resolution_ratio = new["pixels"] / max(1, old["pixels"])
    sharpness_ratio = new["sharpness"] / max(0.01, old["sharpness"])
    duplicate = _perceptual_hash(old_path) == _perceptual_hash(new_path)
    better = (
        (resolution_ratio >= 1.20 and sharpness_ratio >= 0.80)
        or (sharpness_ratio >= 1.15 and resolution_ratio >= 0.90)
        or (resolution_ratio >= 1.05 and sharpness_ratio >= 1.05)
        or (specificity_gain and resolution_ratio >= 0.90 and sharpness_ratio >= 0.85)
    )
    reason = (
        "higher-resolution source"
        if resolution_ratio >= 1.20 and sharpness_ratio >= 0.80
        else "sharper source"
        if sharpness_ratio >= 1.15 and resolution_ratio >= 0.90
        else "improved resolution and sharpness"
        if resolution_ratio >= 1.05 and sharpness_ratio >= 1.05
        else "stronger event/circuit specificity"
        if specificity_gain and better
        else "candidate did not materially improve decoded quality"
    )
    return better, reason, {
        "old": old,
        "new": new,
        "resolution_ratio": round(resolution_ratio, 4),
        "sharpness_ratio": round(sharpness_ratio, 4),
        "perceptual_duplicate": duplicate,
    }


def _source_name(candidate):
    return candidate.source_sha1 or hashlib.sha256(candidate.image_url.encode()).hexdigest()


async def _old_team_photo(session, config, candidate):
    try:
        path, _source = await acquire_candidate_image(session, config, candidate)
        return path
    except RuntimeError:
        return None


async def _select_team_upgrade(session, state, config, year, candidate, logger):
    if candidate.provider == "flickr":
        return None, "already uses Flickr", {}
    roster, roster_source = await load_constructors(session, state, config, year, logger)
    constructor = next(
        (item for item in roster if item.constructor_id == candidate.constructor_id),
        None,
    )
    if constructor is None:
        return None, "stored constructor is absent from the current roster", {}
    candidates, search_source = await search_flickr_team_photos(
        session, state, config, year, constructor, roster, logger
    )
    old_path = await _old_team_photo(session, config, candidate)
    diagnostics = []
    for replacement in candidates:
        try:
            new_path, image_source = await acquire_candidate_image(
                session, config, replacement
            )
        except RuntimeError as error:
            diagnostics.append(str(error))
            continue
        better, reason, evidence = _quality_decision(old_path, new_path)
        if better:
            evidence.update(
                roster_source=roster_source,
                search_source=search_source,
                image_source=image_source,
                reason=reason,
            )
            return (replacement, new_path), reason, evidence
    reason = diagnostics[-1] if diagnostics else "no materially better Flickr team-car source"
    return None, reason, {}


def _background_tier(candidate):
    return {
        "current_season_team_car_fallback": 0,
        "historical_circuit_action_race_car": 1,
        "recent_circuit_action_race_car": 2,
        "exact_event_action_race_car": 3,
    }.get(candidate.match_tier, 0)


async def _select_background_upgrade(session, state, config, race, candidate, logger):
    if candidate.provider == "flickr":
        return None, "already uses Flickr", {}
    candidates, search_source = await search_flickr_backgrounds(
        session, state, config, race, logger
    )
    try:
        old_path, _old_source = await _acquire_background_image(session, config, candidate)
    except RuntimeError:
        old_path = None
    diagnostics = []
    for replacement in candidates:
        try:
            new_path, image_source = await _acquire_background_image(
                session, config, replacement
            )
        except RuntimeError as error:
            diagnostics.append(str(error))
            continue
        specificity_gain = _background_tier(replacement) > _background_tier(candidate)
        better, reason, evidence = _quality_decision(
            old_path, new_path, specificity_gain=specificity_gain
        )
        if better:
            evidence.update(
                search_source=search_source,
                image_source=image_source,
                reason=reason,
                old_match_tier=candidate.match_tier,
                new_match_tier=replacement.match_tier,
            )
            return (replacement, new_path), reason, evidence
    reason = diagnostics[-1] if diagnostics else "no materially better Flickr background source"
    return None, reason, {}


def _record(state, run_id, scope, year, round_number, lane, status, old, new, details):
    audit_details = {
        **details,
        "old_candidate": old.as_dict() if old is not None else None,
        "new_candidate": new.as_dict() if new is not None else None,
    }
    record = {
        "run_id": run_id,
        "policy_version": POLICY_VERSION,
        "scope": scope,
        "season_year": int(year),
        "round_number": int(round_number),
        "lane": lane,
        "status": status,
        "old_provider": getattr(old, "provider", None),
        "new_provider": getattr(new, "provider", None),
        "old_source": _source_name(old) if old is not None else None,
        "new_source": _source_name(new) if new is not None else None,
        "details": audit_details,
    }
    state.record_artwork_upgrade(
        run_id,
        POLICY_VERSION,
        scope,
        year,
        round_number,
        lane,
        status,
        old_provider=record["old_provider"],
        new_provider=record["new_provider"],
        old_source=record["old_source"],
        new_source=record["new_source"],
        details=audit_details,
    )
    return record


async def _upgrade_episode_round(
    session, state, config, show, race, path_data, scope, run_id, logger
):
    binding = state.episode_round_source(show.year, race.round_number)
    if binding is None:
        return [], {}, False
    old = CommonsCandidate.from_dict(binding["source"]["candidate"])
    selected, reason, evidence = await _select_team_upgrade(
        session, state, config, show.year, old, logger
    )
    if selected is None:
        status = "already-flickr" if old.provider == "flickr" else "no-better-candidate"
        return [
            _record(
                state, run_id, scope, show.year, race.round_number,
                "episode", status, old, None, {"reason": reason}
            )
        ], {}, False
    replacement, photo_path = selected
    source_identity = _source_name(replacement)
    references, actions = reconcile_episode_posters(
        state, config, show, race, path_data, photo_path, source_identity
    )
    del references
    changed = any(action in {"create", "update"} for action in actions.values())
    manual = sum(action == "preserve-manual" for action in actions.values())
    if changed:
        _save_round_source(
            state,
            config,
            show.year,
            race.round_number,
            replacement,
            {
                "roster": evidence.get("roster_source"),
                "search": evidence.get("search_source"),
                "image": evidence.get("image_source"),
                "upgrade": f"explicit-{scope}",
            },
            photo_path,
            source_identity,
        )
    status = "upgraded" if changed else "preserved-manual" if manual else "unchanged"
    evidence = {**evidence, "actions": actions, "manual_preserved": manual}
    return [
        _record(
            state, run_id, scope, show.year, race.round_number,
            "episode", status, old, replacement if changed else None, evidence
        )
    ], {(race.round_number, key): value for key, value in actions.items()}, changed


async def _upgrade_active_show(
    session, state, config, show, race, path_data, scope, run_id, logger
):
    logical_key = f"show:{show.year}"
    current = state.show_rotation(logical_key)
    if current is None or int(current["trigger_round"]) != int(race.round_number):
        return [], False
    source = dict(current["source"])
    records = []
    changed = False
    poster_checksum = current["poster_checksum"]
    background_checksum = current["background_checksum"]
    poster = CommonsCandidate.from_dict(source["candidate"])
    poster_integrity = _asset_integrity(current["poster_destination"], poster_checksum)
    if poster_integrity == "manual":
        records.append(
            _record(
                state, run_id, scope, show.year, race.round_number,
                "show_poster", "preserved-manual", poster, None,
                {"reason": "managed poster checksum changed"},
            )
        )
    else:
        selected, reason, evidence = await _select_team_upgrade(
            session, state, config, show.year, poster, logger
        )
        if selected is None:
            status = "already-flickr" if poster.provider == "flickr" else "no-better-candidate"
            records.append(
                _record(
                    state, run_id, scope, show.year, race.round_number,
                    "show_poster", status, poster, None, {"reason": reason},
                )
            )
        else:
            replacement, photo_path = selected
            poster_checksum = render_show_poster(
                show, race, path_data, photo_path, config, Path(current["poster_destination"])
            )
            source.update(
                candidate=replacement.as_dict(),
                photo_cache=str(photo_path),
                source_identity=_source_name(replacement),
                provider_sources={
                    "roster": evidence.get("roster_source"),
                    "search": evidence.get("search_source"),
                    "image": evidence.get("image_source"),
                    "upgrade": f"explicit-{scope}",
                },
            )
            records.append(
                _record(
                    state, run_id, scope, show.year, race.round_number,
                    "show_poster", "upgraded", poster, replacement, evidence,
                )
            )
            changed = True

    background_value = source.get("background_candidate")
    if background_value:
        background = RaceBackgroundCandidate.from_dict(background_value)
        background_integrity = _asset_integrity(
            current["background_destination"], background_checksum
        )
        if background_integrity == "manual":
            records.append(
                _record(
                    state, run_id, scope, show.year, race.round_number,
                    "show_background", "preserved-manual", background, None,
                    {"reason": "managed background checksum changed"},
                )
            )
        else:
            selected, reason, evidence = await _select_background_upgrade(
                session, state, config, race, background, logger
            )
            if selected is None:
                status = (
                    "already-flickr"
                    if background.provider == "flickr"
                    else "no-better-candidate"
                )
                records.append(
                    _record(
                        state, run_id, scope, show.year, race.round_number,
                        "show_background", status, background, None, {"reason": reason},
                    )
                )
            else:
                replacement, photo_path = selected
                background_checksum = render_show_background(
                    show,
                    race,
                    photo_path,
                    config,
                    Path(current["background_destination"]),
                )
                source.update(
                    background_candidate=replacement.as_dict(),
                    background_photo_cache=str(photo_path),
                    background_source_identity=_source_name(replacement),
                    background_perceptual_hash=_perceptual_hash(photo_path),
                    background_provider_sources={
                        "search": evidence.get("search_source"),
                        "image": evidence.get("image_source"),
                        "upgrade": f"explicit-{scope}",
                        "match_tier": replacement.match_tier,
                    },
                )
                records.append(
                    _record(
                        state, run_id, scope, show.year, race.round_number,
                        "show_background", "upgraded", background, replacement, evidence,
                    )
                )
                changed = True

    if changed:
        poster_fingerprint, background_fingerprint = _show_render_fingerprints(config)
        generated = dict(source.get("generated_checksums") or {})
        generated.update(poster=poster_checksum, background=background_checksum)
        source.update(
            generated_checksums=generated,
            poster_render_fingerprint=poster_fingerprint,
            background_render_fingerprint=background_fingerprint,
            render_fingerprint=_show_render_fingerprint(config),
            upgrade_policy_version=POLICY_VERSION,
        )
        state.save_show_rotation(
            logical_key,
            show.year,
            race.round_number,
            source["candidate"]["constructor_id"],
            source,
            current["poster_destination"],
            poster_checksum,
            current["background_destination"],
            background_checksum,
        )
    return records, changed


async def upgrade_formula1_artwork(
    session,
    state,
    config,
    show,
    race_by_round,
    circuit_paths,
    scope,
    run_id,
    logger,
):
    """Upgrade managed Flickr-eligible bindings for the active or all detected rounds."""
    if scope not in {"current", "all"}:
        raise ValueError("Formula 1 artwork upgrade scope must be current or all")
    if not config["providers"].get("flickr_enabled"):
        raise RuntimeError(
            "Formula 1 artwork upgrade requires providers.flickr_api_key or "
            "FORMULA1_FLICKR_API_KEY"
        )
    detected = sorted({episode.round_number for episode in show.episodes})
    if not detected:
        return Formula1UpgradeResult()
    active_round = detected[-1]
    selected_rounds = [active_round] if scope == "current" else detected
    records = []
    episode_actions = {}
    changed = False
    for round_number in selected_rounds:
        race = race_by_round.get(round_number)
        if race is None:
            continue
        round_records, actions, round_changed = await _upgrade_episode_round(
            session,
            state,
            config,
            show,
            race,
            circuit_paths.get(round_number),
            scope,
            run_id,
            logger,
        )
        records.extend(round_records)
        episode_actions.update(actions)
        changed |= round_changed
    active_race = race_by_round.get(active_round)
    if active_race is not None:
        show_records, show_changed = await _upgrade_active_show(
            session,
            state,
            config,
            show,
            active_race,
            circuit_paths.get(active_round),
            scope,
            run_id,
            logger,
        )
        records.extend(show_records)
        changed |= show_changed
    if changed:
        write_attribution_reports(state, config)
    return Formula1UpgradeResult(tuple(records), changed, episode_actions)


def write_upgrade_report(config, run_id, scope, records):
    if config["dry_run"]:
        return None
    report_root = config["paths"]["reports"]
    report_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "policy_version": POLICY_VERSION,
        "scope": scope,
        "records": list(records),
    }
    json_path = report_root / f"formula1-artwork-upgrade-{run_id}.json"
    text_path = report_root / f"formula1-artwork-upgrade-{run_id}.txt"
    atomic_write_json(json_path, payload)
    lines = [
        "MetaFusion Formula 1 artwork upgrade",
        f"Run: {run_id}",
        f"Scope: {scope}",
        f"Policy version: {POLICY_VERSION}",
        "",
    ]
    for record in records:
        lines.append(
            f"{record['season_year']} round {record['round_number']:02d} | "
            f"{record['lane']} | {record['status']} | "
            f"{record.get('old_provider') or 'none'} -> "
            f"{record.get('new_provider') or 'none'}"
        )
        reason = record.get("details", {}).get("reason")
        if reason:
            lines.append(f"  Reason: {reason}")
    atomic_write_text(text_path, "\n".join(lines) + "\n")
    return json_path
