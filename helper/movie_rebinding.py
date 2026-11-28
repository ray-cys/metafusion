"""Inventory proof used to reconcile a movie that Plex recreated under a new key."""

from helper.plex_paths import translate_plex_path
from modules.utils import get_asset_path


def movie_rebind_context(config, scope, inventories):
    destinations = {}
    movies = [
        {**r, "library_name": name}
        for name, rows in inventories.items()
        for r in rows
        if r.get("media_type") == "movie"
    ]
    if all(r.get("movie_dir") for r in movies):
        for record in movies:
            directory = translate_plex_path(
                record["movie_dir"], config.get("plex", {}).get("path_mappings", [])
            )
            media = {
                "library_type": "movie",
                "movie_dir": str(directory),
                "movie_path": directory.name,
            }
            for asset in ("poster", "background"):
                if config.get("settings", {}).get("mode") == "plex":
                    path = directory / ("poster.jpg" if asset == "poster" else "fanart.jpg")
                else:
                    path = get_asset_path(config, media, asset_type=asset)
                if path and path.is_absolute():
                    destinations.setdefault(str(path), []).append(record)
    return {
        "scope": scope,
        "destinations": destinations,
        "live": {str(r["rating_key"]) for r in movies},
    }
