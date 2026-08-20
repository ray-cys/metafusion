import asyncio
import hashlib
import json
import sqlite3
import sys
import tarfile
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest

from helper import (
    database_maintenance,
    identity_diagnostics,
    item_explanation,
    mapping_diagnostics,
    plex_artwork_verification,
    recovery,
)
from helper import io as io_helper
from modules import kometa


def test_identity_tvdb_binding_rejected_and_medium_paths(monkeypatch, tmp_path):
    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "runtime": {},
        "plex": {},
        "tmdb": {},
    }

    async def tvdb_resolver(*_args, **_kwargs):
        return "22"

    monkeypatch.setattr(identity_diagnostics, "resolve_tmdb_id", tvdb_resolver)
    assert asyncio.run(
        identity_diagnostics._resolve_without_binding(config, "tv", {"tvdb_id": "11"}, None)
    ) == ("22", "tvdb_external_id")

    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Shows",
        "library_type": "tv",
        "ratingKey": "1",
        "title": "Show",
        "year": 2026,
    }

    async def get_meta(*_args, **_kwargs):
        return dict(meta)

    async def details(_config, _media_type, tmdb_id, _session):
        if not tmdb_id:
            return None
        return {
            "id": 44,
            "name": "Show",
            "original_name": "Show",
            "first_air_date": "2026-01-01",
        }

    monkeypatch.setattr(identity_diagnostics, "get_plex_metadata", get_meta)
    monkeypatch.setattr(identity_diagnostics, "_fetch_tmdb_details", details)
    monkeypatch.setattr(
        identity_diagnostics,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (True, False, "consistent"),
    )
    monkeypatch.setattr(
        identity_diagnostics,
        "tmdb_identity_consistent",
        lambda *_args, **_kwargs: (True, "consistent"),
    )
    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {
            "status": "current",
            "active": {"confidence": "high", "tmdb_id": "44"},
        },
    )
    learned = asyncio.run(identity_diagnostics.diagnose_identity(SimpleNamespace(), config))
    assert learned["selection"]["source"] == "learned_binding"
    assert learned["selection"]["confidence"] == "high"

    monkeypatch.setattr(
        identity_diagnostics,
        "inspect_identity_binding",
        lambda *_args, **_kwargs: {"status": "missing", "active": {}},
    )

    async def title_resolver(*_args, **_kwargs):
        return "44", "title_year_search"

    monkeypatch.setattr(identity_diagnostics, "_resolve_without_binding", title_resolver)
    medium = asyncio.run(identity_diagnostics.diagnose_identity(SimpleNamespace(), config))
    assert medium["selection"]["confidence"] == "medium"

    calls = iter([("44", "title_year_search"), (None, "unresolved")])

    async def rejected_resolver(*_args, **_kwargs):
        return next(calls)

    monkeypatch.setattr(identity_diagnostics, "_resolve_without_binding", rejected_resolver)
    monkeypatch.setattr(
        identity_diagnostics,
        "tmdb_external_id_consensus",
        lambda *_args, **_kwargs: (False, False, "mismatch"),
    )
    rejected = asyncio.run(identity_diagnostics.diagnose_identity(SimpleNamespace(), config))
    assert rejected["selection"]["confidence"] == "rejected"


def test_read_only_runners_skip_unrequested_inventory(monkeypatch, tmp_path):
    sections = [SimpleNamespace(title="Movies"), SimpleNamespace(title="Other")]
    item = SimpleNamespace(ratingKey="other")

    async def identity_inventory(_section, _runtime, records_only=False):
        if records_only:
            if _section.title == "Other":
                return [
                    {
                        "rating_key": "other",
                        "media_type": "movie",
                        "title": "Other",
                        "year": 2026,
                    }
                ]
            return [
                {
                    "rating_key": "wanted",
                    "media_type": "movie",
                    "title": "Movie",
                    "year": 2026,
                }
            ]
        return [item]

    monkeypatch.setattr(
        identity_diagnostics,
        "load_plex_library_inventory",
        identity_inventory,
    )
    results, _report = asyncio.run(
        identity_diagnostics.run_identity_inspection(
            sections, {"runtime": {}, "output": {}}, ["wanted"], base_dir=tmp_path
        )
    )
    assert results[0]["status"] == "not_found"

    monkeypatch.setattr(item_explanation, "load_plex_library_inventory", identity_inventory)
    results, report = asyncio.run(
        item_explanation.run_item_explanation(
            sections,
            {"runtime": {}, "output": {}},
            ["wanted"],
            base_dir=tmp_path,
        )
    )
    assert results[0]["status"] == "not_found" and report.exists()

    async def no_requested(*_args, **_kwargs):
        return [item]

    monkeypatch.setattr(mapping_diagnostics, "load_plex_library_inventory", no_requested)
    mapped, _report = asyncio.run(
        mapping_diagnostics.run_mapping_diagnosis(
            sections, {"runtime": {}, "output": {}}, ["wanted"], base_dir=tmp_path
        )
    )
    assert mapped[0]["status"] == "not_found"


def test_item_report_season_destination_and_empty_iterable(tmp_path):
    assert item_explanation._display(set()) == "none"
    path = item_explanation.write_item_explanation_report(
        [
            {
                "identity": {
                    "plex": {"localized_title": "Show", "year": 2026},
                    "artwork_destinations": {
                        "seasons": [{"season": 1, "path": "/redacted/Season01.jpg"}]
                    },
                },
                "selection": {},
                "policies": {},
                "episode_mapping": {},
            }
        ],
        base_dir=tmp_path,
    )
    assert "season 1" in path.read_text(encoding="utf-8")


def test_mapping_missing_inventory(monkeypatch):
    async def get_meta(*_args, **_kwargs):
        return {
            "library_name": "Shows",
            "library_type": "tv",
            "ratingKey": "1",
            "title": "Show",
            "year": 2026,
            "seasons_episodes": {},
        }

    async def resolve(*_args, **_kwargs):
        return "10"

    monkeypatch.setattr(mapping_diagnostics, "get_plex_metadata", get_meta)
    monkeypatch.setattr(mapping_diagnostics, "resolve_tmdb_id", resolve)
    result = asyncio.run(
        mapping_diagnostics.diagnose_mapping(SimpleNamespace(), {"runtime": {}, "plex": {}})
    )
    assert result["status"] == "missing_inventory"


def test_plex_artwork_verifier_skips_unrequested(monkeypatch):
    section = SimpleNamespace(
        title="Movies",
        uuid="library",
        _server=SimpleNamespace(machineIdentifier="server"),
    )

    async def inventory(*_args, **_kwargs):
        return [SimpleNamespace(ratingKey="other")]

    monkeypatch.setattr(plex_artwork_verification, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(plex_artwork_verification, "find_media_state", lambda **_kwargs: [])
    monkeypatch.setattr(plex_artwork_verification, "load_asset_ownership", lambda *_args: [])
    records = asyncio.run(
        plex_artwork_verification.verify_plex_artwork([section], {"runtime": {}}, ["wanted"], None)
    )
    assert records[0]["status"] == "not_found"


def test_atomic_writer_failure_cleanup_paths(monkeypatch, tmp_path):
    target = tmp_path / "data.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        io_helper.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError):
        io_helper.atomic_write_json(target, {"new": True}, backup=True)
    assert not list(tmp_path.glob("*.tmp"))

    monkeypatch.setattr(
        io_helper,
        "atomic_replace_file",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError):
        io_helper.atomic_write_text(tmp_path / "text.txt", "text")
    assert not list(tmp_path.glob("*.tmp"))


class FakeConnection:
    def __init__(self, integrity="ok"):
        self.integrity = integrity
        self.closed = False

    def backup(self, _destination):
        return None

    def execute(self, statement):
        if "quick_check" in statement:
            return SimpleNamespace(fetchone=lambda: (self.integrity,))
        return SimpleNamespace(fetchone=lambda: (0,))

    def commit(self):
        return None

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_database_maintenance_failure_and_format_paths(monkeypatch, tmp_path):
    database = tmp_path / "state.sqlite3"
    database.write_bytes(b"sqlite")
    source = FakeConnection()
    backup = FakeConnection("bad")
    connections = iter([source, backup])
    monkeypatch.setattr(
        database_maintenance.sqlite3, "connect", lambda *_args, **_kwargs: next(connections)
    )
    with pytest.raises(sqlite3.DatabaseError, match="backup quick_check"):
        database_maintenance._backup_database(database, tmp_path / "backups", 1)
    assert source.closed and backup.closed

    monkeypatch.setattr(
        database_maintenance,
        "selected_databases",
        lambda _target: {"state": (database, 1)},
    )
    monkeypatch.setattr(
        database_maintenance,
        "inspect_database",
        lambda *_args: {
            "healthy": True,
            "status": "ok",
            "schema": 1,
            "bytes": 1,
            "wal_bytes": 0,
        },
    )
    monkeypatch.setattr(
        database_maintenance.sqlite3,
        "connect",
        lambda *_args, **_kwargs: FakeConnection("bad"),
    )
    result = database_maintenance.maintain_databases("optimize", "state")
    assert "quick_check returned bad" in result[0]["status"]
    formatted = database_maintenance.format_maintenance_results(
        [{**result[0], "backup": "/backup/state.sqlite3"}]
    )
    assert "backup=/backup/state.sqlite3" in formatted


def _recovery_archive(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    database = root / "meta_db.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE state (value TEXT)")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest = {
        "files": {"meta_db.sqlite3": {"sha256": digest}},
        "contents": {"durable_state": "meta_db.sqlite3"},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(root, arcname=root.name)
    return archive_path


def test_recovery_python310_extraction_and_bad_integrity(monkeypatch, tmp_path):
    archive = _recovery_archive(tmp_path)
    warning = pytest.warns(DeprecationWarning) if sys.version_info >= (3, 12) else nullcontext()
    monkeypatch.setattr(recovery, "sys", SimpleNamespace(version_info=(3, 10)))
    with warning:
        assert recovery.verify_recovery_bundle(archive)["valid"] is True

    monkeypatch.setattr(recovery, "sys", SimpleNamespace(version_info=(3, 12)))
    monkeypatch.setattr(
        recovery.sqlite3,
        "connect",
        lambda *_args, **_kwargs: FakeConnection("bad"),
    )
    result = recovery.verify_recovery_bundle(archive)
    assert result["valid"] is False
    assert "SQLite quick_check returned bad" in result["failures"]


def test_kometa_existing_episode_pruning_and_empty_episode_section():
    merged, diagnostics = kometa.merge_generated_metadata(
        {"seasons": {1: {"episodes": {1: {"title": "one"}}}}},
        {"seasons": {1: {"episodes": {2: {"title": "two"}}}}},
        "show",
        authoritative_seasons={1},
        authoritative_episodes={1: [1]},
    )
    assert set(merged["seasons"][1]["episodes"]) == {1}
    assert diagnostics["inventory_removed"] == 0
    assert kometa.validate_metadata_document({"metadata": {"Show": {"seasons": {1: {}}}}}, "show")
