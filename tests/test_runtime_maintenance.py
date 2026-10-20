import json
from pathlib import Path

import pytest

from tools import refresh_runtime_dependencies as maintenance


def _advisory(name="aiohttp", version="3.14.3", fixes=None):
    return {
        "name": name, "version": version,
        "vulns": [{"fix_versions": fixes if fixes is not None else ["3.14.4"]}],
    }


def test_security_repairs_require_every_advisory_to_have_a_stable_compatible_fix():
    dependency = _advisory(fixes=["4.0.0", "3.14.4rc1", "invalid", "3.14.4"])
    dependency["vulns"].append({"fix_versions": ["3.14.5"]})
    assert maintenance.security_repairs({"dependencies": [dependency]}) == {"aiohttp": "3.14.5"}
    assert maintenance.security_repairs({"dependencies": [{"name": "safe", "vulns": []}]}) == {}
    for fixes in ([], ["4.0.0"], ["3.14.3"]):
        with pytest.raises(ValueError, match="manual review"):
            maintenance.security_repairs({"dependencies": [_advisory(fixes=fixes)]})
    with pytest.raises(ValueError, match="invalid dependency"):
        maintenance.security_repairs({"dependencies": [_advisory(name="bad;command")]})


def test_repairs_preserve_other_pins_and_cover_indirect_requirements():
    document = "aiohttp==3.14.3\nplexapi==4.18.2\nurllib3>=2.7.0,<3\n"
    result = maintenance.repair_requirements(document, {
        "aiohttp": "3.14.5", "urllib3": "2.8.0", "multidict": "6.9.1"
    })
    assert "aiohttp==3.14.5" in result and "plexapi==4.18.2" in result
    assert "urllib3>=2.8.0,<3" in result and "multidict>=6.9.1,<7" in result
    assert maintenance.repair_requirements(result, {}) == result


def test_base_refresh_cannot_change_the_supported_python_or_accept_partial_digests():
    document = "FROM python:3.13-slim-bookworm@sha256:" + "a" * 64 + "\nCOPY . /app\n"
    updated = maintenance.repair_base_image(document, "sha256:" + "b" * 64)
    assert updated.endswith("\nCOPY . /app\n") and "b" * 64 in updated
    with pytest.raises(ValueError, match="full sha256"):
        maintenance.repair_base_image(document, "sha256:short")
    with pytest.raises(ValueError, match="supported Python"):
        maintenance.repair_base_image("FROM python:3.14", "sha256:" + "b" * 64)


def test_maintenance_updates_both_hash_locks_and_constrains_development(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("requirements.in").write_text("aiohttp==3.14.3\n", encoding="utf-8")
    Path("Dockerfile").write_text(
        "FROM python:3.13-slim-bookworm@sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"dependencies": [_advisory()]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(maintenance.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    assert maintenance.main(["--audit", str(audit), "--base-image-digest", "sha256:" + "b" * 64]) == 0
    assert "aiohttp==3.14.4" in Path("requirements.in").read_text()
    assert len(calls) == 2 and all(call[1]["check"] for call in calls)
    assert "--upgrade-package" in calls[0][0]
    assert calls[1][0][-4:] == ["-c", "requirements.txt", "-o", "requirements-dev.txt"]


def test_daily_repair_requires_complete_qualification_before_protected_merge():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github/workflows/dependency-maintenance.yml").read_text()
    assert "branch: [main, develop]" in text and 'cron: "17 16 * * *"' in text
    assert text.index("gh run watch") < text.index("gh pr merge")
    assert "--exit-status" in text and "--match-head-commit" in text
    assert "--admin" not in text and "gh pr review" not in text
    assert "--event workflow_dispatch" in text and '--commit "${REPAIR_SHA}"' in text
    assert "--force-with-lease" in text
    assert "refs/tags" not in text
