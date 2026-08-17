from pathlib import Path, PurePosixPath


class PlexPathError(ValueError):
    """Raised when a Plex path mapping is unsafe or malformed."""


def parse_path_mappings(values):
    mappings = []
    sources = set()
    for raw in values or []:
        source, separator, destination = str(raw).partition("=>")
        source = source.strip().replace("\\", "/").rstrip("/")
        destination = destination.strip().replace("\\", "/").rstrip("/")
        if not separator or not source or not destination:
            raise PlexPathError(
                f"Invalid Plex path mapping {raw!r}; use SOURCE=>DESTINATION"
            )
        if not PurePosixPath(source).is_absolute():
            raise PlexPathError(f"Plex mapping source must be absolute: {source}")
        if not PurePosixPath(destination).is_absolute():
            raise PlexPathError(
                f"Plex mapping destination must be absolute: {destination}"
            )
        if (
            ".." in PurePosixPath(source).parts
            or ".." in PurePosixPath(destination).parts
        ):
            raise PlexPathError(f"Plex mapping contains unsafe traversal: {raw}")
        if source in sources:
            raise PlexPathError(f"Duplicate Plex mapping source: {source}")
        sources.add(source)
        mappings.append((source, destination))
    return sorted(mappings, key=lambda pair: len(pair[0]), reverse=True)


def translate_plex_path(value, mappings=None):
    """Translate a Plex-reported path using a longest component-prefix match."""
    original = str(value or "").strip().replace("\\", "/")
    if not original:
        raise PlexPathError("Plex returned an empty media path")
    translated = original
    for source, destination in parse_path_mappings(mappings):
        if original == source or original.startswith(f"{source}/"):
            suffix = original[len(source) :].lstrip("/")
            translated = f"{destination}/{suffix}" if suffix else destination
            break
    normalized = PurePosixPath(translated)
    if not normalized.is_absolute():
        raise PlexPathError(f"Plex returned a non-absolute media path: {value}")
    if ".." in normalized.parts:
        raise PlexPathError(f"Plex path contains unsafe traversal: {value}")
    return Path(str(normalized))
