"""Consistent, redacted disaster-recovery bundles and offline verification."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR, SECRET_PATHS, TEMPLATE_FILE
from helper.state_db import STATE_DATABASE, find_media_state, load_asset_ownership

FILE_MODE = 0o664


class RecoveryBundleError(RuntimeError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redacted_config(config):
    redacted = copy.deepcopy(config)
    for key in list(redacted):
        if str(key).startswith("_"):
            redacted.pop(key, None)
    for path in SECRET_PATHS:
        parent = redacted
        for key in path[:-1]:
            parent = parent.get(key, {}) if isinstance(parent, dict) else {}
        if isinstance(parent, dict) and path[-1] in parent:
            parent[path[-1]] = "<redacted>"
    return redacted


def _online_backup(source, destination):
    source_connection = sqlite3.connect(source, timeout=10)
    backup_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(backup_connection)
        integrity = backup_connection.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RecoveryBundleError(f"SQLite backup quick_check returned {integrity}")
    finally:
        backup_connection.close()
        source_connection.close()


def create_recovery_bundle(config, *, base_dir=None, state_path=STATE_DATABASE):
    """Create one portable bundle without copying artwork or provider caches."""
    source_database = Path(state_path)
    if not source_database.exists():
        raise RecoveryBundleError("Durable state database does not exist")
    base = Path(base_dir or BASE_CONFIG_DIR)
    backup_dir = base / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc)
    destination = backup_dir / (
        f"metafusion-recovery-{generated.strftime('%Y%m%d-%H%M%S%f')}.tar.gz"
    )
    temporary_archive = backup_dir / f".{destination.name}.tmp"
    with tempfile.TemporaryDirectory(prefix="metafusion-recovery-") as work:
        root = Path(work) / "metafusion-recovery"
        root.mkdir()
        database_copy = root / "meta_db.sqlite3"
        _online_backup(source_database, database_copy)
        (root / "effective-config.redacted.yml").write_text(
            yaml.safe_dump(_redacted_config(config), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        if Path(TEMPLATE_FILE).exists():
            (root / "config_template.yml").write_bytes(Path(TEMPLATE_FILE).read_bytes())
        output_files = []
        if str(config.get("settings", {}).get("mode", "kometa")).lower() == "kometa":
            metadata_dir = Path(config.get("settings", {}).get("path", ".")) / "metadata"
            recovery_metadata = root / "kometa-metadata"
            for source in sorted(metadata_dir.glob("*.yml")) if metadata_dir.exists() else []:
                recovery_metadata.mkdir(exist_ok=True)
                target = recovery_metadata / source.name
                target.write_bytes(source.read_bytes())
                output_files.append(str(target.relative_to(root)))
        items = find_media_state(path=state_path)
        ownership = load_asset_ownership(
            [item["cache_key"] for item in items], path=state_path
        )
        ownership_manifest = [
            {
                "cache_key": row.get("cache_key"),
                "media_type": row.get("media_type"),
                "tmdb_id": row.get("tmdb_id"),
                "asset_type": row.get("asset_type"),
                "season_number": row.get("season_number"),
                "destination": row.get("destination"),
                "checksum": row.get("checksum"),
            }
            for row in ownership
        ]
        (root / "asset-ownership.json").write_text(
            json.dumps(ownership_manifest, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        files = [path for path in sorted(root.rglob("*")) if path.is_file()]
        manifest = {
            "schema": 1,
            "generated_at": generated.isoformat(),
            "build": build_info(),
            "contents": {
                "durable_state": "meta_db.sqlite3",
                "effective_config": "effective-config.redacted.yml",
                "asset_ownership": "asset-ownership.json",
                "kometa_metadata": output_files,
                "artwork_files_included": False,
                "provider_caches_included": False,
            },
            "files": {
                str(path.relative_to(root)): {
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for path in files
            },
        }
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            with tarfile.open(temporary_archive, "w:gz") as archive:
                archive.add(root, arcname=root.name, recursive=True)
            os.chmod(temporary_archive, FILE_MODE)
            os.replace(temporary_archive, destination)
        finally:
            temporary_archive.unlink(missing_ok=True)
    return destination


def verify_recovery_bundle(bundle):
    """Verify archive paths, hashes, manifest, and copied SQLite integrity."""
    archive_path = Path(bundle)
    if not archive_path.is_file():
        raise RecoveryBundleError(f"Recovery bundle does not exist: {archive_path}")
    with tempfile.TemporaryDirectory(prefix="metafusion-verify-") as work:
        target = Path(work)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            for member in members:
                resolved = (target / member.name).resolve(strict=False)
                if not resolved.is_relative_to(target.resolve()):
                    raise RecoveryBundleError("Recovery archive contains an unsafe path")
                if member.issym() or member.islnk():
                    raise RecoveryBundleError("Recovery archive contains a link")
            # Every member was validated above before extraction. Python 3.12
            # added the safer filter argument; keep the Python 3.10 fallback
            # until MetaFusion raises its runtime baseline.
            if sys.version_info >= (3, 12):
                archive.extractall(target, filter="data")
            else:
                archive.extractall(target)
        roots = [path for path in target.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RecoveryBundleError("Recovery archive has an invalid root layout")
        root = roots[0]
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RecoveryBundleError("Recovery manifest is missing or invalid") from error
        failures = []
        for relative, expected in (manifest.get("files") or {}).items():
            path = (root / relative).resolve(strict=False)
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                failures.append(f"missing {relative}")
                continue
            if _sha256(path) != str(expected.get("sha256")):
                failures.append(f"checksum mismatch {relative}")
        database = root / str(
            (manifest.get("contents") or {}).get("durable_state", "meta_db.sqlite3")
        )
        if database.is_file():
            with sqlite3.connect(database) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                failures.append(f"SQLite quick_check returned {integrity}")
        else:
            failures.append("durable state database missing")
        return {
            "valid": not failures,
            "failures": failures,
            "manifest": manifest,
            "bundle": str(archive_path),
        }
