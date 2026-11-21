from __future__ import annotations

import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools import maintenance_qualification as qualification

REPOSITORY = "ray-cys/metafusion"
SHA = "a" * 40
BASE_SHA = "b" * 40
RUN_ID = 1234


class GitHubEvidence:
    def __init__(self, branch="main"):
        self.pull = {
            "state": "open", "draft": False, "user": {"login": "github-actions[bot]"},
            "base": {"repo": {"full_name": REPOSITORY}, "ref": branch, "sha": BASE_SHA},
            "head": {"repo": {"full_name": REPOSITORY}, "sha": SHA,
                     "ref": f"automation/runtime-maintenance-{branch}"},
        }
        self.comparison = {
            "status": "ahead", "merge_base_commit": {"sha": BASE_SHA},
            "files": [{"filename": "Dockerfile"}],
        }
        self.run = {
            "id": RUN_ID, "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY}, "head_sha": SHA,
            "head_branch": self.pull["head"]["ref"],
            "path": ".github/workflows/docker-latest.yml", "event": "workflow_dispatch",
            "status": "completed", "conclusion": "success",
        }
        self.jobs = [
            {"name": name, "id": index, "status": "completed", "conclusion": "success",
             "head_sha": SHA,
             "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{RUN_ID}/job/{index}"}
            for index, name in enumerate(qualification.QUALIFICATION_JOBS, start=1)
        ]
        self.calls = []
        self.posts = []

    def __call__(self, endpoint, payload=None):
        self.calls.append(endpoint)
        if payload is not None:
            assert endpoint == f"repos/{REPOSITORY}/statuses/{SHA}"
            self.posts.append(payload)
            return {}
        if "/pulls/" in endpoint:
            return copy.deepcopy(self.pull)
        if "/compare/" in endpoint:
            return copy.deepcopy(self.comparison)
        if "/jobs?" in endpoint:
            return {"jobs": copy.deepcopy(self.jobs), "total_count": len(self.jobs)}
        assert endpoint == f"repos/{REPOSITORY}/actions/runs/{RUN_ID}"
        return copy.deepcopy(self.run)


def _arguments(tmp_path, branch="main", summary=True):
    arguments = [
        "--repository", REPOSITORY, "--pr", "120", "--branch", branch,
        "--sha", SHA, "--run-id", str(RUN_ID),
        "--evidence", str(tmp_path / "evidence.json"),
    ]
    if summary:
        arguments.extend(["--github-summary", str(tmp_path / "summary.md")])
    return arguments


@pytest.mark.parametrize("branch,summary", [("main", True), ("develop", False)])
def test_report_and_premerge_revalidation_require_real_exact_commit_success(
    monkeypatch, tmp_path, capsys, branch, summary,
):
    github = GitHubEvidence(branch)
    monkeypatch.setattr(qualification, "github_json", github)
    arguments = _arguments(tmp_path, branch, summary)
    assert qualification.main(arguments) == 0
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["base_sha"] == BASE_SHA and evidence["head_sha"] == SHA
    assert [status["context"] for status in github.posts] == list(qualification.QUALIFICATION_JOBS)
    for job, status in zip(evidence["jobs"], github.posts, strict=True):
        assert status["state"] == "success" and status["target_url"] == job["url"]
    github.posts.clear()
    assert qualification.main([*arguments, "--verify-only"]) == 0
    assert not github.posts and "Verified all 8 Docker jobs" in capsys.readouterr().out
    if summary:
        assert "exact commit" in (tmp_path / "summary.md").read_text()


@pytest.mark.parametrize("repository,number,branch,sha,run_id", [
    ("bad/repo/extra", 120, "main", SHA, RUN_ID),
    (REPOSITORY, 0, "main", SHA, RUN_ID),
    (REPOSITORY, 120, "feature", SHA, RUN_ID),
    (REPOSITORY, 120, "main", "short", RUN_ID),
    (REPOSITORY, 120, "main", SHA, 0),
])
def test_invalid_inputs_cannot_reach_github(monkeypatch, repository, number, branch, sha, run_id):
    github = GitHubEvidence()
    monkeypatch.setattr(qualification, "github_json", github)
    with pytest.raises(ValueError):
        qualification.collect_evidence(repository, number, branch, sha, run_id)
    assert not github.calls


@pytest.mark.parametrize("path,value", [
    (("state",), "closed"), (("draft",), True),
    (("user", "login"), "external-user"),
    (("base", "repo", "full_name"), "another/repo"),
    (("head", "repo", "full_name"), "fork/repo"),
    (("base", "ref"), "develop"), (("head", "ref"), "automation/not-a-repair"),
    (("head", "sha"), "c" * 40),
])
def test_changed_or_untrusted_repair_pr_never_gets_passing_statuses(
    monkeypatch, tmp_path, path, value,
):
    github = GitHubEvidence()
    target = github.pull
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    monkeypatch.setattr(qualification, "github_json", github)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts and not (tmp_path / "evidence.json").exists()


@pytest.mark.parametrize("comparison", [
    {"status": "behind"}, {"merge_base_commit": {"sha": "c" * 40}},
    {"files": []}, {"files": [{"filename": "metafusion.py"}]},
])
def test_stale_base_and_feature_changes_are_not_eligible_for_auto_repair(
    monkeypatch, tmp_path, comparison,
):
    github = GitHubEvidence()
    github.comparison.update(comparison)
    monkeypatch.setattr(qualification, "github_json", github)
    assert qualification.main(_arguments(tmp_path, summary=False)) == 1
    assert not github.posts


@pytest.mark.parametrize("change", [
    {"id": 999}, {"repository": {"full_name": "other/repo"}},
    {"head_repository": {"full_name": "fork/repo"}}, {"head_sha": "c" * 40},
    {"head_branch": "main"}, {"path": ".github/workflows/other.yml"},
    {"event": "pull_request"}, {"status": "in_progress"}, {"conclusion": "failure"},
])
def test_failed_incomplete_wrong_workflow_or_stale_run_is_rejected(monkeypatch, tmp_path, change):
    github = GitHubEvidence()
    github.run.update(change)
    monkeypatch.setattr(qualification, "github_json", github)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts


@pytest.mark.parametrize("change", [
    {"status": "in_progress"}, {"conclusion": "skipped"}, {"conclusion": "failure"},
    {"head_sha": "c" * 40}, {"html_url": "https://elsewhere.example/job/1"},
])
def test_every_required_job_must_pass_not_merely_the_overall_run(monkeypatch, tmp_path, change):
    github = GitHubEvidence()
    github.jobs[-1].update(change)
    monkeypatch.setattr(qualification, "github_json", github)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_required_jobs_are_rejected(monkeypatch, tmp_path, duplicate):
    github = GitHubEvidence()
    if duplicate:
        github.jobs.append(copy.deepcopy(github.jobs[0]))
    else:
        github.jobs.pop()
    monkeypatch.setattr(qualification, "github_json", github)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts


def test_job_evidence_is_paginated(monkeypatch):
    github = GitHubEvidence()

    def paginated(endpoint, payload=None):
        if "/jobs?" in endpoint:
            start = 0 if endpoint.endswith("page=1") else 4
            return {"jobs": github.jobs[start:start + 4], "total_count": 8}
        return github(endpoint, payload)

    monkeypatch.setattr(qualification, "github_json", paginated)
    assert len(qualification.collect_evidence(REPOSITORY, 120, "main", SHA, RUN_ID)["jobs"]) == 8


@pytest.mark.parametrize("empty", [True, False])
def test_incomplete_pagination_fails_closed_with_a_bound(monkeypatch, tmp_path, empty):
    github = GitHubEvidence()

    def incomplete(endpoint, payload=None):
        if "/jobs?" in endpoint:
            return {"jobs": [] if empty else github.jobs[:1], "total_count": 1000}
        return github(endpoint, payload)

    monkeypatch.setattr(qualification, "github_json", incomplete)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts


@pytest.mark.parametrize("change_head", [False, True])
def test_racing_base_or_head_update_cannot_receive_stale_success(monkeypatch, tmp_path, change_head):
    github = GitHubEvidence()
    pull_reads = 0

    def racing(endpoint, payload=None):
        nonlocal pull_reads
        if "/pulls/" in endpoint:
            pull_reads += 1
            if pull_reads == 2:
                github.pull["head" if change_head else "base"]["sha"] = "c" * 40
        return github(endpoint, payload)

    monkeypatch.setattr(qualification, "github_json", racing)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not github.posts


@pytest.mark.parametrize("broken_file", ["changed", "malformed", "missing"])
def test_premerge_evidence_must_still_match_and_cannot_be_missing(
    monkeypatch, tmp_path, broken_file,
):
    github = GitHubEvidence()
    monkeypatch.setattr(qualification, "github_json", github)
    if broken_file != "missing":
        (tmp_path / "evidence.json").write_text("{}" if broken_file == "changed" else "not json")
    assert qualification.main([*_arguments(tmp_path), "--verify-only"]) == 1
    assert not github.posts


@pytest.mark.parametrize("broken_payload", [None, {}])
def test_invalid_github_payload_is_an_actionable_failure(monkeypatch, tmp_path, broken_payload):
    monkeypatch.setattr(qualification, "github_json", lambda *_a, **_k: broken_payload)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert "requires attention" in (tmp_path / "summary.md").read_text()


def test_status_write_permission_failure_is_not_hidden(monkeypatch, tmp_path):
    github = GitHubEvidence()

    def forbidden(endpoint, payload=None):
        if payload is not None:
            raise RuntimeError("HTTP 403: commit statuses write permission is required")
        return github(endpoint, payload)

    monkeypatch.setattr(qualification, "github_json", forbidden)
    assert qualification.main(_arguments(tmp_path)) == 1
    assert not (tmp_path / "evidence.json").exists()


def test_evidence_storage_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    github = GitHubEvidence()
    monkeypatch.setattr(qualification, "github_json", github)
    (tmp_path / "evidence.json").mkdir()
    assert qualification.main(_arguments(tmp_path)) == 1


@pytest.mark.parametrize("payload,stdout", [(None, '{"ok":true}'), ({"state": "success"}, "")])
def test_github_requests_use_json_stdin_and_bounded_subprocesses(monkeypatch, payload, stdout):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(qualification.subprocess, "run", run)
    result = qualification.github_json("repos/example/repo/statuses/sha", payload)
    command, options = calls[0]
    assert command[:2] == ["gh", "api"] and options["timeout"] == 90
    assert options["check"] and options["capture_output"] and options["text"]
    if payload is None:
        assert result == {"ok": True} and options["input"] is None
    else:
        assert result is None and json.loads(options["input"]) == payload
        assert command[-4:] == ["--method", "POST", "--input", "-"]


@pytest.mark.parametrize("timeout", [False, True])
def test_only_transient_errors_are_retried_with_backoff(monkeypatch, timeout):
    calls = []
    sleeps = []

    def run(*_args, **_kwargs):
        calls.append(1)
        if len(calls) < 3:
            if timeout:
                raise subprocess.TimeoutExpired("gh", 90)
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 503: unavailable")
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(qualification.subprocess, "run", run)
    monkeypatch.setattr(qualification.time, "sleep", sleeps.append)
    assert qualification.github_json("repos/example/repo") == {}
    assert sleeps == [2, 4] and len(calls) == 3


@pytest.mark.parametrize("message,count", [("HTTP 403: forbidden", 1), ("HTTP 502: unavailable", 3)])
def test_permission_failures_and_exhausted_retries_remain_visible(monkeypatch, message, count):
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise subprocess.CalledProcessError(1, "gh", stderr=message)

    monkeypatch.setattr(qualification.subprocess, "run", fail)
    monkeypatch.setattr(qualification.time, "sleep", lambda *_a: None)
    with pytest.raises(RuntimeError, match="GitHub request failed"):
        qualification.github_json("repos/example/repo")
    assert len(calls) == count


def test_cli_entrypoint_supports_help_without_github(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["maintenance_qualification.py", "--help"])
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(qualification.__file__)), run_name="__main__")
    assert result.value.code == 0


def test_workflow_reports_verified_statuses_before_normal_protected_merge():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/dependency-maintenance.yml").read_text()
    assert "statuses: write" in workflow
    assert workflow.index("gh run watch") < workflow.index("python tools/maintenance_qualification.py")
    assert workflow.index("--verify-only") < workflow.index("gh pr merge")
    assert '--match-head-commit "${REPAIR_SHA}"' in workflow
    assert "--admin" not in workflow and "gh pr review" not in workflow
    assert 'git merge-base --is-ancestor "${base_sha}" "${remote}"' in workflow
    assert "Reusing unchanged repair commit" in workflow
    assert workflow.count('--created ">=${requested_at}"') == 2
    assert 'timeout 35m gh run watch "${publish_id}"' in workflow
    assert "maintenance-qualification.json" in workflow
    for name in ("docker-latest.yml", "nightly-reliability.yml"):
        content = (root / ".github/workflows" / name).read_text()
        assert "--cov=tools.maintenance_qualification" in content
        assert "--cov-branch --cov-report=term-missing --cov-fail-under=100" in content
    nightly = (root / ".github/workflows/nightly-reliability.yml").read_text()
    assert "cache: pip" not in nightly
    assert nightly.count("persist-credentials: false") == 2


@pytest.mark.parametrize("existing,base_moved,content_changed", [
    (True, False, False), (True, True, False), (True, False, True), (False, False, False),
])
def test_real_git_repairs_reuse_only_identical_up_to_date_commits(
    tmp_path, existing, base_moved, content_changed,
):
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", *arguments], cwd=repository, capture_output=True, text=True,
            check=True, timeout=20,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main")
    git("config", "user.name", "Test Bot")
    git("config", "user.email", "bot@example.invalid")
    git("remote", "add", "origin", str(remote))
    for filename in qualification.REPAIR_FILES:
        (repository / filename).write_text("original\n")
    git("add", ".")
    git("commit", "-m", "Base")
    git("push", "origin", "main")
    branch = "automation/runtime-maintenance-main"
    previous_sha = None
    if existing:
        git("switch", "-c", branch)
        (repository / "Dockerfile").write_text("repaired\n")
        git("add", "Dockerfile")
        git("commit", "-m", "Existing repair")
        git("push", "origin", branch)
        previous_sha = git("rev-parse", "HEAD")
        git("switch", "main")
    if base_moved:
        (repository / "documentation.txt").write_text("New trusted base\n")
        git("add", "documentation.txt")
        git("commit", "-m", "Advance base")
        git("push", "origin", "main")
    base_sha = git("rev-parse", "HEAD")
    (repository / "Dockerfile").write_text("new repair\n" if content_changed else "repaired\n")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/dependency-maintenance.yml").read_text())
    prepare = next(
        step["run"] for step in workflow["jobs"]["repair"]["steps"]
        if step["name"] == "Create or update maintenance PR"
    ).split("body=", 1)[0]
    environment = os.environ.copy()
    summary = tmp_path / "summary.md"
    environment.update(BASE_BRANCH="main", GITHUB_STEP_SUMMARY=str(summary))
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", prepare + '\nprintf "%s\\n" "$repair_sha"'],
        cwd=repository, env=environment, capture_output=True, text=True, check=True, timeout=30,
    )
    repair_sha = result.stdout.strip().splitlines()[-1]
    git("merge-base", "--is-ancestor", base_sha, repair_sha)
    assert git("rev-parse", f"origin/{branch}") == repair_sha
    if existing and not base_moved and not content_changed:
        assert repair_sha == previous_sha and "Reusing unchanged" in summary.read_text()
    else:
        assert repair_sha != previous_sha and not git("status", "--porcelain")
