#!/usr/bin/env python3
"""Maintain reproducible Kometa and Plex provider compatibility contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / ".github" / "provider-contracts.json"
DEFAULT_REQUIREMENTS = REPO_ROOT / "requirements.in"
KOMETA_RELEASE_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PYTHON_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)*(?:rc\d+)?$")
PROFILE_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
IGNORED_SCHEMA_FIELDS = {"$comment", "description", "examples", "title"}


class ContractError(ValueError):
    """Raised when a provider contract is malformed or inconsistent."""


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"Could not read provider contract {path}: {error}") from error
    return _mapping(document, "provider contract")


def plexapi_pin(requirements_path: Path = DEFAULT_REQUIREMENTS) -> str:
    try:
        requirements = requirements_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ContractError(f"Could not read {requirements_path}: {error}") from error
    match = re.search(r"(?im)^plexapi==([^\s;]+)\s*$", requirements)
    if not match:
        raise ContractError("requirements.in must contain one exact plexapi== version pin")
    version = match.group(1)
    if not PYTHON_VERSION_PATTERN.fullmatch(version):
        raise ContractError(f"Unsupported PlexAPI version pin: {version}")
    return version


def validate_manifest(
    manifest: Mapping[str, Any], requirements_path: Path = DEFAULT_REQUIREMENTS
) -> dict[str, str]:
    if manifest.get("format") != 2:
        raise ContractError("provider contract format must be 2")

    kometa = _mapping(manifest.get("kometa"), "kometa")
    plex = _mapping(manifest.get("plex"), "plex")
    required_kometa = {
        "profile",
        "repository",
        "image",
        "schema_path",
        "baseline",
        "current",
    }
    missing = sorted(required_kometa - set(kometa))
    if missing:
        raise ContractError(f"kometa contract is missing: {', '.join(missing)}")

    for profile_name in (kometa.get("profile"), plex.get("profile")):
        if not PROFILE_PATTERN.fullmatch(str(profile_name or "")):
            raise ContractError(f"Invalid compatibility profile: {profile_name}")

    repository = str(kometa["repository"])
    image = str(kometa["image"])
    schema_path = str(kometa["schema_path"])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ContractError(f"Invalid Kometa repository: {repository}")
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", image):
        raise ContractError(f"Invalid Kometa image: {image}")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", schema_path)
        or ".." in Path(schema_path).parts
    ):
        raise ContractError(f"Invalid Kometa schema path: {schema_path}")

    contracts = {}
    for name in ("baseline", "current"):
        contract = _mapping(kometa.get(name), f"kometa.{name}")
        missing_contract = sorted(
            {"release", "digest", "schema_sha256"} - set(contract)
        )
        if missing_contract:
            raise ContractError(
                f"kometa.{name} contract is missing: {', '.join(missing_contract)}"
            )
        release = str(contract["release"])
        digest = str(contract["digest"])
        schema_sha256 = str(contract["schema_sha256"])
        if not KOMETA_RELEASE_PATTERN.fullmatch(release):
            raise ContractError(f"Invalid stable Kometa release: {release}")
        if not DIGEST_PATTERN.fullmatch(digest):
            raise ContractError("Kometa image digest must be a full sha256 digest")
        if not SHA256_PATTERN.fullmatch(schema_sha256):
            raise ContractError("Kometa schema checksum must be a full sha256 checksum")
        contracts[name] = {
            "release": release,
            "image": f"{image}@{digest}",
            "schema_sha256": schema_sha256,
            "schema_url": (
                f"https://raw.githubusercontent.com/{repository}/{release}/{schema_path}"
            ),
        }

    def semver(value: str) -> tuple[int, int, int]:
        return tuple(int(part) for part in value.removeprefix("v").split("."))

    if semver(contracts["baseline"]["release"]) > semver(
        contracts["current"]["release"]
    ):
        raise ContractError("Kometa baseline release cannot be newer than current")

    if plex.get("package") != "plexapi":
        raise ContractError("Plex contract package must be plexapi")
    if plex.get("version_source") != "requirements.in":
        raise ContractError("PlexAPI version source must be requirements.in")
    replay_tests = plex.get("replay_tests")
    if not isinstance(replay_tests, list) or not replay_tests:
        raise ContractError("Plex contract must declare replay tests")
    if any(
        not isinstance(path, str)
        or not re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", path)
        or ".." in Path(path).parts
        for path in replay_tests
    ):
        raise ContractError("Plex replay tests must be Python paths below tests/")

    current = contracts["current"]
    return {
        "kometa_baseline_release": contracts["baseline"]["release"],
        "kometa_baseline_image": contracts["baseline"]["image"],
        "kometa_baseline_schema_sha256": contracts["baseline"]["schema_sha256"],
        "kometa_baseline_schema_url": contracts["baseline"]["schema_url"],
        "kometa_current_release": current["release"],
        "kometa_current_image": current["image"],
        "kometa_current_schema_sha256": current["schema_sha256"],
        "kometa_current_schema_url": current["schema_url"],
        # Compatibility aliases keep the update workflow focused on the current
        # contract while all validation jobs exercise both supported versions.
        "kometa_release": current["release"],
        "kometa_image": current["image"],
        "kometa_schema_sha256": current["schema_sha256"],
        "kometa_schema_url": current["schema_url"],
        "plexapi_version": plexapi_pin(requirements_path),
        "plex_replay_tests": " ".join(replay_tests),
    }


def _write_outputs(path: Path, values: Mapping[str, str]) -> None:
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ContractError(f"GitHub output {key} must be a single line")
            output.write(f"{key}={value}\n")


def emit_outputs(
    manifest_path: Path,
    requirements_path: Path,
    github_output: Path | None = None,
) -> dict[str, str]:
    outputs = validate_manifest(load_manifest(manifest_path), requirements_path)
    if github_output is not None:
        _write_outputs(github_output, outputs)
    else:
        print(json.dumps(outputs, indent=2, sort_keys=True))
    return outputs


def fetch_json(url: str, *, token: str = "") -> dict[str, Any]:
    if not url.startswith("https://"):
        raise ContractError(f"Provider discovery requires HTTPS: {url}")
    headers = {
        "Accept": "application/json",
        "User-Agent": "MetaFusion-provider-compatibility",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            document = json.load(response)
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"Could not query {url}: {error}") from error
    return _mapping(document, f"response from {url}")


def discover_latest(
    manifest: Mapping[str, Any],
    requirements_path: Path = DEFAULT_REQUIREMENTS,
    *,
    token: str = "",
    fetcher: Callable[..., dict[str, Any]] = fetch_json,
) -> dict[str, str]:
    current = validate_manifest(manifest, requirements_path)
    kometa = _mapping(manifest["kometa"], "kometa")
    plex = _mapping(manifest["plex"], "plex")
    release = fetcher(
        f"https://api.github.com/repos/{kometa['repository']}/releases/latest",
        token=token,
    ).get("tag_name")
    plex_version = _mapping(
        fetcher(f"https://pypi.org/pypi/{plex['package']}/json"),
        "PyPI response info container",
    ).get("info")
    plex_latest = _mapping(plex_version, "PyPI response info").get("version")

    if not KOMETA_RELEASE_PATTERN.fullmatch(str(release or "")):
        raise ContractError(f"Upstream Kometa release is not stable semver: {release}")
    if not PYTHON_VERSION_PATTERN.fullmatch(str(plex_latest or "")):
        raise ContractError(f"Upstream PlexAPI version is invalid: {plex_latest}")

    return {
        "kometa_latest": str(release),
        "kometa_changed": str(release != current["kometa_release"]).lower(),
        "plexapi_latest": str(plex_latest),
        "plexapi_changed": str(plex_latest != current["plexapi_version"]).lower(),
    }


def _schema_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_pointer(path: Sequence[str]) -> str:
    return "/" + "/".join(value.replace("~", "~0").replace("/", "~1") for value in path)


def _flatten_schema(value: Any, path: tuple[str, ...] = ()) -> dict[str, str]:
    flattened: dict[str, str] = {}
    if isinstance(value, dict):
        for key in sorted(value):
            if key in IGNORED_SCHEMA_FIELDS:
                continue
            flattened.update(_flatten_schema(value[key], (*path, key)))
    elif isinstance(value, list):
        canonical = sorted(json.dumps(item, sort_keys=True) for item in value)
        flattened[_json_pointer(path)] = json.dumps(canonical)
    else:
        flattened[_json_pointer(path)] = json.dumps(value, sort_keys=True)
    return flattened


def schema_diff(old_schema: Path, new_schema: Path, *, limit: int = 120) -> str:
    try:
        old_document = json.loads(old_schema.read_text(encoding="utf-8"))
        new_document = json.loads(new_schema.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"Could not compare Kometa schemas: {error}") from error
    old = _flatten_schema(_mapping(old_document, "old Kometa schema"))
    new = _flatten_schema(_mapping(new_document, "new Kometa schema"))
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(path for path in set(old) & set(new) if old[path] != new[path])

    lines = [
        "## Kometa metadata schema comparison",
        "",
        f"- Previous checksum: `{_schema_hash(old_schema)}`",
        f"- Candidate checksum: `{_schema_hash(new_schema)}`",
        f"- Constraint paths added: **{len(added)}**",
        f"- Constraint paths removed: **{len(removed)}**",
        f"- Constraint values changed: **{len(changed)}**",
    ]
    for title, values in (
        ("Added constraint paths", added),
        ("Removed constraint paths", removed),
        ("Changed constraint values", changed),
    ):
        if not values:
            continue
        lines.extend(("", f"### {title}", ""))
        lines.extend(f"- `{value}`" for value in values[:limit])
        if len(values) > limit:
            lines.append(f"- … {len(values) - limit} additional paths omitted")
    if not any((added, removed, changed)):
        lines.extend(("", "No machine-readable constraint changes were detected."))
    return "\n".join(lines) + "\n"


def update_kometa_contract(
    manifest_path: Path,
    *,
    release: str,
    digest: str,
    old_schema: Path,
    new_schema: Path,
    report_path: Path,
) -> bool:
    if not KOMETA_RELEASE_PATTERN.fullmatch(release):
        raise ContractError(f"Invalid stable Kometa release: {release}")
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ContractError("Candidate Kometa image digest must be a full sha256 digest")
    manifest = load_manifest(manifest_path)
    validate_manifest(manifest)
    kometa = _mapping(manifest["kometa"], "kometa")
    current = _mapping(kometa["current"], "kometa.current")
    old_hash = _schema_hash(old_schema)
    if old_hash != current["schema_sha256"]:
        raise ContractError(
            "Downloaded baseline Kometa schema does not match the pinned checksum"
        )
    try:
        _mapping(json.loads(new_schema.read_text(encoding="utf-8")), "candidate schema")
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"Candidate Kometa schema is invalid: {error}") from error

    new_hash = _schema_hash(new_schema)
    changed = any(
        (
            current["release"] != release,
            current["digest"] != digest,
            current["schema_sha256"] != new_hash,
        )
    )
    current["release"] = release
    current["digest"] = digest
    current["schema_sha256"] = new_hash
    temporary = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    report_path.write_text(schema_diff(old_schema, new_schema), encoding="utf-8")
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and update MetaFusion provider compatibility contracts"
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("verify", help="validate the committed provider contract")
    outputs = subparsers.add_parser("outputs", help="emit pinned contract values")
    outputs.add_argument("--github-output", type=Path)
    discover = subparsers.add_parser("discover", help="query latest stable provider versions")
    discover.add_argument("--github-output", type=Path)
    compare = subparsers.add_parser("schema-diff", help="compare two Kometa schemas")
    compare.add_argument("--old-schema", type=Path, required=True)
    compare.add_argument("--new-schema", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    update = subparsers.add_parser("update-kometa", help="update the pinned Kometa contract")
    update.add_argument("--release", required=True)
    update.add_argument("--digest", required=True)
    update.add_argument("--old-schema", type=Path, required=True)
    update.add_argument("--new-schema", type=Path, required=True)
    update.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            values = validate_manifest(load_manifest(args.manifest), args.requirements)
            print(
                "Provider contracts valid: Kometa "
                f"{values['kometa_baseline_release']} and "
                f"{values['kometa_current_release']}; "
                f"PlexAPI {values['plexapi_version']}"
            )
        elif args.command == "outputs":
            emit_outputs(args.manifest, args.requirements, args.github_output)
        elif args.command == "discover":
            values = discover_latest(
                load_manifest(args.manifest),
                args.requirements,
                token=os.environ.get("GITHUB_TOKEN", ""),
            )
            if args.github_output:
                _write_outputs(args.github_output, values)
            else:
                print(json.dumps(values, indent=2, sort_keys=True))
        elif args.command == "schema-diff":
            report = schema_diff(args.old_schema, args.new_schema)
            if args.output:
                args.output.write_text(report, encoding="utf-8")
            else:
                print(report, end="")
        elif args.command == "update-kometa":
            changed = update_kometa_contract(
                args.manifest,
                release=args.release,
                digest=args.digest,
                old_schema=args.old_schema,
                new_schema=args.new_schema,
                report_path=args.report,
            )
            print(f"kometa_contract_changed={str(changed).lower()}")
        return 0
    except ContractError as error:
        print(f"Provider compatibility error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
