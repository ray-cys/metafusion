"""Read-only Plex-to-TMDb season and episode mapping diagnosis."""

from datetime import datetime, timezone
from pathlib import Path

import yaml

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR, report_retention
from helper.episode_identity import (
    anthology_sources,
    episode_evidence,
    episode_target,
    verified_group,
)
from helper.plex import get_plex_metadata, load_plex_library_inventory, read_library_order
from helper.provider_mappings import (
    resolve_episode_overrides,
    resolve_split_series_mapping,
)
from helper.report_identity import item_report_record, item_report_records
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
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


def _numbered_episodes(details):
    episodes = {}
    for entry in (details or {}).get("episodes", []):
        try:
            number = int(entry["episode_number"])
        except (TypeError, ValueError, KeyError):
            continue
        episodes[number] = entry
    return episodes


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
    record = item_report_record(
        {
            "library": meta.get("library_name") or "Unknown library",
            "rating_key": str(meta.get("ratingKey") or "unknown"),
            "title": meta.get("title") or "Unknown title",
            "year": meta.get("year"),
            "media_type": meta.get("library_type") or "unknown",
        },
        meta,
    )
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
    cache = {}
    async def season_data(identifier, season):
        key = (str(identifier), season)
        if key not in cache:
            data = await tmdb_api_request(config, f"tv/{identifier}/season/{season}", session=session, cache=False)
            cache[key] = data or {}
        return cache[key]

    split_mapping = await anthology_sources(meta, split_mapping, lambda identifier: season_data(identifier, 1))
    indexes = {}
    decisions = []
    missing_pairs = set()
    record["episode_sources"] = decisions
    record["episode_ordering"] = meta.get("episode_ordering")
    if split_mapping:
        record["split_series_mapping"] = split_mapping
    for pair in sorted(plex_pairs):
        target = episode_target(split_mapping, overrides, tmdb_id, *pair)
        result = "unverified_installment"
        if target:
            source_id, season, number = target
            key = (source_id, season)
            if key not in indexes:
                indexes[key] = _numbered_episodes(await season_data(source_id, season))
            source = indexes[key].get(number)
            result = episode_evidence(meta.get("episode_evidence", {}).get(pair, {}), source)
        decisions.append({"plex": _pair_label(pair), "tmdb": target, "evidence": result})
        if result in {"missing", "conflict", "unverified_installment"}:
            missing_pairs.add(pair)
    standard_pairs = {
        (season, number) for (identifier, season), episodes in indexes.items()
        if identifier == str(tmdb_id) for number in episodes
    }
    record["tmdb_standard_episode_count"] = len(standard_pairs)
    if overrides:
        record["configured_overrides"] = {_pair_label(k): _pair_label(v) for k, v in overrides.items()}
    if not missing_pairs:
        status = "split_series" if split_mapping else "configured_override" if overrides else "aligned"
        record.update(status=status, explanation="All Plex episode slots have provider sources under the effective mapping.")
        return record

    episode_group = None if split_mapping or overrides else await resolve_episode_group_mapping(
        config,
        tmdb_id,
        plex_inventory,
        episode_ordering=meta.get("episode_ordering"),
        session=session,
        cache=False,
    )
    episode_group = verified_group(meta, episode_group, missing_pairs)
    if episode_group:
        for decision, pair in zip(decisions, sorted(plex_pairs), strict=True):
            if pair in episode_group["episodes"]:
                ep = episode_group["episodes"][pair]
                decision.update(tmdb=(tmdb_id, ep.get("season_number"), ep.get("episode_number")), evidence="episode_group")
        record.update(
            status="episode_group",
            episode_group_id=episode_group.get("group_id"),
            explanation=(
                "One TMDb episode group uniquely covers the complete Plex inventory; "
                "MetaFusion can use it automatically."
            ),
        )
        return record

    missing = sorted(missing_pairs)
    record.update(
        status="unresolved",
        missing_standard=[_pair_label(pair) for pair in missing],
        explanation=(
            "TMDb standard ordering, configured overrides, and episode groups do "
            "not establish every episode identity. Unresolved episodes are preserved; "
            "verified episodes remain eligible for updates."
        ),
    )
    proposal = {}
    if not split_mapping and not overrides and all(d["evidence"] != "conflict" for d in decisions):
        proposal = _offset_override_proposal(plex_pairs, standard_pairs)
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
    records = item_report_records(records)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    generated = datetime.now(timezone.utc)
    timestamp = generated.strftime("%Y%m%d-%H%M%S%f")
    path = report_dir / f"mapping-diagnosis-{timestamp}.txt"
    current = build_info()
    lines = [
        "MetaFusion read-only Plex/TMDb mapping diagnosis",
        f"Generated: {generated.isoformat()}",
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
                f"Effective Plex ordering: {record.get('episode_ordering') or 'unknown'}",
                f"Status: {record.get('status')}",
                f"Explanation: {record.get('explanation')}",
            )
        )
        for source in record.get("episode_sources", []):
            lines.append(f"{source['plex']} -> TMDb {source['tmdb']} ({source['evidence']})")
        if record.get("missing_standard"):
            lines.append(
                "Unresolved Plex episode slots: "
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
    path = write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type="mapping_diagnosis",
        data={"items": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "mapping-diagnosis", retention)
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
            plex_config = {**config.get("plex", {}), "_library_order": await read_library_order(section)}
            records.append(await diagnose_mapping(item, {**config, "plex": plex_config}, session=session))
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
