import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import metafusion
from helper import output_management, recovery, state_db
from helper.config import DEFAULT_CONFIG
from helper.config_impact import compare_configurations, write_config_impact_report
from helper.plex_artwork_verification import _plex_source
from helper.state_db import (
    MediaStateStore,
    apply_library_rebinding,
    cancel_cleanup_candidate,
    complete_cleanup_candidate,
    load_asset_ownership,
    load_cleanup_candidates,
    load_cleanup_history,
    load_identity_overrides,
    load_identity_reviews,
    load_item_exceptions,
    observe_cleanup_candidate,
    plan_library_rebinding,
    reconcile_identity_reviews,
    record_cleanup_history,
    remove_identity_override,
    remove_item_exception,
    resolve_identity_reviews,
    save_identity_override,
    save_item_exception,
)
from helper.state_reporting import (
    write_cleanup_history_report,
    write_state_report,
)
from modules import cleanup as cleanup_module
from modules.processing import apply_manual_tmdb_identity

UTC = timezone.utc


def state_item(path, key, *, library="Movies", rating_key="10", tmdb_id="100", poster=None):
    store = MediaStateStore(path)
    entry = {
        "server_id": "server",
        "library_uuid": f"uuid-{library}",
        "library_name": library,
        "rating_key": str(rating_key),
        "media_type": "movie",
        "tmdb_id": str(tmdb_id),
        "imdb_id": "tt0000100",
        "title": "Example",
        "year": 2020,
    }
    if poster:
        entry.update(
            poster_path=str(poster),
            poster_checksum=output_management.sha256_file(poster),
            poster_source_path="/provider/poster.jpg",
        )
    store[key] = entry
    store.flush()
    store.close()
    return entry


def test_cleanup_confirmation_grace_and_source_history(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    start = datetime(2026, 8, 19, 1, 0, tzinfo=UTC)
    record = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": "10",
        "cache_key": "movie:plex:10",
        "media_type": "movie",
        "title": "Example",
        "year": 2020,
        "tmdb_id": "100",
        "imdb_id": "tt0000100",
    }

    first = observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        confirmations_required=2,
        grace_hours=48,
        observation_id="scan-1",
        path=database,
        now=start,
    )
    duplicate = observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        confirmations_required=2,
        grace_hours=48,
        observation_id="scan-1",
        path=database,
        now=start + timedelta(hours=1),
    )
    eligible = observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        confirmations_required=2,
        grace_hours=48,
        observation_id="scan-2",
        path=database,
        now=start + timedelta(hours=49),
    )

    assert first["eligible"] is False
    assert duplicate["confirmations"] == 1
    assert eligible["eligible"] is True
    record_cleanup_history(
        "automated",
        "remove",
        "completed",
        record,
        output_type="state",
        path=database,
        now=start + timedelta(hours=49),
    )
    record_cleanup_history(
        "manual",
        "forget",
        "forgotten",
        record,
        output_type="poster",
        path=database,
        now=start + timedelta(hours=50),
    )
    assert len(load_cleanup_candidates(path=database)) == 1
    assert complete_cleanup_candidate("title:movie:plex:10", path=database) == 1
    history = load_cleanup_history(path=database)
    assert {row["source"] for row in history} == {"automated", "manual"}
    assert history[0]["imdb_id"] == "tt0000100"


def test_persistent_exceptions_and_identity_override_are_scoped(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    save_item_exception(
        "server",
        "uuid",
        "10",
        "poster",
        library_name="Movies",
        reason="manual art",
        path=database,
    )
    save_item_exception(
        "server",
        "uuid",
        "10",
        "season",
        library_name="Movies",
        season_number=0,
        path=database,
    )
    exceptions = load_item_exceptions("server", "uuid", path=database)
    assert {(row["output_type"], row["season_number"]) for row in exceptions} == {
        ("poster", -1),
        ("season", 0),
    }
    assert remove_item_exception(
        "server", "uuid", "10", "poster", path=database
    ) == 1

    save_identity_override(
        "server",
        "uuid",
        "10",
        "movie",
        "550",
        library_name="Movies",
        reason="verified",
        path=database,
    )
    override = load_identity_overrides("server", "uuid", path=database)[0]
    meta = {
        "ratingKey": "10",
        "library_type": "movie",
        "tmdb_id": "999",
    }
    config = {"_identity_overrides_by_rating_key": {"10": override}}
    assert apply_manual_tmdb_identity(meta, config) is True
    assert meta["tmdb_id"] == "550"
    assert meta["plex_tmdb_id"] == "999"
    assert meta["manual_identity_override"] is True
    assert remove_identity_override("server", "uuid", "10", path=database) == 1


def test_identity_review_queue_reopens_resolves_and_retains_ids(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    gap = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library": "Movies",
        "plex_rating_key": "10",
        "media_type": "Movie",
        "title": "Example (2020)",
        "year": 2020,
        "tmdb_id": "100",
        "imdb_id": "tt0000100",
        "tvdb_id": None,
        "category": "identity_rejected",
        "detail": "year mismatch",
    }
    first = reconcile_identity_reviews([gap], path=database)
    assert first[0]["status"] == "open"
    assert first[0]["imdb_id"] == "tt0000100"
    second = reconcile_identity_reviews([gap], path=database)
    assert second[0]["occurrences"] == 2
    assert resolve_identity_reviews(
        "server", "uuid", "10", path=database
    ) == 1
    assert load_identity_reviews(statuses=["resolved"], path=database)[0][
        "resolved_at"
    ]
    reconcile_identity_reviews([gap], path=database)
    save_identity_override(
        "server",
        "uuid",
        "10",
        "movie",
        "100",
        library_name="Movies",
        path=database,
    )
    assert load_identity_reviews(statuses=["open"], path=database) == []


def test_cleanup_candidate_cancellation_is_audited(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    record = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": "10",
        "cache_key": "movie:plex:10",
        "media_type": "movie",
        "title": "Example",
    }
    observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        path=database,
        observation_id="scan-1",
    )
    assert cancel_cleanup_candidate(
        "title:movie:plex:10", path=database, reason="returned"
    ) is True
    assert cancel_cleanup_candidate(
        "title:movie:plex:10", path=database
    ) is False
    history = load_cleanup_history(path=database)
    assert history[0]["status"] == "cancelled"
    assert history[0]["reason"] == "returned"


def test_targeted_output_remove_is_checksum_and_root_guarded(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    root = tmp_path / "kometa"
    poster = root / "assets" / "movie" / "Example (2020)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"managed")
    state_item(database, "movie:plex:10", poster=poster)
    monkeypatch.setattr(state_db, "STATE_DATABASE", database)

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(root))
    item = state_db.find_media_state(path=database)[0]
    decisions = output_management.plan_output_management(
        config, item, action="remove", output_type="poster"
    )
    results = output_management.apply_output_management(
        config, item, decisions, action="remove"
    )
    assert results[0]["status"] == "removed"
    assert not poster.exists()
    assert load_asset_ownership(path=database) == []
    assert load_cleanup_history(sources=["manual"], path=database)[0]["action"] == "remove"

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"managed")
    state_item(database, "movie:plex:11", rating_key="11", tmdb_id="101", poster=outside)
    outside_item = state_db.find_media_state(rating_keys=["11"], path=database)[0]
    protected = output_management.plan_output_management(
        config, outside_item, action="remove", output_type="poster"
    )
    assert protected[0]["status"] == "protected"
    assert outside.exists()


def test_library_rebinding_transfers_only_safe_state(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    poster = tmp_path / "poster.jpg"
    poster.write_bytes(b"owned")
    state_item(
        database,
        "movie:old:10",
        library="Old Movies",
        rating_key="10",
        tmdb_id="100",
        poster=poster,
    )
    state_item(
        database,
        "movie:new:20",
        library="Movies",
        rating_key="20",
        tmdb_id="100",
    )
    save_item_exception(
        "server",
        "uuid-Old Movies",
        "10",
        "cleanup",
        library_name="Old Movies",
        path=database,
    )

    plan = plan_library_rebinding("Old Movies", "Movies", path=database)
    assert plan[0]["status"] == "ready"
    result = apply_library_rebinding(plan, path=database)
    assert result[0]["status"] == "applied"
    items = state_db.find_media_state(path=database)
    assert [item["cache_key"] for item in items] == ["movie:new:20"]
    claims = load_asset_ownership(path=database)
    assert claims[0]["cache_key"] == "movie:new:20"
    transferred = load_item_exceptions(
        "server", "uuid-Movies", rating_keys=["20"], path=database
    )
    assert transferred[0]["output_type"] == "cleanup"


def test_recovery_bundle_and_sqlite_reports_are_verified(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    state_item(database, "movie:plex:10")
    template = tmp_path / "config_template.yml"
    template.write_text("plex:\n  token: placeholder\n", encoding="utf-8")
    monkeypatch.setattr(recovery, "TEMPLATE_FILE", template)

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(tmp_path / "kometa"))
    config["plex"]["token"] = "secret-token"
    metadata = Path(config["settings"]["path"]) / "metadata" / "movie_metadata.yml"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("metadata: {}\n", encoding="utf-8")

    bundle = recovery.create_recovery_bundle(
        config, base_dir=tmp_path, state_path=database
    )
    verified = recovery.verify_recovery_bundle(bundle)
    assert verified["valid"] is True
    assert verified["manifest"]["contents"]["artwork_files_included"] is False

    report = write_state_report(
        path=database, base_dir=tmp_path, include_items=True
    )
    payload = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["report_type"] == "sqlite_state"
    assert payload["data"]["items"][0]["rating_key"] == "10"
    history_report = write_cleanup_history_report(path=database, base_dir=tmp_path)
    assert history_report.exists()
    assert history_report.with_suffix(".json").exists()


def test_configuration_impact_and_season_plex_source(tmp_path):
    current = copy.deepcopy(DEFAULT_CONFIG)
    proposed = copy.deepcopy(DEFAULT_CONFIG)
    proposed["settings"]["mode"] = "plex"
    proposed["cleanup"]["grace_hours"] = 0
    result = compare_configurations(current, proposed)
    assert result["summary"]["high_risk"] == 2
    assert result["summary"]["cleanup_requires_live_plan"] is True
    _result, report = write_config_impact_report(
        current, proposed, base_dir=tmp_path
    )
    assert report.exists() and report.with_suffix(".json").exists()

    assert _plex_source(
        {"plex_artwork": {"seasons": {"2": "/library/season/2/poster"}}},
        {"asset_type": "season", "season_number": "2"},
    ) == "/library/season/2/poster"


def test_cleanup_requires_distinct_scans_and_plex_artwork_opt_in(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    monkeypatch.setattr(state_db, "STATE_DATABASE", database)
    media_root = tmp_path / "media"
    poster = media_root / "Example (2020)" / "poster.jpg"
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"managed")
    cache = {
        "movie:plex:10": {
            "server_id": "server",
            "library_uuid": "uuid",
            "library_name": "Movies",
            "rating_key": "10",
            "media_type": "movie",
            "tmdb_id": "100",
            "title": "Example",
            "year": 2020,
            "poster_path": str(poster),
            "poster_checksum": cleanup_module.sha256_file(poster),
        }
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)
    config = {
        "settings": {"mode": "plex", "path": str(tmp_path / "kometa")},
        "plex": {"path_mappings": [f"/plex=>{media_root}"]},
        "cleanup": {
            "confirmation_scans": 2,
            "grace_hours": 0,
            "plex_remove_managed_artwork": True,
        },
    }
    flags = {
        "dry_run": False,
        "metadata_basic": False,
        "metadata_enhanced": False,
        "poster": True,
        "season": False,
        "background": False,
    }

    first = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            config,
            flags,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )
    assert first.candidates_pending == 1
    assert poster.exists() and "movie:plex:10" in cache

    second = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            config,
            flags,
            preloaded_plex_metadata={},
            safe_library_types={"movie"},
        )
    )
    assert second.candidates_pending == 0
    assert not poster.exists()
    assert "movie:plex:10" not in cache
    history = load_cleanup_history(sources=["automated"], path=database)
    assert {row["output_type"] for row in history} == {"poster", "state"}


class FakeLock:
    def __init__(self, _path):
        self.acquired = False

    def acquire(self):
        self.acquired = True

    def release(self):
        self.acquired = False


def cli_args(**updates):
    args = metafusion.parse_cli_args([])
    for name, value in updates.items():
        setattr(args, name, value)
    return args


def operator_config(tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(tmp_path / "kometa"))
    config["_execution"] = {"rating_keys": []}
    return config


def test_sqlite_only_command_dispatch(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.txt"
    monkeypatch.setattr(metafusion, "write_dashboard_report", lambda **_kwargs: report)
    assert metafusion._handle_sqlite_only_command(
        cli_args(dashboard_report=True)
    ) == 0
    monkeypatch.setattr(metafusion, "write_state_report", lambda **_kwargs: report)
    assert metafusion._handle_sqlite_only_command(
        cli_args(state_report=True, include_state_items=True, library=["Movies"])
    ) == 0
    monkeypatch.setattr(
        metafusion, "write_cleanup_history_report", lambda **_kwargs: report
    )
    assert metafusion._handle_sqlite_only_command(
        cli_args(cleanup_history_report=True, history_source=["manual"])
    ) == 0
    monkeypatch.setattr(metafusion, "load_identity_reviews", lambda **_kwargs: [{"x": 1}])
    monkeypatch.setattr(
        metafusion, "write_identity_review_report", lambda *_args, **_kwargs: report
    )
    assert metafusion._handle_sqlite_only_command(
        cli_args(identity_review_queue=True)
    ) == 0
    monkeypatch.setattr(
        metafusion,
        "verify_recovery_bundle",
        lambda _path: {"valid": True, "bundle": "bundle.tar.gz"},
    )
    assert metafusion._handle_sqlite_only_command(
        cli_args(verify_recovery="bundle.tar.gz")
    ) == 0
    assert "saved" in capsys.readouterr().out


def test_exception_and_override_command_dispatch(monkeypatch, tmp_path, capsys):
    config = operator_config(tmp_path)
    item = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": "10",
        "media_type": "movie",
    }
    monkeypatch.setattr(metafusion, "JobRunLock", FakeLock)
    monkeypatch.setattr(metafusion, "_state_target", lambda *_args, **_kwargs: item)
    monkeypatch.setattr(
        metafusion, "load_item_exceptions", lambda **_kwargs: [{"output_type": "poster"}]
    )
    assert metafusion._handle_operator_command(
        cli_args(exception_action="list"), config
    ) == 0
    saved = []
    monkeypatch.setattr(
        metafusion, "save_item_exception", lambda *args, **kwargs: saved.append((args, kwargs))
    )
    assert metafusion._handle_operator_command(
        cli_args(
            exception_action="add",
            exception_output="poster",
            reason="manual",
        ),
        config,
    ) == 0
    assert saved[0][1]["reason"] == "manual"
    monkeypatch.setattr(metafusion, "remove_item_exception", lambda *_args, **_kwargs: 1)
    assert metafusion._handle_operator_command(
        cli_args(exception_action="remove", exception_output="poster"), config
    ) == 0

    monkeypatch.setattr(
        metafusion, "load_identity_overrides", lambda **_kwargs: [{"tmdb_id": "550"}]
    )
    assert metafusion._handle_operator_command(
        cli_args(identity_override_action="list"), config
    ) == 0
    monkeypatch.setattr(metafusion, "save_identity_override", lambda *_args, **_kwargs: True)
    assert metafusion._handle_operator_command(
        cli_args(identity_override_action="set", tmdb_id=["550"]), config
    ) == 0
    monkeypatch.setattr(metafusion, "remove_identity_override", lambda *_args: 1)
    assert metafusion._handle_operator_command(
        cli_args(identity_override_action="remove"), config
    ) == 0
    assert "record(s)" in capsys.readouterr().out


def test_rebind_recovery_and_config_impact_dispatch(monkeypatch, tmp_path):
    config = operator_config(tmp_path)
    report = tmp_path / "report.txt"
    plan = [{"status": "ready"}]
    monkeypatch.setattr(metafusion, "JobRunLock", FakeLock)
    monkeypatch.setattr(metafusion, "plan_library_rebinding", lambda *_args: plan)
    monkeypatch.setattr(
        metafusion, "write_rebinding_report", lambda *_args, **_kwargs: report
    )
    assert metafusion._handle_operator_command(
        cli_args(
            library_rebind="plan",
            from_library="Old Movies",
            to_library="Movies",
        ),
        config,
    ) == 0
    monkeypatch.setattr(
        metafusion, "apply_library_rebinding", lambda _plan: [{"status": "applied"}]
    )
    assert metafusion._handle_operator_command(
        cli_args(
            library_rebind="apply",
            from_library="Old Movies",
            to_library="Movies",
        ),
        config,
    ) == 0

    bundle = tmp_path / "bundle.tar.gz"
    monkeypatch.setattr(metafusion, "create_recovery_bundle", lambda _config: bundle)
    assert metafusion._handle_operator_command(
        cli_args(recovery_bundle=True), config
    ) == 0

    proposed = tmp_path / "proposed.yml"
    proposed.write_text("settings:\n  mode: plex\n", encoding="utf-8")
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (copy.deepcopy(DEFAULT_CONFIG), {}),
    )
    monkeypatch.setattr(
        metafusion,
        "write_config_impact_report",
        lambda *_args, **_kwargs: ({"changes": []}, report),
    )
    assert metafusion._handle_operator_command(
        cli_args(config_impact=str(proposed)), config
    ) == 0


def test_plex_verify_and_output_action_dispatch(monkeypatch, tmp_path):
    config = operator_config(tmp_path)
    report = tmp_path / "report.txt"

    async def verified(_config, _rating_keys):
        return [{"status": "selected"}]

    monkeypatch.setattr(metafusion, "plex_artwork_verification_connectors", verified)
    monkeypatch.setattr(
        metafusion,
        "write_plex_artwork_verification_report",
        lambda *_args, **_kwargs: report,
    )
    assert metafusion._handle_operator_command(
        cli_args(plex_artwork_verify=True), config
    ) == 0

    item = {"library_name": "Movies", "rating_key": "10", "media_type": "movie"}
    monkeypatch.setattr(metafusion, "_state_target", lambda *_args, **_kwargs: item)
    monkeypatch.setattr(metafusion, "JobRunLock", FakeLock)
    monkeypatch.setattr(
        metafusion,
        "write_output_management_report",
        lambda *_args, **_kwargs: report,
    )
    monkeypatch.setattr(
        metafusion,
        "plan_output_management",
        lambda *_args, **_kwargs: [{"status": "eligible", "output_type": "poster"}],
    )
    assert metafusion._handle_operator_command(
        cli_args(output_action="preview", output_type="poster"), config
    ) == 0
    monkeypatch.setattr(
        metafusion,
        "apply_output_management",
        lambda *_args, **_kwargs: [{"status": "removed", "output_type": "poster"}],
    )
    assert metafusion._handle_operator_command(
        cli_args(output_action="remove", output_type="poster"), config
    ) == 0

    rebuilt = operator_config(tmp_path)
    assert metafusion._handle_operator_command(
        cli_args(output_action="rebuild", output_type="poster"), rebuilt
    ) is None
    assert rebuilt["metafusion_run"] is True
    assert rebuilt["_execution"]["rating_keys"] == ["10"]
    assert rebuilt["assets"]["run_poster"] is True
    assert rebuilt["metadata"]["run_basic"] is False


def test_target_resolution_and_operator_validation_branches(monkeypatch, tmp_path):
    item = {"rating_key": "10", "media_type": "movie"}
    monkeypatch.setattr(metafusion, "find_media_state", lambda **_kwargs: [item])
    assert metafusion._state_target(
        cli_args(library=["Movies"], rating_key=["10"], media_type=["movie"])
    ) == item
    monkeypatch.setattr(metafusion, "find_media_state", lambda **_kwargs: [])
    try:
        metafusion._state_target(cli_args(rating_key=["missing"]))
    except ValueError as error:
        assert "exactly one" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing target should fail")

    assert metafusion._handle_sqlite_only_command(cli_args()) is None
    monkeypatch.setattr(
        metafusion, "verify_recovery_bundle", lambda _path: {"valid": False}
    )
    assert metafusion._handle_sqlite_only_command(
        cli_args(verify_recovery="invalid.tar.gz")
    ) == 1
    config = operator_config(tmp_path)
    monkeypatch.setattr(metafusion, "_state_target", lambda *_args, **_kwargs: item)
    try:
        metafusion._handle_operator_command(
            cli_args(exception_action="add"), config
        )
    except ValueError as error:
        assert "exception-output" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing exception output should fail")
    missing = tmp_path / "missing.yml"
    try:
        metafusion._handle_operator_command(
            cli_args(config_impact=str(missing)), config
        )
    except ValueError as error:
        assert "does not exist" in str(error)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("missing proposed config should fail")


def test_cleanup_cancels_pending_candidate_when_media_returns(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    monkeypatch.setattr(state_db, "STATE_DATABASE", database)
    assert state_db.find_media_state(path=tmp_path / "missing.sqlite3") == []
    record = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": "10",
        "cache_key": "movie:plex:10",
        "media_type": "movie",
        "title": "Example",
        "year": 2020,
    }
    observe_cleanup_candidate(
        "title:movie:plex:10",
        record,
        "title",
        path=database,
        observation_id="missing-scan",
    )
    cache = {"movie:plex:10": dict(record)}
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: cache)
    monkeypatch.setattr(cleanup_module, "mark_cache_dirty", lambda: None)
    result = asyncio.run(
        cleanup_module.cleanup_title_orphans(
            {
                "settings": {"mode": "plex", "path": str(tmp_path)},
                "cleanup": {"confirmation_scans": 2, "grace_hours": 48},
            },
            {"dry_run": False},
            preloaded_plex_metadata={"item": {**record, "library_type": "movie", "ratingKey": "10"}},
            safe_library_types={"movie"},
        )
    )
    assert result.cache_entries == 0
    assert load_cleanup_candidates(path=database) == []
    assert load_cleanup_history(path=database)[0]["status"] == "cancelled"


def test_lifecycle_cli_rejects_ambiguous_or_orphaned_modifiers(capsys):
    invalid_commands = [
        ["--state-report", "--cleanup-history-report"],
        ["--state-report", "--doctor"],
        ["--history-source", "manual"],
        ["--include-state-items"],
        ["--from-library", "Old Movies"],
        ["--season-number", "1"],
        ["--acknowledge-metadata-loss"],
        ["--exception-output", "poster"],
        ["--reason", "operator note"],
    ]
    for command in invalid_commands:
        assert metafusion.main(command) == 2
    assert "Configuration error" in capsys.readouterr().err
