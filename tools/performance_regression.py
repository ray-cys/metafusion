#!/usr/bin/env python3
"""Measure a repeatable MetaFusion state/YAML workload against CI budgets."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from helper.state_db import MediaStateStore
from modules.kometa import validate_metadata_document
from tools.generate_kometa_contract_corpus import generated_documents

DEFAULT_BASELINE = REPO_ROOT / ".github" / "performance-baseline.json"


class PerformanceBudgetError(ValueError):
    """Raised when the committed performance budget is malformed."""


def load_baseline(path=DEFAULT_BASELINE):
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PerformanceBudgetError(f"Could not read performance baseline: {error}") from error
    if document.get("format") != 1:
        raise PerformanceBudgetError("performance baseline format must be 1")
    workload = document.get("workload")
    maximum = document.get("maximum")
    if not isinstance(workload, dict) or not isinstance(maximum, dict):
        raise PerformanceBudgetError("baseline must contain workload and maximum objects")
    required_workload = {
        "movies",
        "shows",
        "seasons",
        "episodes",
        "targeted_reads",
        "corpus_render_iterations",
    }
    required_maximum = {
        "total_seconds",
        "state_write_seconds",
        "targeted_read_seconds",
        "corpus_render_seconds",
        "peak_memory_mib",
        "database_mib",
    }
    if required_workload - set(workload) or required_maximum - set(maximum):
        raise PerformanceBudgetError("performance baseline is missing required measurements")
    if any(float(value) <= 0 for value in (*workload.values(), *maximum.values())):
        raise PerformanceBudgetError("performance baseline values must be positive")
    return document


def _season_counts(show_count, season_count):
    base, extra = divmod(season_count, show_count)
    return [base + (1 if index < extra else 0) for index in range(show_count)]


def _episode_counts(season_count, episode_count):
    base, extra = divmod(episode_count, season_count)
    return [base + (1 if index < extra else 0) for index in range(season_count)]


def synthetic_entries(workload):
    """Build a value-safe workload shaped like the documented large library."""
    movies = int(workload["movies"])
    shows = int(workload["shows"])
    seasons = int(workload["seasons"])
    episodes = int(workload["episodes"])
    entries = {}
    for index in range(movies):
        rating_key = str(index + 1)
        entries[f"movie:plex:{rating_key}"] = {
            "server_id": "benchmark-server",
            "library_uuid": "benchmark-movies",
            "library_name": "Movies",
            "rating_key": rating_key,
            "media_type": "movie",
            "tmdb_id": str(100_000 + index),
            "title": f"Movie {index:04d}",
            "year": 1980 + (index % 47),
            "plex_updated_at": "2026-01-01T00:00:00+00:00",
            "config_fingerprint": "benchmark",
            "poster_path": f"/assets/movie/{index:04d}/poster.jpg",
            "poster_checksum": "a" * 64,
        }

    season_distribution = _season_counts(shows, seasons)
    episode_distribution = iter(_episode_counts(seasons, episodes))
    for show_index, show_seasons in enumerate(season_distribution):
        rating_key = str(movies + show_index + 1)
        season_records = {}
        for season_number in range(show_seasons):
            episode_total = next(episode_distribution)
            season_records[str(season_number)] = {
                "season_path": f"/assets/tv/{show_index:03d}/Season{season_number:02d}.jpg",
                "season_checksum": "b" * 64,
                "episodes": {
                    str(episode): {"tmdb_episode_id": episode, "status": "complete"}
                    for episode in range(1, episode_total + 1)
                },
            }
        entries[f"tv:plex:{rating_key}"] = {
            "server_id": "benchmark-server",
            "library_uuid": "benchmark-shows",
            "library_name": "TV Shows",
            "rating_key": rating_key,
            "media_type": "tv",
            "tmdb_id": str(200_000 + show_index),
            "title": f"Show {show_index:03d}",
            "year": 1990 + (show_index % 37),
            "plex_updated_at": "2026-01-01T00:00:00+00:00",
            "config_fingerprint": "benchmark",
            "poster_path": f"/assets/tv/{show_index:03d}/poster.jpg",
            "poster_checksum": "c" * 64,
            "seasons": season_records,
        }
    return entries


def run_workload(workload, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "meta_db.sqlite3"
    started = time.perf_counter()
    tracemalloc.start()
    entries = synthetic_entries(workload)

    write_started = time.perf_counter()
    store = MediaStateStore(database)
    for key, entry in entries.items():
        store[key] = entry
    store.flush()
    write_seconds = time.perf_counter() - write_started

    targeted_started = time.perf_counter()
    target_count = min(int(workload["targeted_reads"]), int(workload["movies"]))
    targeted = store.entries_for_scope(
        "benchmark-server",
        "benchmark-movies",
        rating_keys=[str(index + 1) for index in range(target_count)],
    )
    targeted_seconds = time.perf_counter() - targeted_started
    if len(targeted) != target_count:
        raise RuntimeError(
            f"targeted state read returned {len(targeted)} of {target_count} rows"
        )
    store.close()

    corpus = generated_documents()
    corpus_started = time.perf_counter()
    rendered_bytes = 0
    for _index in range(int(workload["corpus_render_iterations"])):
        for document in corpus.values():
            validate_metadata_document(document)
            rendered_bytes += len(
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode(
                    "utf-8"
                )
            )
    corpus_seconds = time.perf_counter() - corpus_started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total_seconds = time.perf_counter() - started
    database_bytes = sum(
        path.stat().st_size
        for path in directory.glob("meta_db.sqlite3*")
        if path.is_file()
    )
    return {
        "total_seconds": round(total_seconds, 6),
        "state_write_seconds": round(write_seconds, 6),
        "targeted_read_seconds": round(targeted_seconds, 6),
        "corpus_render_seconds": round(corpus_seconds, 6),
        "peak_memory_mib": round(peak / (1024 * 1024), 3),
        "database_mib": round(database_bytes / (1024 * 1024), 3),
        "items_per_second": round(len(entries) / max(write_seconds, 0.000001), 3),
        "state_items": len(entries),
        "state_seasons": int(workload["seasons"]),
        "state_episodes": int(workload["episodes"]),
        "targeted_rows": len(targeted),
        "corpus_documents": len(corpus),
        "corpus_rendered_bytes": rendered_bytes,
    }


def evaluate(metrics, maximum):
    failures = []
    for name, limit in maximum.items():
        actual = float(metrics[name])
        if actual > float(limit):
            failures.append(f"{name} {actual:g} exceeded budget {float(limit):g}")
    return failures


def _summary(metrics, maximum, failures):
    lines = [
        "## MetaFusion performance regression measurement",
        "",
        "| Measurement | Result | Maximum |",
        "| --- | ---: | ---: |",
    ]
    for name, limit in maximum.items():
        lines.append(f"| `{name}` | {metrics[name]} | {limit} |")
    lines.extend(
        (
            "",
            f"- State throughput: **{metrics['items_per_second']} items/second**",
            f"- Result: **{'failed' if failures else 'passed'}**",
        )
    )
    if failures:
        lines.extend(f"- {failure}" for failure in failures)
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        with tempfile.TemporaryDirectory(prefix="metafusion-performance-") as directory:
            metrics = run_workload(baseline["workload"], directory)
        failures = evaluate(metrics, baseline["maximum"])
    except (OSError, RuntimeError, PerformanceBudgetError, ValueError) as error:
        print(f"Performance measurement failed: {error}", file=sys.stderr)
        return 1
    report = {
        "format": 1,
        "workload": baseline["workload"],
        "maximum": baseline["maximum"],
        "measurements": metrics,
        "passed": not failures,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    if args.github_summary:
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write(_summary(metrics, baseline["maximum"], failures))
    if failures:
        for failure in failures:
            print(f"Performance regression: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
