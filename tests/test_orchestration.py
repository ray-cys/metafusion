import asyncio
import copy
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import metafusion
from helper.config import DEFAULT_CONFIG, ENV_BINDINGS, SECRET_FILE_BINDINGS


class Section:
    def __init__(self, title, library_type, items=None):
        self.title = title
        self.type = library_type
        self._items = list(items or [])

    def all(self):
        return list(self._items)


def test_operations_document_every_public_cli_option(capsys):
    with pytest.raises(SystemExit, match="0"):
        metafusion.parse_cli_args(["--help"])

    help_text = capsys.readouterr().out
    public_options = set(re.findall(r"--[a-z][a-z0-9_-]*", help_text))
    operations = (Path(__file__).parents[1] / "docs" / "operations.md").read_text(
        encoding="utf-8"
    )

    missing = sorted(
        option for option in public_options if f"`{option}`" not in operations
    )
    assert missing == []
    assert "`-h`" in operations


def test_targeted_cli_controls_library_item_and_metadata_only_scope():
    args = metafusion.parse_cli_args(
        [
            "--library",
            "Movies,Kids Movies",
            "--rating-key",
            "123",
            "--metadata-only",
            "--full-scan",
        ]
    )
    config = {
        "plex_libraries": ["Old"],
        "assets": {"run_poster": True, "run_season": True, "run_background": True},
        "cleanup": {"run_cleanup": True},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "settings": {},
    }

    metafusion.override_config_with_cli(config, args)

    assert config["plex_libraries"] == ["Movies", "Kids Movies"]
    assert config["assets"] == {
        "run_poster": False,
        "run_season": False,
        "run_background": False,
    }
    assert config["cleanup"]["run_cleanup"] is False
    assert config["_execution"] == {
        "rating_keys": ["123"],
        "targeted": True,
        "full_scan": True,
        "metadata_only": True,
        "asset_only": False,
        "asset_audit": False,
        "metadata_audit": False,
        "explain_selection": False,
    }


def test_metadata_audit_forces_read_only_metadata_full_scan():
    args = metafusion.parse_cli_args(["--metadata-audit"])
    config = {
        "metafusion_run": False,
        "settings": {"mode": "plex", "dry_run": False},
        "metadata": {"run_basic": False, "run_enhanced": True},
        "assets": {
            "run_poster": True,
            "run_season": True,
            "run_background": True,
        },
        "cleanup": {"run_cleanup": True},
        "plex_metadata": {"enabled": False},
        "plex_libraries": ["Movies"],
    }

    metafusion.override_config_with_cli(config, args)

    assert config["metafusion_run"] is True
    assert config["settings"]["dry_run"] is True
    assert config["metadata"]["run_basic"] is True
    assert not any(config["assets"].values())
    assert config["cleanup"]["run_cleanup"] is False
    assert config["plex_metadata"]["enabled"] is True
    assert config["_execution"]["metadata_audit"] is True
    assert config["_execution"]["full_scan"] is True


def test_asset_only_cli_disables_metadata_and_cleanup():
    args = metafusion.parse_cli_args(["--asset-only"])
    config = {
        "plex_libraries": ["Movies"],
        "assets": {
            "run_poster": True,
            "run_season": False,
            "run_background": True,
        },
        "cleanup": {"run_cleanup": True},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "settings": {"dry_run": False},
    }

    metafusion.override_config_with_cli(config, args)

    assert config["metadata"] == {"run_basic": False, "run_enhanced": False}
    assert config["cleanup"]["run_cleanup"] is False
    assert config["assets"] == {
        "run_poster": True,
        "run_season": False,
        "run_background": True,
    }
    assert config["_execution"]["asset_only"] is True


def test_explain_selection_cli_is_read_only():
    args = metafusion.parse_cli_args(["--explain-selection"])
    config = {
        "plex_libraries": ["Movies"],
        "assets": {
            "run_poster": True,
            "run_season": False,
            "run_background": False,
        },
        "cleanup": {"run_cleanup": True},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "settings": {"dry_run": False},
    }

    metafusion.override_config_with_cli(config, args)

    assert config["settings"]["dry_run"] is True
    assert config["cleanup"]["run_cleanup"] is False
    assert config["_execution"]["explain_selection"] is True


def test_connector_preflight_returns_reusable_plex_connection(monkeypatch):
    plex = object()
    calls = []

    def fake_plex(_config):
        calls.append("plex")
        return plex

    async def fake_tmdb(*_args, **_kwargs):
        calls.append("tmdb")
        return {"images": {}}

    monkeypatch.setattr(metafusion, "connect_plex_server", fake_plex)
    monkeypatch.setattr(metafusion, "tmdb_api_request", fake_tmdb)

    result = asyncio.run(metafusion.preflight_connectors({}, object()))

    assert result is plex
    assert set(calls) == {"plex", "tmdb"}


def test_complete_inventory_types_are_media_scoped():
    all_libraries = [
        {"title": "Movies", "type": "movie"},
        {"title": "Kids Movies", "type": "movie"},
        {"title": "TV", "type": "show"},
    ]

    assert metafusion.complete_inventory_types(
        all_libraries,
        [Section("Movies", "movie"), Section("Kids Movies", "movie")],
    ) == {"movie"}


def test_failed_run_returns_false_and_flushes_cache(monkeypatch):
    flushed = []

    async def fail_run(_config, _logger):
        raise RuntimeError("run failed")

    monkeypatch.setattr(metafusion, "metafusion_main", fail_run)
    monkeypatch.setattr(
        metafusion, "begin_cache_session", lambda **_kwargs: None
    )
    monkeypatch.setattr(metafusion, "flush_cache", lambda: flushed.append(True))

    successful = metafusion.run_metafusion_job(
        {"plex": {"token": "plex-secret"}, "tmdb": {"api_key": "tmdb-secret"}},
        logging.getLogger("orchestration-test"),
    )

    assert successful is False
    assert flushed == [True]


def test_two_consecutive_dry_run_jobs_reinitialize_cleanly(monkeypatch):
    calls = []
    flushed = []
    reports = []

    async def successful_run(config, _logger):
        calls.append(id(config))

    monkeypatch.setattr(metafusion, "metafusion_main", successful_run)
    monkeypatch.setattr(metafusion, "begin_cache_session", lambda **_kwargs: None)
    monkeypatch.setattr(metafusion, "begin_tmdb_cache", lambda _config: None)
    monkeypatch.setattr(metafusion, "begin_plex_metadata_run", lambda _config: None)
    monkeypatch.setattr(metafusion, "finish_plex_metadata_run", lambda _config: None)
    monkeypatch.setattr(metafusion, "flush_cache", lambda: flushed.append("metadata"))
    monkeypatch.setattr(metafusion, "flush_tmdb_cache", lambda: flushed.append("tmdb"))
    monkeypatch.setattr(
        metafusion,
        "write_artwork_gap_report",
        lambda *_args, **_kwargs: reports.append(True),
    )
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)

    config = {
        "settings": {"dry_run": True},
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
    }
    logger = logging.getLogger("consecutive-jobs-test")

    assert metafusion.run_metafusion_job(config, logger) is True
    assert metafusion.run_metafusion_job(config, logger) is True
    assert len(calls) == 2
    assert flushed == ["metadata", "tmdb", "metadata", "tmdb"]
    assert reports == []


def test_cleanup_is_disabled_after_a_library_failure(monkeypatch, tmp_path):
    movie = Section("Movies", "movie")
    cleanup_scopes = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fail_library(**_kwargs):
        raise RuntimeError("scan failed")

    async def capture_cleanup(**kwargs):
        cleanup_scopes.append(kwargs["safe_library_types"])
        return 0

    monkeypatch.setattr(metafusion, "get_meta_banner", lambda *_args: None)
    monkeypatch.setattr(metafusion, "check_sys_requirements", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(metafusion, "get_disabled_features", lambda *_args: None)
    monkeypatch.setattr(metafusion, "log_final_summary", lambda *_args: None)
    async def fake_preflight(_config, _session):
        return object()

    monkeypatch.setattr(metafusion, "preflight_connectors", fake_preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: ([movie], ["Movies"], [{"title": "Movies", "type": "movie"}]),
    )
    monkeypatch.setattr(metafusion, "process_library", fail_library)
    monkeypatch.setattr(metafusion, "cleanup_title_orphans", capture_cleanup)
    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())

    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "runtime": {},
        "cleanup": {"run_cleanup": True},
        "metadata": {},
        "assets": {},
        "plex": {},
        "tmdb": {},
    }
    with pytest.raises(RuntimeError, match="scan failed"):
        asyncio.run(metafusion.metafusion_main(config, logging.getLogger("main-test")))

    assert cleanup_scopes == [set()]


def test_shutdown_watchdog_does_not_force_exit_after_clean_shutdown(monkeypatch):
    exits = []
    monkeypatch.setattr(metafusion.os, "_exit", lambda code: exits.append(code))
    metafusion.shutdown_complete.set()
    try:
        metafusion._force_exit_after_timeout(0)
    finally:
        metafusion.shutdown_complete.clear()

    assert exits == []


def test_status_command_does_not_require_connector_configuration(
    monkeypatch, tmp_path, capsys
):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"state": "idle"}), encoding="utf-8")
    monkeypatch.setenv("STATUS_FILE", str(status_path))
    monkeypatch.setattr(metafusion, "retry_queue_summary", lambda **_kwargs: {})

    assert metafusion.main(["--status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "state": "idle",
        "retry_queue": {},
    }


def test_scheduler_run_on_start_executes_before_wait_loop(monkeypatch, tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(
        {"run_on_start": True, "schedule": True, "run_times": ["23:59"]}
    )
    config["metafusion_run"] = False
    calls = []

    class Status:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self, _mode):
            pass

        def idle(self):
            pass

        def stopping(self):
            pass

        def stop(self):
            pass

    def run_job(*_args, **_kwargs):
        calls.append("run")
        metafusion.shutdown_requested.set()
        return True

    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_runtime_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion, "get_setup_logging", lambda _config: logging.getLogger("run-on-start")
    )
    monkeypatch.setattr(metafusion, "RuntimeStatus", Status)
    monkeypatch.setattr(metafusion, "run_metafusion_job", run_job)
    metafusion.schedule.clear()
    try:
        assert metafusion.main([]) == 0
    finally:
        metafusion.schedule.clear()

    assert calls == ["run"]


def test_schedule_catch_up_uses_durable_success_history():
    now = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
    missed = metafusion.missed_schedule_due(
        ["06:00", "18:30"], [], max_hours=24, now=now
    )
    assert missed == datetime(2026, 1, 2, 6, 0, tzinfo=timezone.utc)

    completed = [
        {
            "status": "success",
            "finished_at": "2026-01-02T06:30:00+00:00",
        }
    ]
    assert metafusion.missed_schedule_due(
        ["06:00", "18:30"], completed, max_hours=24, now=now
    ) is None
    assert metafusion.missed_schedule_due(
        ["06:00"], [], max_hours=0.5, now=now
    ) is None


def test_shutdown_watchdog_forces_bounded_exit(monkeypatch):
    exits = []
    monkeypatch.setattr(metafusion.os, "_exit", lambda code: exits.append(code))
    metafusion.shutdown_complete.clear()

    metafusion._force_exit_after_timeout(0)

    assert exits == [128 + metafusion.signal.SIGTERM]


def test_signal_handlers_are_installed_before_runtime_status_is_published(
    monkeypatch, tmp_path
):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["metafusion_run"] = False
    config["settings"].update({"schedule": False, "run_times": []})
    handlers_seen = []
    previous_term_handler = metafusion.signal.getsignal(metafusion.signal.SIGTERM)

    class Status:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self, _mode):
            handlers_seen.append(
                metafusion.signal.getsignal(metafusion.signal.SIGTERM)
            )

        def stopping(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_runtime_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion,
        "get_setup_logging",
        lambda _config: logging.getLogger("signal-order"),
    )
    monkeypatch.setattr(metafusion, "RuntimeStatus", Status)

    assert metafusion.main([]) == 0
    assert handlers_seen == [metafusion.request_shutdown]
    assert (
        metafusion.signal.getsignal(metafusion.signal.SIGTERM)
        is previous_term_handler
    )


def test_idle_scheduler_stops_promptly_on_sigterm(tmp_path):
    repo_root = Path(__file__).parents[1]
    config_dir = tmp_path / "config"
    status_file = config_dir / "status.json"
    environment = os.environ.copy()
    for env_name, _path, _converter in ENV_BINDINGS:
        environment.pop(env_name, None)
    for env_name, _path, _direct in SECRET_FILE_BINDINGS:
        environment.pop(env_name, None)
    environment.update(
        {
            "CONFIG_DIR": str(config_dir),
            "STATUS_FILE": str(status_file),
            "KOMETA_PATH": str(tmp_path / "kometa"),
            "PLEX_URL": "http://plex:32400",
            "PLEX_TOKEN": "test-token",
            "TMDB_API_KEY": "test-key",
            "METAFUSION_RUN": "false",
            "RUN_SCHEDULE": "true",
            "SCHEDULE_CATCH_UP": "false",
            "RUN_TIMES": "23:59",
            "SHUTDOWN_TIMEOUT": "2",
            "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache"),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "metafusion.py"],
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not status_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not status_file.exists():
            process.terminate()
            output, _ = process.communicate(timeout=3)
            pytest.fail(f"scheduler did not start: {output}")

        started = time.monotonic()
        process.terminate()
        process.wait(timeout=3)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)

    assert process.returncode == 0
    assert elapsed < 3
