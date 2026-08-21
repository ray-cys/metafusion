"""Recoverable, checksum-guarded quarantine for automated artwork cleanup."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from helper.config import BASE_CONFIG_DIR, mode_check
from helper.io import sha256_file
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import (
    complete_cleanup_quarantine,
    load_cleanup_quarantine,
    record_cleanup_quarantine,
)


class QuarantineError(RuntimeError):
    pass


def quarantine_root(base_dir=None):
    return Path(base_dir or BASE_CONFIG_DIR) / "quarantine" / "cleanup"


def _managed_roots(config):
    if mode_check(config, "kometa"):
        return [
            (Path(config.get("settings", {}).get("path", ".")) / "assets").resolve(
                strict=False
            )
        ]
    roots = []
    for mapping in config.get("plex", {}).get("path_mappings", []):
        _source, separator, destination = str(mapping).partition("=>")
        if separator and destination.strip():
            roots.append(Path(destination.strip()).resolve(strict=False))
    return roots


def _inside(path, roots):
    resolved = Path(path).resolve(strict=False)
    return any(resolved.is_relative_to(root) for root in roots)


def _copy_verified(source, destination, checksum):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        if sha256_file(temporary) != checksum:
            raise QuarantineError("quarantine copy checksum did not match the source")
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def quarantine_managed_asset(
    config,
    source,
    record,
    *,
    output_type,
    checksum,
    season_number=None,
    base_dir=None,
):
    source = Path(source)
    roots = _managed_roots(config)
    if not roots or not _inside(source, roots):
        raise QuarantineError("artwork destination is outside configured managed roots")
    if source.is_symlink() or not source.is_file():
        raise QuarantineError("only a regular managed artwork file can be quarantined")
    if sha256_file(source) != str(checksum):
        raise QuarantineError("artwork changed after its cleanup checksum was verified")
    root = quarantine_root(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix if source.suffix else ".bin"
    destination = root / f"{uuid.uuid4().hex}{suffix}"
    size_bytes = source.stat().st_size
    _copy_verified(source, destination, str(checksum))
    try:
        source.unlink()
        history_id = record_cleanup_quarantine(
            record,
            output_type=output_type,
            season_number=season_number,
            source_path=source,
            quarantine_path=destination,
            checksum=checksum,
            size_bytes=size_bytes,
            retention_days=config.get("cleanup", {}).get("quarantine_days", 14),
        )
    except Exception:
        if not source.exists() and destination.exists():
            _copy_verified(destination, source, str(checksum))
            destination.unlink()
        raise
    return history_id


def restore_quarantined_asset(config, history_id, *, base_dir=None):
    records = load_cleanup_quarantine(statuses=["active"], history_id=history_id)
    if len(records) != 1:
        raise QuarantineError(
            "the cleanup history ID does not identify one active quarantined file"
        )
    record = records[0]
    source = Path(record["source_path"])
    stored = Path(record["quarantine_path"])
    root = quarantine_root(base_dir).resolve(strict=False)
    if not stored.resolve(strict=False).is_relative_to(root):
        raise QuarantineError("recorded quarantine path is outside the quarantine root")
    if not _inside(source, _managed_roots(config)):
        raise QuarantineError("original destination is outside current managed roots")
    if source.exists():
        raise QuarantineError("original destination already exists; it was not overwritten")
    if stored.is_symlink() or not stored.is_file():
        raise QuarantineError("quarantined artwork is missing or is not a regular file")
    checksum = str(record["checksum"])
    if sha256_file(stored) != checksum:
        raise QuarantineError("quarantined artwork checksum no longer matches its record")
    _copy_verified(stored, source, checksum)
    try:
        stored.unlink()
        complete_cleanup_quarantine(
            history_id,
            "restore",
            reason="operator restored checksum-proven quarantined artwork",
        )
    except Exception:
        if source.exists() and not stored.exists():
            _copy_verified(source, stored, checksum)
            source.unlink()
        raise
    return {**record, "status": "restored"}


def purge_expired_quarantine(config, *, base_dir=None, source="automated", now=None):
    current = now or datetime.now(timezone.utc)
    root = quarantine_root(base_dir).resolve(strict=False)
    results = []
    for record in load_cleanup_quarantine(
        statuses=["active"], expired_before=current
    ):
        result = dict(record)
        stored = Path(record["quarantine_path"])
        resolved = stored.resolve(strict=False)
        if not resolved.is_relative_to(root) or stored.is_symlink():
            result.update(status="protected", reason="unsafe quarantine path")
            results.append(result)
            continue
        if not stored.exists():
            complete_cleanup_quarantine(
                record["history_id"],
                "purge",
                status="missing",
                source=source,
                reason="expired quarantine file was already absent",
            )
            result["status"] = "missing"
            results.append(result)
            continue
        if not stored.is_file() or sha256_file(stored) != record.get("checksum"):
            result.update(status="protected", reason="quarantine checksum mismatch")
            results.append(result)
            continue
        stored.unlink()
        complete_cleanup_quarantine(
            record["history_id"],
            "purge",
            source=source,
            reason="quarantine retention expired",
        )
        result["status"] = "purged"
        results.append(result)
    return results


def write_quarantine_report(*, base_dir=None, retention=10):
    generated = datetime.now(timezone.utc)
    records = load_cleanup_quarantine()
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    report_path = report_dir / (
        f"cleanup-quarantine-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    lines = [
        "MetaFusion cleanup quarantine",
        f"Generated: {generated.isoformat()}",
        "Source: SQLite and recorded quarantine paths only; Plex was not contacted.",
        f"Records: {len(records)}",
        "",
    ]
    if not records:
        lines.append("- none")
    for record in records:
        lines.append(
            f"- history={record.get('history_id')} | {record.get('status')} | "
            f"{record.get('library_name') or 'unknown library'} | "
            f"{record.get('title') or record.get('cache_key') or 'unknown item'} | "
            f"{record.get('output_type')} | expires={record.get('expires_at')} | "
            f"bytes={record.get('size_bytes')}"
        )
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type="cleanup_quarantine",
        data={"records": records},
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "cleanup-quarantine", retention)
    return report_path
