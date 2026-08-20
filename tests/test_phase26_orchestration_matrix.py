import asyncio
import logging
from types import SimpleNamespace

import pytest

import metafusion


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Section:
    def __init__(self, title="Movies", media_type="movie", items=None):
        self.title = title
        self.type = media_type
        self.uuid = f"uuid-{title}"
        self._items = list(items or [])

    def all(self):
        return list(self._items)


def _item(key="10", media_type="movie"):
    return SimpleNamespace(
        ratingKey=key,
        title="Example",
        year=2020,
        type=media_type,
        editionTitle=None,
    )


def _record(key="10", media_type="movie", tmdb_id="100"):
    return {
        "rating_key": key,
        "title": "Example",
        "year": 2020,
        "media_type": media_type,
        "edition": None,
        "tmdb_id": tmdb_id,
    }


def _patch_runtime(monkeypatch, sections, selected, available, *, inventory=None):
    plex = SimpleNamespace(
        machineIdentifier="server", version="1.0", friendlyName="Plex"
    )

    async def preflight(*_args, **_kwargs):
        return plex

    async def load(section, _runtime, records_only=False):
        if records_only:
            return list((inventory or {}).get(section.title, []))
        return list(section.all())

    async def canary(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(metafusion, "get_meta_banner", lambda *_args: None)
    monkeypatch.setattr(
        metafusion, "check_sys_requirements", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(metafusion, "get_disabled_features", lambda *_args: None)
    monkeypatch.setattr(metafusion, "log_final_summary", lambda *_args: None)
    monkeypatch.setattr(metafusion, "preflight_connectors", preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda *_args, **_kwargs: (list(sections), list(selected), list(available)),
    )
    monkeypatch.setattr(metafusion, "load_plex_library_inventory", load)
    monkeypatch.setattr(metafusion, "run_upgrade_canary", canary)
    monkeypatch.setattr(metafusion, "load_cache", lambda: {})
    monkeypatch.setattr(metafusion, "tracker_for", lambda _config: None)
    monkeypatch.setattr(metafusion, "mark_library_scan_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(metafusion, "mark_library_scan_complete", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        metafusion.aiohttp, "ClientSession", lambda **_kwargs: _Session()
    )
    monkeypatch.setattr(
        metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object()
    )
    return plex


def _config(tmp_path, **execution):
    return {
        "settings": {"mode": "plex", "path": str(tmp_path)},
        "runtime": {"max_concurrency": 1},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {
            "run_poster": True,
            "run_background": True,
            "run_season": True,
        },
        "cleanup": {"run_cleanup": True},
        "plex": {},
        "tmdb": {},
        "_execution": execution,
    }


def test_metafusion_retry_without_matching_queue_returns_early(monkeypatch, tmp_path):
    section = _Section(items=[_item()])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": [_record()]},
    )
    monkeypatch.setattr(metafusion, "load_item_retries", lambda **_kwargs: [])
    monkeypatch.setattr(
        metafusion,
        "library_full_scan_decisions",
        lambda *_args, **_kwargs: {("server", "uuid-Movies"): True},
    )
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {"status": "disabled"},
    )
    config = _config(tmp_path, retry_failed=True, retry_status="permanent")
    assert asyncio.run(
        metafusion.metafusion_main(config, logging.getLogger("retry-empty"))
    ) is None


def test_metafusion_targeted_discovery_and_change_feed_fail_closed(
    monkeypatch, tmp_path
):
    movie = _Section(items=[_item()])
    show = _Section("Shows", "show", [_item("20", "show")])
    _patch_runtime(
        monkeypatch,
        [movie, show],
        ["Movies", "Shows"],
        [
            {"title": "Movies", "type": "movie"},
            {"title": "Shows", "type": "show"},
            {"title": "Music", "type": "artist"},
        ],
        inventory={"Movies": [_record()]},
    )
    monkeypatch.setattr(
        metafusion,
        "get_feature_flags",
        lambda _config: {
            "dry_run": False,
            "cleanup": True,
            "metadata_basic": True,
            "metadata_enhanced": True,
            "plex_metadata": False,
            "poster": True,
            "background": True,
            "season": True,
        },
    )
    monkeypatch.setattr(
        metafusion,
        "load_item_retries",
        lambda **_kwargs: [{"rating_key": "10"}],
    )
    monkeypatch.setattr(
        metafusion,
        "library_full_scan_decisions",
        lambda *_args, **_kwargs: {("server", "uuid-Movies"): False},
    )
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "force_full_scan": True,
            "start_date": "2026-08-19",
            "end_date": "2026-08-20",
            "checkpoint_candidate": "2026-08-20",
        },
    )

    async def failed_change_feed(*_args, **_kwargs):
        raise metafusion.TMDbChangeFeedError("rate limited")

    monkeypatch.setattr(metafusion, "collect_tmdb_change_rechecks", failed_change_feed)
    monkeypatch.setattr(
        metafusion,
        "reconcile_library_inventory",
        lambda *_args: [
            {"library_name": "Missing Shows", "library_uuid": "missing"}
        ],
    )

    async def process(**kwargs):
        kwargs["metadata_summaries"]["Movies"] = {
            "total_items": 0,
            "library_summary": {"item_failures": 0, "incremental_skipped": 1},
        }

    monkeypatch.setattr(metafusion, "process_library", process)
    config = _config(
        tmp_path,
        targeted=True,
        retry_failed=True,
        retry_status="transient",
        rating_keys=["old"],
        tmdb_ids=["999"],
        media_types=["movie"],
        full_scan=True,
        plan=True,
    )
    config["_library_discovery_auto"] = True
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            metafusion.metafusion_main(config, logging.getLogger("targeted-matrix"))
        )
    assert "rating keys not found: 10" in str(caught.value)
    assert "TMDb IDs not exposed by Plex GUIDs: 999" in str(caught.value)
    assert config["_tmdb_change_plan"]["status"] == "feed_unavailable"
    assert config["_tmdb_change_plan"]["checkpoint_candidate"] is None
    assert config["_library_audit_records"][0]["status"] == "loaded"
    assert config["_cleanup_result"].skipped_reason.startswith("targeted run")


def test_metafusion_empty_selection_reports_all_missing_configuration(
    monkeypatch, tmp_path
):
    _patch_runtime(
        monkeypatch,
        [],
        ["Ghost"],
        [{"title": "Movies", "type": "movie"}],
    )
    monkeypatch.setattr(
        metafusion,
        "get_feature_flags",
        lambda _config: {"dry_run": True, "cleanup": False},
    )
    monkeypatch.setattr(
        metafusion, "library_full_scan_decisions", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {"status": "disabled"},
    )
    config = _config(tmp_path)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(metafusion.metafusion_main(config, logging.getLogger("empty")))
    assert "Configured Plex libraries were not found: Ghost" in str(caught.value)
    assert "No configured Plex libraries were available" in str(caught.value)


@pytest.mark.parametrize("auto_discovery", [False, True])
def test_metafusion_incremental_cleanup_skip_and_missing_kometa_output(
    monkeypatch, tmp_path, auto_discovery
):
    section = _Section(items=[_item()])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": [_record()]},
    )
    monkeypatch.setattr(
        metafusion,
        "get_feature_flags",
        lambda _config: {
            "dry_run": False,
            "cleanup": True,
            "metadata_basic": False,
            "metadata_enhanced": False,
            "plex_metadata": False,
            "poster": False,
            "background": False,
            "season": False,
        },
    )
    monkeypatch.setattr(
        metafusion,
        "library_full_scan_decisions",
        lambda *_args, **_kwargs: {("server", "uuid-Movies"): False},
    )
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {"status": "disabled"},
    )
    monkeypatch.setattr(
        metafusion,
        "reconcile_library_inventory",
        lambda *_args: (
            [{"library_name": "Unavailable", "library_uuid": "gone"}]
            if auto_discovery
            else []
        ),
    )

    async def process(**kwargs):
        kwargs["metadata_summaries"]["Movies"] = {
            "total_items": 1,
            "library_summary": {"item_failures": 0},
        }

    monkeypatch.setattr(metafusion, "process_library", process)
    config = _config(tmp_path)
    config["settings"]["mode"] = "kometa"
    config["_library_discovery_auto"] = auto_discovery
    asyncio.run(metafusion.metafusion_main(config, logging.getLogger("incremental")))
    reason = config["_cleanup_result"].skipped_reason
    if auto_discovery:
        assert reason.startswith("previously discovered")
    else:
        # Missing Kometa output requires an authoritative scan before generation.
        assert reason is None
        assert "Movies" in config["_successful_full_scan_libraries"]


class _JobLock:
    instances = []

    def __init__(self, _path):
        self.acquired = False
        self.released = False
        self.instances.append(self)

    def acquire(self):
        self.acquired = True

    def release(self):
        self.released = True


class _RuntimeStatus:
    def __init__(self):
        self.started = 0
        self.finished = []

    def run_started(self):
        self.started += 1

    def run_finished(self, success, **details):
        self.finished.append((success, details))


class _CacheFacade:
    def __init__(self, checkpointed=True):
        self.checkpointed = checkpointed
        self.reset = 0

    def maintain(self):
        return {"checkpointed": self.checkpointed}

    def reset_memory(self):
        self.reset += 1


def _patch_job_services(monkeypatch, tmp_path, *, fail_job=None):
    _JobLock.instances.clear()
    monkeypatch.setattr(metafusion, "JobRunLock", _JobLock)
    monkeypatch.setattr(metafusion, "begin_cache_session", lambda **_kwargs: None)
    monkeypatch.setattr(metafusion, "begin_tmdb_cache", lambda _config: None)
    monkeypatch.setattr(metafusion, "begin_fanart_cache", lambda _config: None)
    monkeypatch.setattr(metafusion, "begin_plex_metadata_run", lambda _config: None)
    monkeypatch.setattr(metafusion, "begin_performance_tracking", lambda _tracker: "perf")
    monkeypatch.setattr(metafusion, "reset_performance_tracking", lambda _token: None)
    monkeypatch.setattr(
        metafusion, "begin_adaptive_concurrency", lambda _config: ("controller", "token")
    )
    monkeypatch.setattr(
        metafusion, "finish_adaptive_concurrency", lambda *_args, **_kwargs: None
    )

    async def job(config, _logger):
        config["_artwork_gaps"] = [{"category": "tmdb_missing"}]
        config["_adoption_audit_records"] = [{"status": "adopted"}]
        config["_asset_audit_records"] = [{"status": "managed"}]
        config["_library_audit_records"] = [{"status": "loaded"}]
        config["_metadata_audit_records"] = [{"status": "updated"}]
        config["_successful_full_scan_work"] = {"Movies": {"poster"}}
        config["_job_library_results"] = {"Movies": {"status": "success"}}
        config["_upgrade_canary_result"] = {"status": "passed"}
        config["_tmdb_change_plan"] = {"status": "ready"}
        config["_tmdb_change_summary"] = {"changed": 1}
        if fail_job:
            raise fail_job

    monkeypatch.setattr(metafusion, "metafusion_main", job)
    monkeypatch.setattr(metafusion, "finish_plex_metadata_run", lambda _config: tmp_path / "plex.txt")
    monkeypatch.setattr(metafusion, "report_retention", lambda _config: 3)
    monkeypatch.setattr(metafusion, "load_cache", lambda: {"item": {}})
    monkeypatch.setattr(metafusion, "reconcile_unresolved_work", lambda *_a, **_k: [{"status": "open"}])
    monkeypatch.setattr(metafusion, "reconcile_identity_reviews", lambda *_a, **_k: [{"status": "open"}])
    monkeypatch.setattr(metafusion, "load_identity_reviews", lambda **_k: [{"status": "open"}])
    for name in (
        "write_artwork_gap_report",
        "write_destination_history_report",
        "write_identity_review_report",
        "write_unresolved_work_report",
        "write_adoption_audit_report",
        "write_asset_audit_report",
        "write_metadata_audit_report",
        "write_change_plan_report",
        "write_library_asset_audit_report",
    ):
        monkeypatch.setattr(
            metafusion,
            name,
            lambda *_a, _name=name, **_k: tmp_path / f"{_name}.txt",
        )
    monkeypatch.setattr(metafusion, "flush_cache", lambda: True)
    monkeypatch.setattr(metafusion, "flush_tmdb_cache", lambda: True)
    monkeypatch.setattr(metafusion, "flush_fanart_cache", lambda: True)
    monkeypatch.setattr(metafusion, "maintain_state_database", lambda: {"checkpointed": True})
    monkeypatch.setattr(metafusion, "retry_queue_summary", lambda: {"due": 1})
    monkeypatch.setattr(metafusion, "commit_upgrade_canary", lambda _result: True)
    monkeypatch.setattr(metafusion, "commit_tmdb_change_checkpoint", lambda *_args: True)
    monkeypatch.setattr(metafusion, "log_performance_summary", lambda *_args: None)
    monkeypatch.setattr(metafusion, "fanart_project_api_key", lambda: "project-key")
    tmdb = _CacheFacade()
    fanart = _CacheFacade()
    monkeypatch.setattr(metafusion, "tmdb_response_cache", tmdb)
    monkeypatch.setattr(metafusion, "fanart_response_cache", fanart)
    return tmdb, fanart


@pytest.mark.parametrize(
    "execution",
    [
        {"asset_audit": True},
        {"metadata_audit": True},
        {"plan": True},
        {"library_audit": True},
    ],
)
def test_run_job_finalizes_each_diagnostic_mode(monkeypatch, tmp_path, execution):
    tmdb, fanart = _patch_job_services(monkeypatch, tmp_path)
    status = _RuntimeStatus()
    config = {
        "settings": {"mode": "kometa", "dry_run": False},
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
        "_execution": execution,
    }
    assert metafusion.run_metafusion_job(
        config, logging.getLogger(f"job-{next(iter(execution))}"), status
    ) is True
    assert status.started == 1
    assert status.finished[-1][0] is True
    assert _JobLock.instances[-1].released is True
    assert tmdb.reset == 1 and fanart.reset == 1


def test_run_job_handles_busy_lock_initialization_cancel_and_report_failures(
    monkeypatch, tmp_path
):
    class BusyLock(_JobLock):
        def acquire(self):
            raise metafusion.JobAlreadyRunningError("busy")

    monkeypatch.setattr(metafusion, "JobRunLock", BusyLock)
    status = _RuntimeStatus()
    config = {"settings": {"dry_run": False}, "plex": {}, "tmdb": {}}
    assert metafusion.run_metafusion_job(config, logging.getLogger("busy"), status) is False
    assert status.finished[-1][0] is False

    _patch_job_services(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metafusion,
        "begin_tmdb_cache",
        lambda _config: (_ for _ in ()).throw(RuntimeError("cache init")),
    )
    with pytest.raises(RuntimeError, match="cache init"):
        metafusion.run_metafusion_job(config, logging.getLogger("init"))
    assert _JobLock.instances[-1].released is True

    _patch_job_services(monkeypatch, tmp_path, fail_job=asyncio.CancelledError())
    assert metafusion.run_metafusion_job(config, logging.getLogger("cancel")) is False

    _patch_job_services(monkeypatch, tmp_path)
    config["_execution"] = {"asset_audit": True}
    monkeypatch.setattr(
        metafusion,
        "write_asset_audit_report",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("report disk")),
    )
    assert metafusion.run_metafusion_job(config, logging.getLogger("report")) is False


def test_run_job_degrades_optional_reports_and_maintenance(monkeypatch, tmp_path):
    _patch_job_services(monkeypatch, tmp_path)
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "plex": {},
        "tmdb": {},
        "_execution": {},
    }
    monkeypatch.setattr(
        metafusion,
        "reconcile_unresolved_work",
        lambda *_a, **_k: (_ for _ in ()).throw(metafusion.StateDatabaseError("state")),
    )
    monkeypatch.setattr(
        metafusion,
        "maintain_state_database",
        lambda: (_ for _ in ()).throw(RuntimeError("maintenance")),
    )
    assert metafusion.run_metafusion_job(config, logging.getLogger("degraded")) is True

    _patch_job_services(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metafusion,
        "finish_plex_metadata_run",
        lambda _config: (_ for _ in ()).throw(OSError("plex report")),
    )
    assert metafusion.run_metafusion_job(config, logging.getLogger("plex-report")) is False
