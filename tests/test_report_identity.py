import json
import sqlite3
from contextlib import closing

from helper import state_db
from helper.diagnostics import (
    record_kometa_metadata_audit,
    write_adoption_audit_report,
    write_artwork_gap_report,
    write_asset_audit_report,
    write_change_plan_report,
    write_destination_history_report,
    write_library_asset_audit_report,
    write_metadata_audit_report,
    write_unresolved_work_report,
)
from helper.identity_diagnostics import write_identity_inspection_report
from helper.item_explanation import write_item_explanation_report
from helper.mapping_diagnostics import write_mapping_diagnosis_report
from helper.plex_metadata import PlexMetadataReporter
from helper.provider_replay import write_sanitized_replay_capture
from helper.report_identity import IDENTITY_REPORT_FIELDS, item_report_record
from modules import builder

IDENTITY = {
    "ratingKey": 42,
    "tmdb_id": 100,
    "imdb_id": "tt0000100",
    "tvdb_id": 200,
    "edition_title": "Director's Cut",
    "identity_source": "plex_tmdb_guid",
}


def _json_data(report):
    return json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))["data"]


def _assert_identity(record, *, season_number=None):
    assert set(IDENTITY_REPORT_FIELDS) <= set(record)
    assert record["plex_rating_key"] == "42"
    assert record["tmdb_id"] == "100"
    assert record["imdb_id"] == "tt0000100"
    assert record["tvdb_id"] == "200"
    assert record["edition"] == "Director's Cut"
    assert record["season_number"] == season_number
    assert record["identity_source"] == "plex_tmdb_guid"


def _assert_identity_contract(record):
    assert set(IDENTITY_REPORT_FIELDS) <= set(record)


def test_item_report_identity_normalizes_aliases_and_nullable_fields():
    record = item_report_record({"title": "Example"}, IDENTITY, season_number="3")

    _assert_identity(record, season_number=3)
    missing = item_report_record({"title": "No identity"})
    assert all(missing[field] is None for field in IDENTITY_REPORT_FIELDS)


def test_builder_identity_source_hints_are_specific_and_stable():
    assert builder._identity_source_hint({"identity_source": "binding"}, 1) == "binding"
    assert (
        builder._identity_source_hint({"plex_provider_tmdb_id": "1"}, 1)
        == "plex_tmdb_guid"
    )
    assert builder._identity_source_hint({}, 1) == "tmdb_id"
    assert builder._identity_source_hint({}, None, "tt1", 2) == "external_id_resolution"
    assert builder._identity_source_hint({}, None, "tt1") == "imdb_external_id"
    assert builder._identity_source_hint({}, None, tvdb_id=2) == "tvdb_external_id"
    assert builder._identity_source_hint({}, None) == "title_year_search"


def test_artwork_asset_and_adoption_json_share_identity_contract(tmp_path):
    gap = item_report_record(
        {
            "library": "TV Shows",
            "media_type": "TV Show",
            "title": "Example (2024)",
            "asset_type": "season 2 poster",
            "category": "artwork_missing",
            "detail": "No provider candidate",
        },
        IDENTITY,
        season_number=2,
    )
    gap_report = write_artwork_gap_report([gap], base_dir=tmp_path)
    _assert_identity(_json_data(gap_report)["entries"][0], season_number=2)

    audit_report = write_asset_audit_report(
        [
            item_report_record(
                {
                    "library": "TV Shows",
                    "media_type": "TV Show",
                    "title": "Example (2024)",
                    "asset_type": "season",
                    "season_number": 2,
                    "action": "would_download",
                    "ownership": "missing",
                    "candidate": {"quality_score": 50},
                },
                IDENTITY,
                season_number=2,
            )
        ],
        [gap],
        base_dir=tmp_path,
    )
    audit = _json_data(audit_report)
    _assert_identity(audit["candidates"][0], season_number=2)
    _assert_identity(audit["gaps"][0], season_number=2)

    adoption_report = write_adoption_audit_report(
        [
            item_report_record(
                {
                    "library": "TV Shows",
                    "title": "Example (2024)",
                    "asset_type": "season",
                    "season_number": 2,
                    "provider": "TMDb",
                    "destination": "/managed/Season02.jpg",
                    "status": "filesystem_verified",
                },
                IDENTITY,
                season_number=2,
            )
        ],
        base_dir=tmp_path,
    )
    _assert_identity(_json_data(adoption_report)["entries"][0], season_number=2)


def test_unresolved_work_persists_identity_and_upgrades_schema_five(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE unresolved_work (
                fingerprint TEXT PRIMARY KEY,
                library_name TEXT NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                category TEXT NOT NULL,
                detail TEXT,
                status TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                resolved_at TEXT
            ) WITHOUT ROWID;
            PRAGMA user_version = 5;
            """
        )
    problem = item_report_record(
        {
            "library": "Movies",
            "media_type": "Movie",
            "title": "Example (2024)",
            "asset_type": "poster",
            "category": "artwork_missing",
            "detail": "No provider candidate",
        },
        IDENTITY,
    )

    records = state_db.reconcile_unresolved_work([problem], path=database)

    _assert_identity(records[0])
    with closing(sqlite3.connect(database)) as connection, connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == state_db.SCHEMA_VERSION
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(unresolved_work)")}
    assert set(IDENTITY_REPORT_FIELDS) <= columns
    report = write_unresolved_work_report(records, base_dir=tmp_path)
    _assert_identity(_json_data(report)["entries"][0])


def test_metadata_reports_include_identity_for_kometa_and_plex(tmp_path):
    config = {
        "_execution": {"metadata_audit": True},
        "_metadata_audit_records": [],
    }
    record_kometa_metadata_audit(
        config,
        library="Movies",
        media_type="Movie",
        title="Example (2024)",
        existing={},
        generated={"summary": "value"},
        identity=IDENTITY,
    )
    _assert_identity(config["_metadata_audit_records"][0])

    reporter = PlexMetadataReporter(
        {
            "settings": {"mode": "plex"},
            "plex_metadata": {"enabled": True},
        }
    )
    reporter.record(
        "Movies",
        "Example (2024)",
        "item",
        "summary",
        "filled",
        identity=IDENTITY,
    )
    report = reporter.write(base_dir=tmp_path)
    _assert_identity(_json_data(report)["items"][0])


def test_all_item_level_report_writers_guarantee_identity_contract(tmp_path):
    bare = {"library": "Movies", "media_type": "Movie", "title": "Example"}
    candidate = {**bare, "candidate": {}, "action": "would_download"}

    metadata = write_metadata_audit_report(
        [{**bare, "field": "summary", "state": "missing"}],
        [bare],
        mode="kometa",
        base_dir=tmp_path,
    )
    _assert_identity_contract(_json_data(metadata)["field_decisions"][0])

    change_plan = write_change_plan_report(
        [{**bare, "field": "summary", "proposed_action": "add"}],
        [candidate],
        [],
        [bare],
        mode="kometa",
        base_dir=tmp_path,
    )
    change_data = _json_data(change_plan)
    for name in ("metadata", "artwork", "gaps"):
        _assert_identity_contract(change_data[name][0])

    library_audit = write_library_asset_audit_report(
        [], [candidate], [bare], mode="kometa", base_dir=tmp_path
    )
    library_data = _json_data(library_audit)
    _assert_identity_contract(library_data["artwork"][0])
    _assert_identity_contract(library_data["gaps"][0])

    cache = {
        "movie:1": {
            "title": "Example",
            "destination_history": [
                {
                    "asset_type": "poster",
                    "previous_destination": str(tmp_path / "old.jpg"),
                    "new_destination": str(tmp_path / "new.jpg"),
                    "reported_at": None,
                }
            ],
        }
    }
    destination = write_destination_history_report(cache, base_dir=tmp_path)
    _assert_identity_contract(_json_data(destination)["entries"][0])

    identity = write_identity_inspection_report([bare], base_dir=tmp_path)
    _assert_identity_contract(_json_data(identity)["items"][0])
    mapping = write_mapping_diagnosis_report([bare], base_dir=tmp_path)
    _assert_identity_contract(_json_data(mapping)["items"][0])
    explanation = write_item_explanation_report([bare], base_dir=tmp_path)
    _assert_identity_contract(_json_data(explanation)["items"][0])
    replay = write_sanitized_replay_capture([bare], base_dir=tmp_path)
    _assert_identity_contract(_json_data(replay)["items"][0])
