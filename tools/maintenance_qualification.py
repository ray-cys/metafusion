#!/usr/bin/env python3
"""Attach verified, exact-commit Docker qualification evidence to a repair PR."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

REPAIR_FILES = {"Dockerfile", "requirements.in", "requirements.txt", "requirements-dev.txt"}
QUALIFICATION_JOBS = (
    "release-guard", "test (3.11)", "test (3.13)", "kometa-contract",
    "plex-contract", "performance-regression", "security", "build",
)


def github_json(endpoint, payload=None):
    """Retry bounded transport failures, never permissions or failed validation."""
    command = ["gh", "api", endpoint]
    if payload is not None:
        command.extend(["--method", "POST", "--input", "-"])
    for attempt in range(3):
        try:
            result = subprocess.run(
                command, input=None if payload is None else json.dumps(payload),
                capture_output=True, text=True, check=True, timeout=90,
            )
            return json.loads(result.stdout) if result.stdout.strip() else None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            message = str(getattr(error, "stderr", "") or "GitHub request timed out")
            transient = isinstance(error, subprocess.TimeoutExpired) or any(
                marker in message for marker in (
                    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
                    "connection reset", "timed out",
                )
            )
            if not transient or attempt == 2:
                raise RuntimeError(f"GitHub request failed: {message[:500]}") from error
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


def repair_pull(repository, number, branch, sha):
    pull = github_json(f"repos/{repository}/pulls/{number}")
    expected = (
        pull["state"] == "open", not pull["draft"],
        pull["user"]["login"] == "github-actions[bot]",
        pull["base"]["repo"]["full_name"] == repository,
        pull["head"]["repo"]["full_name"] == repository,
        pull["base"]["ref"] == branch,
        pull["head"]["ref"] == f"automation/runtime-maintenance-{branch}",
        pull["head"]["sha"] == sha,
    )
    if not all(expected):
        raise ValueError("repair PR repository, author, branch, state or exact head changed")
    return pull


def collect_evidence(repository, number, branch, sha, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid repository")
    if branch not in {"main", "develop"} or number < 1 or run_id < 1:
        raise ValueError("invalid repair branch, PR number or run ID")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("qualification requires a full commit SHA")
    pull = repair_pull(repository, number, branch, sha)
    base_sha = pull["base"]["sha"]
    comparison = github_json(f"repos/{repository}/compare/{base_sha}...{sha}")
    files = {item["filename"] for item in comparison["files"]}
    if (
        comparison["status"] != "ahead"
        or comparison["merge_base_commit"]["sha"] != base_sha
        or not files or not files <= REPAIR_FILES
    ):
        raise ValueError("repair must include the current base and change only maintenance files")
    run = github_json(f"repos/{repository}/actions/runs/{run_id}")
    expected = (
        run["id"] == run_id,
        run["repository"]["full_name"] == repository,
        run["head_repository"]["full_name"] == repository,
        run["head_sha"] == sha, run["head_branch"] == pull["head"]["ref"],
        run["path"] == ".github/workflows/docker-latest.yml",
        run["event"] == "workflow_dispatch", run["status"] == "completed",
        run["conclusion"] == "success",
    )
    if not all(expected):
        raise ValueError("Docker qualification is incomplete, failed or belongs to another commit")
    jobs = []
    page = 1
    while True:
        result = github_json(
            f"repos/{repository}/actions/runs/{run_id}/jobs"
            f"?filter=latest&per_page=100&page={page}"
        )
        jobs.extend(result["jobs"])
        if len(jobs) >= result["total_count"]:
            break
        if not result["jobs"] or page == 20:
            raise ValueError("incomplete qualification job evidence")
        page += 1
    run_url = f"https://github.com/{repository}/actions/runs/{run_id}"
    qualified = []
    for name in QUALIFICATION_JOBS:
        matches = [job for job in jobs if job["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"qualification requires exactly one {name} job")
        job = matches[0]
        if (
            job["status"] != "completed" or job["conclusion"] != "success"
            or job["head_sha"] != sha
            or job["html_url"] != f"{run_url}/job/{job['id']}"
        ):
            raise ValueError(f"qualification job {name} did not pass on the exact repair commit")
        qualified.append({"name": name, "url": job["html_url"]})
    if repair_pull(repository, number, branch, sha)["base"]["sha"] != base_sha:
        raise ValueError("base branch moved during qualification; regenerate and requalify the repair")
    return {
        "repository": repository, "pr_number": number, "branch": branch,
        "head_sha": sha, "base_sha": base_sha, "run_id": run_id,
        "run_url": run_url, "jobs": qualified,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--branch", choices=("main", "develop"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-summary", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        evidence = collect_evidence(args.repository, args.pr, args.branch, args.sha, args.run_id)
        if args.verify_only:
            if json.loads(args.evidence.read_text(encoding="utf-8")) != evidence:
                raise ValueError("qualification evidence changed before merge; requalify the repair")
        else:
            # workflow_dispatch job checks do not satisfy PR branch protection.
            # These statuses report actual successful jobs, not synthetic test results.
            for job in evidence["jobs"]:
                github_json(f"repos/{args.repository}/statuses/{args.sha}", {
                    "state": "success", "context": job["name"], "target_url": job["url"],
                    "description": f"Verified Docker qualification run {args.run_id}",
                })
            args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
        message = f"Maintenance qualification refused: {error}"
        print(message)
        if args.github_summary:
            with args.github_summary.open("a", encoding="utf-8") as summary:
                summary.write(f"## Dependency maintenance requires attention\n\n{message}\n")
        return 1
    message = (
        f"Verified all {len(evidence['jobs'])} Docker jobs for {args.branch} PR #{args.pr}, "
        f"exact commit {args.sha}. Evidence: {evidence['run_url']}"
    )
    print(message)
    if args.github_summary:
        with args.github_summary.open("a", encoding="utf-8") as summary:
            summary.write(f"{message}\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
