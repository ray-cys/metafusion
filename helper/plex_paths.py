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


def visible_mount_roots(mountinfo_path="/proc/self/mountinfo"):
    """Return plausible user volume roots without walking their contents."""
    roots = set()
    try:
        lines = Path(mountinfo_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    ignored = (
        "/dev",
        "/proc",
        "/sys",
        "/etc",
        "/run",
        "/usr",
        "/var/lib",
    )
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4].replace("\\040", " ")
        if mount_point == "/" or mount_point.startswith(ignored):
            continue
        path = Path(mount_point)
        if path.is_dir():
            roots.add(path)
    for conventional in ("/media", "/mnt", "/movies", "/tv"):
        path = Path(conventional)
        if path.is_dir():
            roots.add(path)
    return sorted(roots, key=lambda path: (len(path.parts), str(path)))


def advise_path_mappings(
    reported_paths,
    mappings=None,
    *,
    mount_roots=None,
    exists=None,
):
    """Resolve visible Plex paths and infer only unique suffix-based mappings."""
    exists = (lambda path: Path(path).exists()) if exists is None else exists
    roots = list(visible_mount_roots() if mount_roots is None else mount_roots)
    records = []
    suggestions = set()
    for raw in sorted({str(value) for value in reported_paths or [] if value}):
        translated = translate_plex_path(raw, mappings)
        if exists(translated):
            records.append(
                {
                    "reported": raw,
                    "translated": str(translated),
                    "status": "resolved",
                }
            )
            continue
        parts = list(PurePosixPath(raw).parts)
        if parts and parts[0] == "/":
            parts = parts[1:]
        inferred = set()
        for root in roots:
            root = Path(root)
            for cut in range(1, len(parts)):
                candidate = root.joinpath(*parts[cut:])
                if not exists(candidate):
                    continue
                source = "/" + "/".join(parts[:cut])
                inferred.add((source, str(root), str(candidate)))
        if len(inferred) == 1:
            source, destination, candidate = next(iter(inferred))
            suggestion = f"{source}=>{destination}"
            suggestions.add(suggestion)
            records.append(
                {
                    "reported": raw,
                    "translated": candidate,
                    "status": "suggested",
                    "suggestion": suggestion,
                }
            )
        else:
            records.append(
                {
                    "reported": raw,
                    "translated": str(translated),
                    "status": "unresolved",
                }
            )
    return {"records": records, "suggestions": sorted(suggestions)}
