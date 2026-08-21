import sqlite3
from contextlib import closing

from helper import (
    dashboard,
    item_explanation,
    metadata_provenance,
    plex_metadata,
    state_db,
    state_reporting,
)


def _identity(cache_key="movie:plex:1"):
    return {
        "cache_key": cache_key,
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "ratingKey": "1",
        "rating_key": "1",
        "library_type": "movie",
        "media_type": "movie",
        "title": "Example",
        "year": 2026,
        "tmdb_id": "100",
    }


def _seed_media(database, cache_key="movie:plex:1"):
    store = state_db.MediaStateStore(path=database)
    store[cache_key] = _identity(cache_key)
    store.flush()
    store.close()


def test_value_free_kometa_provenance_classifies_sources_actions_and_children():
    identity = _identity()
    existing = {
        "match": {"title": "Example", "year": 2026, "mapping_id": 99},
        "summary": "Keep",
        "studio": "Old",
        "seasons": {1: {"episodes": {2: {"title": "Old episode"}}}},
    }
    generated = {
        "match": {"title": "Example", "year": 2026, "mapping_id": 100},
        "sort_title": "Example",
        "summary": "",
        "studio": "New",
        "tagline": "",
        "seasons": {
            1: {"title": "Season One", "episodes": {2: {"title": "New episode"}}}
        },
    }
    merged = {
        "match": generated["match"],
        "sort_title": "Example",
        "summary": "Keep",
        "studio": "New",
        "seasons": generated["seasons"],
    }
    records = metadata_provenance.kometa_provenance_records(
        identity, existing=existing, generated=generated, merged=merged
    )
    by_path = {record["field_path"]: record for record in records}

    assert by_path["match.title"]["action"] == "unchanged"
    assert by_path["match.title"]["source_provider"] == "Plex / existing"
    assert by_path["match.mapping_id"]["source_provider"] == "Identity resolver"
    assert by_path["sort_title"]["source_provider"] == "Plex"
    assert by_path["summary"]["action"] == "preserved"
    assert by_path["studio"]["action"] == "updated"
    assert by_path["tagline"]["action"] == "source_missing"
    assert by_path["tagline"]["source_provider"] == "TMDb"
    episode = by_path["seasons.1.episodes.2.title"]
    assert episode["child_key"] == "episode:1:2"
    assert episode["field_name"] == "title"
    assert by_path["seasons.1.title"]["child_key"] == "season:1"
    assert episode["value_fingerprint"] != metadata_provenance.value_fingerprint(
        "Old episode"
    )
    assert metadata_provenance.value_fingerprint(None) is None
    assert metadata_provenance.value_fingerprint({"b": 2, "a": 1}) == (
        metadata_provenance.value_fingerprint({"a": 1, "b": 2})
    )

    retained_by_policy = metadata_provenance.kometa_provenance_records(
        identity,
        existing={"nested": "current", "studio": "Manual"},
        generated={"nested": {"field": "source"}, "studio": "Provider"},
        merged={"nested": "current", "studio": "Manual"},
    )
    assert {record["field_path"]: record for record in retained_by_policy}[
        "studio"
    ]["reason"] == "Merge policy retained the existing value"


def test_metadata_provenance_database_upsert_prune_filter_and_cascade(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    _seed_media(database)
    record = metadata_provenance.provenance_record(
        _identity(),
        target="kometa_yaml",
        child_key="item",
        field_name="summary",
        source_provider="TMDb",
        source_id="100",
        action="created",
        policy="kometa_merge",
        reason="missing",
        value="metadata value is not stored",
    )
    assert state_db.save_metadata_provenance([], path=database) == 0
    assert state_db.save_metadata_provenance([record], path=tmp_path / "orphan.sqlite3") == 0
    assert state_db.save_metadata_provenance([record], path=database) == 1
    assert state_db.save_metadata_provenance([record], path=database) == 0
    loaded = state_db.load_metadata_provenance(
        libraries=["Movies"],
        rating_keys=["1"],
        targets=["kometa_yaml"],
        path=database,
    )
    assert len(loaded) == 1
    assert "metadata value" not in str(loaded)
    first_recorded = loaded[0]["first_recorded_at"]

    changed = {**record, "action": "updated", "reason": "provider changed"}
    assert state_db.save_metadata_provenance([changed], path=database) == 1
    loaded = state_db.load_metadata_provenance(
        cache_keys=["movie:plex:1"], path=database
    )
    assert loaded[0]["first_recorded_at"] == first_recorded
    assert loaded[0]["action"] == "updated"

    second = {**record, "field_name": "studio", "field_path": "studio"}
    assert state_db.save_metadata_provenance([second], prune=False, path=database) == 1
    assert len(state_db.load_metadata_provenance(path=database)) == 2
    assert state_db.save_metadata_provenance([second], path=database) == 1
    assert [row["field_path"] for row in state_db.load_metadata_provenance(path=database)] == [
        "studio"
    ]

    store = state_db.MediaStateStore(path=database)
    del store["movie:plex:1"]
    store.flush()
    store.close()
    assert state_db.load_metadata_provenance(path=database) == []
    assert state_db.load_metadata_provenance(path=tmp_path / "missing.sqlite3") == []

    legacy = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(legacy)) as connection, connection:
        connection.execute("PRAGMA user_version=1")
    state_db._integrity_checked_databases.clear()
    assert state_db.load_metadata_provenance(path=legacy) == []


def _dashboard_snapshot():
    return {
        "schema": 1,
        "generated_at": "2026-08-21T12:00:00+00:00",
        "build": {"version": "1.2.0", "commit": "abc123"},
        "notice": "Recorded evidence",
        "overview": {
            "latest_job_status": "failed",
            "latest_job_finished": "now",
            "active_libraries": 1,
            "open_unresolved": 1,
            "pending_retries": 1,
            "pending_cleanup": 1,
            "field_provenance": 1,
        },
        "databases": {
            "state": {
                "status": "ok",
                "schema": 9,
                "expected_schema": 9,
                "bytes": 1024,
            },
            "tmdb": {
                "status": "missing",
                "schema": None,
                "expected_schema": 1,
                "bytes": 0,
            },
            "fanart": {
                "status": "error",
                "schema": 0,
                "expected_schema": 1,
                "bytes": 0,
            },
        },
        "libraries": [
            {
                "server_id": "server",
                "library_uuid": "library",
                "library_name": "Movies",
                "library_type": "movie",
                "active": 1,
                "last_seen": "today",
            }
        ],
        "scan_state": [
            {
                "server_id": "server",
                "library_uuid": "library",
                "last_full_scan_completed": "today",
                "last_successful_incremental": "today",
            }
        ],
        "jobs": [
            {
                "finished_at": "today",
                "mode": "oneshot",
                "status": "failed",
                "error": "api_key=secret-value",
            }
        ],
        "unresolved_work": [
            {
                "library_name": "Movies",
                "title": "Example <script>",
                "asset_type": "poster",
                "category": "provider_failure",
                "detail": "token: hidden-value",
                "last_seen": "today",
            }
        ],
        "retries": [
            {
                "library_name": "Movies",
                "rating_key": "1",
                "status": "pending",
                "failure_class": "transient",
                "error_type": "TimeoutError",
                "next_retry_at": "later",
            }
        ],
        "identity_reviews": [
            {
                "library_name": "Movies",
                "title": "Example",
                "category": "identity",
                "proposed_tmdb_id": "100",
                "reason": "ambiguous",
                "last_seen": "today",
            }
        ],
        "cleanup_pending": [
            {
                "library_name": "Movies",
                "title": "Old",
                "scope": "title",
                "confirmations": 1,
                "eligible_after": "tomorrow",
                "reason": "missing",
            }
        ],
        "cleanup_history": [
            {
                "occurred_at": "yesterday",
                "source": "automated",
                "status": "quarantined",
                "library_name": "Movies",
                "title": "Old",
                "action": "remove",
                "output_type": "poster",
            }
        ],
        "provider_health": [
            {
                "provider": "TMDb",
                "consecutive_failures": 1,
                "open_until": None,
                "last_success_at": "today",
                "updated_at": "today",
            }
        ],
        "metadata_provenance": [
            {
                "library_name": "Movies",
                "title": "Example",
                "target": "kometa_yaml",
                "field_path": "summary",
                "source_provider": "TMDb",
                "action": "updated",
                "policy": "kometa_merge",
                "value_fingerprint": "abcdef0123456789",
                "last_changed_at": "today",
            }
        ],
    }


def test_dashboard_render_write_retention_and_redaction(tmp_path):
    snapshot = _dashboard_snapshot()
    snapshot["internal"] = {
        "api_key": "dictionary-secret",
        "nested": {"plex_token": "nested-secret"},
    }
    rendered = dashboard.render_dashboard(snapshot)
    assert "MetaFusion Diagnostics" in rendered
    assert "Metadata provenance" in rendered
    assert "abcdef012345" in rendered
    assert "fetch(" not in rendered
    assert "https://" not in rendered
    assert "secret-value" not in rendered
    assert "hidden-value" not in rendered
    assert "Example &lt;script&gt;" in rendered
    assert dashboard._safe_text("abcdef", limit=4) == "abc…"
    assert "No rows" in dashboard._table(("Field",), [], empty="No rows")

    first = dashboard.write_dashboard_report(
        base_dir=tmp_path, retention=1, snapshot=snapshot
    )
    first.with_suffix(".json").unlink()
    second = dashboard.write_dashboard_report(
        base_dir=tmp_path, retention=1, snapshot=snapshot
    )
    reports = list((tmp_path / "reports").glob("metafusion-dashboard-[0-9]*.html"))
    assert reports == [second]
    assert not first.exists()
    assert (tmp_path / "reports" / "metafusion-dashboard-latest.html").exists()
    latest_json = tmp_path / "reports" / "metafusion-dashboard-latest.json"
    json_text = latest_json.read_text(encoding="utf-8")
    assert '"report_type": "offline_dashboard"' in json_text
    assert "secret-value" not in json_text
    assert "hidden-value" not in json_text
    assert "dictionary-secret" not in json_text
    assert "nested-secret" not in json_text


def test_dashboard_snapshot_is_sqlite_only_and_bounded(tmp_path, monkeypatch):
    database = tmp_path / "meta_db.sqlite3"
    _seed_media(database)
    state_db.record_job_run(
        "oneshot", "start", "finish", "success", summary={"Movies": {}}, path=database
    )
    connection = state_db._connect(database, writable=True)
    with connection:
        connection.execute(
            "INSERT INTO plex_library_inventory VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("server", "library", "Movies", "movie", "first", "last", None, 1),
        )
        connection.execute(
            "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?, ?)",
            ("tmdb", "TMDb", 0, None, "today", "today"),
        )
    connection.close()
    monkeypatch.setattr(
        dashboard,
        "DATABASES",
        {"state": (database, state_db.SCHEMA_VERSION)},
    )
    snapshot = dashboard.collect_dashboard_snapshot(path=database)
    assert snapshot["overview"]["latest_job_status"] == "success"
    assert snapshot["overview"]["active_libraries"] == 1
    assert snapshot["provider_health"][0]["provider"] == "TMDb"
    assert "path" not in snapshot["databases"]["state"]
    assert dashboard._readonly_rows(tmp_path / "missing.sqlite3", "none") == []
    assert dashboard._readonly_rows(database, "none") == []
    assert len(dashboard._readonly_rows(database, "media_state")) == 1


def test_state_and_item_reports_expose_recorded_provenance(tmp_path, monkeypatch):
    database = tmp_path / "meta_db.sqlite3"
    _seed_media(database)
    record = metadata_provenance.provenance_record(
        _identity(),
        target="kometa_yaml",
        child_key="item",
        field_name="summary",
        source_provider="TMDb",
        action="updated",
        policy="kometa_merge",
        reason="changed",
        value="private",
    )
    state_db.save_metadata_provenance([record], path=database)
    report = state_reporting.write_state_report(
        section="provenance", base_dir=tmp_path, path=database
    )
    assert "Recorded metadata provenance" in report.read_text(encoding="utf-8")
    assert "source=TMDb" in report.read_text(encoding="utf-8")

    monkeypatch.setattr(item_explanation, "build_info", lambda: {"version": "1", "commit": "abc"})
    explanation = item_explanation.write_item_explanation_report(
        [
            {
                "status": "accepted",
                "library": "Movies",
                "rating_key": "1",
                "media_type": "movie",
                "identity": {"plex": {"localized_title": "Example", "year": 2026}},
                "selection": {},
                "policies": {},
                "episode_mapping": {},
                "metadata_provenance": [record],
            }
        ],
        base_dir=tmp_path,
    )
    assert "kometa_yaml | summary | source=TMDb" in explanation.read_text(
        encoding="utf-8"
    )


def test_plex_reporter_queues_value_free_provenance():
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "plex_metadata": {"enabled": True, "policy": "fill_missing"},
        "output": {"report_retention": 1},
    }
    reporter = plex_metadata.PlexMetadataReporter(config)
    reporter.record(
        "Movies",
        "Example",
        "item",
        "summary",
        "filled",
        identity=_identity(),
        value="private summary",
    )
    queued = {}
    assert reporter.queue_provenance(queued) == 1
    assert queued["_metadata_provenance_records"] == reporter.provenance_entries
    assert reporter.provenance_entries[0]["source_provider"] == "TMDb"
    assert reporter.provenance_entries[0]["title"] == "Example"
    assert "private summary" not in str(reporter.provenance_entries)

    dry = plex_metadata.PlexMetadataReporter(
        {**config, "settings": {"mode": "plex", "dry_run": True}}
    )
    dry.provenance_entries = reporter.provenance_entries
    assert dry.queue_provenance({}) == 0
