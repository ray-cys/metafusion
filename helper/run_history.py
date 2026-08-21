"""Durable performance history and schedule-capacity guidance."""

from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path

from helper.config import BASE_CONFIG_DIR
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import JOB_HISTORY_LIMIT, recent_job_runs


def _duration(record):
    metrics = record.get("metrics") or {}
    if metrics.get("elapsed_seconds") is not None:
        return max(0.0, float(metrics["elapsed_seconds"]))
    try:
        started = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(record["finished_at"]).replace("Z", "+00:00"))
        return max(0.0, (finished - started).total_seconds())
    except (KeyError, TypeError, ValueError):
        return 0.0


def _percentile(values, percentile):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _schedule_spacing(run_times):
    minutes = []
    for value in run_times or []:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except (TypeError, ValueError):
            continue
        minutes.append(hour * 60 + minute)
    minutes = sorted(set(minutes))
    if not minutes:
        return None
    if len(minutes) == 1:
        return 24 * 60
    gaps = [right - left for left, right in pairwise(minutes)]
    gaps.append(24 * 60 - minutes[-1] + minutes[0])
    return min(gaps)


def analyze_run_history(config, *, limit=JOB_HISTORY_LIMIT):
    runs = recent_job_runs(limit=limit)
    durations = [_duration(record) for record in runs if _duration(record) > 0]
    successful = [record for record in runs if record.get("status") == "success"]
    failed = [record for record in runs if record.get("status") != "success"]
    throughput = [
        float((record.get("metrics") or {}).get("items_per_minute") or 0.0)
        for record in successful
    ]
    throughput = [value for value in throughput if value > 0]
    median_seconds = statistics.median(durations) if durations else 0.0
    p95_seconds = _percentile(durations, 0.95)
    median_throughput = statistics.median(throughput) if throughput else 0.0
    spacing_minutes = _schedule_spacing(
        config.get("settings", {}).get("run_times", [])
    )
    advice = []
    if not runs:
        advice.append("No durable jobs are available yet; run MetaFusion before assessing capacity.")
    if failed:
        advice.append(
            f"{len(failed)} of {len(runs)} retained jobs failed; review the unresolved-work and retry reports."
        )
    if spacing_minutes and p95_seconds >= spacing_minutes * 60 * 0.8:
        advice.append(
            "The 95th-percentile run duration uses at least 80% of the shortest schedule interval; increase spacing."
        )
    elif spacing_minutes and p95_seconds:
        advice.append(
            "Retained run duration fits within the shortest configured schedule interval."
        )
    if len(throughput) >= 3 and throughput[-1] < median_throughput * 0.7:
        advice.append(
            "The latest successful throughput is more than 30% below the retained median; inspect provider waits and slow items."
        )
    if runs and not advice:
        advice.append("No schedule-capacity or retained-run regression warning was detected.")
    return {
        "summary": {
            "retained_runs": len(runs),
            "successful_runs": len(successful),
            "failed_runs": len(failed),
            "median_seconds": round(median_seconds, 3),
            "p95_seconds": round(p95_seconds, 3),
            "median_items_per_minute": round(median_throughput, 3),
            "shortest_schedule_interval_minutes": spacing_minutes,
        },
        "advice": advice,
        "runs": runs,
    }


def write_run_history_report(
    config,
    *,
    advice_only=False,
    base_dir=None,
    retention=10,
):
    generated = datetime.now(timezone.utc)
    analysis = analyze_run_history(config)
    summary = analysis["summary"]
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    stem = "schedule-advice" if advice_only else "run-history"
    report_path = report_dir / f"{stem}-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    lines = [
        "MetaFusion schedule advice" if advice_only else "MetaFusion durable run history",
        f"Generated: {generated.isoformat()}",
        "Source: SQLite job history only; providers and Plex were not contacted.",
        "",
        "Summary",
        f"- retained runs: {summary['retained_runs']}",
        f"- successful: {summary['successful_runs']}",
        f"- failed: {summary['failed_runs']}",
        f"- median duration: {summary['median_seconds']:.1f}s",
        f"- 95th-percentile duration: {summary['p95_seconds']:.1f}s",
        f"- median throughput: {summary['median_items_per_minute']:.1f} items/min",
        "- shortest schedule interval: "
        + (
            f"{summary['shortest_schedule_interval_minutes']} minutes"
            if summary["shortest_schedule_interval_minutes"] is not None
            else "not available"
        ),
        "",
        "Advice",
    ]
    lines.extend(f"- {value}" for value in analysis["advice"])
    if not advice_only:
        lines.extend(("", "Retained jobs"))
        for record in reversed(analysis["runs"]):
            metrics = record.get("metrics") or {}
            lines.append(
                f"- {record.get('finished_at')} | {record.get('status')} | "
                f"duration={_duration(record):.1f}s | "
                f"items/min={float(metrics.get('items_per_minute') or 0.0):.1f} | "
                f"libraries={len(record.get('library_results') or {})}"
            )
    write_diagnostic_report(
        report_path,
        "\n".join(lines) + "\n",
        report_type=stem.replace("-", "_"),
        data=analysis,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, stem, retention)
    return report_path
