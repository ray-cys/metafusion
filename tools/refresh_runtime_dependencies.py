#!/usr/bin/env python3
"""Prepare bounded security repairs; CI and protected PR merging qualify them."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BASE_IMAGE = re.compile(r"(?m)^FROM python:3\.13-slim-bookworm@sha256:[0-9a-f]{64}$")


def security_repairs(report):
    """Require a stable same-major fix for every advisory before preparing repairs."""
    dependencies = report["dependencies"]
    repairs = {}
    for dependency in dependencies:
        vulnerabilities = dependency.get("vulns", [])
        if not vulnerabilities:
            continue
        name = str(dependency["name"]).lower().replace("_", "-")
        if not PACKAGE.fullmatch(name):
            raise ValueError("audit contains an invalid dependency name")
        current = Version(dependency["version"])
        fixes = []
        for vulnerability in vulnerabilities:
            candidates = []
            for raw in vulnerability.get("fix_versions", []):
                try:
                    version = Version(raw)
                except InvalidVersion:
                    continue
                if (
                    version.major == current.major and version > current
                    and not version.is_prerelease and not version.is_devrelease
                ):
                    candidates.append(version)
            if not candidates:
                raise ValueError(f"{name}: no stable same-major fix; manual review required")
            fixes.append(min(candidates))
        repairs[name] = str(max(fixes))
    return repairs


def repair_requirements(document, repairs):
    for name, version in sorted(repairs.items()):
        pin = re.compile(rf"(?im)^{re.escape(name)}==[^\s;]+\s*$")
        floor = re.compile(rf"(?im)^{re.escape(name)}>=[^\n]+$")
        if pin.search(document):
            document = pin.sub(f"{name}=={version}", document)
        elif floor.search(document):
            document = floor.sub(f"{name}>={version},<{Version(version).major + 1}", document)
        else:
            document = document.rstrip() + f"\n{name}>={version},<{Version(version).major + 1}\n"
    return document


def repair_base_image(document, digest):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("base image requires a full sha256 digest")
    if len(BASE_IMAGE.findall(document)) != 1:
        raise ValueError("Dockerfile must retain the supported Python 3.13 Bookworm base")
    return BASE_IMAGE.sub(f"FROM python:3.13-slim-bookworm@{digest}", document)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--base-image-digest", required=True)
    args = parser.parse_args(argv)
    repairs = security_repairs(json.loads(args.audit.read_text(encoding="utf-8")))
    requirements = Path("requirements.in")
    dockerfile = Path("Dockerfile")
    updated_requirements = repair_requirements(requirements.read_text(encoding="utf-8"), repairs)
    updated_dockerfile = repair_base_image(
        dockerfile.read_text(encoding="utf-8"), args.base_image_digest
    )
    requirements.write_text(updated_requirements, encoding="utf-8")
    dockerfile.write_text(updated_dockerfile, encoding="utf-8")
    common = [
        sys.executable, "-m", "uv", "pip", "compile", "--universal",
        "--python-version", "3.10", "--generate-hashes", "--quiet",
    ]
    upgrades = [argument for name in sorted(repairs) for argument in ("--upgrade-package", name)]
    subprocess.run([*common, *upgrades, "requirements.in", "-o", "requirements.txt"], check=True)
    subprocess.run([
        *common, "requirements-dev.in", "-c", "requirements.txt", "-o", "requirements-dev.txt"
    ], check=True)
    print(json.dumps({"security_repairs": repairs, "base_image_digest": args.base_image_digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
