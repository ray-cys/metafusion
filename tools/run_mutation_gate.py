#!/usr/bin/env python3
"""Run a small deterministic mutation gate over MetaFusion safety invariants."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Mutation:
    name: str
    source: str
    original: str
    replacement: str
    test: str


MUTATIONS = (
    Mutation(
        "empty item batches must not write state",
        "helper/state_db.py",
        "if not items:\n        return False",
        "if not items:\n        return True",
        "tests/test_phase27_state_integration.py::test_state_empty_batches_filters_and_season_asset_forget",
    ),
    Mutation(
        "provider IDs must remain positive",
        "helper/provider_mappings.py",
        "if source_tmdb_id is None or int(source_tmdb_id) <= 0:",
        "if source_tmdb_id is None or int(source_tmdb_id) < 0:",
        "tests/test_phase27_high_risk_branches.py::test_provider_mapping_invalid_and_empty_boundaries",
    ),
    Mutation(
        "worker cancellation must propagate",
        "modules/processing.py",
        "if worker_cancelled:\n            raise asyncio.CancelledError",
        "if False and worker_cancelled:\n            raise asyncio.CancelledError",
        "tests/test_phase26_processing_matrix.py::test_process_item_and_library_cancellation_are_never_normalized",
    ),
    Mutation(
        "cleanup must verify managed checksums",
        "modules/cleanup.py",
        "if actual_checksum not in checksums:",
        "if actual_checksum in checksums:",
        "tests/test_phase26_cleanup_edges.py::test_plex_cleanup_checksum_and_scope_matrix",
    ),
)


def apply_mutation(root: Path, mutation: Mutation) -> None:
    path = root / mutation.source
    source = path.read_text(encoding="utf-8")
    occurrences = source.count(mutation.original)
    if occurrences != 1:
        raise RuntimeError(
            f"Mutation '{mutation.name}' expected one source match, found {occurrences}"
        )
    path.write_text(
        source.replace(mutation.original, mutation.replacement, 1), encoding="utf-8"
    )


def _copy_repository(destination: Path) -> None:
    shutil.copytree(
        REPO_ROOT,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".coverage",
            "coverage.json",
            "provider-coverage.json",
            "__pycache__",
            "*.pyc",
        ),
    )


def run_mutation(mutation: Mutation) -> bool:
    with tempfile.TemporaryDirectory(prefix="metafusion-mutation-") as work:
        root = Path(work) / "repository"
        _copy_repository(root)
        apply_mutation(root, mutation)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", mutation.test],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    if result.returncode == 0:
        print(f"[SURVIVED] {mutation.name}")
        return False
    print(f"[KILLED] {mutation.name}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that focused safety tests kill representative mutations"
    )
    parser.parse_args(argv)
    return 0 if all(run_mutation(mutation) for mutation in MUTATIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
