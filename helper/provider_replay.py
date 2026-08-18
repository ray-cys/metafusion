"""Sanitize and validate offline Plex/TMDb provider replay fixtures."""

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "password",
    "plex_token",
    "token",
    "x-plex-token",
}
_LOCAL_PATH_KEYS = {
    "file",
    "location",
    "locations",
    "movie_dir",
    "season_dirs",
    "show_dir",
}
_PRIVATE_PATH = re.compile(
    r"^(?:[A-Za-z]:\\|/(?:Users|home|mnt|media|storage|volume\d*|config|kometa)/)",
    re.IGNORECASE,
)


def _stable_identifier(value, prefix):
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _redact_url(value):
    parts = urlsplit(str(value))
    if not parts.scheme or not parts.netloc:
        return value
    query = [
        (key, "***" if key.casefold() in _SECRET_KEYS else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    hostname = parts.hostname or "provider.example.invalid"
    if hostname not in {"api.themoviedb.org", "image.tmdb.org"}:
        hostname = "provider.example.invalid"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, hostname + port, parts.path, urlencode(query), parts.fragment)
    )


def sanitize_provider_payload(payload):
    """Return a deterministic, share-safe copy of a provider response bundle."""

    def sanitize(value, key=None):
        normalized_key = str(key or "").casefold()
        if normalized_key in _SECRET_KEYS:
            return "***"
        if normalized_key in _LOCAL_PATH_KEYS:
            if isinstance(value, dict):
                return {
                    str(child): "<redacted-media-path>" for child in sorted(value)
                }
            if isinstance(value, list):
                return ["<redacted-media-path>" for _item in value]
            return "<redacted-media-path>"
        if normalized_key in {"ratingkey", "machineidentifier"} and value is not None:
            return _stable_identifier(value, "replay")
        if isinstance(value, dict):
            return {
                str(child): sanitize(item, child)
                for child, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [sanitize(item, key) for item in value]
        if isinstance(value, tuple):
            return [sanitize(item, key) for item in value]
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return _redact_url(value)
        return copy.deepcopy(value)

    return sanitize(payload)


def provider_replay_issues(payload):
    """Return any credential, private-host or local-path leaks in a replay."""
    issues = []

    def inspect_value(value, path="root", key=None):
        normalized_key = str(key or "").casefold()
        if normalized_key in _SECRET_KEYS and value != "***":
            issues.append(f"{path} contains an unredacted secret")
        if isinstance(value, dict):
            for child, item in value.items():
                inspect_value(item, f"{path}.{child}", child)
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                inspect_value(item, f"{path}[{index}]", key)
            return
        if not isinstance(value, str):
            return
        if _PRIVATE_PATH.match(value) and normalized_key != "file_path":
            issues.append(f"{path} contains a local filesystem path")
        if value.startswith(("http://", "https://")):
            parts = urlsplit(value)
            if parts.hostname not in {
                "api.themoviedb.org",
                "image.tmdb.org",
                "provider.example.invalid",
            }:
                issues.append(f"{path} contains a private provider host")
            for query_key, query_value in parse_qsl(parts.query, keep_blank_values=True):
                if query_key.casefold() in _SECRET_KEYS and query_value != "***":
                    issues.append(f"{path} contains an unredacted query secret")

    inspect_value(payload)
    return issues


def load_provider_replay(path):
    """Load a checked replay fixture and reject unsafe committed material."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    issues = provider_replay_issues(document)
    if issues:
        raise ValueError("Unsafe provider replay: " + "; ".join(issues))
    return document
