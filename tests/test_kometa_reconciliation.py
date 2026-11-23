import json

import pytest
import yaml

from helper import kometa_reconciliation as module


def setup(tmp_path, kind="movie"):
    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "_kometa_reconciliation_library": "Library",
        "_kometa_reconciliation_scope": "server|library",
    }
    path = tmp_path / "metadata" / f"{kind}_metadata.yml"
    path.parent.mkdir(exist_ok=True)
    entry = {"match": {"mapping_id": 1}, "title": "Owned", "summary": "manual addition retained"}
    manual = {"match": {"mapping_id": 9}, "summary": "manual"}
    path.write_text(yaml.safe_dump({"metadata": {"Owned": entry, "Manual": manual}}))
    module.record_generated_definition(config, kind, "Owned", entry)
    records = [
        {
            "library_name": "Library",
            "scope": "server|library",
            "media_type": kind,
            "tmdb_id": "1",
            "tvdb_id": "1",
            "imdb_id": "tt1",
        }
    ]
    return config, path, entry, records


def read(path):
    return yaml.safe_load(path.read_text())["metadata"]


def test_ownership_defer_restore_and_manual_preservation(tmp_path):
    config, path, entry, records = setup(tmp_path)
    assert module.reconcile_kometa_metadata(config, records, complete=True)["deferred"] == 0
    config["_kometa_generated_definitions"] = {}
    records[0]["tmdb_id"] = "2"
    result = module.reconcile_kometa_metadata(config, records, complete=True)
    assert result["deferred"] == 1 and read(path) == {
        "Manual": {"match": {"mapping_id": 9}, "summary": "manual"}
    }
    records[0]["tmdb_id"] = "1"
    result = module.reconcile_kometa_metadata(config, records, complete=True)
    assert result["restored"] == 1 and read(path)["Owned"] == entry
    assert module.reconcile_kometa_metadata(config, records, complete=True)["restored"] == 0


def test_unknown_scope_identity_and_modified_claim_preserved(tmp_path):
    config, path, _, records = setup(tmp_path)
    module.reconcile_kometa_metadata(config, records, complete=True)
    config["_kometa_generated_definitions"] = {}
    records[0]["library_name"] = "Other"
    records[0]["scope"] = "different-server|library"
    assert module.reconcile_kometa_metadata(config, records, complete=True)["protected"] == 1
    d = read(path)
    d["Owned"]["match"]["mapping_id"] = 3
    path.write_text(yaml.safe_dump({"metadata": d}))
    assert module.reconcile_kometa_metadata(config, records, complete=True)["protected"] == 1


def test_partial_dry_run_and_plex_have_no_writes(tmp_path):
    config, path, _, records = setup(tmp_path)
    original = path.read_bytes()
    assert module.reconcile_kometa_metadata(config, records, complete=False)["status"] == "deferred"
    config["settings"]["dry_run"] = True
    assert module.reconcile_kometa_metadata(config, records, complete=True)["status"] == "deferred"
    config["settings"]["mode"] = "plex"
    module.record_generated_definition(config, "movie", "ignored", {})
    assert (
        module.reconcile_kometa_metadata(config, records, complete=True)["status"]
        == "not_applicable"
    )
    assert path.read_bytes() == original and not (tmp_path / ".metafusion").exists()


def test_legacy_import_durable_interruption_and_restore(tmp_path, monkeypatch):
    config, path, _, records = setup(tmp_path)
    legacy = tmp_path / ".preflight/movie_metadata.yml.deferred.yml"
    legacy.parent.mkdir()
    legacy.write_text(
        yaml.safe_dump({"Legacy": {"match": {"mapping_id": 2}, "summary": "retained"}})
    )
    module.reconcile_kometa_metadata(config, records, complete=True)
    records[0]["tmdb_id"] = "2"
    writer = module.write_kometa_metadata
    monkeypatch.setattr(
        module,
        "write_kometa_metadata",
        lambda *a, **k: (_ for _ in ()).throw(OSError("interrupted")),
    )
    with pytest.raises(OSError):
        module.reconcile_kometa_metadata(config, records, complete=True)
    state = json.loads((tmp_path / ".metafusion/metadata-reconciliation.json").read_text())
    assert state["deferred"]["movie"]["Owned"]["summary"]
    monkeypatch.setattr(module, "write_kometa_metadata", writer)
    result = module.reconcile_kometa_metadata(config, records, complete=True)
    assert result["restored"] == 1 and "Legacy" in read(path)
    assert legacy.exists()


def test_corrupt_state_and_legacy_fail_closed(tmp_path):
    config, _path, _, records = setup(tmp_path)
    state = tmp_path / ".metafusion/metadata-reconciliation.json"
    state.parent.mkdir()
    state.write_text('{"schema":2}')
    with pytest.raises(ValueError):
        module.reconcile_kometa_metadata(config, records, complete=True)
    state.unlink()
    legacy = tmp_path / ".preflight/movie_metadata.yml.deferred.yml"
    legacy.parent.mkdir()
    legacy.write_text("- invalid")
    with pytest.raises(TypeError):
        module.reconcile_kometa_metadata(config, records, complete=True)


@pytest.mark.parametrize(
    ("match", "kind", "records", "expected"),
    [
        ({}, "tv", [], None),
        ({"mapping_id": "tt1"}, "tv", [{"imdb_id": "tt1"}], True),
        ({"mapping_id": 1}, "movie", [{}], None),
        ({"mapping_id": 1}, "tv", [{"tvdb_id": "2"}], False),
        (
            {"mapping_id": 1, "edition": "Extended"},
            "movie",
            [{"tmdb_id": "1", "edition": "Other"}],
            False,
        ),
        (
            {"mapping_id": 1, "blank_edition": True},
            "movie",
            [{"tmdb_id": "1", "edition": None}],
            True,
        ),
    ],
)
def test_exact_provider_and_edition_matching(match, kind, records, expected):
    assert module._present(match, kind, records) is expected


def test_fresh_alias_wins_legacy_and_unknown_identity_is_preserved(tmp_path):
    config, path, _, records = setup(tmp_path)
    legacy = tmp_path / ".preflight/movie_metadata.yml.deferred.yml"
    legacy.parent.mkdir()
    legacy.write_text(
        yaml.safe_dump({"Old Alias": {"match": {"mapping_id": 1, "title": "Old Name"}}})
    )
    result = module.reconcile_kometa_metadata(config, records, complete=True)
    assert result["restored"] == 0 and "Old Alias" not in read(path)
    assert module._present({"mapping_id": 1}, "movie", []) is None
    # A malformed/generated document never authorizes deletion of a manual file.
    path.unlink()
    assert module.reconcile_kometa_metadata(config, records, complete=True)["status"] == "ok"


def test_permission_denied_retries_without_blocking_native_reconciliation(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    config, path, _, records = setup(tmp_path)
    legacy = tmp_path / ".preflight/movie_metadata.yml.deferred.yml"
    legacy.parent.mkdir()
    legacy.write_text("Legacy:\n  match:\n    mapping_id: 2\n")
    state = tmp_path / ".metafusion/metadata-reconciliation.json"
    records[0]["tmdb_id"] = "2"
    original = Path.read_bytes

    def denied(target):
        if target == legacy:
            raise PermissionError(13, "Permission denied", str(target))
        return original(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        result = module.reconcile_kometa_metadata(config, records, complete=True)
    assert result["deferred"] == 1 and "Owned" not in read(path)
    assert "movie" not in json.loads(state.read_text())["legacy_imports"]
    assert "Legacy import pending" in caplog.text and str(legacy) in caplog.text
    assert module.reconcile_kometa_metadata(config, records, complete=True)["restored"] == 1
    assert "Legacy" in read(path)

    def forbid_legacy(operation):
        def checked(target, *args, **kwargs):
            assert legacy.parent not in target.parents, "Completed import accessed .preflight"
            return operation(target, *args, **kwargs)
        return checked

    for method in ("stat", "open"):
        monkeypatch.setattr(Path, method, forbid_legacy(getattr(Path, method)))
    assert module.reconcile_kometa_metadata(config, records, complete=True)["imported"] == 0
