#!/usr/bin/env python3
"""Run a bounded, read-only live canary against MetaFusion artwork providers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from helper.provider_credentials import fanart_project_api_key

TMDB_API = "https://api.themoviedb.org/3"
FANART_API = "https://webservice.fanart.tv/v3"
JOLPICA_API = "https://api.jolpi.ca/ergast/f1"
FORMULA1_CALENDAR = "https://www.formula1.com/en/racing"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
CIRCUIT_SAMPLE = (
    "https://raw.githubusercontent.com/julesr0y/f1-circuits-svg/"
    "main/circuits/detailed/black-outline/silverstone-8.svg"
)
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
USER_AGENT = "MetaFusion-provider-canary/1.0 (+https://github.com/ray-cys/metafusion)"


class CanaryError(RuntimeError):
    """Raised when a live provider violates the bounded canary contract."""


def _redact(value, secrets):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    text = re.sub(r"(?i)(api_key=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(?i)(authorization:\s*(?:bearer\s+)?)\S+", r"\1***", text)
    return text


def _request(
    provider,
    path,
    *,
    query=None,
    headers=None,
    expected=(200,),
    attempts=3,
    maximum_bytes=MAX_JSON_BYTES,
):
    query_string = urlencode(query or {})
    url = f"{path}?{query_string}" if query_string else path
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    last_error = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        started = time.monotonic()
        try:
            with urlopen(Request(url, headers=request_headers), timeout=20) as response:
                status = int(response.status)
                body = response.read(maximum_bytes + 1)
                response_headers = response.headers
            if len(body) > maximum_bytes:
                raise CanaryError(f"{provider} response exceeded {maximum_bytes} bytes")
            if status not in expected:
                raise CanaryError(f"{provider} returned unexpected HTTP {status}")
            return status, body, response_headers, time.monotonic() - started
        except HTTPError as error:
            status = int(error.code)
            if status in expected:
                body = error.read(maximum_bytes + 1)
                return status, body, error.headers, time.monotonic() - started
            last_error = CanaryError(f"{provider} returned HTTP {status}")
            if status == 429 and attempt < attempts:
                try:
                    retry_after = float(error.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    retry_after = 1.0
                time.sleep(max(0.0, min(10.0, retry_after)))
                continue
            if status >= 500 and attempt < attempts:
                time.sleep(min(4.0, float(2 ** (attempt - 1))))
                continue
            break
        except (TimeoutError, URLError, OSError) as error:
            last_error = CanaryError(f"{provider} transport failure: {type(error).__name__}")
            if attempt < attempts:
                time.sleep(min(4.0, float(2 ** (attempt - 1))))
                continue
            break
    raise last_error or CanaryError(f"{provider} request failed")


def _request_json(provider, path, **kwargs):
    status, body, headers, elapsed = _request(provider, path, **kwargs)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CanaryError(f"{provider} returned malformed JSON") from error
    if not isinstance(payload, dict):
        raise CanaryError(f"{provider} returned a non-object JSON response")
    return status, payload, headers, elapsed


def _request_image(url):
    started = time.monotonic()
    with urlopen(
        Request(url, headers={"Accept": "image/*", "User-Agent": USER_AGENT}),
        timeout=20,
    ) as response:
        body = response.read(MAX_IMAGE_BYTES + 1)
        content_type = str(response.headers.get("Content-Type", "")).lower()
    if len(body) > MAX_IMAGE_BYTES:
        raise CanaryError(f"TMDb sample image exceeded {MAX_IMAGE_BYTES} bytes")
    signatures = (
        body.startswith(b"\xff\xd8\xff"),
        body.startswith(b"\x89PNG\r\n\x1a\n"),
        body.startswith(b"RIFF") and body[8:12] == b"WEBP",
    )
    if not content_type.startswith("image/") or not any(signatures):
        raise CanaryError("TMDb sample artwork was not a recognized image")
    return len(body), content_type.split(";", 1)[0], time.monotonic() - started


def _tmdb_auth(api_key):
    normalized = str(api_key).strip()
    if normalized.startswith("eyJ") or normalized.count(".") == 2:
        return {}, {"Authorization": f"Bearer {normalized}"}
    return {"api_key": normalized}, {}


def run_tmdb(api_key):
    query_auth, headers = _tmdb_auth(api_key)
    results = []

    status, configuration, _headers, elapsed = _request_json(
        "TMDb configuration",
        f"{TMDB_API}/configuration",
        query=query_auth,
        headers=headers,
    )
    images = configuration.get("images")
    if not isinstance(images, dict) or not images.get("secure_base_url"):
        raise CanaryError("TMDb configuration omitted the secure image base URL")
    results.append(
        {
            "provider": "TMDb",
            "check": "configuration",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
        }
    )

    sample_poster = None
    for media_type, identifier, required_name in (
        ("movie", 550, "title"),
        ("tv", 1399, "name"),
    ):
        query = {
            **query_auth,
            "language": "en-US",
            "append_to_response": "external_ids,images",
            "include_image_language": "en,null",
        }
        status, payload, _headers, elapsed = _request_json(
            f"TMDb {media_type}",
            f"{TMDB_API}/{media_type}/{identifier}",
            query=query,
            headers=headers,
        )
        if int(payload.get("id", 0)) != identifier or not payload.get(required_name):
            raise CanaryError(f"TMDb {media_type} identity contract changed")
        image_payload = payload.get("images")
        posters = image_payload.get("posters") if isinstance(image_payload, dict) else None
        if not isinstance(posters, list) or not posters:
            raise CanaryError(f"TMDb {media_type} returned no sample posters")
        if sample_poster is None:
            sample_poster = posters[0].get("file_path")
        results.append(
            {
                "provider": "TMDb",
                "check": f"{media_type}-identity-and-images",
                "status": status,
                "duration_ms": round(elapsed * 1000, 1),
                "posters": len(posters),
            }
        )

    status, not_found, _headers, elapsed = _request_json(
        "TMDb controlled 404",
        f"{TMDB_API}/movie/0",
        query=query_auth,
        headers=headers,
        expected=(404,),
        attempts=1,
    )
    if int(not_found.get("status_code", 0)) != 34:
        raise CanaryError("TMDb controlled 404 response contract changed")
    results.append(
        {
            "provider": "TMDb",
            "check": "controlled-not-found",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
        }
    )

    if not sample_poster:
        raise CanaryError("TMDb sample poster path was empty")
    image_url = f"{str(images['secure_base_url']).rstrip('/')}/w92{sample_poster}"
    size, content_type, elapsed = _request_image(image_url)
    results.append(
        {
            "provider": "TMDb",
            "check": "image-delivery",
            "status": 200,
            "duration_ms": round(elapsed * 1000, 1),
            "bytes": size,
            "content_type": content_type,
        }
    )
    return results


def run_fanart(project_key):
    status, payload, _headers, elapsed = _request_json(
        "Fanart.tv movie artwork",
        f"{FANART_API}/movies/550",
        query={"api_key": project_key},
    )
    artwork_groups = {
        key: value
        for key, value in payload.items()
        if isinstance(value, list) and value and key not in {"name"}
    }
    if not artwork_groups:
        raise CanaryError("Fanart.tv returned no artwork groups for the sample movie")
    urls = [
        item.get("url")
        for values in artwork_groups.values()
        for item in values[:1]
        if isinstance(item, dict)
    ]
    if not urls or any(not str(url).startswith("https://") for url in urls):
        raise CanaryError("Fanart.tv returned an invalid artwork URL")
    return [
        {
            "provider": "Fanart.tv",
            "check": "movie-artwork",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
            "artwork_groups": len(artwork_groups),
        }
    ]


def run_formula1():
    """Exercise every live, keyless provider used by the private F1 extension."""
    year = datetime.now(timezone.utc).year
    checks = []
    status, schedule, _headers, elapsed = _request_json(
        "Jolpica Formula 1 schedule", f"{JOLPICA_API}/{year}.json"
    )
    races = schedule.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not isinstance(races, list) or not races:
        raise CanaryError(f"Jolpica returned no races for {year}")
    first = races[0]
    if not first.get("round") or not first.get("raceName") or not first.get("Circuit"):
        raise CanaryError("Jolpica schedule identity contract changed")
    checks.append(
        {
            "provider": "Jolpica",
            "check": "current-season-schedule",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
            "year": year,
            "races": len(races),
        }
    )

    status, body, _headers, elapsed = _request(
        "Formula1.com calendar",
        f"{FORMULA1_CALENDAR}/{year}",
        headers={"Accept": "text/html"},
        maximum_bytes=MAX_HTML_BYTES,
    )
    document = body.decode("utf-8", errors="replace")
    if not re.search(rf"/en/racing/{year}/[a-z0-9-]+", document, re.IGNORECASE):
        raise CanaryError("Formula1.com calendar links could not be discovered")
    checks.append(
        {
            "provider": "Formula1.com",
            "check": "calendar-markup",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
            "year": year,
        }
    )

    status, body, headers, elapsed = _request(
        "F1 circuit SVG", CIRCUIT_SAMPLE, headers={"Accept": "image/svg+xml"}
    )
    content_type = str(headers.get("Content-Type", "")).casefold()
    if b"<svg" not in body[:1000].lower() or "svg" not in content_type:
        raise CanaryError("circuit provider returned no usable SVG")
    checks.append(
        {
            "provider": "f1-circuits-svg",
            "check": "circuit-shape",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
        }
    )

    status, commons, _headers, elapsed = _request_json(
        "Wikimedia Commons Formula 1",
        COMMONS_API,
        query={
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrlimit": "3",
            "gsrsearch": f'intitle:{year} "Formula 1"',
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
        },
    )
    pages = commons.get("query", {}).get("pages", [])
    if not isinstance(pages, list) or not pages:
        raise CanaryError("Wikimedia Commons returned no current-season image candidates")
    checks.append(
        {
            "provider": "Wikimedia Commons",
            "check": "current-season-search",
            "status": status,
            "duration_ms": round(elapsed * 1000, 1),
            "candidates": len(pages),
        }
    )
    return checks


def _write_summary(path, report):
    if not path:
        return
    lines = ["## MetaFusion live provider canary", ""]
    for provider in report["providers"]:
        lines.append(
            f"- **{provider['provider']}**: {provider['status']}"
            + (f" — {provider.get('message')}" if provider.get("message") else "")
        )
    lines.extend(("", f"Overall result: **{report['status']}**"))
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="provider-canary-report.json")
    parser.add_argument("--github-summary")
    parser.add_argument("--require-tmdb", action="store_true")
    args = parser.parse_args(argv)

    tmdb_key = os.environ.get("TMDB_CANARY_API_KEY", "").strip()
    fanart_key = fanart_project_api_key().strip()
    secrets = (tmdb_key, fanart_key)
    providers = []
    failures = []

    if tmdb_key:
        try:
            checks = run_tmdb(tmdb_key)
            providers.append({"provider": "TMDb", "status": "passed", "checks": checks})
        except Exception as error:
            message = _redact(error, secrets)
            providers.append({"provider": "TMDb", "status": "failed", "message": message})
            failures.append(f"TMDb: {message}")
    else:
        message = "skipped; configure the TMDB_CANARY_API_KEY repository secret"
        providers.append({"provider": "TMDb", "status": "not_configured", "message": message})
        if args.require_tmdb:
            failures.append(f"TMDb: {message}")

    if not fanart_key:
        failures.append("Fanart.tv: bundled project key is missing")
        providers.append(
            {"provider": "Fanart.tv", "status": "failed", "message": "project key missing"}
        )
    else:
        try:
            checks = run_fanart(fanart_key)
            providers.append(
                {"provider": "Fanart.tv", "status": "passed", "checks": checks}
            )
        except Exception as error:
            message = _redact(error, secrets)
            providers.append(
                {"provider": "Fanart.tv", "status": "failed", "message": message}
            )
            failures.append(f"Fanart.tv: {message}")

    try:
        checks = run_formula1()
        providers.append(
            {"provider": "Formula 1 extension", "status": "passed", "checks": checks}
        )
    except Exception as error:
        message = _redact(error, secrets)
        providers.append(
            {"provider": "Formula 1 extension", "status": "failed", "message": message}
        )
        failures.append(f"Formula 1 extension: {message}")

    report = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if failures else "passed",
        "providers": providers,
        "failures": failures,
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_summary(args.github_summary, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
