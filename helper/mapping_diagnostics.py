"""Read-only Plex-to-TMDb season and episode mapping diagnosis."""

from datetime import datetime, timezone
from pathlib import Path

import yaml

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR, report_retention
from helper.io import atomic_write_text
from helper.plex import get_plex_metadata, load_plex_library_inventory
from helper.provider_mappings import (
    resolve_episode_overrides,
    resolve_split_series_mapping,
)
from helper.tmdb import (
    resolve_episode_group_mapping,
    resolve_tmdb_id,
    tmdb_api_request,
)


def _inventory_pairs(inventory):
    pairs = set()
    for season, episodes in (inventory or {}).items():
        try:
            season_number = int(season)
        except (TypeError, ValueError):
            continue
        for episode in episodes or []:
            try:
                pairs.add((season_number, int(episode)))
            except (TypeError, ValueError):
                continue
    return pairs


def _pair_label(pair):
    return f"S{int(pair[0]):02d}E{int(pair[1]):02d}"


async def _standard_episode_pairs(config, tmdb_id, plex_inventory, session):
    pairs = set()
    for season_number in sorted({pair[0] for pair in _inventory_pairs(plex_inventory)}):
        details = await tmdb_api_request(
            config,
            f"tv/{tmdb_id}/season/{season_number}",
            session=session,
            cache=False,
        )
        for episode in details.get("episodes", []) if isinstance(details, dict) else []:
            try:
                pairs.add(
                    (
                        int(episode.get("season_number", season_number)),
                        int(episode["episode_number"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return pairs


async def _split_series_pairs(
    config, tmdb_id, plex_inventory, split_mapping, session
):
    pairs = set()
    sources = split_mapping.get("seasons", {}) if split_mapping else {}
    for plex_season in sorted(
        {pair[0] for pair in _inventory_pairs(plex_inventory)}
    ):
        source = sources.get(plex_season, {})
        source_tmdb_id = str(source.get("tmdb_id") or tmdb_id)
        source_season = int(source.get("season_number", plex_season))
        details = await tmdb_api_request(
            config,
            f"tv/{source_tmdb_id}/season/{source_season}",
            session=session,
            cache=False,
        )
        for episode in details.get("episodes", []) if isinstance(details, dict) else []:
            try:
                pairs.add((plex_season, int(episode["episode_number"])))
            except (KeyError, TypeError, ValueError):
                continue
    return pairs


def _offset_override_proposal(plex_pairs, tmdb_pairs):
    """Propose only a unique, complete one-step numbering translation."""
    proposals = []
    for season_delta in (-1, 0, 1):
        for episode_delta in (-1, 0, 1):
            if not season_delta and not episode_delta:
                continue
            translated = {
                pair: (pair[0] + season_delta, pair[1] + episode_delta)
                for pair in plex_pairs
            }
            if all(target in tmdb_pairs for target in translated.values()):
                proposals.append(translated)
    if len(proposals) != 1:
        return {}
    return {
        _pair_label(source): _pair_label(target)
        for source, target in sorted(proposals[0].items())
        if source != target
    }


async def diagnose_mapping(item, config, session=None):
    """Compare one Plex show's numbering with safe TMDb mapping sources."""
    meta = await get_plex_metadata(
        item,
        _runtime_config=config.get("runtime", {}),
        _plex_config=config.get("plex", {}),
    )
    record = {
        "library": meta.get("library_name") or "Unknown library",
        "rating_key": str(meta.get("ratingKey") or "unknown"),
        "title": meta.get("title") or "Unknown title",
        "year": meta.get("year"),
        "media_type": meta.get("library_type") or "unknown",
    }
    if str(record["media_type"]).lower() not in {"show", "tv"}:
        record.update(
            status="unsupported",
            explanation="Mapping diagnosis applies only to Plex TV shows.",
        )
        return record

    tmdb_id = await resolve_tmdb_id(
        config,
        "tv",
        tmdb_id=meta.get("tmdb_id"),
        imdb_id=meta.get("imdb_id"),
        tvdb_id=meta.get("tvdb_id"),
        title=meta.get("title"),
        year=meta.get("year"),
        session=session,
        cache=False,
    )
    record["tmdb_id"] = tmdb_id
    if not tmdb_id:
        record.update(
            status="missing_identity",
            explanation="No safe TMDb show identity could be resolved.",
        )
        return record

    plex_inventory = meta.get("seasons_episodes") or {}
    plex_pairs = _inventory_pairs(plex_inventory)
    record["plex_episode_count"] = len(plex_pairs)
    if not plex_pairs:
        record.update(
            status="missing_inventory",
            explanation="Plex returned no numbered episodes for this show.",
        )
        return record

    tmdb_pairs = await _standard_episode_pairs(
        config, tmdb_id, plex_inventory, session
    )
    record["tmdb_standard_episode_count"] = len(tmdb_pairs)
    split_mapping = resolve_split_series_mapping(
        config,
        tmdb_id=tmdb_id,
        tvdb_id=meta.get("tvdb_id"),
        imdb_id=meta.get("imdb_id"),
    )
    overrides = resolve_episode_overrides(
        config,
        tmdb_id=tmdb_id,
        tvdb_id=meta.get("tvdb_id"),
        imdb_id=meta.get("imdb_id"),
    )
    translated_pairs = {overrides.get(pair, pair) for pair in plex_pairs}

    if plex_pairs <= tmdb_pairs:
        record.update(
            status="aligned",
            explanation="Every Plex episode exists in TMDb standard ordering.",
        )
        return record
    if overrides and translated_pairs <= tmdb_pairs:
        record.update(
            status="configured_override",
            explanation=(
                "Configured episode overrides safely cover the complete Plex inventory."
            ),
            configured_overrides={
                _pair_label(source): _pair_label(target)
                for source, target in sorted(overrides.items())
            },
        )
        return record
    if split_mapping:
        record["split_series_mapping"] = split_mapping
        split_pairs = await _split_series_pairs(
            config,
            tmdb_id,
            plex_inventory,
            split_mapping,
            session,
        )
        if plex_pairs <= split_pairs:
            record.update(
                status="split_series",
                explanation=(
                    "The configured split-series season sources safely cover the "
                    "complete Plex inventory."
                ),
            )
            return record

    episode_group = await resolve_episode_group_mapping(
        config,
        tmdb_id,
        plex_inventory,
        episode_ordering=meta.get("episode_ordering"),
        session=session,
        cache=False,
    )
    if episode_group:
        record.update(
            status="episode_group",
            episode_group_id=episode_group.get("group_id"),
            explanation=(
                "One TMDb episode group uniquely covers the complete Plex inventory; "
                "MetaFusion can use it automatically."
            ),
        )
        return record

    missing = sorted(plex_pairs - tmdb_pairs)
    record.update(
        status="unresolved",
        missing_standard=[_pair_label(pair) for pair in missing],
        explanation=(
            "TMDb standard ordering, configured overrides, and episode groups do "
            "not uniquely cover the complete Plex inventory. Existing metadata is "
            "preserved."
        ),
    )
    proposal = _offset_override_proposal(plex_pairs, tmdb_pairs)
    if proposal:
        identity = f"tmdb:{tmdb_id}"
        record["proposed_configuration"] = {
            "tmdb": {"episode_overrides": {identity: proposal}}
        }
        record["proposal_note"] = (
            "The proposed one-step offset is read-only guidance and must be reviewed."
        )
    return record


def write_mapping_diagnosis_report(records, *, base_dir=None, retention=10):
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"mapping-diagnosis-{timestamp}.txt"
    current = build_info()
    lines = [
        "MetaFusion read-only Plex/TMDb mapping diagnosis",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Version: {current['version']}",
        f"Commit: {current['commit']}",
        f"Items: {len(records)}",
        "",
        "No mapping or metadata was changed. Proposed configuration requires review.",
        "",
    ]
    for record in records:
        year = f" ({record.get('year')})" if record.get("year") else ""
        lines.extend(
            (
                f"## {record.get('title')}{year}",
                f"Library: {record.get('library')}",
                f"Plex rating key: {record.get('rating_key')}",
                f"TMDb ID: {record.get('tmdb_id') or 'unresolved'}",
                f"Status: {record.get('status')}",
                f"Explanation: {record.get('explanation')}",
            )
        )
        if record.get("missing_standard"):
            lines.append(
                "Missing from standard ordering: "
                + ", ".join(record["missing_standard"])
            )
        if record.get("episode_group_id"):
            lines.append(f"Unique episode group: {record['episode_group_id']}")
        if record.get("proposed_configuration"):
            lines.extend(
                (
                    "Proposed configuration:",
                    yaml.safe_dump(
                        record["proposed_configuration"],
                        sort_keys=False,
                    ).rstrip(),
                    record.get("proposal_note", "Review before applying."),
                )
            )
        lines.append("")
    atomic_write_text(path, "\n".join(lines).rstrip() + "\n")

    reports = sorted(report_dir.glob("mapping-diagnosis-*.txt"), reverse=True)
    for stale in reports[max(1, int(retention)) :]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path


async def run_mapping_diagnosis(
    sections, config, rating_keys, session=None, *, base_dir=None
):
    requested = {str(value) for value in rating_keys or [] if str(value).strip()}
    records = []
    found = set()
    for section in sections:
        items = await load_plex_library_inventory(
            section, config.get("runtime", {})
        )
        for item in items:
            rating_key = str(getattr(item, "ratingKey", ""))
            if rating_key not in requested:
                continue
            found.add(rating_key)
            records.append(await diagnose_mapping(item, config, session=session))
    for rating_key in sorted(requested - found):
        records.append(
            {
                "library": "not found",
                "rating_key": rating_key,
                "title": "Unknown title",
                "year": None,
                "status": "not_found",
                "explanation": "The rating key was not found in selected libraries.",
            }
        )
    report = write_mapping_diagnosis_report(
        records,
        base_dir=base_dir,
        retention=report_retention(config),
    )
    return records, report
