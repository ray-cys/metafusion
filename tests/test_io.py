import pytest
import yaml

from helper import io as io_module


def test_atomic_yaml_write_replaces_complete_document(tmp_path):
    target = tmp_path / "metadata" / "movie_metadata.yml"
    data = {"metadata": {"Example (2020)": {"summary": "Complete"}}}

    io_module.atomic_write_yaml(target, data)

    assert yaml.safe_load(target.read_text(encoding="utf-8")) == data
    assert list(target.parent.glob("*.tmp")) == []


def test_atomic_yaml_failure_preserves_previous_file(monkeypatch, tmp_path):
    target = tmp_path / "movie_metadata.yml"
    target.write_text("metadata:\n  old: true\n", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(io_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failure"):
        io_module.atomic_write_yaml(target, {"metadata": {"new": True}})

    assert target.read_text(encoding="utf-8") == "metadata:\n  old: true\n"
    assert list(tmp_path.glob("*.tmp")) == []
