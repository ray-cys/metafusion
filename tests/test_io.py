import pytest
import yaml

from helper import io as io_module


def test_sha256_file_streams_a_stable_content_fingerprint(tmp_path):
    target = tmp_path / "asset.jpg"
    target.write_bytes(b"MetaFusion managed artwork")

    assert io_module.sha256_file(target) == (
        "7f7d50425c1a89cbd1861dce6756d91e14b1fdc3b468163292ce05ee7832150a"
    )


def test_atomic_yaml_write_replaces_complete_document(tmp_path):
    target = tmp_path / "metadata" / "movie_metadata.yml"
    data = {"metadata": {"Example (2020)": {"summary": "Complete"}}}

    io_module.atomic_write_yaml(target, data)

    assert yaml.safe_load(target.read_text(encoding="utf-8")) == data
    assert target.stat().st_mode & 0o777 == 0o664
    assert list(target.parent.glob("*.tmp")) == []


def test_atomic_yaml_write_preserves_existing_owner_group_and_mode(tmp_path):
    target = tmp_path / "movie_metadata.yml"
    target.write_text("metadata:\n  old: true\n", encoding="utf-8")
    target.chmod(0o640)
    before = target.stat()

    io_module.atomic_write_yaml(target, {"metadata": {"new": True}})

    after = target.stat()
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o777 == 0o640


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


def test_atomic_bytes_failure_preserves_previous_file(monkeypatch, tmp_path):
    target = tmp_path / "poster.jpg"
    target.write_bytes(b"old-image")

    def fail_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(io_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement failure"):
        io_module.atomic_write_bytes(target, b"new-image")

    assert target.read_bytes() == b"old-image"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_bytes_write_preserves_existing_asset_identity_and_mode(tmp_path):
    target = tmp_path / "poster.jpg"
    target.write_bytes(b"old-image")
    target.chmod(0o660)
    before = target.stat()

    io_module.atomic_write_bytes(target, b"new-image")

    after = target.stat()
    assert target.read_bytes() == b"new-image"
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o777 == 0o660


def test_json_read_recovers_from_backup_and_returns_default(tmp_path):
    target = tmp_path / "state.json"
    backup = io_module.backup_path_for(target)
    target.write_text("broken", encoding="utf-8")
    backup.write_text('{"recovered": true}', encoding="utf-8")

    assert io_module.read_json_with_backup(target) == {"recovered": True}
    target.unlink()
    backup.unlink()
    assert io_module.read_json_with_backup(target, default={"empty": True}) == {
        "empty": True
    }


def test_atomic_json_write_keeps_last_valid_backup(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"version": 1}\n', encoding="utf-8")

    io_module.atomic_write_json(target, {"version": 2}, backup=True)

    assert io_module.read_json_with_backup(target) == {"version": 2}
    assert io_module.read_json_with_backup(io_module.backup_path_for(target)) == {
        "version": 1
    }
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_does_not_backup_invalid_previous_document(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("broken", encoding="utf-8")

    io_module.atomic_write_json(target, {"valid": True}, backup=True)

    assert io_module.read_json_with_backup(target) == {"valid": True}
    assert not io_module.backup_path_for(target).exists()


def test_atomic_text_normalizes_trailing_newline(tmp_path):
    target = tmp_path / "reports" / "report.txt"
    io_module.atomic_write_text(target, "line")
    assert target.read_text(encoding="utf-8") == "line\n"

    io_module.atomic_write_text(target, "already\n")
    assert target.read_text(encoding="utf-8") == "already\n"


def test_directory_fsync_tolerates_open_and_sync_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(
        io_module.os,
        "open",
        lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
    )
    io_module._fsync_directory(tmp_path)

    closed = []
    monkeypatch.setattr(io_module.os, "open", lambda *_args: 99)
    monkeypatch.setattr(
        io_module.os,
        "fsync",
        lambda *_args: (_ for _ in ()).throw(OSError("unsupported")),
    )
    monkeypatch.setattr(io_module.os, "close", lambda value: closed.append(value))
    io_module._fsync_directory(tmp_path)
    assert closed == [99]
