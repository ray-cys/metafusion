"""Sanitize and validate offline Plex/TMDb provider replay fixtures."""

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report

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
    "destination",
    "file",
    "location",
    "locations",
    "movie_dir",
    "movie_path",
    "new_destination",
    "path",
    "previous_destination",
    "season_dirs",
    "season_path",
    "show_dir",
    "show_path",
}
_IDENTIFIER_KEYS = {
    "library_uuid",
    "libraryuuid",
    "machine_identifier",
    "machineidentifier",
    "rating_key",
    "ratingkey",
    "server_id",
    "serverid",
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
    private_provider = hostname not in {
        "api.themoviedb.org",
        "image.tmdb.org",
    }
    if private_provider:
        hostname = "provider.example.invalid"
    path = parts.path
    if private_provider:
        path = re.sub(r"/\d+(?=/|$)", "/<redacted-id>", path)
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, hostname + port, path, urlencode(query), parts.fragment)
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
        if normalized_key in _IDENTIFIER_KEYS and value is not None:
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
        if isinstance(value, str):
            if _PRIVATE_PATH.match(value):
                return "<redacted-media-path>"
            if value.startswith(("http://", "https://")):
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
        if _PRIVATE_PATH.match(value):
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


def write_sanitized_replay_capture(records, *, base_dir=None, retention=10):
    """Write a deterministic, share-safe replay bundle and text manifest."""
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / (
        f"provider-replay-capture-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    document = sanitize_provider_payload(
        {
            "schema": 1,
            "description": "Sanitized MetaFusion item replay capture",
            "build": build_info(),
            "items": list(records or []),
        }
    )
    issues = provider_replay_issues(document)
    if issues:
        raise ValueError("Unsafe replay capture refused: " + "; ".join(issues))
    lines = [
        "MetaFusion sanitized replay capture",
        f"Generated: {generated.isoformat()}",
        f"Items: {len(document['items'])}",
        "The JSON companion is sanitized for a GitHub issue; review it before sharing.",
        "Connector credentials and local media paths are not retained.",
    ]
    write_diagnostic_report(
        path,
        "\n".join(lines) + "\n",
        report_type="provider_replay_capture",
        data=document,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "provider-replay-capture", retention)
    return path.with_suffix(".json")
