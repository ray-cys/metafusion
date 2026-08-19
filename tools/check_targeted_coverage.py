#!/usr/bin/env python3
"""Enforce useful coverage floors on high-risk MetaFusion modules."""

import argparse
import json
from pathlib import Path

TARGETS = {
    "modules/builder.py": 78.0,
    "helper/tmdb_cache.py": 85.0,
    "helper/logging.py": 85.0,
    "helper/provider_mappings.py": 90.0,
    "metafusion.py": 84.0,
    "helper/state_db.py": 86.0,
    "helper/identity_diagnostics.py": 85.0,
    "helper/item_explanation.py": 85.0,
    "tools/provider_compatibility.py": 85.0,
}


def evaluate(report):
    files = report.get("files", {})
    results = []
    for filename, minimum in TARGETS.items():
        summary = files.get(filename, {}).get("summary", {})
        percent = float(summary.get("percent_covered", 0.0))
        results.append(
            {
                "filename": filename,
                "minimum": minimum,
                "percent": percent,
                "passed": percent + 1e-9 >= minimum,
            }
        )
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check targeted module coverage floors"
    )
    parser.add_argument("report", nargs="?", default="coverage.json")
    args = parser.parse_args(argv)
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    results = evaluate(report)
    for result in results:
        state = "PASS" if result["passed"] else "FAIL"
        print(
            f"[{state}] {result['filename']}: {result['percent']:.2f}% "
            f"(minimum {result['minimum']:.2f}%)"
        )
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
