import io
import json
import sqlite3
import tarfile

import pytest

from helper import config_impact, recovery, state_reporting


def test_config_impact_classifies_every_configuration_family(monkeypatch):
    monkeypatch.setattr(config_impact, "find_media_state", lambda: [{}, {}])
    current = {
        "settings": {"mode": "kometa", "path": "/old"},
        "plex_libraries": ["Movies"],
        "assets": {"update_policy": "managed", "run_poster": False},
        "metadata": {"run_basic": False},
        "poster_set": {"min_width": 1000},
        "incremental": {"days": 7},
        "cleanup": {"grace_hours": 48},
        "plex": {"token": "old"},
        "other": "old",
        "_private": "ignored",
    }
    proposed = {
        "settings": {"mode": "plex", "path": "/new"},
        "plex_libraries": ["Shows"],
        "assets": {"update_policy": "overwrite", "run_poster": True},
        "metadata": {"run_basic": True},
        "poster_set": {"min_width": 1500},
        "incremental": {"days": 1},
        "cleanup": {"grace_hours": 0},
        "plex": {"token": "new"},
        "other": "new",
        "_private": "still ignored",
    }
    result = config_impact.compare_configurations(current, proposed)
    changes = {entry["path"]: entry for entry in result["changes"]}
    assert changes["settings.mode"]["severity"] == "high"
    assert changes["plex_libraries"]["severity"] == "high"
    assert changes["assets.update_policy"]["severity"] == "high"
    assert changes["assets.run_poster"]["severity"] == "medium"
    assert changes["metadata.run_basic"]["severity"] == "medium"
    assert changes["poster_set.min_width"]["severity"] == "medium"
    assert changes["incremental.days"]["severity"] == "medium"
    assert changes["cleanup.grace_hours"]["severity"] == "high"
    assert changes["plex.token"]["before"] == "<redacted>"
    assert changes["other"]["severity"] == "information"
    assert result["summary"]["recorded_items_potentially_reselected"] == 2
    assert "_private" not in changes


def test_state_report_sections_and_record_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(
        state_reporting,
        "_database_counts",
        lambda _path: (
            {"media_state": 1},
            [
                {
                    "server_id": "server",
                    "library_uuid": "uuid",
                    "last_full_scan_completed": "full",
                    "last_successful_incremental": "incremental",
                }
            ],
            [
                {
                    "server_id": "server",
                    "library_uuid": "uuid",
                    "library_name": "Movies",
                    "library_type": "movie",
                    "active": 1,
                    "last_seen": "now",
                }
            ],
        ),
    )
    item = {
        "cache_key": "movie:plex:1",
        "library_name": "Movies",
        "media_type": "movie",
        "title": "Example",
        "year": 2020,
        "rating_key": "1",
        "tmdb_id": "100",
        "imdb_id": "tt100",
        "tvdb_id": "200",
    }
    monkeypatch.setattr(state_reporting, "find_media_state", lambda **_kwargs: [item])
    monkeypatch.setattr(
        state_reporting,
        "load_asset_ownership",
        lambda *_args, **_kwargs: [
            {"asset_type": "poster"},
            {"asset_type": "background"},
            {"asset_type": "season"},
        ],
    )
    monkeypatch.setattr(
        state_reporting, "load_item_exceptions", lambda **_kwargs: [{"id": 1}]
    )
    monkeypatch.setattr(
        state_reporting,
        "load_identity_overrides",
        lambda **_kwargs: [{"active": 1}, {"active": 0}],
    )
    monkeypatch.setattr(
        state_reporting,
        "load_identity_reviews",
        lambda **_kwargs: [{"status": "open"}],
    )
    monkeypatch.setattr(
        state_reporting,
        "load_item_retries",
        lambda **_kwargs: [{"status": "due"}],
    )
    monkeypatch.setattr(
        state_reporting,
        "load_cleanup_candidates",
        lambda **_kwargs: [{"status": "pending"}],
    )
    monkeypatch.setattr(
        state_reporting,
        "load_cleanup_history",
        lambda **_kwargs: [{"source": "automatic"}],
    )
    monkeypatch.setattr(
        state_reporting,
        "load_library_rebinding_history",
        lambda **_kwargs: [{"status": "applied"}],
    )
    monkeypatch.setattr(
        state_reporting,
        "recent_job_runs",
        lambda **_kwargs: [
            {"status": "failed", "finished_at": "now", "mode": "full", "error": "boom"}
        ],
    )
    monkeypatch.setattr(
        state_reporting,
        "inspect_database",
        lambda *_args: {
            "status": "ok",
            "schema": 1,
            "bytes": 2048,
            "wal_bytes": 1024,
        },
    )
    monkeypatch.setattr(
        state_reporting, "DATABASES", {"state": (tmp_path / "state.db", 1)}
    )
    monkeypatch.setattr(
        state_reporting,
        "build_info",
        lambda: {"version": "test", "commit": "abc"},
    )
    report = state_reporting.write_state_report(
        libraries=["Movies"],
        include_items=True,
        base_dir=tmp_path,
        path=tmp_path / "state.db",
    )
    text = report.read_text(encoding="utf-8")
    assert "Recorded libraries" in text
    assert "error=boom" in text
    assert "poster claims: 1" in text
    assert "TMDb=100 | IMDb=tt100 | TVDB=200" in text
    assert state_reporting._format_bytes(0) == "0 B"
    assert state_reporting._format_bytes(1024**4) == "1.00 TiB"

    empty = state_reporting.write_identity_review_report([], base_dir=tmp_path)
    assert "- none" in empty.read_text(encoding="utf-8")
    review = state_reporting.write_identity_review_report(
        [
            {
                "status": "open",
                "library_name": "Movies",
                "title": "Example",
                "rating_key": "1",
                "proposed_tmdb_id": "100",
                "category": "identity",
                "reason": "review",
            }
        ],
        base_dir=tmp_path,
    )
    assert "proposed TMDb=100" in review.read_text(encoding="utf-8")
    rebinding = state_reporting.write_rebinding_report(
        [
            {
                "status": "ready",
                "title": "Example",
                "year": 2020,
                "tmdb_id": "100",
                "source": {"library_name": "Old", "rating_key": "1"},
                "destination": {"library_name": "New", "rating_key": "2"},
                "reason": "unique",
            }
        ],
        applied=True,
        base_dir=tmp_path,
    )
    assert "Mode: applied" in rebinding.read_text(encoding="utf-8")

    cleanup = state_reporting.write_cleanup_history_report(
        libraries=["Movies"],
        base_dir=tmp_path,
        path=tmp_path / "state.db",
    )
    cleanup_text = cleanup.read_text(encoding="utf-8")
    assert "automatic=1" in cleanup_text
    assert "confirmations=None" in cleanup_text


def test_state_reporting_handles_absent_database_and_empty_item_selection(tmp_path):
    assert state_reporting._readonly_connection(tmp_path / "missing.sqlite3") is None
    assert state_reporting._database_counts(tmp_path / "missing.sqlite3") == ({}, [], [])


def _write_archive(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = content.decode()
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))


def test_recovery_verification_rejects_missing_unsafe_and_invalid_archives(tmp_path):
    with pytest.raises(recovery.RecoveryBundleError, match="does not exist"):
        recovery.verify_recovery_bundle(tmp_path / "missing.tar.gz")

    unsafe = tmp_path / "unsafe.tar.gz"
    _write_archive(unsafe, [("../escape", b"x", "file")])
    with pytest.raises(recovery.RecoveryBundleError, match="unsafe path"):
        recovery.verify_recovery_bundle(unsafe)

    linked = tmp_path / "linked.tar.gz"
    _write_archive(linked, [("root/link", b"target", "symlink")])
    with pytest.raises(recovery.RecoveryBundleError, match="contains a link"):
        recovery.verify_recovery_bundle(linked)

    roots = tmp_path / "roots.tar.gz"
    _write_archive(
        roots,
        [("one/file", b"x", "file"), ("two/file", b"y", "file")],
    )
    with pytest.raises(recovery.RecoveryBundleError, match="root layout"):
        recovery.verify_recovery_bundle(roots)

    invalid = tmp_path / "invalid.tar.gz"
    _write_archive(invalid, [("root/manifest.json", b"not-json", "file")])
    with pytest.raises(recovery.RecoveryBundleError, match="manifest"):
        recovery.verify_recovery_bundle(invalid)


def test_recovery_verification_reports_missing_and_tampered_files(tmp_path):
    bundle = tmp_path / "bundle.tar.gz"
    manifest = {
        "files": {
            "missing.txt": {"sha256": "none"},
            "changed.txt": {"sha256": "wrong"},
        },
        "contents": {"durable_state": "missing.db"},
    }
    _write_archive(
        bundle,
        [
            ("root/manifest.json", json.dumps(manifest).encode(), "file"),
            ("root/changed.txt", b"changed", "file"),
        ],
    )
    result = recovery.verify_recovery_bundle(bundle)
    assert result["valid"] is False
    assert result["failures"] == [
        "missing missing.txt",
        "checksum mismatch changed.txt",
        "durable state database missing",
    ]


def test_online_backup_rejects_failed_integrity(monkeypatch, tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE example(value TEXT)")

    class _Connection:
        def backup(self, _destination):
            return None

        def execute(self, _query):
            return SimpleRow("corrupt")

        def close(self):
            return None

    class SimpleRow:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    connections = [_Connection(), _Connection()]
    monkeypatch.setattr(recovery.sqlite3, "connect", lambda *_args, **_kwargs: connections.pop(0))
    with pytest.raises(recovery.RecoveryBundleError, match="quick_check"):
        recovery._online_backup(source, tmp_path / "copy.db")


def test_recovery_rejects_missing_state_and_strips_private_config(tmp_path):
    assert "_runtime" not in recovery._redacted_config({"_runtime": True, "settings": {}})
    with pytest.raises(recovery.RecoveryBundleError, match="does not exist"):
        recovery.create_recovery_bundle(
            {"settings": {}},
            base_dir=tmp_path,
            state_path=tmp_path / "missing.sqlite3",
        )
