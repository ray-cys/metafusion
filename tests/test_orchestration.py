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
from types import SimpleNamespace

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


def test_version_command_reports_runtime_and_database_schema(capsys):
    with pytest.raises(SystemExit, match="0"):
        metafusion.parse_cli_args(["--version"])

    output = " ".join(capsys.readouterr().out.split())
    assert output.startswith("MetaFusion ")
    assert "Python " in output
    assert "architecture " in output
    assert "state schema " in output
    assert "TMDb cache schema " in output


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (["--metadata-only", "--asset-only"], "cannot be combined"),
        (["--metadata-audit", "--asset-audit"], "cannot be combined"),
        (
            ["--plex-metadata-restore", "--plex-metadata-unlock", "--rating-key", "1"],
            "choose only one",
        ),
        (["--plex-metadata-restore"], "requires --rating-key"),
    ],
)
def test_main_rejects_conflicting_cli_actions(arguments, expected, capsys):
    assert metafusion.main(arguments) == 2
    assert expected in capsys.readouterr().err


def test_release_check_passes_and_does_not_create_config(monkeypatch, tmp_path, capsys):
    config = {
        "settings": {"mode": "kometa", "dry_run": False},
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {"run_poster": True, "run_season": True, "run_background": True},
        "cleanup": {"run_cleanup": False},
        "plex_metadata": {"enabled": False},
        "plex_libraries": ["Movies"],
    }
    load_calls = []
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **kwargs: (load_calls.append(kwargs) or (config, {})),
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def preflight(_config):
        return {"available_count": 1, "path_advice": {"records": []}}

    monkeypatch.setattr(metafusion, "connector_preflight", preflight)
    monkeypatch.setattr(
        metafusion,
        "write_release_qualification_report",
        lambda *_args: (tmp_path / "qualification.txt", True),
    )

    assert metafusion.main(["--release-check"]) == 0
    output = capsys.readouterr().out
    assert "qualification passed" in output
    assert "qualification.txt" in output
    assert load_calls == [{"create_if_missing": False, "return_sources": True}]


def test_release_check_returns_failure_and_redacts_connector_secrets(
    monkeypatch, capsys
):
    config = {
        "settings": {"mode": "plex"},
        "plex": {"token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
        "metadata": {},
        "assets": {},
        "cleanup": {},
        "plex_metadata": {},
        "plex_libraries": [],
    }
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def failed_preflight(_config):
        raise RuntimeError("connector plex-secret tmdb-secret")

    monkeypatch.setattr(metafusion, "connector_preflight", failed_preflight)

    assert metafusion.main(["--release-check"]) == 1
    error = capsys.readouterr().err
    assert "Release qualification failed" in error
    assert "plex-secret" not in error
    assert "tmdb-secret" not in error


def test_release_check_returns_report_failure(monkeypatch, tmp_path, capsys):
    config = {
        "settings": {"mode": "kometa"},
        "plex": {},
        "tmdb": {},
        "metadata": {},
        "assets": {},
        "cleanup": {},
        "plex_metadata": {},
        "plex_libraries": [],
    }
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def preflight(_config):
        return {}

    monkeypatch.setattr(metafusion, "connector_preflight", preflight)
    monkeypatch.setattr(
        metafusion,
        "write_release_qualification_report",
        lambda *_args: (tmp_path / "qualification.txt", False),
    )

    assert metafusion.main(["--release-check"]) == 1
    assert "qualification failed" in capsys.readouterr().out


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


def test_cli_override_applies_all_independent_switches_and_unlock_action():
    args = metafusion.parse_cli_args(
        [
            "--metafusion_run",
            "--schedule",
            "--run_times",
            "01:00, 02:30",
            "--dry_run",
            "--mode",
            "plex",
            "--run_basic",
            "--run_enhanced",
            "--run_poster",
            "--run_season",
            "--run_background",
            "--library",
            "Movies",
            "--rating-key",
            "10,11",
            "--full-scan",
            "--plex-metadata-unlock",
        ]
    )
    config = {
        "metafusion_run": False,
        "settings": {"schedule": False, "run_times": [], "dry_run": False, "mode": "kometa"},
        "metadata": {"run_basic": False, "run_enhanced": False},
        "assets": {"run_poster": False, "run_season": False, "run_background": False},
        "cleanup": {"run_cleanup": True},
        "plex_metadata": {"enabled": False},
        "plex_libraries": [],
    }

    metafusion.override_config_with_cli(config, args)

    assert config["metafusion_run"] is True
    assert config["settings"] == {
        "schedule": True,
        "run_times": ["01:00", "02:30"],
        "dry_run": True,
        "mode": "plex",
    }
    assert all(config["metadata"].values())
    assert all(config["assets"].values())
    assert config["plex_metadata"]["enabled"] is True
    assert config["_execution"]["rating_keys"] == ["10", "11"]
    assert config["_execution"]["plex_metadata_maintenance"] == "unlock"


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


def test_connector_preflight_handles_optional_tmdb_and_connector_failures(monkeypatch):
    plex = object()
    monkeypatch.setattr(metafusion, "connect_plex_server", lambda _config: plex)
    assert asyncio.run(
        metafusion.preflight_connectors({}, object(), require_tmdb=False)
    ) is plex

    def fail_plex(_config):
        raise OSError("Plex failed")

    async def valid_tmdb(*_args, **_kwargs):
        return {"images": {}}

    monkeypatch.setattr(metafusion, "connect_plex_server", fail_plex)
    monkeypatch.setattr(metafusion, "tmdb_api_request", valid_tmdb)
    with pytest.raises(RuntimeError, match="Plex connector"):
        asyncio.run(metafusion.preflight_connectors({}, object()))

    monkeypatch.setattr(metafusion, "connect_plex_server", lambda _config: plex)

    async def fail_tmdb(*_args, **_kwargs):
        raise OSError("TMDb failed")

    monkeypatch.setattr(metafusion, "tmdb_api_request", fail_tmdb)
    with pytest.raises(RuntimeError, match="TMDb connector"):
        asyncio.run(metafusion.preflight_connectors({}, object()))

    async def empty_tmdb(*_args, **_kwargs):
        return None

    monkeypatch.setattr(metafusion, "tmdb_api_request", empty_tmdb)
    with pytest.raises(RuntimeError, match="returned no configuration"):
        asyncio.run(metafusion.preflight_connectors({}, object()))


def test_library_type_scope_and_schedule_edge_cases():
    plex = SimpleNamespace(machineIdentifier="server")
    sections = [
        SimpleNamespace(title="Movies", type="movies", uuid="movie-uuid"),
        SimpleNamespace(title="Shows", TYPE="shows", key="show-key"),
    ]
    scopes = metafusion.build_scan_scopes(plex, sections, copy.deepcopy(DEFAULT_CONFIG))

    assert [scope["library_uuid"] for scope in scopes] == ["movie-uuid", "show-key"]
    assert metafusion.normalize_library_type("MOVIES") == "movie"
    assert metafusion.normalize_library_type("Shows") == "tv"
    assert metafusion.normalize_library_type("music") == "music"

    naive_now = datetime(2026, 1, 2, 7, 0)  # noqa: DTZ001 -- exercises normalization
    recent = [
        {"status": "failed", "finished_at": "2026-01-02T06:30:00"},
        {"status": "success", "finished_at": "invalid"},
        {"status": "success", "finished_at": "2026-01-02T06:30:00"},
    ]
    assert metafusion.missed_schedule_due(
        [None, "invalid"], recent, now=naive_now
    ) is None
    assert metafusion.missed_schedule_due(
        ["06:00"], recent, now=naive_now
    ) is None


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


def test_plex_metadata_maintenance_uses_plex_only_and_selected_rating_keys(
    monkeypatch, tmp_path
):
    item = SimpleNamespace(
        title="Example", year=2020, type="movie", ratingKey="10"
    )
    section = Section("Movies", "movie", [item])
    section.uuid = "library"
    plex = SimpleNamespace(machineIdentifier="server")
    calls = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def fake_preflight(_config, _session, require_tmdb=True):
        calls.append(("preflight", require_tmdb))
        return plex

    async def plex_call(call, *_args, **_kwargs):
        return call()

    async def metadata(*_args, **_kwargs):
        return {
            "title": "Example",
            "year": 2020,
            "library_type": "movie",
            "ratingKey": "10",
        }

    async def restore(*_args, **kwargs):
        calls.append(("restore", kwargs["unlock_only"]))
        return {"writes": 1, "failures": 0}

    monkeypatch.setattr(metafusion, "get_meta_banner", lambda *_args: None)
    monkeypatch.setattr(metafusion, "check_sys_requirements", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(metafusion, "get_disabled_features", lambda *_args: None)
    monkeypatch.setattr(
        metafusion,
        "get_feature_flags",
        lambda _config: {"dry_run": False, "cleanup": False},
    )
    monkeypatch.setattr(metafusion, "preflight_connectors", fake_preflight)
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config, plex=None: (
            [section],
            ["Movies"],
            [{"title": "Movies", "type": "movie"}],
        ),
    )
    monkeypatch.setattr(metafusion, "library_full_scan_decisions", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(metafusion, "plex_operation", plex_call)
    monkeypatch.setattr(metafusion, "get_plex_metadata", metadata)
    monkeypatch.setattr(metafusion, "restore_plex_metadata", restore)
    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())

    config = {
        "settings": {"mode": "plex", "path": str(tmp_path)},
        "runtime": {},
        "plex": {},
        "_execution": {
            "plex_metadata_maintenance": "unlock",
            "rating_keys": ["10"],
            "targeted": True,
        },
    }
    asyncio.run(metafusion.metafusion_main(config, logging.getLogger("maintenance")))

    assert calls == [("preflight", False), ("restore", True)]


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


def test_status_command_handles_invalid_file_and_state_database_errors(
    monkeypatch, tmp_path, capsys
):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("STATUS_FILE", str(missing))
    assert metafusion.main(["--status"]) == 1
    assert "Unable to read runtime status" in capsys.readouterr().err

    status_path = tmp_path / "status.json"
    status_path.write_text('{"state": "idle"}', encoding="utf-8")
    monkeypatch.setenv("STATUS_FILE", str(status_path))
    monkeypatch.setattr(
        metafusion,
        "recent_job_runs",
        lambda **_kwargs: (_ for _ in ()).throw(
            metafusion.StateDatabaseError("history unavailable")
        ),
    )
    monkeypatch.setattr(
        metafusion,
        "retry_queue_summary",
        lambda **_kwargs: (_ for _ in ()).throw(
            metafusion.StateDatabaseError("retry unavailable")
        ),
    )
    assert metafusion.main(["--status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["history_error"] == "history unavailable"
    assert result["retry_queue_error"] == "retry unavailable"


def test_doctor_reports_sources_validation_and_load_errors(monkeypatch, capsys):
    config = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (config, {("settings", "mode"): "environment"}),
    )
    monkeypatch.setattr(
        metafusion,
        "config_source_report",
        lambda *_args: ["settings.mode: environment"],
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    assert metafusion.main(["--doctor"]) == 0
    output = capsys.readouterr().out
    assert "settings.mode: environment" in output
    assert "Configuration is valid" in output

    monkeypatch.setattr(
        metafusion, "validate_config", lambda _config: ["PLEX_TOKEN is required"]
    )
    assert metafusion.main(["--doctor"]) == 2
    assert "PLEX_TOKEN is required" in capsys.readouterr().err

    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (_ for _ in ()).throw(metafusion.ConfigError("bad YAML")),
    )
    assert metafusion.main(["--doctor"]) == 2
    assert "bad YAML" in capsys.readouterr().err


def test_support_report_success_and_write_failure(monkeypatch, tmp_path, capsys):
    config = copy.deepcopy(DEFAULT_CONFIG)
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: ["ignored"])
    report = tmp_path / "support.txt"
    monkeypatch.setattr(
        metafusion, "write_support_report", lambda *_args: report
    )
    assert metafusion.main(["--support-report"]) == 0
    assert str(report) in capsys.readouterr().out

    monkeypatch.setattr(
        metafusion,
        "write_support_report",
        lambda *_args: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert metafusion.main(["--support-report"]) == 1
    assert "read-only" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path_advice, expected",
    [
        ({"suggestions": ["/host=>/media"], "records": []}, "Suggested PLEX_PATH_MAPPINGS"),
        ({"suggestions": [], "records": [{"status": "visible"}]}, "visible inside the container"),
        (
            {"suggestions": [], "records": [{"status": "unresolved"}]},
            "mapping needs attention for 1 sample",
        ),
    ],
)
def test_preflight_reports_path_mapping_outcomes(
    monkeypatch, path_advice, expected, capsys
):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"]["token"] = "plex-secret"
    config["tmdb"]["api_key"] = "tmdb-secret"
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def preflight(_config):
        return {
            "plex_version": "1.0",
            "libraries": ["Movies"],
            "available_count": 1,
            "library_discovery": "auto",
            "path_advice": path_advice,
        }

    monkeypatch.setattr(metafusion, "connector_preflight", preflight)
    assert metafusion.main(["--preflight"]) == 0
    output = capsys.readouterr().out
    assert expected in output
    assert "Library selection: auto" in output


def test_preflight_failure_redacts_secrets(monkeypatch, capsys):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["plex"]["token"] = "plex-secret"
    config["tmdb"]["api_key"] = "tmdb-secret"
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (config, {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def failure(_config):
        raise RuntimeError("plex-secret tmdb-secret")

    monkeypatch.setattr(metafusion, "connector_preflight", failure)
    assert metafusion.main(["--preflight"]) == 1
    error = capsys.readouterr().err
    assert "plex-secret" not in error
    assert "tmdb-secret" not in error


def test_main_rejects_regular_validation_errors(monkeypatch, capsys):
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (copy.deepcopy(DEFAULT_CONFIG), {}),
    )
    monkeypatch.setattr(
        metafusion, "validate_config", lambda _config: ["invalid one", "invalid two"]
    )
    assert metafusion.main([]) == 2
    error = capsys.readouterr().err
    assert "invalid one" in error
    assert "invalid two" in error


def test_main_oneshot_failure_and_invalid_scheduler_time(monkeypatch):
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

    base = copy.deepcopy(DEFAULT_CONFIG)
    base["metafusion_run"] = True
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_runtime_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion, "get_setup_logging", lambda _config: logging.getLogger("phase21")
    )
    monkeypatch.setattr(metafusion, "RuntimeStatus", Status)
    monkeypatch.setattr(metafusion, "run_metafusion_job", lambda *_args: False)
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (base, {})
    )
    assert metafusion.main([]) == 1

    scheduler = copy.deepcopy(DEFAULT_CONFIG)
    scheduler["metafusion_run"] = False
    scheduler["settings"].update(
        {
            "schedule": True,
            "run_times": ["invalid"],
            "run_on_start": False,
            "schedule_catch_up": False,
        }
    )
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (scheduler, {})
    )
    metafusion.schedule.clear()
    try:
        assert metafusion.main([]) == 1
    finally:
        metafusion.schedule.clear()


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
