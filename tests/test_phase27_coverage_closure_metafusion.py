import asyncio
import copy
import logging
import runpy
import sys
from datetime import datetime, timezone

import pytest
from test_lifecycle_management import FakeLock, cli_args, operator_config
from test_phase26_orchestration_matrix import (
    _config,
    _item,
    _patch_runtime,
    _record,
    _Section,
)

import metafusion
from helper.config import DEFAULT_CONFIG


def _orchestration_defaults(monkeypatch, *, full_scan=False):
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
        "library_full_scan_decisions",
        lambda *_args, **_kwargs: {("server", "uuid-Movies"): full_scan},
    )
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *_args, **_kwargs: {"status": "disabled"},
    )


def test_metafusion_incremental_cleanup_reason(monkeypatch, tmp_path):
    section = _Section(items=[_item()])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": [_record()]},
    )
    _orchestration_defaults(monkeypatch, full_scan=False)

    async def process(**kwargs):
        kwargs["metadata_summaries"]["Movies"] = {
            "total_items": 1,
            "library_summary": {"item_failures": 0},
        }

    monkeypatch.setattr(metafusion, "process_library", process)
    config = _config(tmp_path)
    asyncio.run(metafusion.metafusion_main(config, logging.getLogger("incremental")))
    assert config["_cleanup_result"].skipped_reason.startswith("incremental run")


def test_metafusion_inventory_failure_skips_failed_section(monkeypatch, tmp_path):
    failed = _Section(items=[_item("10")])
    healthy = _Section("Shows", "show", [_item("20", "show")])
    _patch_runtime(
        monkeypatch,
        [failed, healthy],
        ["Movies", "Shows"],
        [
            {"title": "Movies", "type": "movie"},
            {"title": "Shows", "type": "show"},
        ],
    )
    _orchestration_defaults(monkeypatch, full_scan=True)

    async def inventory(section, _runtime, records_only=False):
        if records_only and section.title == "Movies":
            raise RuntimeError("inventory unavailable")
        if records_only:
            return [_record("20", "show")]
        return list(section.all())

    processed = []

    async def process(**kwargs):
        processed.append(kwargs["library_section"].title)

    monkeypatch.setattr(metafusion, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(metafusion, "process_library", process)
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        asyncio.run(
            metafusion.metafusion_main(
                _config(tmp_path), logging.getLogger("inventory-failure")
            )
        )
    assert processed == ["Shows"]


@pytest.mark.parametrize("failure_stage", ["inventory", "processing"])
def test_metafusion_cancellation_is_never_aggregated(
    monkeypatch, tmp_path, failure_stage
):
    section = _Section(items=[_item()])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": [_record()]},
    )
    _orchestration_defaults(monkeypatch, full_scan=True)

    if failure_stage == "inventory":

        async def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(metafusion, "load_plex_library_inventory", cancelled)
    else:

        async def cancelled(**_kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(metafusion, "process_library", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            metafusion.metafusion_main(
                _config(tmp_path), logging.getLogger(f"cancel-{failure_stage}")
            )
        )


def _maintenance_config(tmp_path, *, mode="plex", keys=None):
    config = _config(
        tmp_path,
        plex_metadata_maintenance="restore",
        rating_keys=list(keys or []),
        targeted=True,
    )
    config["settings"]["mode"] = mode
    return config


def _patch_maintenance(monkeypatch, section, records):
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": records},
    )
    _orchestration_defaults(monkeypatch, full_scan=True)


def test_plex_maintenance_rejects_wrong_mode_and_empty_target(monkeypatch, tmp_path):
    section = _Section(items=[_item()])
    _patch_maintenance(monkeypatch, section, [_record()])
    with pytest.raises(RuntimeError, match="RUN_MODE=plex"):
        asyncio.run(
            metafusion.metafusion_main(
                _maintenance_config(tmp_path, mode="kometa", keys=["10"]),
                logging.getLogger("maintenance-mode"),
            )
        )
    with pytest.raises(RuntimeError, match="requires --rating-key"):
        asyncio.run(
            metafusion.metafusion_main(
                _maintenance_config(tmp_path),
                logging.getLogger("maintenance-target"),
            )
        )


@pytest.mark.parametrize("restore_fails", [False, True])
def test_plex_maintenance_aggregates_missing_and_writer_failures(
    monkeypatch, tmp_path, restore_fails
):
    key = "10" if restore_fails else "11"
    section = _Section(items=[_item(key)])
    _patch_maintenance(monkeypatch, section, [_record(key)])

    async def metadata(*_args, **_kwargs):
        return {"ratingKey": key, "library_type": "movie"}

    async def restore(*_args, **_kwargs):
        return {"failures": 1 if restore_fails else 0}

    monkeypatch.setattr(metafusion, "get_plex_metadata", metadata)
    monkeypatch.setattr(metafusion, "restore_plex_metadata", restore)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(
            metafusion.metafusion_main(
                _maintenance_config(tmp_path, keys=["10"]),
                logging.getLogger("maintenance-failure"),
            )
        )
    expected = "Movies/10" if restore_fails else "rating keys not found: 10"
    assert expected in str(caught.value)


def test_operator_validation_and_protected_output(monkeypatch, tmp_path):
    config = operator_config(tmp_path)
    monkeypatch.setattr(metafusion, "JobRunLock", FakeLock)
    monkeypatch.setattr(
        metafusion,
        "_state_target",
        lambda *_args, **_kwargs: {
            "server_id": "server",
            "library_uuid": "library",
            "library_name": "Movies",
            "rating_key": "10",
            "media_type": "movie",
        },
    )
    with pytest.raises(ValueError, match="season-number"):
        metafusion._handle_operator_command(
            cli_args(exception_action="add", exception_output="season"), config
        )
    with pytest.raises(ValueError, match="exactly one"):
        metafusion._handle_operator_command(
            cli_args(identity_override_action="set", tmdb_id=[]), config
        )
    with pytest.raises(ValueError, match="from-library"):
        metafusion._handle_operator_command(
            cli_args(library_rebind="plan", from_library="Old"), config
        )

    monkeypatch.setattr(
        metafusion,
        "plan_output_management",
        lambda *_args, **_kwargs: [{"status": "protected"}],
    )
    monkeypatch.setattr(
        metafusion,
        "apply_output_management",
        lambda *_args, **_kwargs: [{"status": "protected"}],
    )
    monkeypatch.setattr(
        metafusion,
        "write_output_management_report",
        lambda *_args, **_kwargs: tmp_path / "output.txt",
    )
    assert (
        metafusion._handle_operator_command(
            cli_args(output_action="remove", output_type="poster"), config
        )
        == 1
    )

    config["settings"]["mode"] = "plex"
    monkeypatch.setattr(
        metafusion, "plan_output_management", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        metafusion, "apply_output_management", lambda *_args, **_kwargs: []
    )
    assert (
        metafusion._handle_operator_command(
            cli_args(output_action="rebuild", output_type="metadata"), config
        )
        is None
    )
    assert config["plex_metadata"]["enabled"] is True


def _patch_cli_config(monkeypatch, tmp_path):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="plex", path=str(tmp_path))
    config["metafusion_run"] = False
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (copy.deepcopy(config), {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    return config


def test_cli_failure_and_standalone_validation_matrix(monkeypatch, tmp_path, capsys):
    _patch_cli_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metafusion,
        "_handle_sqlite_only_command",
        lambda _args: (_ for _ in ()).throw(metafusion.StateDatabaseError("state")),
    )
    assert metafusion.main(["--state-report"]) == 1
    monkeypatch.setattr(metafusion, "_handle_sqlite_only_command", lambda _args: 0)
    assert metafusion.main(["--state-report"]) == 0

    monkeypatch.setattr(
        metafusion,
        "load_unresolved_work",
        lambda **_kwargs: (_ for _ in ()).throw(metafusion.StateDatabaseError("ledger")),
    )
    assert metafusion.main(["--problems"]) == 1
    assert metafusion.main(["--metadata-audit", "--asset-only"]) == 2
    assert (
        metafusion.main(
            ["--mapping-diagnose", "--asset-audit", "--rating-key", "10"]
        )
        == 2
    )
    assert (
        metafusion.main(["--explain-item", "--full-scan", "--rating-key", "10"])
        == 2
    )
    assert "standalone" in capsys.readouterr().err


def test_operator_dispatch_catches_and_returns(monkeypatch, tmp_path):
    _patch_cli_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metafusion,
        "_handle_operator_command",
        lambda *_args: (_ for _ in ()).throw(ValueError("invalid operator")),
    )
    assert metafusion.main(["--exception-action", "list"]) == 1
    assert metafusion.main(["--plex-artwork-verify"]) == 1

    monkeypatch.setattr(metafusion, "_handle_operator_command", lambda *_args: 0)
    assert metafusion.main(["--exception-action", "list"]) == 0
    assert metafusion.main(["--plex-artwork-verify"]) == 0


@pytest.mark.parametrize(
    ("arguments", "attribute", "message"),
    [
        (
            ["--capture-replay", "--rating-key", "10"],
            "item_explanation_connectors",
            "Replay capture failed",
        ),
        (
            ["--explain-item", "--rating-key", "10"],
            "item_explanation_connectors",
            "Item explanation failed",
        ),
        (
            ["--compatibility-check"],
            "connector_preflight",
            "Compatibility check failed",
        ),
    ],
)
def test_diagnostic_connector_failures_are_redacted(
    monkeypatch, tmp_path, capsys, arguments, attribute, message
):
    _patch_cli_config(monkeypatch, tmp_path)
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def failed(*_args, **_kwargs):
        raise RuntimeError("connector failed")

    monkeypatch.setattr(metafusion, attribute, failed)
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)
    assert metafusion.main(arguments) == 1
    assert message in capsys.readouterr().err


class _Status:
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


def _patch_scheduler(monkeypatch, config):
    monkeypatch.setattr(
        metafusion, "load_config_file", lambda **_kwargs: (copy.deepcopy(config), {})
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_runtime_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion, "get_setup_logging", lambda _config: logging.getLogger("scheduler")
    )
    monkeypatch.setattr(metafusion, "RuntimeStatus", _Status)
    monkeypatch.setattr(metafusion.signal, "getsignal", lambda _signal: None)
    monkeypatch.setattr(metafusion.signal, "signal", lambda *_args: None)


def test_scheduler_catch_up_survives_history_failure(monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["metafusion_run"] = False
    config["settings"].update(
        schedule=True,
        run_times=["23:59"],
        run_on_start=False,
        schedule_catch_up=True,
    )
    _patch_scheduler(monkeypatch, config)
    monkeypatch.setattr(
        metafusion,
        "recent_job_runs",
        lambda **_kwargs: (_ for _ in ()).throw(metafusion.StateDatabaseError("history")),
    )
    monkeypatch.setattr(
        metafusion,
        "missed_schedule_due",
        lambda *_args, **_kwargs: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    def run(*_args, **_kwargs):
        metafusion.shutdown_requested.set()
        return True

    monkeypatch.setattr(metafusion, "run_metafusion_job", run)
    metafusion.schedule.clear()
    try:
        assert metafusion.main([]) == 0
    finally:
        metafusion.schedule.clear()


def test_scheduler_executes_pending_jobs_before_waiting(monkeypatch):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["metafusion_run"] = False
    config["settings"].update(
        schedule=True,
        run_times=["23:59"],
        run_on_start=False,
        schedule_catch_up=False,
    )
    _patch_scheduler(monkeypatch, config)
    pending = []

    class OneLoopEvent:
        def __init__(self):
            self.stopped = False

        def clear(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    monkeypatch.setattr(metafusion, "shutdown_requested", OneLoopEvent())
    monkeypatch.setattr(
        metafusion.schedule, "run_pending", lambda: pending.append("pending")
    )
    metafusion.schedule.clear()
    try:
        assert metafusion.main([]) == 0
    finally:
        metafusion.schedule.clear()
    assert pending == ["pending"]


def test_module_entrypoint_executes_main(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["metafusion.py", "--version"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(metafusion.__file__, run_name="__main__")
