#!/usr/bin/env python3
"""Enforce the independent provider-maintenance coverage contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

LINE_MINIMUM = 100.0
BRANCH_MINIMUM = 100.0


def _percentage(covered: int, total: int) -> float:
    return 100.0 if not total else covered * 100.0 / total


def evaluate(report: dict[str, Any]) -> dict[str, float | bool]:
    totals = report.get("totals", {})
    line_percent = _percentage(
        int(totals.get("covered_lines", 0)), int(totals.get("num_statements", 0))
    )
    branch_total = int(totals.get("num_branches", 0))
    branch_percent = (
        _percentage(int(totals.get("covered_branches", 0)), branch_total)
        if branch_total
        else 0.0
    )
    return {
        "line_percent": line_percent,
        "branch_percent": branch_percent,
        "passed": (
            bool(totals.get("num_statements"))
            and branch_total > 0
            and line_percent + 1e-9 >= LINE_MINIMUM
            and branch_percent + 1e-9 >= BRANCH_MINIMUM
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check provider-maintenance line and branch coverage floors"
    )
    parser.add_argument("report", nargs="?", default="provider-coverage.json")
    args = parser.parse_args(argv)
    result = evaluate(json.loads(Path(args.report).read_text(encoding="utf-8")))
    state = "PASS" if result["passed"] else "FAIL"
    print(
        f"[{state}] provider maintenance: "
        f"line {result['line_percent']:.2f}% (minimum {LINE_MINIMUM:.2f}%), "
        f"branch {result['branch_percent']:.2f}% (minimum {BRANCH_MINIMUM:.2f}%)"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
