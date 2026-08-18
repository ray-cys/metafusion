import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from tools import provider_compatibility

REPO_ROOT = Path(__file__).parents[1]
MANIFEST_PATH = REPO_ROOT / ".github" / "provider-contracts.json"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.in"


def committed_manifest():
    return provider_compatibility.load_manifest(MANIFEST_PATH)


def test_committed_provider_contract_is_reproducible_and_complete():
    values = provider_compatibility.validate_manifest(
        committed_manifest(), REQUIREMENTS_PATH
    )

    assert values["kometa_release"] == "v2.4.8"
    assert values["kometa_image"].startswith("kometateam/kometa@sha256:")
    assert values["kometa_schema_url"].endswith(
        "/v2.4.8/json-schema/metadata-schema.json"
    )
    assert values["plexapi_version"] == "4.18.2"
    replay_tests = values["plex_replay_tests"].split()
    assert "tests/test_phase23_post_release.py" in replay_tests
    assert "tests/test_plex_metadata.py" in replay_tests
    assert all((REPO_ROOT / path).is_file() for path in replay_tests)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("release", "nightly", "stable Kometa release"),
        ("digest", "sha256:short", "full sha256 digest"),
        ("schema_sha256", "short", "full sha256 checksum"),
    ],
)
def test_manifest_rejects_unpinned_or_unstable_kometa_values(field, value, message):
    manifest = copy.deepcopy(committed_manifest())
    manifest["kometa"][field] = value

    with pytest.raises(provider_compatibility.ContractError, match=message):
        provider_compatibility.validate_manifest(manifest, REQUIREMENTS_PATH)


def test_manifest_rejects_unsafe_or_missing_plex_replay_paths():
    manifest = copy.deepcopy(committed_manifest())
    manifest["plex"]["replay_tests"] = ["../private-response.json"]

    with pytest.raises(provider_compatibility.ContractError, match="below tests"):
        provider_compatibility.validate_manifest(manifest, REQUIREMENTS_PATH)


def test_outputs_are_machine_readable_and_safe_for_github(tmp_path):
    output = tmp_path / "github-output"
    values = provider_compatibility.emit_outputs(
        MANIFEST_PATH, REQUIREMENTS_PATH, output
    )

    parsed = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert parsed == values
    assert all("\n" not in value for value in parsed.values())


def test_github_outputs_reject_multiline_values(tmp_path):
    with pytest.raises(provider_compatibility.ContractError, match="single line"):
        provider_compatibility._write_outputs(
            tmp_path / "output", {"unsafe": "one\ntwo"}
        )


def test_fetch_json_requires_https_and_applies_github_auth(monkeypatch):
    captured = {}

    def open_response(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return io.BytesIO(b'{"tag_name":"v2.4.8"}')

    monkeypatch.setattr(provider_compatibility, "urlopen", open_response)

    assert provider_compatibility.fetch_json(
        "https://api.github.com/example", token="secret"
    ) == {"tag_name": "v2.4.8"}
    assert captured == {"authorization": "Bearer secret", "timeout": 30}
    with pytest.raises(provider_compatibility.ContractError, match="requires HTTPS"):
        provider_compatibility.fetch_json("http://example.invalid")


def test_fetch_json_normalizes_network_failures(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(provider_compatibility, "urlopen", fail)

    with pytest.raises(provider_compatibility.ContractError, match="Could not query"):
        provider_compatibility.fetch_json("https://api.github.com/example")


def test_discovery_compares_stable_kometa_and_plexapi_releases():
    calls = []

    def fetcher(url, *, token=""):
        calls.append((url, token))
        if "api.github.com" in url:
            return {"tag_name": "v2.5.0"}
        return {"info": {"version": "4.19.0"}}

    result = provider_compatibility.discover_latest(
        committed_manifest(), REQUIREMENTS_PATH, token="token", fetcher=fetcher
    )

    assert result == {
        "kometa_latest": "v2.5.0",
        "kometa_changed": "true",
        "plexapi_latest": "4.19.0",
        "plexapi_changed": "true",
    }
    assert calls[0][1] == "token"
    assert calls[1][1] == ""


def test_discovery_rejects_nightly_or_malformed_upstream_versions():
    def fetcher(url, *, token=""):
        del token
        if "api.github.com" in url:
            return {"tag_name": "nightly"}
        return {"info": {"version": "4.19.0"}}

    with pytest.raises(provider_compatibility.ContractError, match="stable semver"):
        provider_compatibility.discover_latest(
            committed_manifest(), REQUIREMENTS_PATH, fetcher=fetcher
        )


def test_schema_update_is_atomic_and_writes_a_constraint_report(tmp_path):
    old_schema = tmp_path / "old.json"
    new_schema = tmp_path / "new.json"
    old_schema.write_text(
        json.dumps({"type": "object", "properties": {"metadata": {"type": "object"}}}),
        encoding="utf-8",
    )
    new_schema.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "metadata": {"type": "object"},
                    "new_field": {"type": "string"},
                },
                "required": ["metadata"],
            }
        ),
        encoding="utf-8",
    )
    manifest = committed_manifest()
    manifest["kometa"]["schema_sha256"] = hashlib.sha256(old_schema.read_bytes()).hexdigest()
    manifest_path = tmp_path / "provider-contracts.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = tmp_path / "report.md"
    digest = "sha256:" + ("a" * 64)

    changed = provider_compatibility.update_kometa_contract(
        manifest_path,
        release="v2.5.0",
        digest=digest,
        old_schema=old_schema,
        new_schema=new_schema,
        report_path=report,
    )

    updated = provider_compatibility.load_manifest(manifest_path)
    assert changed is True
    assert updated["kometa"]["release"] == "v2.5.0"
    assert updated["kometa"]["digest"] == digest
    assert updated["kometa"]["schema_sha256"] == hashlib.sha256(
        new_schema.read_bytes()
    ).hexdigest()
    assert not manifest_path.with_suffix(".json.tmp").exists()
    report_text = report.read_text(encoding="utf-8")
    assert "Constraint paths added: **2**" in report_text
    assert "/properties/new_field/type" in report_text
    assert "/required" in report_text


def test_schema_update_refuses_an_untrusted_baseline(tmp_path):
    old_schema = tmp_path / "old.json"
    new_schema = tmp_path / "new.json"
    old_schema.write_text("{}", encoding="utf-8")
    new_schema.write_text("{}", encoding="utf-8")
    manifest_path = tmp_path / "provider-contracts.json"
    manifest_path.write_text(json.dumps(committed_manifest()), encoding="utf-8")

    with pytest.raises(provider_compatibility.ContractError, match="pinned checksum"):
        provider_compatibility.update_kometa_contract(
            manifest_path,
            release="v2.5.0",
            digest="sha256:" + ("b" * 64),
            old_schema=old_schema,
            new_schema=new_schema,
            report_path=tmp_path / "report.md",
        )


def test_schema_diff_reports_no_semantic_change_and_normalizes_pointer_names(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "title": "Ignored documentation",
                "properties": {"slash/name~": {"enum": ["b", "a"]}},
            }
        ),
        encoding="utf-8",
    )

    report = provider_compatibility.schema_diff(schema, schema)

    assert "No machine-readable constraint changes" in report
    assert provider_compatibility._json_pointer(("slash/name~",)) == "/slash~1name~0"


def test_cli_commands_cover_machine_and_operator_interfaces(tmp_path, monkeypatch, capsys):
    common = [
        "--manifest",
        str(MANIFEST_PATH),
        "--requirements",
        str(REQUIREMENTS_PATH),
    ]
    assert provider_compatibility.main([*common, "verify"]) == 0
    assert "Provider contracts valid" in capsys.readouterr().out

    github_output = tmp_path / "github-output"
    assert (
        provider_compatibility.main(
            [*common, "outputs", "--github-output", str(github_output)]
        )
        == 0
    )
    assert "kometa_release=v2.4.8" in github_output.read_text(encoding="utf-8")

    monkeypatch.setattr(
        provider_compatibility,
        "discover_latest",
        lambda *_args, **_kwargs: {
            "kometa_latest": "v2.4.8",
            "kometa_changed": "false",
            "plexapi_latest": "4.18.2",
            "plexapi_changed": "false",
        },
    )
    assert provider_compatibility.main([*common, "discover"]) == 0
    assert '"kometa_changed": "false"' in capsys.readouterr().out

    discovery_output = tmp_path / "discovery-output"
    assert (
        provider_compatibility.main(
            [*common, "discover", "--github-output", str(discovery_output)]
        )
        == 0
    )
    assert "plexapi_changed=false" in discovery_output.read_text(encoding="utf-8")


def test_cli_schema_and_update_commands_and_normalized_errors(tmp_path, monkeypatch, capsys):
    old_schema = tmp_path / "old.json"
    new_schema = tmp_path / "new.json"
    old_schema.write_text('{"type":"object"}', encoding="utf-8")
    new_schema.write_text('{"type":"string"}', encoding="utf-8")
    report = tmp_path / "schema-report.md"

    assert (
        provider_compatibility.main(
            [
                "schema-diff",
                "--old-schema",
                str(old_schema),
                "--new-schema",
                str(new_schema),
                "--output",
                str(report),
            ]
        )
        == 0
    )
    assert "Constraint values changed: **1**" in report.read_text(encoding="utf-8")
    assert (
        provider_compatibility.main(
            [
                "schema-diff",
                "--old-schema",
                str(old_schema),
                "--new-schema",
                str(new_schema),
            ]
        )
        == 0
    )
    assert "Kometa metadata schema comparison" in capsys.readouterr().out

    monkeypatch.setattr(provider_compatibility, "update_kometa_contract", lambda *_a, **_k: True)
    assert (
        provider_compatibility.main(
            [
                "update-kometa",
                "--release",
                "v2.5.0",
                "--digest",
                "sha256:" + ("c" * 64),
                "--old-schema",
                str(old_schema),
                "--new-schema",
                str(new_schema),
                "--report",
                str(report),
            ]
        )
        == 0
    )
    assert "kometa_contract_changed=true" in capsys.readouterr().out

    invalid_manifest = tmp_path / "invalid.json"
    invalid_manifest.write_text("{}", encoding="utf-8")
    assert (
        provider_compatibility.main(
            ["--manifest", str(invalid_manifest), "verify"]
        )
        == 1
    )
    assert "Provider compatibility error" in capsys.readouterr().err


def test_provider_workflows_are_non_publishing_and_blocked_by_default():
    update_workflow = (
        REPO_ROOT / ".github" / "workflows" / "provider-compatibility.yml"
    ).read_text(encoding="utf-8")
    release_workflow = (
        REPO_ROOT / ".github" / "workflows" / "docker-latest.yml"
    ).read_text(encoding="utf-8")

    assert 'cron: "23 5 * * 1"' in update_workflow
    assert "ref: develop" in update_workflow
    assert "releases/latest" not in update_workflow
    assert "provider_compatibility.py discover" in update_workflow
    assert "--draft" in update_workflow
    assert "gh pr merge" not in update_workflow
    assert "--auto" not in update_workflow
    assert "gh workflow run docker-latest.yml" in update_workflow
    assert "plex-contract:" in release_workflow
    assert "tests/golden/kometa_contract.yml" in release_workflow
    assert "provider-contracts.json" not in release_workflow
    assert "provider_compatibility.py outputs" in release_workflow
    assert "workflow_dispatch:" in release_workflow
    assert "plex-contract" in release_workflow.split("needs:", 1)[1]
