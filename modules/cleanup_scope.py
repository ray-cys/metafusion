"""Protect outputs shared with Plex libraries outside a full processing run."""

import asyncio
from dataclasses import dataclass, field

from helper.plex import load_plex_library_inventory
from helper.plex_paths import translate_plex_path


@dataclass
class PartialCleanupScope:
    owners: set = field(default_factory=set)
    protected_titles: set = field(default_factory=set)
    protected_dirs: set = field(default_factory=set)
    protected_keys: set = field(default_factory=set)


async def protected_inventory(plex, library, scope, config):
    section = await asyncio.to_thread(plex.library.section, library["title"])
    if str(section.uuid or section.key) != str(library["uuid"]):
        raise ValueError("Plex library identity changed")
    items = await load_plex_library_inventory(section, config.get("runtime", {}))
    for item in items:
        locations = getattr(item, "locations", None)
        if not item.ratingKey or not item.title or item.year is None or not locations:
            raise ValueError("Plex returned incomplete identity or location information")
        scope.protected_keys.add(str(item.ratingKey))
        scope.protected_titles.add((item.title, item.year))
        for location in locations:
            path = translate_plex_path(location, config.get("plex", {}).get("path_mappings"))
            scope.protected_dirs.add(path.parent if section.type == "movie" else path)


async def cleanup_scopes(plex, libraries, scans, complete, config):
    partial = {}
    descriptions = {}
    for kind, aliases in (("movie", {"movie", "movies"}), ("tv", {"tv", "show", "shows"})):
        detected = [lib for lib in libraries if lib["type"].lower() in aliases]
        if not detected:
            continue
        if kind in complete:
            descriptions[kind] = "Full inventory"
            continue
        names = {lib["title"] for lib in detected}
        owners = {
            (s["server_id"], s["library_uuid"])
            for s in scans
            if s["library_name"] in names and s["server_id"] != "unknown"
        }
        excluded = [
            lib for lib in detected if lib["title"] not in {s["library_name"] for s in scans}
        ]
        excluded_names = ", ".join(lib["title"] for lib in excluded)
        if not owners:
            descriptions[kind] = (
                f"Deferred | No verified library owner | Excluded: {excluded_names}"
            )
            continue
        scope = PartialCleanupScope(owners=owners)
        try:
            for lib in excluded:
                await protected_inventory(plex, lib, scope, config)
        except Exception as error:
            descriptions[kind] = (
                f"Deferred | Excluded: {excluded_names} | Inventory unavailable ({type(error).__name__})"
            )
            continue
        partial[kind] = scope
        descriptions[kind] = f"Library-owned outputs only | Excluded: {excluded_names}"
    return partial, descriptions
