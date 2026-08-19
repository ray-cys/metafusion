#!/usr/bin/env python3
"""Generate representative Kometa YAML from MetaFusion's real serializers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.kometa import (
    build_episode_metadata,
    merge_generated_metadata,
    normalize_metadata_order,
    validate_metadata_document,
)

DEFAULT_OUTPUT = REPO_ROOT / "tests" / "golden" / "kometa_contract_corpus"


def _movie_document():
    standard, _diagnostics = merge_generated_metadata(
        {
            "label": ["Manual label is preserved"],
            "tagline": "A manually supplied fallback tagline.",
        },
        {
            "match": {
                "title": "MetaFusion Contract Movie",
                "year": 2020,
                "mapping_id": 123,
            },
            "sort_title": "MetaFusion Contract Movie",
            "original_title": "Original Contract Movie",
            "originally_available": "2020-01-02",
            "content_rating": "PG-13",
            "studio": "Contract Studio",
            "tagline": "",
            "summary": "Contract movie summary.",
            "country": ["United States of America"],
            "genre": ["Science Fiction"],
            "director": ["Example Director"],
            "writer": ["Example Writer"],
            "producer": ["Example Producer"],
        },
        "movie",
    )
    edition, _diagnostics = merge_generated_metadata(
        {},
        {
            "match": {
                "title": "MetaFusion Edition Contract",
                "year": 2022,
                "mapping_id": 789,
                "edition": "Director's Cut",
            },
            "sort_title": "MetaFusion Edition Contract",
            "original_title": "MetaFusion Edition Contract",
            "originally_available": "2022-03-04",
            "content_rating": "R",
            "studio": "Contract Studio",
            "summary": "Edition-aware contract entry.",
            "genre.sync": ["Drama"],
        },
        "movie",
    )
    document = {
        "metadata": {
            "MetaFusion Contract Movie (2020)": standard,
            "MetaFusion Edition Contract (2022) [Director's Cut]": edition,
        }
    }
    normalize_metadata_order(document)
    validate_metadata_document(document, library_type="movie")
    return document


def _show_document():
    special = build_episode_metadata(
        {
            "name": "Contract Special",
            "air_date": "2021-02-03",
            "overview": "Contract special summary.",
        },
        directors=["Example Director"],
        writers=["Example Writer"],
        enhanced=True,
    )
    episode = build_episode_metadata(
        {
            "name": "Contract Premiere",
            "air_date": "2021-02-10",
            "overview": "Contract episode summary.",
        },
        directors=["Example Director"],
        writers=["Example Writer"],
        enhanced=True,
    )
    show, _diagnostics = merge_generated_metadata(
        {"collection": ["Manual collection"]},
        {
            "match": {
                "title": "MetaFusion Contract Show",
                "year": 2021,
                "mapping_id": 456,
            },
            "sort_title": "MetaFusion Contract Show",
            "original_title": "Original Contract Show",
            "originally_available": "2021-02-03",
            "content_rating": "TV-14",
            "studio": "Contract Network",
            "tagline": "Contract show tagline.",
            "summary": "Contract show summary.",
            "genre": ["Drama"],
            "seasons": {
                0: {
                    "title": "Specials",
                    "summary": "Contract specials.",
                    "episodes": {1: special},
                },
                1: {
                    "title": "Season 1",
                    "summary": "Contract first season.",
                    "episodes": {1: episode},
                },
            },
        },
        "show",
        authoritative_seasons={0, 1},
        authoritative_episodes={0: {1}, 1: {1}},
    )
    document = {"metadata": {"MetaFusion Contract Show (2021)": show}}
    normalize_metadata_order(document)
    validate_metadata_document(document, library_type="show")
    return document


def generated_documents():
    """Return the deterministic corpus keyed by its committed filename."""
    return {
        "movies.yml": _movie_document(),
        "shows.yml": _show_document(),
    }


def rendered_documents():
    return {
        name: yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        for name, document in generated_documents().items()
    }


def generate(output: Path, *, check=False):
    stale = []
    for name, contents in rendered_documents().items():
        path = output / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == contents:
            continue
        stale.append(path)
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")
    unexpected = sorted(
        path for path in output.glob("*.yml") if path.name not in rendered_documents()
    )
    stale.extend(unexpected)
    if not check:
        for path in unexpected:
            path.unlink()
    return stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed generated corpus is stale",
    )
    args = parser.parse_args(argv)
    stale = generate(args.output, check=args.check)
    if stale and args.check:
        print("Stale Kometa contract corpus:", file=sys.stderr)
        for path in stale:
            print(f"- {path}", file=sys.stderr)
        return 1
    if stale:
        print("Generated Kometa contract corpus: " + ", ".join(map(str, stale)))
    else:
        print("Kometa contract corpus is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
