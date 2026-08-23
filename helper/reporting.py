"""Shared atomic diagnostic report output and logical-report retention."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helper.io import atomic_write_json, atomic_write_text

REPORT_SCHEMA_VERSION = 1
REPORT_FORMATS = frozenset({"text", "json", "both"})
_report_format = "both"


def configure_reporting(config=None, *, report_format=None):
    """Select the process-wide diagnostic format from effective configuration."""
    global _report_format
    selected = report_format
    if selected is None and isinstance(config, dict):
        selected = config.get("output", {}).get("report_format", "both")
    selected = str(selected or "both").strip().lower()
    if selected not in REPORT_FORMATS:
        selected = "both"
    _report_format = selected
    return selected


def report_format():
    """Return the effective diagnostic format."""
    return _report_format


def write_diagnostic_report(
    path,
    text,
    *,
    report_type,
    data=None,
    generated_at=None,
    output_format=None,
):
    """Write the configured text, JSON, or paired diagnostic representation."""
    text_path = Path(path).with_suffix(".txt")
    json_path = text_path.with_suffix(".json")
    generated = generated_at or datetime.now(timezone.utc)
    selected = report_format() if output_format is None else str(output_format)
    if selected in {"text", "both"}:
        atomic_write_text(text_path, text)
    payload: dict[str, Any] = {
        "schema": REPORT_SCHEMA_VERSION,
        "report_type": str(report_type),
        "generated_at": generated.isoformat(),
        "text_report": text_path.name if selected in {"text", "both"} else None,
        "json_report": json_path.name if selected in {"json", "both"} else None,
        "data": data if data is not None else {},
    }
    if selected in {"json", "both"}:
        atomic_write_json(json_path, payload)
    return json_path if selected == "json" else text_path


def retain_diagnostic_reports(report_dir, stem, retention, *, output_format=None):
    """Retain configured report representations as logical report units."""
    report_dir = Path(report_dir)
    keep = max(1, int(retention))
    selected = report_format() if output_format is None else str(output_format)
    if selected == "json":
        json_reports = sorted(
            report_dir.glob(f"{stem}-*.json"),
            key=lambda report: (report.stat().st_mtime_ns, report.name),
            reverse=True,
        )
        for stale in json_reports[keep:]:
            for candidate in (stale, stale.with_suffix(".txt")):
                try:
                    candidate.unlink()
                except (FileNotFoundError, OSError):
                    pass
        return

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
    existing_stems = {report.stem for report in report_dir.glob(f"{stem}-*.txt")}
    for companion in report_dir.glob(f"{stem}-*.json"):
        if selected == "text" or companion.stem not in existing_stems:
            try:
                companion.unlink()
            except OSError:
                pass
