"""Durable TMDb change-feed planning for incremental rechecks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from helper.state_db import (
    STATE_DATABASE,
    StateDatabaseError,
    load_application_record,
    save_application_record,
)
from helper.tmdb import tmdb_api_request

CHANGE_STATE_KEY = "tmdb_change_feed_v1"
MAX_CHANGE_PAGES = 1000


class TMDbChangeFeedError(RuntimeError):
    """Raised when a complete, trustworthy TMDb change window cannot be read."""


def _utc(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def prepare_tmdb_change_plan(
    config,
    scan_decisions,
    *,
    targeted=False,
    now=None,
    path=None,
):
    """Plan a bounded change window or require one authoritative baseline scan."""
    incremental = config.get("incremental", {})
    current = _utc(now)
    plan = {
        "enabled": bool(
            incremental.get("enabled", True) and incremental.get("tmdb_change_rechecks", True)
        ),
        "status": "disabled",
        "force_full_scan": False,
        "checkpoint_candidate": None,
    }
    if not plan["enabled"]:
        return plan
    if targeted:
        plan["status"] = "targeted_run"
        return plan
    if config.get("settings", {}).get("dry_run", False):
        plan["status"] = "dry_run"
        return plan

    plan["checkpoint_candidate"] = current.isoformat()
    if scan_decisions and all(bool(value) for value in scan_decisions.values()):
        plan["status"] = "baseline_full_scan"
        return plan

    try:
        saved = load_application_record(CHANGE_STATE_KEY, path=path or STATE_DATABASE)
    except StateDatabaseError:
        saved = None
    try:
        completed = _utc((saved or {}).get("completed_at"))
    except (TypeError, ValueError):
        completed = None
    if completed is None or completed > current:
        plan.update(status="baseline_required", force_full_scan=True)
        return plan

    # TMDb accepts at most a 14-day change window. Date parameters are inclusive,
    # so a 13-day difference is the largest unambiguous 14-calendar-day window.
    if (current.date() - completed.date()).days > 13:
        plan.update(status="checkpoint_stale", force_full_scan=True)
        return plan

    plan.update(
        status="ready",
        start_date=completed.date().isoformat(),
        end_date=current.date().isoformat(),
        previous_checkpoint=completed.isoformat(),
    )
    return plan


async def _changed_ids_for_type(config, media_type, local_ids, plan, session):
    local = {str(value) for value in local_ids or [] if str(value).strip()}
    if not local:
        return set(), 0
    matched = set()
    page = 1
    total_pages = 1
    while page <= total_pages:
        response = await tmdb_api_request(
            config,
            f"{media_type}/changes",
            params={
                "start_date": plan["start_date"],
                "end_date": plan["end_date"],
                "page": page,
            },
            cache=False,
            include_locale=False,
            session=session,
        )
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise TMDbChangeFeedError(
                f"TMDb {media_type} change page {page} was unavailable or invalid"
            )
        try:
            advertised_pages = max(1, int(response.get("total_pages") or 1))
        except (TypeError, ValueError) as error:
            raise TMDbChangeFeedError(f"TMDb {media_type} change page count was invalid") from error
        if advertised_pages > MAX_CHANGE_PAGES:
            raise TMDbChangeFeedError(
                f"TMDb {media_type} change window exceeds the {MAX_CHANGE_PAGES}-page safety limit"
            )
        total_pages = max(total_pages, advertised_pages)
        for record in response["results"]:
            if isinstance(record, dict) and record.get("id") is not None:
                candidate = str(record["id"])
                if candidate in local:
                    matched.add(candidate)
        page += 1
    return matched, total_pages


async def collect_tmdb_change_rechecks(config, plan, inventory_by_library, session):
    """Return changed Plex rating keys only after both feeds complete."""
    if plan.get("status") != "ready":
        return {
            "rating_keys": {},
            "changed_ids": {"movie": [], "tv": []},
            "pages": {"movie": 0, "tv": 0},
            "selected_items": {"movie": 0, "tv": 0},
        }
    local_ids = {"movie": set(), "tv": set()}
    for records in inventory_by_library.values():
        for record in records or []:
            media_type = str(record.get("media_type") or "").lower()
            if media_type in {"show", "shows"}:
                media_type = "tv"
            if media_type in local_ids and record.get("tmdb_id") is not None:
                local_ids[media_type].add(str(record["tmdb_id"]))

    movie_result, tv_result = await asyncio.gather(
        _changed_ids_for_type(config, "movie", local_ids["movie"], plan, session),
        _changed_ids_for_type(config, "tv", local_ids["tv"], plan, session),
    )
    changed_ids = {"movie": movie_result[0], "tv": tv_result[0]}
    rating_keys = {}
    selected_items = {"movie": set(), "tv": set()}
    for library, records in inventory_by_library.items():
        selected = set()
        for record in records or []:
            media_type = str(record.get("media_type") or "").lower()
            if media_type in {"show", "shows"}:
                media_type = "tv"
            tmdb_id = record.get("tmdb_id")
            rating_key = record.get("rating_key")
            if (
                media_type in changed_ids
                and tmdb_id is not None
                and str(tmdb_id) in changed_ids[media_type]
                and rating_key is not None
            ):
                selected.add(str(rating_key))
                selected_items[media_type].add((str(library), str(rating_key)))
        if selected:
            rating_keys[str(library)] = selected
    return {
        "rating_keys": rating_keys,
        "changed_ids": {media_type: sorted(values) for media_type, values in changed_ids.items()},
        "pages": {"movie": movie_result[1], "tv": tv_result[1]},
        "selected_items": {
            media_type: len(values) for media_type, values in selected_items.items()
        },
    }


def commit_tmdb_change_checkpoint(plan, summary, *, path=None):
    """Advance the feed only after its associated MetaFusion job succeeded."""
    completed_at = plan.get("checkpoint_candidate")
    if not completed_at or plan.get("status") not in {
        "ready",
        "baseline_full_scan",
        "baseline_required",
        "checkpoint_stale",
    }:
        return False
    record = {
        "completed_at": str(completed_at),
        "mode": str(plan.get("status")),
        "movie_pages": int((summary or {}).get("pages", {}).get("movie", 0)),
        "tv_pages": int((summary or {}).get("pages", {}).get("tv", 0)),
        "movie_matches": len((summary or {}).get("changed_ids", {}).get("movie", [])),
        "tv_matches": len((summary or {}).get("changed_ids", {}).get("tv", [])),
    }
    return save_application_record(CHANGE_STATE_KEY, record, path=path or STATE_DATABASE)
