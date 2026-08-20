#!/usr/bin/env python3
"""Enforce repository and high-risk MetaFusion coverage floors."""

import argparse
import json
from pathlib import Path

GLOBAL_LINE_MINIMUM = 95.0
GLOBAL_BRANCH_MINIMUM = 85.0

# Floors are intentionally below the measured values so that CI catches a
# regression without depending on platform-specific rounding. Each value is a
# (line, branch) percentage pair.
TARGETS = {
    "modules/builder.py": (93.0, 82.0),
    "modules/processing.py": (94.5, 88.0),
    "modules/cleanup.py": (94.0, 86.5),
    "modules/utils.py": (93.0, 84.0),
    "helper/tmdb_cache.py": (94.0, 90.0),
    "helper/fanart.py": (98.0, 90.0),
    "helper/logging.py": (97.0, 89.0),
    "helper/plex_metadata.py": (92.0, 85.0),
    "helper/diagnostics.py": (98.0, 94.0),
    "helper/provider_replay.py": (96.0, 87.0),
    "helper/reporting.py": (82.0, 95.0),
    "helper/provider_mappings.py": (99.0, 98.0),
    "metafusion.py": (94.5, 91.0),
    "helper/state_db.py": (96.0, 92.0),
    "helper/identity_diagnostics.py": (95.0, 85.0),
    "helper/item_explanation.py": (96.0, 80.0),
    "helper/tmdb_changes.py": (100.0, 100.0),
    "helper/upgrade_canary.py": (100.0, 100.0),
    "helper/kometa_application_verification.py": (100.0, 100.0),
}


def _percentage(covered, total):
    return 100.0 if not total else float(covered) * 100.0 / float(total)


def _summary_percentages(summary):
    branch_total = summary.get("num_branches", 0)
    return (
        _percentage(summary.get("covered_lines", 0), summary.get("num_statements", 0)),
        (
            _percentage(summary.get("covered_branches", 0), branch_total)
            if branch_total
            else 0.0
        ),
    )


def evaluate(report):
    files = report.get("files", {})
    results = []
    totals = report.get("totals", {})
    line_percent, branch_percent = _summary_percentages(totals)
    results.append(
        {
            "filename": "application total",
            "line_minimum": GLOBAL_LINE_MINIMUM,
            "branch_minimum": GLOBAL_BRANCH_MINIMUM,
            "line_percent": line_percent,
            "branch_percent": branch_percent,
            "passed": (
                line_percent + 1e-9 >= GLOBAL_LINE_MINIMUM
                and branch_percent + 1e-9 >= GLOBAL_BRANCH_MINIMUM
            ),
        }
    )
    for filename, (line_minimum, branch_minimum) in TARGETS.items():
        summary = files.get(filename, {}).get("summary", {})
        line_percent, branch_percent = _summary_percentages(summary)
        results.append(
            {
                "filename": filename,
                "line_minimum": line_minimum,
                "branch_minimum": branch_minimum,
                "line_percent": line_percent,
                "branch_percent": branch_percent,
                "passed": (
                    bool(summary)
                    and line_percent + 1e-9 >= line_minimum
                    and branch_percent + 1e-9 >= branch_minimum
                ),
            }
        )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check application and targeted line/branch coverage floors"
    )
    parser.add_argument("report", nargs="?", default="coverage.json")
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    results = evaluate(report)
    for result in results:
        state = "PASS" if result["passed"] else "FAIL"
        print(
            f"[{state}] {result['filename']}: "
            f"line {result['line_percent']:.2f}% "
            f"(minimum {result['line_minimum']:.2f}%), "
            f"branch {result['branch_percent']:.2f}% "
            f"(minimum {result['branch_minimum']:.2f}%)"
        )
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
