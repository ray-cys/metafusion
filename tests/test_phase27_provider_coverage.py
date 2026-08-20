import copy
import json
import runpy
import sys
from pathlib import Path

import pytest

from tools import check_provider_coverage, provider_compatibility

REPO_ROOT = Path(__file__).parents[1]
MANIFEST_PATH = REPO_ROOT / ".github" / "provider-contracts.json"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.in"


def _manifest():
    return provider_compatibility.load_manifest(MANIFEST_PATH)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("[]", "must be an object"),
        ("{", "Could not read provider contract"),
    ],
)
def test_manifest_loader_rejects_invalid_documents(tmp_path, document, message):
    path = tmp_path / "contract.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(provider_compatibility.ContractError, match=message):
        provider_compatibility.load_manifest(path)

    with pytest.raises(provider_compatibility.ContractError, match="Could not read"):
        provider_compatibility.load_manifest(tmp_path / "missing.json")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("requests==1.0\n", "exact plexapi"),
        ("plexapi==latest\n", "Unsupported PlexAPI"),
    ],
)
def test_plex_pin_rejects_missing_or_unstable_versions(tmp_path, content, message):
    path = tmp_path / "requirements.in"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(provider_compatibility.ContractError, match=message):
        provider_compatibility.plexapi_pin(path)
    with pytest.raises(provider_compatibility.ContractError, match="Could not read"):
        provider_compatibility.plexapi_pin(tmp_path / "missing.in")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(format=1), "format must be 2"),
        (lambda value: value["kometa"].pop("image"), "contract is missing"),
        (lambda value: value["kometa"].update(profile="Bad Profile"), "profile"),
        (lambda value: value["kometa"].update(repository="invalid"), "repository"),
        (lambda value: value["kometa"].update(image="UPPER/image"), "image"),
        (lambda value: value["kometa"].update(schema_path="../schema.json"), "schema path"),
        (lambda value: value["kometa"]["baseline"].pop("digest"), "contract is missing"),
        (
            lambda value: value["kometa"]["baseline"].update(release="v9.0.0"),
            "baseline release cannot be newer",
        ),
        (lambda value: value["plex"].update(package="other"), "package must be plexapi"),
        (
            lambda value: value["plex"].update(version_source="floating"),
            "version source must be requirements.in",
        ),
        (lambda value: value["plex"].update(replay_tests=[]), "declare replay tests"),
        (lambda value: value["plex"].update(replay_tests=[3]), "below tests"),
    ],
)
def test_contract_validation_rejects_each_unsafe_shape(mutate, message):
    manifest = copy.deepcopy(_manifest())
    mutate(manifest)
    with pytest.raises(provider_compatibility.ContractError, match=message):
        provider_compatibility.validate_manifest(manifest, REQUIREMENTS_PATH)


def test_output_without_github_file_is_json(capsys):
    values = provider_compatibility.emit_outputs(MANIFEST_PATH, REQUIREMENTS_PATH)
    assert json.loads(capsys.readouterr().out) == values


def test_fetch_and_discovery_reject_non_objects_and_invalid_plex(monkeypatch):
    monkeypatch.setattr(
        provider_compatibility,
        "urlopen",
        lambda *_args, **_kwargs: __import__("io").BytesIO(b"[]"),
    )
    with pytest.raises(provider_compatibility.ContractError, match="must be an object"):
        provider_compatibility.fetch_json("https://example.invalid")

    def fetcher(url, **_kwargs):
        if "github" in url:
            return {"tag_name": "v2.5.0"}
        return {"info": {"version": "latest"}}

    with pytest.raises(provider_compatibility.ContractError, match="PlexAPI version"):
        provider_compatibility.discover_latest(
            _manifest(), REQUIREMENTS_PATH, fetcher=fetcher
        )


def test_schema_comparison_rejects_bad_input_and_reports_truncation(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    valid = tmp_path / "valid.json"
    valid.write_text("{}", encoding="utf-8")
    with pytest.raises(provider_compatibility.ContractError, match="Could not compare"):
        provider_compatibility.schema_diff(invalid, valid)

    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(json.dumps({"properties": {}}), encoding="utf-8")
    new.write_text(
        json.dumps({"properties": {f"field_{index}": index for index in range(4)}}),
        encoding="utf-8",
    )
    report = provider_compatibility.schema_diff(old, new, limit=1)
    assert "additional paths omitted" in report


@pytest.mark.parametrize(
    ("release", "digest", "message"),
    [
        ("nightly", "sha256:" + "a" * 64, "stable Kometa release"),
        ("v2.5.0", "short", "full sha256 digest"),
    ],
)
def test_contract_update_rejects_invalid_candidate_coordinates(
    tmp_path, release, digest, message
):
    with pytest.raises(provider_compatibility.ContractError, match=message):
        provider_compatibility.update_kometa_contract(
            MANIFEST_PATH,
            release=release,
            digest=digest,
            old_schema=tmp_path / "old.json",
            new_schema=tmp_path / "new.json",
            report_path=tmp_path / "report.md",
        )


def test_contract_update_rejects_invalid_candidate_schema(tmp_path):
    old = tmp_path / "old.json"
    old.write_text("{}", encoding="utf-8")
    new = tmp_path / "new.json"
    new.write_text("{", encoding="utf-8")
    manifest = _manifest()
    manifest["kometa"]["current"]["schema_sha256"] = provider_compatibility._schema_hash(old)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(provider_compatibility.ContractError, match="Candidate Kometa schema is invalid"):
        provider_compatibility.update_kometa_contract(
            manifest_path,
            release="v2.5.0",
            digest="sha256:" + "a" * 64,
            old_schema=old,
            new_schema=new,
            report_path=tmp_path / "report.md",
        )


def test_provider_coverage_checker_enforces_both_dimensions(tmp_path, capsys):
    passing = {
        "totals": {
            "num_statements": 100,
            "covered_lines": 100,
            "num_branches": 100,
            "covered_branches": 100,
        }
    }
    report = tmp_path / "provider.json"
    report.write_text(json.dumps(passing), encoding="utf-8")
    assert check_provider_coverage.main([str(report)]) == 0
    assert "[PASS]" in capsys.readouterr().out

    passing["totals"].update(covered_branches=99)
    report.write_text(json.dumps(passing), encoding="utf-8")
    assert check_provider_coverage.main([str(report)]) == 1
    assert "[FAIL]" in capsys.readouterr().out

    passing["totals"].update(covered_lines=99, covered_branches=100)
    report.write_text(json.dumps(passing), encoding="utf-8")
    assert check_provider_coverage.main([str(report)]) == 1
    assert "[FAIL]" in capsys.readouterr().out

    missing = check_provider_coverage.evaluate({})
    assert missing["passed"] is False
    assert missing["line_percent"] == 100.0
    assert missing["branch_percent"] == 0.0


def test_provider_compatibility_module_entrypoint(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provider_compatibility.py",
            "--manifest",
            str(MANIFEST_PATH),
            "--requirements",
            str(REQUIREMENTS_PATH),
            "verify",
        ],
    )
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(provider_compatibility.__file__, run_name="__main__")
    assert "Provider contracts valid" in capsys.readouterr().out
