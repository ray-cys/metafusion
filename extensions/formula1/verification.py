"""Delayed, read-only verification of Formula 1 output after Kometa applies it."""

from __future__ import annotations

import asyncio
from pathlib import Path

from helper.io import atomic_write_json
from helper.kometa_application_verification import compare_kometa_entry
from helper.plex_artwork_verification import _download_plex_image, _hash_distance
from helper.plex_metadata import _existing_children
from modules.utils import analyze_image_content


def _selected_path(show_item, child_key, asset_type):
    target = show_item if not child_key else _existing_children(show_item).get(child_key)
    if target is None:
        return None
    return getattr(target, "art" if asset_type == "background" else "thumb", None)


async def _verify_artwork(show_item, expectation, core_config, extension_config, session):
    destination = Path(expectation["destination"])
    record = dict(expectation)
    if not destination.is_file():
        record.update(status="local_missing", reason="generated local artwork is missing")
        return record
    selected = _selected_path(
        show_item, expectation.get("child_key"), expectation["asset_type"]
    )
    plex_content, error = await _download_plex_image(core_config, selected, session)
    if not plex_content:
        record.update(status="plex_unavailable", reason=error)
        return record
    try:
        local_content = await asyncio.to_thread(destination.read_bytes)
        local_analysis, plex_analysis = await asyncio.gather(
            asyncio.to_thread(
                analyze_image_content,
                local_content,
                asset_type=expectation["asset_type"],
            ),
            asyncio.to_thread(
                analyze_image_content,
                plex_content,
                asset_type=expectation["asset_type"],
            ),
        )
    except (OSError, ValueError) as validation_error:
        record.update(status="unverifiable", reason=str(validation_error))
        return record
    distance = _hash_distance(
        local_analysis.get("perceptual_hash"), plex_analysis.get("perceptual_hash")
    )
    exact = local_analysis.get("content_sha256") == plex_analysis.get("content_sha256")
    accepted = exact or (
        distance is not None
        and distance <= extension_config["verification"]["perceptual_distance"]
    )
    record.update(
        status="selected" if accepted else "not_selected",
        exact_match=exact,
        perceptual_distance=distance,
        reason=(
            "Plex-selected artwork matches generated output"
            if accepted
            else "Plex currently exposes different selected artwork"
        ),
    )
    return record


async def verify_due_applications(
    state, shows, config, core_config, session, run_id, detail_logger
):
    """Verify prior-run expectations only after their configured delay has elapsed."""
    if not config["verification"]["enabled"]:
        return [], None
    by_identity = {(show.year, str(show.plex_rating_key)): show for show in shows}
    records = []
    for queued in state.due_application_verifications():
        show = by_identity.get((queued["season_year"], str(queued["plex_rating_key"])))
        if show is None or show.plex_item is None:
            records.append(
                {
                    "season_year": queued["season_year"],
                    "status": "unverifiable",
                    "reason": "the original Plex show identity is not present",
                }
            )
            state.complete_application_verification(queued["season_year"])
            continue
        payload = queued["payload"]
        try:
            metadata = await asyncio.to_thread(
                compare_kometa_entry, payload["metadata"], show.plex_item, "show"
            )
        except Exception as error:
            metadata = {
                "status": "unverifiable",
                "reason": f"Plex metadata readback failed: {type(error).__name__}",
            }
        artwork = []
        for expectation in payload.get("artwork", []):
            artwork.append(
                await _verify_artwork(
                    show.plex_item, expectation, core_config, config, session
                )
            )
        artwork_ok = all(item["status"] == "selected" for item in artwork)
        status = "applied" if metadata.get("status") == "applied" and artwork_ok else "partial"
        record = {
            "season_year": queued["season_year"],
            "plex_rating_key": queued["plex_rating_key"],
            "show": show.title,
            "status": status,
            "metadata": metadata,
            "artwork": artwork,
        }
        records.append(record)
        state.complete_application_verification(queued["season_year"])
        detail_logger.info(
            "[Verification] %s | Status: %s | Metadata: %s | Artwork selected: %d/%d",
            show.title,
            status,
            metadata.get("status"),
            sum(item["status"] == "selected" for item in artwork),
            len(artwork),
        )
    if not records or config["dry_run"]:
        return records, None
    report_dir = config["paths"]["reports"]
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / f"formula1-application-verification-{run_id}.json"
    atomic_write_json(report, {"records": records})
    files = sorted(report_dir.glob("formula1-application-verification-*.json"), reverse=True)
    for path in files[config["verification"]["retention"] :]:
        path.unlink(missing_ok=True)
    return records, report


def queue_application_verification(
    state, show, entry, artwork, config
):
    if config["dry_run"] or not config["verification"]["enabled"]:
        return
    state.queue_application_verification(
        show.year,
        show.plex_rating_key,
        {"metadata": entry, "artwork": artwork},
        config["verification"]["delay_hours"],
    )
