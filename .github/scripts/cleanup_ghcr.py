#!/usr/bin/env python3
"""Delete only old, untagged, unreferenced GHCR manifests (fail closed)."""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def _request_json(url, *, token=None, headers=None, method="GET"):
    request_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "metafusion-ghcr-retention",
        **(headers or {}),
    }
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
        request_headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = Request(url, headers=request_headers, method=method)
    with urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def protected_manifest_digests(versions, fetch_manifest, root_digests=None):
    """Include retained roots and every recursively referenced manifest."""
    protected = set(root_digests or ())
    protected.update(
        str(version.get("name"))
        for version in versions
        if version.get("name")
        and version.get("metadata", {}).get("container", {}).get("tags")
    )
    queue = list(protected)
    inspected = set()
    while queue:
        digest = queue.pop()
        if digest in inspected:
            continue
        manifest = fetch_manifest(digest)
        inspected.add(digest)
        for child in manifest.get("manifests", []) if isinstance(manifest, dict) else []:
            child_digest = str(child.get("digest") or "")
            if child_digest and child_digest not in protected:
                protected.add(child_digest)
                queue.append(child_digest)
    return protected


def retention_root_digests(
    versions, *, now, retention_days, keep_untagged
):
    """Return every tagged or otherwise retained package-version digest."""
    roots = {
        str(version.get("name"))
        for version in versions
        if version.get("name")
        and version.get("metadata", {}).get("container", {}).get("tags")
    }
    untagged = sorted(
        (
            version
            for version in versions
            if not version.get("metadata", {}).get("container", {}).get("tags")
        ),
        key=lambda version: str(version.get("created_at") or ""),
        reverse=True,
    )
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    for index, version in enumerate(untagged):
        digest = str(version.get("name") or "")
        try:
            created = datetime.fromisoformat(
                str(version.get("created_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            created = None
        if digest and (
            index < max(0, int(keep_untagged))
            or created is None
            or created >= cutoff
        ):
            roots.add(digest)
    return roots


def deletion_candidates(versions, protected, *, now, retention_days, keep_untagged):
    untagged = sorted(
        (
            version
            for version in versions
            if not version.get("metadata", {}).get("container", {}).get("tags")
        ),
        key=lambda version: str(version.get("created_at") or ""),
        reverse=True,
    )
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    candidates = []
    for version in untagged[max(0, int(keep_untagged)) :]:
        digest = str(version.get("name") or "")
        try:
            created = datetime.fromisoformat(
                str(version.get("created_at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if digest and digest not in protected and created < cutoff:
            candidates.append(version)
    return candidates


def _package_api_prefix(owner, package, token):
    owner_data = _request_json(f"https://api.github.com/users/{quote(owner)}", token=token)
    scope = "orgs" if owner_data.get("type") == "Organization" else "users"
    return (
        f"https://api.github.com/{scope}/{quote(owner)}/packages/container/"
        f"{quote(package, safe='')}/versions"
    )


def _all_versions(prefix, token):
    versions = []
    page = 1
    while True:
        batch = _request_json(
            f"{prefix}?{urlencode({'per_page': 100, 'page': page})}", token=token
        )
        versions.extend(batch or [])
        if not batch or len(batch) < 100:
            return versions
        page += 1


def _registry_token(owner, package, github_token, actor=None):
    basic = base64.b64encode(
        f"{actor or owner}:{github_token}".encode()
    ).decode()
    query = urlencode(
        {
            "service": "ghcr.io",
            "scope": f"repository:{owner.lower()}/{package.lower()}:pull",
        }
    )
    payload = _request_json(
        f"https://ghcr.io/token?{query}",
        headers={"Authorization": f"Basic {basic}"},
    )
    return payload["token"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or "/" not in repository:
        print("GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2
    owner, repository_name = repository.split("/", 1)
    package = os.environ.get("GHCR_PACKAGE", repository_name).lower()
    retention_days = int(os.environ.get("GHCR_RETENTION_DAYS", "30"))
    keep_untagged = int(os.environ.get("GHCR_KEEP_UNTAGGED", "50"))
    prefix = _package_api_prefix(owner, package, token)
    versions = _all_versions(prefix, token)
    registry_token = _registry_token(
        owner, package, token, actor=os.environ.get("GITHUB_ACTOR")
    )

    def fetch_manifest(digest):
        return _request_json(
            f"https://ghcr.io/v2/{owner.lower()}/{package}/manifests/{digest}",
            headers={
                "Authorization": f"Bearer {registry_token}",
                "Accept": MANIFEST_ACCEPT,
            },
        )

    try:
        roots = retention_root_digests(
            versions,
            now=datetime.now(timezone.utc),
            retention_days=retention_days,
            keep_untagged=keep_untagged,
        )
        protected = protected_manifest_digests(
            versions, fetch_manifest, root_digests=roots
        )
    except (HTTPError, OSError, KeyError, ValueError) as error:
        print(
            f"Unable to resolve every tagged manifest; refusing cleanup: {error}",
            file=sys.stderr,
        )
        return 1
    candidates = deletion_candidates(
        versions,
        protected,
        now=datetime.now(timezone.utc),
        retention_days=retention_days,
        keep_untagged=keep_untagged,
    )
    print(
        f"GHCR versions={len(versions)} protected={len(protected)} "
        f"eligible={len(candidates)} dry_run={args.dry_run}"
    )
    for version in candidates:
        print(f"{'Would delete' if args.dry_run else 'Deleting'} {version['name']}")
        if not args.dry_run:
            _request_json(f"{prefix}/{version['id']}", token=token, method="DELETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
