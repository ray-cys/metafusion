import datetime
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from helper import logging as app_logging


class CaptureLogger:
    def __init__(self):
        self.records = []

    def _add(self, level, message, *args):
        self.records.append((level, message % args if args else message))

    def debug(self, message, *args):
        self._add("debug", message, *args)

    def info(self, message, *args):
        self._add("info", message, *args)

    def warning(self, message, *args):
        self._add("warning", message, *args)

    def error(self, message, *args):
        self._add("error", message, *args)


def test_artwork_gap_snapshot_logging_warns_for_open_and_infos_for_zero(tmp_path):
    logger = CaptureLogger()
    report = tmp_path / "artwork-gaps.txt"
    app_logging.log_artwork_gap_snapshot(
        logger,
        {
            "summary": {
                "artwork_current_run": 1,
                "artwork_carried_forward": 2,
                "artwork_open": 3,
                "artwork_not_due": 2,
                "artwork_resolved": 1,
            },
            "libraries": {
                "Movies": {
                    "open": 3,
                    "current_run": 1,
                    "carried_forward": 2,
                    "not_due": 2,
                }
            },
        },
        report,
    )
    app_logging.log_artwork_gap_snapshot(logger, None, report)

    assert logger.records[0][0] == "warning"
    assert "Current run: 1 | Carried forward: 2 | Open: 3" in logger.records[0][1]
    assert logger.records[1] == (
        "warning",
        "[Artwork] Movies | Persistent gaps | Open: 3 | Current run: 1 | "
        "Carried forward: 2 | Not due: 2",
    )
    assert logger.records[2][0] == "info"
    assert "Open: 0" in logger.records[2][1]


def test_size_handler_rollover_retention_and_error_path(tmp_path, monkeypatch):
    path = tmp_path / "metafusion-2026-08-22_01-02-03_000001.log"
    path.write_text("old", encoding="utf-8")
    handler = app_logging.SizeRotatingFileHandler(
        path, max_bytes=1, backup_count=1
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("test", logging.INFO, "", 0, "new", (), None)
    assert handler.shouldRollover(record)
    handler.doRollover()
    assert len(list(tmp_path.glob(f"{path.name}.*"))) == 1

    path.write_text("second", encoding="utf-8")
    handler.doRollover()
    assert len(list(tmp_path.glob(f"{path.name}.*"))) == 1

    monkeypatch.setattr(app_logging.os.path, "getsize", lambda _path: (_ for _ in ()).throw(OSError()))
    assert handler.shouldRollover(record)
    monkeypatch.setattr(handler, "shouldRollover", lambda _record: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(handler, "handleError", lambda _record: None)
    handler.emit(record)
    handler.close()


def test_logging_setup_dry_run_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr(app_logging, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(app_logging, "fanart_project_api_key", lambda: "fanart-secret")
    config = {
        "settings": {"dry_run": True, "log_level": "DEBUG"},
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
    }
    logger = app_logging.get_setup_logging(config)
    assert len(logger.handlers) == 1
    assert app_logging.begin_run_file_logging(config, logger) is None
    assert app_logging.finish_run_file_logging(config, logger, None) is None
    assert not (tmp_path / "logs").exists()
    record = logging.LogRecord(
        "test", logging.INFO, "", 0,
        "plex-secret tmdb-secret fanart-secret", (), None
    )
    assert logger.handlers[0].filters[0].filter(record)
    assert record.msg == "*** *** ***"
    assert app_logging.format_fields(("Missing", None), ("Empty", "")) == (
        "Missing: None | Empty: None"
    )
    logger.handlers.clear()


def test_per_run_logging_creates_distinct_files_and_retains_complete_runs(
    tmp_path, monkeypatch
):
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(app_logging, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(app_logging, "fanart_project_api_key", lambda: "fanart-secret")
    config = {
        "settings": {
            "dry_run": False,
            "log_level": "INFO",
            "log_max_mb": 0,
            "log_backup_count": 2,
        },
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
    }
    logger = app_logging.get_setup_logging(config)
    legacy = logs_dir / "metafusion.log"
    logs_dir.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    completed = []

    for index in range(3):
        handler = app_logging.begin_run_file_logging(
            config,
            logger,
            now=datetime.datetime(
                2026, 8, 22, 1, 2, index, tzinfo=datetime.timezone.utc
            ),
        )
        logger.info("run %d plex-secret tmdb-secret fanart-secret", index)
        if index == 0:
            Path(handler.baseFilename + ".part-001").write_text(
                "part", encoding="utf-8"
            )
        completed.append(app_logging.finish_run_file_logging(config, logger, handler))

    retained = app_logging._run_log_files(logs_dir)
    assert len(retained) == 2
    assert completed[0] not in retained
    assert not Path(str(completed[0]) + ".part-001").exists()
    assert retained[0].name.endswith("02_000000.log")
    assert legacy.read_text(encoding="utf-8") == "legacy"
    assert config["_run_log_path"] == str(completed[-1])
    text = completed[-1].read_text(encoding="utf-8")
    assert "run 2 *** *** ***" in text
    assert "Run log saved" in text
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_system_requirements_cover_recommendations_and_network_failures(monkeypatch):
    logger = CaptureLogger()
    memory = SimpleNamespace(total=2 * 1024**3, used=1, available=1)
    monkeypatch.setattr(app_logging.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(app_logging.psutil, "cpu_percent", lambda interval=None: 1.0)
    monkeypatch.setattr(app_logging.os, "cpu_count", lambda: 2)
    assert app_logging.check_sys_requirements(logger, {}, check_network=False)
    assert any("CPU cores" in line for _level, line in logger.records)
    assert any("RAM total" in line for _level, line in logger.records)

    class Response:
        def __init__(self, status):
            self.status_code = status

    responses = iter([Response(500), Response(503)])
    monkeypatch.setattr(app_logging.requests, "get", lambda *_args, **_kwargs: next(responses))
    assert not app_logging.check_sys_requirements(
        logger,
        {
            "plex": {"url": "http://plex", "token": "token"},
            "tmdb": {"api_key": "key"},
        },
    )
    assert any("HTTP status: 500" in line for _level, line in logger.records)
    assert any("HTTP status: 503" in line for _level, line in logger.records)

    monkeypatch.setattr(app_logging, "MIN_PYTHON", (99, 0))
    with pytest.raises(RuntimeError, match=r"Python 99\.0"):
        app_logging.check_sys_requirements(logger, {}, check_network=False)


def test_event_loggers_cover_default_info_warning_error_and_debug(capsys):
    logger = CaptureLogger()
    original_get_logger = app_logging.logging.getLogger
    app_logging.logging.getLogger = lambda *_args: logger
    try:
        app_logging.log_main_event("main_scheduled_run", run_time="12:00")
        assert "Scheduled run" in capsys.readouterr().out
        app_logging.log_main_event("unknown")

        app_logging.log_config_event("unknown")
        app_logging.log_config_event("unknown_key", key="x")
        app_logging.log_config_event("invalid_env_var", key="x", value="y", default="z")
        app_logging.log_config_event("config_loaded", config_file="config.yml")
        app_logging.log_config_event(
            "config_source",
            config_file="/config/kometa.yml",
            selection="single run-type profile",
            yaml_values=50,
            environment_overrides=5,
            secret_file_overrides=0,
            cli_overrides=1,
        )

        app_logging.log_cache_event("unknown")
        app_logging.log_cache_event("cache_load_failed", cache_file="db", error="bad")
        app_logging.log_cache_event("cache_loaded", count=1, cache_file="db")

        app_logging.log_plex_event("unknown")
        app_logging.log_tmdb_event("unknown")
        app_logging.log_fanart_event("unknown")
        app_logging.log_processing_event("unknown")
        app_logging.log_builder_event("unknown")
        app_logging.log_cleanup_event("unknown")
    finally:
        app_logging.logging.getLogger = original_get_logger
    assert {level for level, _message in logger.records} >= {
        "info", "warning", "error", "debug"
    }
    assert (
        "info",
        "[Configuration] Source | File: /config/kometa.yml | "
        "Selection: single run-type profile | YAML values: 50 | "
        "Environment overrides: 5 | Secret-file overrides: 0 | CLI overrides: 1",
    ) in logger.records


def test_item_outcomes_cover_unknown_completeness_and_season_details():
    logger = CaptureLogger()
    app_logging.log_item_outcomes(
        "Shows",
        "Example (2020)",
        {
            "metadata_action": "preserved",
            "percent": "bad",
            "poster_action": "preserved",
            "background_action": "missing",
            "artwork_selection_stages": {
                "background": "missing_only_download_failover"
            },
            "season_poster_actions": {
                None: "missing",
                1: "failed",
                2: "skipped",
                3: "not_due",
                4: "missing",
            },
            "season_artwork_providers": {"1": "fanart", 2: None},
            "season_artwork_attempts": {
                None: [
                    {"provider": "Plex", "status": "no_candidate"}
                ],
                "4": [{"provider": "TMDb", "status": "no_candidates"}],
            },
            "season_artwork_selection_stages": {
                "1": "missing_only_relaxed",
                2: "missing_only_download_failover",
            },
        },
        {
            "mode": "plex",
            "plex_metadata": True,
            "poster": True,
            "background": True,
            "season": True,
        },
        logger=logger,
    )
    text = "\n".join(message for _level, message in logger.records)
    assert "Field coverage: unknown" in text
    assert "Missing seasons: unknown" in text
    assert "Automatic relaxation: 1" in text
    assert "Download failover: 1" in text
    assert any(level == "error" and "Season posters" in message for level, message in logger.records)


def test_progress_empty_library_and_forced_update():
    logger = CaptureLogger()
    empty = app_logging.PlexMetadataProgress("Empty", 0, logger=logger)
    assert not empty.start()
    assert not empty.update(1, changed=0, api_batches=0, unchanged=0, failed=0)

    clock = [0.0]
    progress = app_logging.PlexMetadataProgress(
        "Movies", 1, logger=logger, clock=lambda: clock[0], minimum_seconds=0
    )
    assert progress.update(
        5, changed=1, api_batches=1, unchanged=0, failed=0, force=True
    )
    assert any("Checked: 1/1" in message for _level, message in logger.records)


def test_storage_helpers_and_full_summary_cover_plex_provider_paths(tmp_path, monkeypatch):
    file_path = tmp_path / "file"
    file_path.write_bytes(b"1234")
    assert app_logging._file_group_bytes([file_path, tmp_path / "missing"]) == 4
    assert app_logging._nearest_existing_path(file_path) == file_path
    assert app_logging._nearest_existing_path(tmp_path / "missing") is None

    usage = SimpleNamespace(total=1000, used=950, free=50)
    monkeypatch.setattr(app_logging, "BASE_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(app_logging, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(app_logging.shutil, "disk_usage", lambda _path: usage)
    monkeypatch.setattr(app_logging, "storage_pressure_threshold", lambda *_args: (100, 100))
    monkeypatch.setattr(app_logging, "_runtime_storage_bytes", lambda: {"State DB": 10})

    logger = CaptureLogger()
    summary = {
        "Unavailable": None,
        "Shows": {
            "total_items": 1,
            "library_type": "unknown",
            "complete": 1,
            "incomplete": 0,
            "percent_complete": 100,
            "library_summary": {
                "meta_upgraded": 1,
                "metadata_schedule_destinations": 10,
                "metadata_schedule_due": 2,
                "metadata_schedule_required": 1,
                "metadata_schedule_forced": 3,
                "metadata_schedule_not_due": 4,
                "poster_downloaded": 1,
                "background_downloaded": 1,
                "season_poster_downloaded": 1,
                "artwork_provider_writes": {"fanart": 2, "custom": 1},
                "artwork_current_providers": {
                    "tmdb": 1,
                    "existing": 1,
                    "unknown": 1,
                },
                "artwork_file_expected": 4,
                "artwork_file_present": 3,
                "artwork_file_absent": 1,
                "poster_schedule_destinations": 10,
                "poster_schedule_due": 2,
                "poster_schedule_required": 1,
                "poster_schedule_forced": 3,
                "poster_schedule_not_due": 4,
                "poster_policy_preserved": 1,
                "background_policy_missing": 1,
                "artwork_bytes": 20,
            },
        },
        "Movies Archive": {
            "total_items": 0,
            "library_type": "unknown",
            "complete": 0,
            "incomplete": 0,
            "percent_complete": 100,
            "library_summary": {},
        },
        "Misc": {
            "total_items": 0,
            "library_type": "unknown",
            "complete": 0,
            "incomplete": 0,
            "percent_complete": 100,
            "library_summary": {},
        },
    }
    cleanup = SimpleNamespace(
        titles=1,
        seasons=1,
        episodes=1,
        assets=1,
        cache_entries=1,
        yaml_entries=0,
        assets_preserved=0,
        assets_skipped=0,
        candidates_pending=0,
        failures=0,
        dry_run=False,
        mode="plex",
        failed_reason=None,
        skipped_reason=None,
    )
    config = {
        "settings": {"mode": "plex", "dry_run": False},
        "plex": {"path_mappings": [f"/source=>{tmp_path}"]},
        "plex_libraries": ["auto"],
        "cleanup": {"plex_remove_managed_artwork": True},
    }
    app_logging.log_final_summary(
        logger,
        1,
        summary,
        {"Shows": 20},
        cleanup,
        True,
        ["Shows"],
        [{"title": "Shows"}],
        config,
        feature_flags={
            "plex_metadata": True,
            "poster": True,
            "background": True,
            "season": True,
            "cleanup": True,
        },
    )
    text = "\n".join(message for _level, message in logger.records)
    assert "Fanart.tv: 2" in text
    assert "Artwork write sources" in text
    assert (
        "Artwork files | Scope: processed items | Evaluated destinations: 4 | "
        "Present: 3 | Absent: 1"
    ) in text
    assert "Artwork current sources | Existing/manual: 1, TMDb: 1, Unknown: 1" in text
    assert "Policy preserved: 1" in text
    assert "Policy missing: 1" in text
    assert (
        "Metadata schedule | Destinations: 10 | Due: 2 | Required: 1 | "
        "Forced: 3 | Not due: 4"
    ) in text
    assert "Metadata result | Target: Plex | Changed: 1" in text
    assert (
        "Series poster schedule | Destinations: 10 | Due: 2 | Required: 1 | "
        "Forced: 3 | Not due: 4"
    ) in text
    assert "Season poster schedule | Destinations: 0" in text
    assert "Season poster result | Evaluated: 1 | Downloaded: 1" in text
    assert "Missing this run:" in text
    assert "Plex metadata: Server-managed" in text
    assert "Low free space" in text
    assert app_logging.human_readable_size(1024) == "1.00 KB"


def test_storage_helpers_tolerate_os_errors_and_missing_mounts(monkeypatch, tmp_path):
    class BrokenFile:
        def is_file(self):
            raise OSError("unreadable")

    class BrokenMount:
        def stat(self):
            raise OSError("unreadable")

    assert app_logging._file_group_bytes([BrokenFile()]) == 0
    monkeypatch.setattr(app_logging, "BASE_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(app_logging, "_nearest_existing_path", lambda _path: None)
    assert app_logging._storage_mounts({"settings": {"mode": "kometa"}}) == []
    monkeypatch.setattr(
        app_logging,
        "_nearest_existing_path",
        lambda _path: BrokenMount(),
    )
    assert app_logging._storage_mounts({"settings": {"mode": "kometa"}}) == []
