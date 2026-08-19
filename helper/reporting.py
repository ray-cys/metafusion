"""Shared atomic text/JSON diagnostic report output and paired retention."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helper.io import atomic_write_json, atomic_write_text

REPORT_SCHEMA_VERSION = 1


def write_diagnostic_report(
    path,
    text,
    *,
    report_type,
    data=None,
    generated_at=None,
):
    """Write a human report and a machine-readable JSON companion atomically."""
    path = Path(path)
    generated = generated_at or datetime.now(timezone.utc)
    atomic_write_text(path, text)
    payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA_VERSION,
        "report_type": str(report_type),
        "generated_at": generated.isoformat(),
        "text_report": path.name,
        "data": data if data is not None else {},
    }
    atomic_write_json(path.with_suffix(".json"), payload)
    return path


def retain_diagnostic_reports(report_dir, stem, retention):
    """Retain text reports and their JSON companions as one logical unit."""
    report_dir = Path(report_dir)
    keep = max(1, int(retention))
    text_reports = sorted(
        report_dir.glob(f"{stem}-*.txt"),
        key=lambda report: (report.stat().st_mtime_ns, report.name),
        reverse=True,
    )
    for stale in text_reports[keep:]:
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            stale.with_suffix(".json").unlink()
        except (FileNotFoundError, OSError):
            pass
    existing_stems = {
        report.stem for report in report_dir.glob(f"{stem}-*.txt")
    }
    for companion in report_dir.glob(f"{stem}-*.json"):
        if companion.stem not in existing_stems:
            try:
                companion.unlink()
            except OSError:
                pass
