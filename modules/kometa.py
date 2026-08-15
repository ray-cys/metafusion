EPISODE_BASIC_FIELDS = (
    "title",
    "sort_title",
    "originally_available",
    "summary",
)
EPISODE_ENHANCED_FIELDS = (
    "director",
    "writer",
)


def build_episode_metadata(episode, directors=None, writers=None, enhanced=True):
    name = episode.get("name") or ""
    metadata = {
        "title": name,
        "sort_title": name,
        "originally_available": episode.get("air_date") or "",
        "summary": episode.get("overview") or "",
    }
    if enhanced:
        metadata["director"] = [name for name in (directors or []) if name]
        metadata["writer"] = [name for name in (writers or []) if name]
    return metadata
