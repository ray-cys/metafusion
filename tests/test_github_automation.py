from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load_script(name):
    path = REPO_ROOT / ".github" / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _workflow(name):
    payload = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_dependency_review_is_pinned_and_scoped_to_runtime_risk():
    text = (WORKFLOWS / "dependency-review.yml").read_text(encoding="utf-8")
    assert "actions/dependency-review-action@a1d282b36b6f3519aa1f3fc636f609c47dddb294" in text
    assert "fail-on-severity: high" in text
    assert "fail-on-scopes: runtime" in text
    assert "retry-on-snapshot-warnings: true" in text
    assert "pull_request:" in text
    assert 'branches: ["main", "develop"]' in text


def test_repository_integrity_pins_actions_and_verifies_actionlint():
    text = (WORKFLOWS / "repository-integrity.yml").read_text(encoding="utf-8")
    assert "actionlint_1.7.12_linux_amd64.tar.gz" in text
    assert "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8" in text
    assert "-shellcheck shellcheck" in text
    for owner in (
        "actions/checkout",
        "actions/setup-python",
        "zizmorcore/zizmor-action",
        "hadolint/hadolint-action",
        "DavidAnson/markdownlint-cli2-action",
        "lycheeverse/lychee-action",
    ):
        match = re.search(rf"uses: {re.escape(owner)}@([0-9a-f]+)", text)
        assert match and len(match.group(1)) == 40
    assert "advanced-security: false" in text
    assert "--offline" in text
    assert "github.event_name == 'schedule'" in text


def test_provider_canary_and_recovery_workflows_are_scheduled_read_only():
    provider_text = (WORKFLOWS / "provider-canary.yml").read_text(encoding="utf-8")
    recovery_text = (WORKFLOWS / "recovery-drill.yml").read_text(encoding="utf-8")
    assert 'cron: "47 19 * * *"' in provider_text
    assert "TMDB_CANARY_API_KEY: ${{ secrets.TMDB_CANARY_API_KEY }}" in provider_text
    assert "ref: develop" in provider_text
    assert "contents: read" in provider_text
    assert "packages: write" not in provider_text
    assert 'cron: "37 20 * * 4"' in recovery_text
    assert "state_recovery_drill.py" in recovery_text
    assert "test_temporary_plex_disconnect_retries_without_duplicate_mutation" in recovery_text
    assert "contents: read" in recovery_text
    assert "packages: write" not in recovery_text


def test_provider_canary_redacts_keys_and_supports_unconfigured_tmdb(monkeypatch, tmp_path):
    canary = _load_script("provider_canary.py")
    output = tmp_path / "canary.json"
    monkeypatch.setenv("TMDB_CANARY_API_KEY", "tmdb-secret")
    monkeypatch.setattr(canary, "fanart_project_api_key", lambda: "fanart-secret")
    monkeypatch.setattr(
        canary,
        "run_tmdb",
        lambda key: [{"provider": "TMDb", "check": "sample", "status": 200}],
    )
    monkeypatch.setattr(
        canary,
        "run_fanart",
        lambda key: [{"provider": "Fanart.tv", "check": "sample", "status": 200}],
    )
    monkeypatch.setattr(
        canary,
        "run_formula1",
        lambda: [{"provider": "Jolpica", "check": "sample", "status": 200}],
    )
    assert canary.main(["--output", str(output)]) == 0
    rendered = output.read_text(encoding="utf-8")
    assert "tmdb-secret" not in rendered
    assert "fanart-secret" not in rendered
    assert json.loads(rendered)["status"] == "passed"

    monkeypatch.delenv("TMDB_CANARY_API_KEY")
    assert canary.main(["--output", str(output), "--require-tmdb"]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["providers"][0]["status"] == "not_configured"


def test_provider_canary_validates_formula1_provider_surface(monkeypatch):
    canary = _load_script("provider_canary.py")
    year = canary.datetime.now(canary.timezone.utc).year

    def fake_json(provider, _path, **_kwargs):
        if provider.startswith("Jolpica"):
            return (
                200,
                {
                    "MRData": {
                        "RaceTable": {
                            "Races": [
                                {
                                    "round": "1",
                                    "raceName": "Example Grand Prix",
                                    "Circuit": {"circuitId": "example"},
                                }
                            ]
                        }
                    }
                },
                {},
                0.01,
            )
        return 200, {"query": {"pages": [{"pageid": 1}]}}, {}, 0.01

    def fake_request(provider, _path, **_kwargs):
        if provider == "Formula1.com calendar":
            return 200, f'<a href="/en/racing/{year}/example">Race</a>'.encode(), {}, 0.01
        return 200, b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', {
            "Content-Type": "image/svg+xml"
        }, 0.01

    monkeypatch.setattr(canary, "_request_json", fake_json)
    monkeypatch.setattr(canary, "_request", fake_request)
    checks = canary.run_formula1()
    assert [check["provider"] for check in checks] == [
        "Jolpica",
        "Formula1.com",
        "f1-circuits-svg",
        "Wikimedia Commons",
    ]


def test_state_recovery_drill_restores_and_survives_interruption(tmp_path):
    drill = _load_script("state_recovery_drill.py")
    output = tmp_path / "drill.json"
    report = drill.run_drill(output)
    assert report["status"] == "passed"
    assert report["source_counts"] == report["restored_counts"]
    assert report["restored_database"]["quick_check"] == "ok"
    assert report["bundle"]["secrets_redacted"] is True
    assert report["interrupted_writer"]["committed_items"] >= 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"


def test_published_architectures_run_operational_acceptance():
    text = (WORKFLOWS / "docker-latest.yml").read_text(encoding="utf-8")
    section = text.split("Exercise published AMD64 and ARM64 operational CLI", 1)[1]
    assert "for platform in linux/amd64 linux/arm64" in section
    for command in (
        "--doctor",
        "--support-report",
        "--sqlite-maintenance check --sqlite-target state",
        "--state-report --include-state-items",
        "--recovery-bundle",
        "--verify-recovery",
    ):
        assert command in section
    assert 'test ! -e "${config_dir}/config.yml"' in section


def test_release_documentation_explains_operational_automation():
    docs = (REPO_ROOT / "docs" / "release-testing.md").read_text(encoding="utf-8")
    for phrase in (
        "Dependency review",
        "Repository integrity",
        "TMDB_CANARY_API_KEY",
        "Live provider canary",
        "State and recovery drill",
        "AMD64 and ARM64",
    ):
        assert phrase in docs
