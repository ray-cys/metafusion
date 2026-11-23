"""Read-only evidence for Plex episode ordering, sampled only on library audit."""

import asyncio

from helper.provider_mappings import resolve_episode_overrides, resolve_split_series_mapping
from helper.tmdb import tmdb_api_request


async def audit_episode_order(section, shows, records, config, session):
    try:
        settings = await asyncio.to_thread(section.settings)
        default = next(
            (s.value for s in settings if s.id in {"showOrdering", "episodeOrdering"}), None
        )
    except Exception:
        default = None
    by_key = {r["rating_key"]: r for r in records}
    result = {"library_default": default or "unknown", "shows": []}
    for show in shows:
        record = by_key[str(show.ratingKey)]
        ids = {key: record.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")}
        split = resolve_split_series_mapping(config, **ids) or {}
        overrides = resolve_episode_overrides(config, **ids)
        row = {
            "title": record["title"],
            "rating_key": record["rating_key"],
            "split_mapping": split.get("identity"),
            "samples": [],
        }
        result["shows"].append(row)
        try:
            override = getattr(show, "showOrdering", "unknown")
            row["override"] = override or "library default"
            row["effective"] = override or result["library_default"]
            episodes = await asyncio.to_thread(show.episodes, maxresults=3)
            seasons = {}
            for episode in episodes[:3]:
                season, number = episode.parentIndex, episode.index
                source = split.get("seasons", {}).get(season, {})
                target_season, target_number = overrides.get(
                    (season, number), (source.get("season_number", season), number)
                )
                source_id = source.get("tmdb_id", ids["tmdb_id"])
                key = (source_id, target_season)
                if source_id and key not in seasons:
                    data = await tmdb_api_request(
                        config,
                        f"tv/{source_id}/season/{target_season}",
                        session=session,
                        cache=False,
                    )
                    seasons[key] = {
                        e["episode_number"]: e for e in (data or {}).get("episodes", [])
                    }
                target = seasons.get(key, {}).get(target_number, {})
                evidence = {
                    "plex_number": [season, number],
                    "tmdb_id": source_id,
                    "tmdb_number": [target_season, target_number],
                    "plex_title": episode.title,
                    "tmdb_title": target.get("name"),
                    "plex_date": str(episode.originallyAvailableAt or "")[:10],
                    "tmdb_date": target.get("air_date"),
                }
                comparisons = [
                    str(a).strip().casefold() == str(b).strip().casefold()
                    for a, b in (
                        (evidence["plex_title"], evidence["tmdb_title"]),
                        (evidence["plex_date"], evidence["tmdb_date"]),
                    )
                    if a and b
                ]
                evidence["status"] = (
                    "unknown" if not comparisons else "match" if all(comparisons) else "review"
                )
                row["samples"].append(evidence)
        except Exception as error:
            row["unavailable"] = type(error).__name__
    return result
