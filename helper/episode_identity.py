"""Evidence for matching a Plex episode to the provider episode at a numbered slot."""


def episode_evidence(plex, source):
    if not source:
        return "missing"
    if plex.get("tmdb_id") and source.get("id"):
        return "id" if str(plex["tmdb_id"]) == str(source["id"]) else "conflict"
    date = plex.get("date")
    title = str(plex.get("title") or "").strip().casefold()
    other = str(source.get("name") or "").strip().casefold()
    if date and source.get("air_date"):
        if date == source["air_date"]:
            return "title_date" if title and title == other else "date"
        if title and other and title != other:
            return "conflict"
    return "number"


async def anthology_sources(meta, mapping, fetch):
    """Resolve additional installments only with unique corroborating episode evidence."""
    ids = set(meta.get("plex_tmdb_ids") or ())
    if len(ids) < 2 or meta.get("manual_identity_override"):
        return mapping
    sources = dict((mapping or {}).get("seasons", {}))
    remaining = ids - {str(s["tmdb_id"]) for s in sources.values()}
    evidence = meta.get("episode_evidence") or {}
    candidates = {}
    for identifier in sorted(remaining):
        details = await fetch(identifier)
        if not isinstance(details, dict) or not isinstance(details.get("episodes"), list):
            # An unavailable candidate could tie a successful candidate.
            return {**(mapping or {}), "seasons": sources, "ambiguous_installments": True}
        candidates[identifier] = {
            e["episode_number"]: e
            for e in details["episodes"]
            if e.get("episode_number") is not None
        }
    proposals = {}
    for season, episodes in (meta.get("seasons_episodes") or {}).items():
        if int(season) in sources:
            continue
        matches = []
        for identifier, indexed in candidates.items():
            results = [
                episode_evidence(evidence.get((int(season), int(n)), {}), indexed.get(n))
                for n in episodes
            ]
            if "conflict" not in results and ("id" in results or results.count("title_date") >= 2):
                matches.append(identifier)
        if len(matches) == 1:
            proposals[int(season)] = matches[0]
    for season, identifier in proposals.items():
        if list(proposals.values()).count(identifier) == 1:
            sources[season] = {"tmdb_id": identifier, "season_number": 1}
    return {**(mapping or {}), "seasons": sources, "ambiguous_installments": True}


def episode_target(mapping, overrides, tmdb_id, season, episode):
    source = (mapping or {}).get("seasons", {}).get(season, {})
    if not source and (mapping or {}).get("ambiguous_installments"):
        return None
    source_season, source_episode = overrides.get(
        (season, episode), (source.get("season_number", season), episode)
    )
    return str(source.get("tmdb_id") or tmdb_id), int(source_season), int(source_episode)


def verified_group(meta, group, missing_pairs):
    if not group:
        return None
    direct = str(meta.get("episode_ordering") or "").casefold() in {
        "tmdb",
        "tmdbaired",
        "tmdbairing",
        "tmdb_aired",
    }
    selected = {}
    for pair, episode in group.get("episodes", {}).items():
        evidence = episode_evidence(meta.get("episode_evidence", {}).get(pair, {}), episode)
        if evidence == "conflict":
            return None
        if direct and pair in missing_pairs and evidence not in {"id", "title_date"}:
            return None
        if not direct or pair in missing_pairs:
            selected[pair] = episode
    return {**group, "episodes": selected}
