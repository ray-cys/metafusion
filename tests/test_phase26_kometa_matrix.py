from pathlib import Path

import pytest

from modules import kometa


def test_generated_metadata_validation_and_order_edge_cases():
    assert kometa._numeric_key("bad") is None
    assert kometa._match_first([]) == []
    assert kometa.normalize_metadata_order([]) == 0
    with pytest.raises(kometa.KometaSchemaError, match="unsupported generated"):
        kometa.validate_generated_metadata({}, "artist")
    with pytest.raises(kometa.KometaSchemaError, match="must be a mapping"):
        kometa.validate_generated_metadata([], "movie")
    with pytest.raises(kometa.KometaSchemaError, match="unsupported generated movie"):
        kometa.validate_generated_metadata({"runtime": 90}, "movie")

    show = {
        "seasons": {
            1: {
                "episodes": {
                    1: {"title": "Pilot", "writer": ["Writer"]},
                }
            }
        }
    }
    assert kometa.validate_generated_metadata(show, "show")


def test_merge_generated_metadata_preserves_manual_and_reconciles_inventory():
    existing = {
        "summary": "manual summary",
        "genre": ["Old"],
        "country": ["US"],
        "cast": ["Deprecated"],
        "seasons": {
            "bad": "manual",
            "1": {
                "runtime": 40,
                "episodes": {
                    "1": {"title": "Old", "runtime": 42},
                    "2": {"title": "Removed"},
                },
            },
            "2": {"title": "Removed season"},
        },
    }
    generated = {
        "match": {"mapping_id": 100},
        "summary": "",
        "genre.sync": ["Drama"],
        "seasons": {
            1: {
                "title": "Season One",
                "episodes": {
                    1: {"title": "Pilot", "summary": ""},
                    3: {"title": "New"},
                },
            }
        },
    }
    merged, diagnostics = kometa.merge_generated_metadata(
        existing,
        generated,
        "show",
        authoritative_seasons={1},
        authoritative_episodes={"1": [1, 3]},
    )
    assert next(iter(merged)) == "match"
    assert merged["summary"] == "manual summary"
    assert "genre" not in merged and merged["genre.sync"] == ["Drama"]
    assert "cast" not in merged
    assert 2 not in merged["seasons"] and "2" not in merged["seasons"]
    assert set(merged["seasons"][1]["episodes"]) == {1, 3}
    assert diagnostics["inventory_removed"] >= 2
    assert diagnostics["existing_preserved"] >= 1
    assert diagnostics["deprecated_removed"] >= 2

    movie, _ = kometa.merge_generated_metadata({}, {"summary": "ok"}, "movie")
    assert movie == {"summary": "ok"}


def test_merge_handles_non_mapping_seasons_episodes_and_manual_keys():
    generated = {
        "seasons": {
            0: {"title": "Specials", "episodes": {1: {"title": "Special"}}}
        }
    }
    merged, _diagnostics = kometa.merge_generated_metadata(
        {"seasons": []}, generated, "show", authoritative_episodes={0: [1]}
    )
    assert merged["seasons"][0]["episodes"][1]["title"] == "Special"
    merged, _ = kometa.merge_generated_metadata(
        {"seasons": {0: {"episodes": []}}}, generated, "show"
    )
    assert merged["seasons"][0]["episodes"][1]["title"] == "Special"


def test_remove_deprecated_nested_fields_and_episode_basic_mode():
    document = {
        "metadata": {
            "ignored": "bad",
            "Show": {
                "cast": [],
                "seasons": {
                    0: "bad",
                    1: {
                        "runtime": 42,
                        "episodes": {1: "bad", 2: {"cast.sync": []}},
                    },
                },
            },
        }
    }
    assert kometa.remove_deprecated_metadata_fields(document, "shows") == 3
    episode = kometa.build_episode_metadata({"name": "Pilot"}, enhanced=False)
    assert "director" not in episode and episode["title"] == "Pilot"


@pytest.mark.parametrize(
    "document,message",
    [
        ([], "root"),
        ({}, "metadata mapping"),
        ({"metadata": {1: {}}}, "item names"),
        ({"metadata": {"Movie": []}}, "must be a mapping"),
        ({"metadata": {"Movie": {"match": []}}}, "match"),
        ({"metadata": {"Movie": {"match": {"year": 2020}}}}, "supported matching"),
        ({"metadata": {"Movie": {"runtime": 90}}}, "unsupported movie"),
        ({"metadata": {"Show": {"seasons": []}}}, "seasons"),
        ({"metadata": {"Show": {"seasons": {"one": {}}}}}, "season key"),
        ({"metadata": {"Show": {"seasons": {1: []}}}}, "season 1"),
        ({"metadata": {"Show": {"seasons": {1: {"runtime": 1}}}}}, "unsupported season"),
        ({"metadata": {"Show": {"seasons": {1: {"episodes": []}}}}}, "episodes"),
        ({"metadata": {"Show": {"seasons": {1: {"episodes": {"one": {}}}}}}}, "numeric mapping"),
        ({"metadata": {"Show": {"seasons": {1: {"episodes": {1: {"runtime": 1}}}}}}}, "unsupported episode"),
    ],
)
def test_document_validation_rejects_each_contract_violation(document, message):
    with pytest.raises(kometa.KometaSchemaError, match=message):
        kometa.validate_metadata_document(document, library_type="show" if "Show" in str(document) else "movie")


def test_write_metadata_snapshot_backup_and_rollback_paths(monkeypatch, tmp_path):
    path = tmp_path / "metadata.yml"
    path.write_text("metadata:\n  Old: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="changed while"):
        kometa.write_kometa_metadata(
            path,
            {"metadata": {"New": {}}},
            expected_snapshot=(True, "wrong"),
        )

    original = path.read_text(encoding="utf-8")

    def corrupt_write(target, _document):
        Path(target).write_text("metadata: []\n", encoding="utf-8")

    monkeypatch.setattr(kometa, "atomic_write_yaml", corrupt_write)
    with pytest.raises(kometa.KometaSchemaError):
        kometa.write_kometa_metadata(path, {"metadata": {"New": {}}}, backup_count=1)
    assert path.read_text(encoding="utf-8") == original

    missing = tmp_path / "missing.yml"
    with pytest.raises(kometa.KometaSchemaError):
        kometa.write_kometa_metadata(missing, {"metadata": {"New": {}}})
    assert not missing.exists()

    backup_dir = tmp_path / ".metafusion-backups"
    backup_dir.mkdir(exist_ok=True)
    for index in range(3):
        (backup_dir / f"metadata.yml.{index}.bak").write_text("x", encoding="utf-8")
    kometa._prune_backups(backup_dir, "metadata.yml", 1)
    assert len(list(backup_dir.glob("metadata.yml.*.bak"))) == 1
