import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml

import metafusion
from helper import kometa_application_verification, tmdb_changes, upgrade_canary
from helper.config import DEFAULT_CONFIG
from helper.incremental import plan_items
from helper.tmdb import _change_refresh_requested

UTC = timezone.utc


def complete_config(tmp_path, *, mode="kometa"):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode=mode, path=str(tmp_path / "kometa"))
    config["plex"].update(url="http://plex:32400", token="token")
    config["tmdb"]["api_key"] = "key"
    config["plex_libraries"] = ["Movies"]
    return config


def test_tmdb_change_plan_baselines_then_uses_durable_bounded_window(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    config = complete_config(tmp_path)
    started = datetime(2026, 8, 20, 1, 2, tzinfo=UTC)
    decisions = {("server", "movies"): False}

    first = tmdb_changes.prepare_tmdb_change_plan(config, decisions, now=started, path=database)
    assert first["status"] == "baseline_required"
    assert first["force_full_scan"] is True
    assert tmdb_changes.commit_tmdb_change_checkpoint(
        first,
        {"pages": {"movie": 0, "tv": 0}, "changed_ids": {}},
        path=database,
    )

    next_run = tmdb_changes.prepare_tmdb_change_plan(
        config,
        decisions,
        now=started + timedelta(days=2, hours=3),
        path=database,
    )
    assert next_run["status"] == "ready"
    assert next_run["start_date"] == "2026-08-20"
    assert next_run["end_date"] == "2026-08-22"


def test_tmdb_change_plan_safety_states(tmp_path):
    config = complete_config(tmp_path)
    decisions = {("server", "movies"): False}
    now = datetime(2026, 8, 20, tzinfo=UTC)

    config["incremental"]["tmdb_change_rechecks"] = False
    assert tmdb_changes.prepare_tmdb_change_plan(config, decisions)["status"] == "disabled"
    config["incremental"]["tmdb_change_rechecks"] = True
    assert (
        tmdb_changes.prepare_tmdb_change_plan(config, decisions, targeted=True)["status"]
        == "targeted_run"
    )
    config["settings"]["dry_run"] = True
    assert tmdb_changes.prepare_tmdb_change_plan(config, decisions)["status"] == "dry_run"
    config["settings"]["dry_run"] = False
    assert (
        tmdb_changes.prepare_tmdb_change_plan(config, dict.fromkeys(decisions, True), now=now)[
            "status"
        ]
        == "baseline_full_scan"
    )

    database = tmp_path / "stale.sqlite3"
    old = {
        "status": "baseline_full_scan",
        "checkpoint_candidate": (now - timedelta(days=14)).isoformat(),
    }
    assert tmdb_changes.commit_tmdb_change_checkpoint(old, {}, path=database)
    stale = tmdb_changes.prepare_tmdb_change_plan(config, decisions, now=now, path=database)
    assert stale["status"] == "checkpoint_stale"
    assert stale["force_full_scan"] is True
    assert not tmdb_changes.commit_tmdb_change_checkpoint(
        {"status": "feed_unavailable", "checkpoint_candidate": now.isoformat()},
        {},
        path=database,
    )


def test_tmdb_change_feed_paginates_and_maps_only_local_ids(monkeypatch, tmp_path):
    calls = []

    async def fake_request(config, endpoint, params=None, **kwargs):
        calls.append((endpoint, params["page"], kwargs))
        if endpoint == "movie/changes":
            return {
                "results": [{"id": 100 if params["page"] == 1 else 999}],
                "total_pages": 2,
            }
        return {"results": [{"id": 200}], "total_pages": 1}

    monkeypatch.setattr(tmdb_changes, "tmdb_api_request", fake_request)
    plan = {
        "status": "ready",
        "start_date": "2026-08-19",
        "end_date": "2026-08-20",
    }
    inventory = {
        "Movies": [
            {"media_type": "movie", "tmdb_id": "100", "rating_key": "10"},
            {"media_type": "movie", "tmdb_id": "101", "rating_key": "11"},
        ],
        "TV": [{"media_type": "show", "tmdb_id": "200", "rating_key": "20"}],
    }
    result = asyncio.run(
        tmdb_changes.collect_tmdb_change_rechecks(
            complete_config(tmp_path), plan, inventory, object()
        )
    )

    assert result["rating_keys"] == {"Movies": {"10"}, "TV": {"20"}}
    assert result["changed_ids"] == {"movie": ["100"], "tv": ["200"]}
    assert result["pages"] == {"movie": 2, "tv": 1}
    assert all(call[2]["cache"] is False for call in calls)


def test_tmdb_change_feed_rejects_partial_or_unbounded_responses(monkeypatch, tmp_path):
    async def invalid(*args, **kwargs):
        return {"results": None}

    monkeypatch.setattr(tmdb_changes, "tmdb_api_request", invalid)
    plan = {"status": "ready", "start_date": "2026-08-19", "end_date": "2026-08-20"}
    with pytest.raises(tmdb_changes.TMDbChangeFeedError, match="invalid"):
        asyncio.run(
            tmdb_changes.collect_tmdb_change_rechecks(
                complete_config(tmp_path),
                plan,
                {"Movies": [{"media_type": "movie", "tmdb_id": 1}]},
                object(),
            )
        )

    async def unbounded(*args, **kwargs):
        return {"results": [], "total_pages": tmdb_changes.MAX_CHANGE_PAGES + 1}

    monkeypatch.setattr(tmdb_changes, "tmdb_api_request", unbounded)
    with pytest.raises(tmdb_changes.TMDbChangeFeedError, match="safety limit"):
        asyncio.run(
            tmdb_changes.collect_tmdb_change_rechecks(
                complete_config(tmp_path),
                plan,
                {"Movies": [{"media_type": "movie", "tmdb_id": 1}]},
                object(),
            )
        )


def test_tmdb_change_selection_rechecks_unchanged_item():
    updated = datetime(2026, 8, 1, tzinfo=UTC)
    item = SimpleNamespace(ratingKey="10", updatedAt=updated, type="movie")
    cache = {
        "movie": {
            "rating_key": "10",
            "plex_updated_at": updated.isoformat(),
            "config_fingerprint": "same",
        }
    }
    result = plan_items(
        [item],
        cache,
        "same",
        change_rating_keys={"10"},
        feature_flags={"metadata_basic": True, "poster": True},
    )
    assert result[0].selection_causes == frozenset({"tmdb_change_detected"})
    assert result[0].reasons == frozenset({"metadata", "poster"})


def test_tmdb_changed_detail_requests_refresh_instead_of_reading_stale_cache():
    config = {"_tmdb_refresh_ids": {"movie": [100], "tv": [200]}}
    assert _change_refresh_requested(config, "movie/100")
    assert _change_refresh_requested(config, "tv/200/season/1")
    assert not _change_refresh_requested(config, "movie/101")
    assert not _change_refresh_requested(config, "movie/changes")
    assert not _change_refresh_requested(config, "https://image.tmdb.org/poster.jpg")


def test_upgrade_canary_passes_reports_and_commits_only_after_job(monkeypatch, tmp_path):
    config = complete_config(tmp_path)
    database = tmp_path / "meta_db.sqlite3"
    section = SimpleNamespace(title="Movies")
    records = [
        {"media_type": "movie", "title": "B", "year": 2020, "rating_key": "2"},
        {"media_type": "movie", "title": "A", "year": 2019, "rating_key": "1"},
    ]
    items = [SimpleNamespace(ratingKey="1"), SimpleNamespace(ratingKey="2")]

    async def inventory(*args, **kwargs):
        return items

    async def explain(item, config, **kwargs):
        return {"status": "accepted", "plex_rating_key": item.ratingKey}

    monkeypatch.setattr(upgrade_canary, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(upgrade_canary, "explain_item", explain)
    current = {"version": "1.2.0-rc1", "commit": "abc123"}
    result, report = asyncio.run(
        upgrade_canary.run_upgrade_canary(
            [section],
            {"Movies": records},
            config,
            session=object(),
            server_id="server",
            plex_version="1.43.0",
            current=current,
            base_dir=tmp_path,
            path=database,
        )
    )

    assert result["passed"] is True
    assert report.is_file()
    assert report.with_suffix(".json").is_file()
    assert upgrade_canary.upgrade_canary_required(config, "server", current=current, path=database)
    assert upgrade_canary.commit_upgrade_canary(result, path=database)
    assert not upgrade_canary.upgrade_canary_required(
        config, "server", current=current, path=database
    )


def test_upgrade_canary_blocks_failed_library_before_output(monkeypatch, tmp_path):
    config = complete_config(tmp_path)
    section = SimpleNamespace(title="Movies")

    async def inventory(*args, **kwargs):
        return [SimpleNamespace(ratingKey="1")]

    async def explain(*args, **kwargs):
        raise ValueError("bad sample")

    monkeypatch.setattr(upgrade_canary, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(upgrade_canary, "explain_item", explain)
    with pytest.raises(upgrade_canary.UpgradeCanaryError, match="before output writes"):
        asyncio.run(
            upgrade_canary.run_upgrade_canary(
                [section],
                {"Movies": [{"media_type": "movie", "title": "A", "rating_key": "1"}]},
                config,
                session=object(),
                server_id="server",
                plex_version="1.43.0",
                current={"version": "1.2.0", "commit": "bad123"},
                base_dir=tmp_path,
                path=tmp_path / "state.sqlite3",
            )
        )


def test_upgrade_canary_skips_development_dry_run_and_disabled_builds(tmp_path):
    config = complete_config(tmp_path)
    assert not upgrade_canary.upgrade_canary_required(
        config,
        "server",
        current={"version": "development", "commit": "unknown"},
        path=tmp_path / "missing.sqlite3",
    )
    config["settings"]["dry_run"] = True
    assert not upgrade_canary.upgrade_canary_required(
        config,
        "server",
        current={"version": "1.2.0", "commit": "abc"},
        path=tmp_path / "missing.sqlite3",
    )
    config["settings"]["dry_run"] = False
    config["compatibility"]["upgrade_canary"] = False
    assert not upgrade_canary.upgrade_canary_required(
        config,
        "server",
        current={"version": "1.2.0", "commit": "abc"},
        path=tmp_path / "missing.sqlite3",
    )
    assert not upgrade_canary.commit_upgrade_canary(None, path=tmp_path / "state.sqlite3")


class Reloadable(SimpleNamespace):
    def reload(self):
        self.reload_count = getattr(self, "reload_count", 0) + 1


def test_kometa_comparison_checks_generated_subset_and_reloads_each_child_once():
    episode = Reloadable(index=1, title="Pilot", directors=["Director"], reload_count=0)
    season = Reloadable(index=1, title="Season 1", reload_count=0)
    season.episodes = lambda: [episode]
    item = Reloadable(
        title="Example",
        titleSort="Example",
        genres=["Drama", "Extra Plex Genre"],
        reload_count=0,
    )
    item.seasons = lambda: [season]
    entry = {
        "title": "Example",
        "sort_title": "Example",
        "genre": ["Drama"],
        "seasons": {1: {"episodes": {1: {"title": "Pilot", "director": ["Director"]}}}},
    }

    result = kometa_application_verification.compare_kometa_entry(entry, item, "show")
    assert result["status"] == "applied"
    assert result["fields_checked"] == 5
    assert item.reload_count == episode.reload_count == 1
    assert season.reload_count == 0

    episode.title = "Different"
    result = kometa_application_verification.compare_kometa_entry(entry, item, "show")
    assert result["status"] == "partial"
    assert result["fields_missing_or_different"] == 1


def test_kometa_report_has_text_json_and_retention(tmp_path):
    metadata = [
        {
            "library": "Movies",
            "title": "Example",
            "status": "partial",
            "plex_rating_key": "1",
            "fields_matched": 1,
            "fields_checked": 2,
            "mismatches": [{"child": "item", "field": "summary", "reason": "differs"}],
        }
    ]
    artwork = [
        {
            "library": "Movies",
            "title": "Example",
            "status": "selected",
            "asset_type": "poster",
            "reason": "matches",
        }
    ]
    report = kometa_application_verification.write_kometa_application_report(
        metadata, artwork, base_dir=tmp_path, retention=2
    )
    companion = json.loads(report.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.is_file()
    assert companion["report_type"] == "kometa_application_audit"
    assert companion["data"]["summary"]["metadata"] == {"partial": 1}


def test_kometa_application_runner_matches_yaml_and_reports_missing_key(monkeypatch, tmp_path):
    config = complete_config(tmp_path)
    metadata_dir = tmp_path / "kometa" / "metadata"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "movie_metadata.yml").write_text(
        yaml.safe_dump({"metadata": {"Example (2020)": {"title": "Example"}}}),
        encoding="utf-8",
    )
    section = SimpleNamespace(title="Movies")
    item = Reloadable(ratingKey="1", title="Example", reload_count=0)

    async def inventory(section, runtime, records_only=False):
        if records_only:
            return [
                {
                    "rating_key": "1",
                    "media_type": "movie",
                    "title": "Example",
                    "year": 2020,
                    "edition": None,
                }
            ]
        return [item]

    async def metadata(*args, **kwargs):
        return {
            "ratingKey": "1",
            "library_type": "movie",
            "title": "Example",
            "year": 2020,
            "tmdb_id": 100,
            "library_name": "Movies",
        }

    async def artwork(*args, **kwargs):
        return [{"plex_rating_key": "1", "status": "selected"}]

    monkeypatch.setattr(kometa_application_verification, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(kometa_application_verification, "get_plex_metadata", metadata)
    monkeypatch.setattr(kometa_application_verification, "verify_plex_artwork", artwork)
    metadata_records, artwork_records, report = asyncio.run(
        kometa_application_verification.run_kometa_application_audit(
            [section], config, ["1", "999"], object(), base_dir=tmp_path
        )
    )
    assert metadata_records[0]["status"] == "applied"
    assert metadata_records[1]["status"] == "not_found"
    assert artwork_records[0]["status"] == "selected"
    assert report.is_file()


def test_kometa_application_cli_is_standalone_and_mode_guarded(monkeypatch, tmp_path):
    args = metafusion.parse_cli_args(["--kometa-application-audit"])
    assert args.kometa_application_audit is True
    config = complete_config(tmp_path, mode="plex")
    config["_execution"] = {"rating_keys": []}
    with pytest.raises(ValueError, match="RUN_MODE=kometa"):
        metafusion._handle_operator_command(args, config)


def test_kometa_application_connector_uses_plex_only_bounded_session(monkeypatch, tmp_path):
    config = complete_config(tmp_path)
    plex = SimpleNamespace(machineIdentifier="server")
    section = SimpleNamespace(title="Movies")
    calls = []

    class Session:
        async def __aenter__(self):
            calls.append("enter")
            return self

        async def __aexit__(self, *_args):
            calls.append("exit")
            return False

    async def preflight(_config, session, require_tmdb=True):
        assert isinstance(session, Session)
        assert require_tmdb is False
        return plex

    async def audit(sections, _config, keys, session):
        assert sections == [section]
        assert keys == ["10"]
        assert isinstance(session, Session)
        return ([{"status": "applied"}], [], tmp_path / "audit.txt")

    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: Session())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())
    monkeypatch.setattr(metafusion, "preflight_connectors", preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: ([section], ["Movies"], []),
    )
    monkeypatch.setattr(metafusion, "run_kometa_application_audit", audit)

    result = asyncio.run(metafusion.kometa_application_audit_connectors(config, ["10"]))
    assert result[0][0]["status"] == "applied"
    assert calls == ["enter", "exit"]


def test_kometa_application_operator_reports_review_count(monkeypatch, tmp_path, capsys):
    args = metafusion.parse_cli_args(["--kometa-application-audit", "--rating-key", "10"])
    config = complete_config(tmp_path)
    config["_execution"] = {"rating_keys": ["10"]}
    report = tmp_path / "audit.txt"

    async def audit(_config, keys):
        assert keys == ["10"]
        return (
            [{"status": "partial"}, {"status": "no_verifiable_fields"}],
            [{"status": "selected"}, {"status": "not_selected"}],
            report,
        )

    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)
    monkeypatch.setattr(metafusion, "kometa_application_audit_connectors", audit)
    assert metafusion._handle_operator_command(args, config) == 0
    output = capsys.readouterr().out
    assert "2 entry(s) need review" in output
    assert str(report) in output


def test_successful_job_commits_canary_and_change_checkpoint_last(monkeypatch, tmp_path):
    committed = []

    class Lock:
        def __init__(self, *_args):
            pass

        def acquire(self):
            pass

        def release(self):
            pass

    async def successful(config, _logger):
        config["_upgrade_canary_result"] = {"passed": True}
        config["_tmdb_change_plan"] = {"status": "ready"}
        config["_tmdb_change_summary"] = {"changed_ids": {}}

    monkeypatch.setattr(metafusion, "JobRunLock", Lock)
    monkeypatch.setattr(metafusion, "metafusion_main", successful)
    for name in (
        "begin_cache_session",
        "begin_tmdb_cache",
        "begin_fanart_cache",
        "begin_plex_metadata_run",
        "flush_cache",
        "flush_tmdb_cache",
        "flush_fanart_cache",
    ):
        monkeypatch.setattr(metafusion, name, lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metafusion, "finish_plex_metadata_run", lambda _config: None)
    monkeypatch.setattr(metafusion, "write_artwork_gap_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        metafusion, "write_destination_history_report", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(metafusion, "reconcile_unresolved_work", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(metafusion, "reconcile_identity_reviews", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(metafusion, "write_unresolved_work_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metafusion, "write_adoption_audit_report", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metafusion, "maintain_state_database", lambda: {"checkpointed": True})
    monkeypatch.setattr(metafusion, "retry_queue_summary", lambda: {})
    monkeypatch.setattr(metafusion.tmdb_response_cache, "maintain", lambda: {"checkpointed": True})
    monkeypatch.setattr(metafusion.fanart_response_cache, "maintain", lambda: {"checkpointed": True})
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)
    monkeypatch.setattr(metafusion.fanart_response_cache, "reset_memory", lambda: None)
    monkeypatch.setattr(
        metafusion,
        "commit_upgrade_canary",
        lambda result: committed.append(("canary", result)) or True,
    )
    monkeypatch.setattr(
        metafusion,
        "commit_tmdb_change_checkpoint",
        lambda plan, summary: committed.append(("tmdb", plan, summary)) or True,
    )

    config = complete_config(tmp_path)
    config["settings"]["dry_run"] = False
    assert metafusion.run_metafusion_job(config, metafusion.logging.getLogger("phase25"))
    assert [record[0] for record in committed] == ["canary", "tmdb"]
