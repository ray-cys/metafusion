from pathlib import Path

import pytest
import yaml

from helper.io import sha256_file
from modules import kometa
from modules.kometa import (
    KometaSchemaError,
    validate_metadata_document,
    write_kometa_metadata,
)


def document(summary="Original"):
    return {
        "metadata": {
            "Example (2020)": {
                "match": {"title": "Example", "year": 2020, "mapping_id": 123},
                "summary": summary,
                "seasons": {
                    0: {
                        "episodes": {
                            1: {
                                "title": "Special",
                                "originally_available": "2020-01-01",
                            }
                        }
                    }
                },
            }
        }
    }


def test_kometa_contract_accepts_specials_and_rejects_corrupt_shapes():
    assert validate_metadata_document(document()) is True

    with pytest.raises(KometaSchemaError, match="metadata mapping"):
        validate_metadata_document({"metadata": []})
    with pytest.raises(KometaSchemaError, match="season key"):
        invalid = document()
        invalid["metadata"]["Example (2020)"]["seasons"] = {"specials": {}}
        validate_metadata_document(invalid)
    with pytest.raises(KometaSchemaError, match="episode"):
        invalid = document()
        invalid["metadata"]["Example (2020)"]["seasons"][0]["episodes"][1] = []
        validate_metadata_document(invalid)


def test_validated_output_keeps_rotating_known_good_backups(tmp_path):
    path = tmp_path / "metadata" / "movie_metadata.yml"
    write_kometa_metadata(path, document("One"), backup_count=2)
    write_kometa_metadata(path, document("Two"), backup_count=2)
    write_kometa_metadata(path, document("Three"), backup_count=2)
    write_kometa_metadata(path, document("Four"), backup_count=2)

    written = yaml.safe_load(path.read_text(encoding="utf-8"))
    backups = list((path.parent / ".metafusion-backups").glob("*.bak"))
    assert written["metadata"]["Example (2020)"]["summary"] == "Four"
    assert len(backups) == 2
    assert all(validate_metadata_document(yaml.safe_load(item.read_text())) for item in backups)


def test_failed_post_write_validation_restores_previous_output(monkeypatch, tmp_path):
    path = tmp_path / "movie_metadata.yml"
    original = document("Known good")
    path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    def corrupt_write(output_path, _document):
        Path(output_path).write_text("metadata: []\n", encoding="utf-8")

    monkeypatch.setattr(kometa, "atomic_write_yaml", corrupt_write)
    with pytest.raises(KometaSchemaError):
        write_kometa_metadata(path, document("New"), backup_count=1)

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original

def test_invalid_document_never_replaces_existing_output(tmp_path):
    path = tmp_path / "movie_metadata.yml"
    original = document()
    path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    with pytest.raises(KometaSchemaError):
        write_kometa_metadata(path, {"metadata": []})

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original


def test_type_validation_rejects_deprecated_metafusion_fields():
    invalid = document()
    invalid["metadata"]["Example (2020)"]["cast.sync"] = ["Actor"]

    with pytest.raises(KometaSchemaError, match="cast.sync"):
        validate_metadata_document(invalid, library_type="movie")


def test_structural_read_accepts_legacy_fields_before_phase12_sanitizes_them():
    legacy = document()
    legacy["metadata"]["Example (2020)"]["seasons"][0][
        "originally_available"
    ] = "2020-01-01"

    assert validate_metadata_document(legacy) is True
    with pytest.raises(KometaSchemaError, match="originally_available"):
        validate_metadata_document(legacy, library_type="show")


def test_output_refuses_to_overwrite_external_change(tmp_path):
    path = tmp_path / "movie_metadata.yml"
    path.write_text(yaml.safe_dump(document("One")), encoding="utf-8")
    snapshot = (True, sha256_file(path))
    path.write_text(yaml.safe_dump(document("External")), encoding="utf-8")

    with pytest.raises(RuntimeError, match="changed while MetaFusion"):
        write_kometa_metadata(
            path,
            document("MetaFusion"),
            library_type="movie",
            expected_snapshot=snapshot,
        )

    assert yaml.safe_load(path.read_text(encoding="utf-8")) == document("External")
