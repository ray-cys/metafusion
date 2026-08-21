import asyncio
import copy
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import metafusion
from helper import concurrency, quarantine, run_history, state_db
from helper.config import DEFAULT_CONFIG
from helper.config_reload import (
    ScheduledConfigReloader,
    configuration_watch_signature,
)
from helper.io import sha256_file
from modules import cleanup as cleanup_module


def test_scheduled_config_reload_accepts_valid_and_retains_invalid(tmp_path):
    watched = tmp_path / "config.yml"
    watched.write_text("settings: {}\n", encoding="utf-8")
    current = copy.deepcopy(DEFAULT_CONFIG)
    candidate = copy.deepcopy(DEFAULT_CONFIG)
    candidate["settings"]["run_times"] = ["07:35"]
    loaded = []
    path_checks = []

    reloader = ScheduledConfigReloader(
        tmp_path,
        lambda: loaded.append(True) or copy.deepcopy(candidate),
        lambda config: [],
        lambda config: path_checks.append(config["settings"]["run_times"]),
        environ={},
    )
    unchanged, changed, error = reloader.reload_if_changed(current)
    assert unchanged is current and changed is False and error is None

    watched.write_text("settings:\n  run_times: ['07:35']\n", encoding="utf-8")
    accepted, changed, error = reloader.reload_if_changed(current)
    assert changed is True and error is None
    assert accepted["settings"]["run_times"] == ["07:35"]
    assert loaded and path_checks == [["07:35"]]

    reloader.validator = lambda config: ["invalid schedule"]
    watched.write_text("settings:\n  run_times: ['bad']\n", encoding="utf-8")
    retained, changed, error = reloader.reload_if_changed(accepted)
    assert retained is accepted and changed is False
    assert error == "invalid schedule"


def test_scheduled_config_reload_watches_secrets_disable_and_loader_errors(tmp_path):
    token = tmp_path / "plex-token"
    token.write_text("one", encoding="utf-8")
    environ = {"PLEX_TOKEN_FILE": str(token), "TMDB_API_KEY_FILE": "  "}
    signature = configuration_watch_signature(tmp_path, environ)
    assert any(row[0] == str(token) and row[1] for row in signature)
    assert any(row[0].endswith("config.yml") and not row[1] for row in signature)

    current = copy.deepcopy(DEFAULT_CONFIG)
    current["runtime"]["config_reload"] = False
    reloader = ScheduledConfigReloader(
        tmp_path,
        lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
        lambda _config: [],
        lambda _config: None,
        environ=environ,
    )
    assert reloader.reload_if_changed(current) == (current, False, None)

    current["runtime"]["config_reload"] = True
    token.write_text("two", encoding="utf-8")
    retained, changed, error = reloader.reload_if_changed(current)
    assert retained is current and changed is False
    assert error == "reload failed"


def test_run_history_capacity_analysis_and_paired_report(monkeypatch, tmp_path):
    records = [
        {
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:10:00+00:00",
            "status": "success",
            "library_results": {"Movies": {"status": "success"}},
            "metrics": {"elapsed_seconds": 600, "items_per_minute": 120},
        },
        {
            "started_at": "2026-01-02T00:00:00+00:00",
            "finished_at": "2026-01-02T00:20:00+00:00",
            "status": "failed",
            "library_results": {"TV Shows": {"status": "failed"}},
            "metrics": {"elapsed_seconds": 1200, "items_per_minute": 60},
        },
    ]
    monkeypatch.setattr(run_history, "recent_job_runs", lambda limit: records)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"]["run_times"] = ["00:00", "00:15"]

    analysis = run_history.analyze_run_history(config)
    assert analysis["summary"]["retained_runs"] == 2
    assert analysis["summary"]["p95_seconds"] == 1200
    assert any("95th-percentile" in value for value in analysis["advice"])
    report = run_history.write_run_history_report(config, base_dir=tmp_path)
    assert report.exists()
    assert report.with_suffix(".json").exists()


def test_run_history_handles_sparse_history_and_all_advice_branches(
    monkeypatch, tmp_path
):
    assert run_history._duration({}) == 0.0
    assert run_history._duration(
        {
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
        }
    ) == 60.0
    assert run_history._percentile([], 0.95) == 0.0
    assert run_history._schedule_spacing(["bad", None]) is None
    assert run_history._schedule_spacing(["12:00"]) == 24 * 60

    monkeypatch.setattr(run_history, "recent_job_runs", lambda limit: [])
    empty = run_history.analyze_run_history({"settings": {"run_times": []}})
    assert empty["advice"] == [
        "No durable jobs are available yet; run MetaFusion before assessing capacity."
    ]
    advice_report = run_history.write_run_history_report(
        {"settings": {"run_times": []}}, advice_only=True, base_dir=tmp_path
    )
    assert advice_report.name.startswith("schedule-advice-")

    records = []
    for index, throughput in enumerate((100, 100, 50), start=1):
        records.append(
            {
                "started_at": f"2026-01-0{index}T00:00:00+00:00",
                "finished_at": f"2026-01-0{index}T00:01:00+00:00",
                "status": "success",
                "metrics": {"items_per_minute": throughput},
            }
        )
    monkeypatch.setattr(run_history, "recent_job_runs", lambda limit: records)
    regressed = run_history.analyze_run_history(
        {"settings": {"run_times": ["00:00", "12:00"]}}
    )
    assert any("30% below" in value for value in regressed["advice"])
    assert any("fits within" in value for value in regressed["advice"])

    monkeypatch.setattr(run_history, "recent_job_runs", lambda limit: records[:1])
    healthy = run_history.analyze_run_history({"settings": {"run_times": []}})
    assert healthy["advice"] == [
        "No schedule-capacity or retained-run regression warning was detected."
    ]


def test_state_persists_metrics_provider_health_and_quarantine(tmp_path):
    database = tmp_path / "meta.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_db.record_job_run(
        "scheduler",
        now.isoformat(),
        now.isoformat(),
        "success",
        metrics={"elapsed_seconds": 12.5, "items_per_minute": 20},
        path=database,
    )
    assert state_db.recent_job_runs(path=database)[0]["metrics"][
        "elapsed_seconds"
    ] == 12.5

    state_db.save_provider_health(
        "tmdb:hashed",
        "tmdb",
        consecutive_failures=2,
        cooldown_seconds=30,
        path=database,
        now=now,
    )
    health = state_db.load_provider_health(
        ["tmdb:hashed"], path=database, now=now
    )["tmdb:hashed"]
    assert health["cooldown_seconds"] == 30
    assert health["consecutive_failures"] == 2

    history_id = state_db.record_cleanup_quarantine(
        {"title": "Example", "rating_key": "10", "media_type": "movie"},
        output_type="poster",
        source_path="/media/Example/poster.jpg",
        quarantine_path="/config/quarantine/cleanup/example.jpg",
        checksum="a" * 64,
        size_bytes=123,
        path=database,
        now=now,
    )
    rows = state_db.load_cleanup_quarantine(path=database)
    assert rows[0]["history_id"] == history_id
    assert rows[0]["status"] == "active"
    assert state_db.load_cleanup_quarantine(
        statuses=["active", ""],
        history_id=history_id,
        expired_before=now + timedelta(days=15),
        path=database,
    )[0]["history_id"] == history_id
    assert state_db.complete_cleanup_quarantine(
        history_id, "restore", path=database, now=now
    )
    assert not state_db.complete_cleanup_quarantine(
        9999, "purge", path=database, now=now
    )
    with pytest.raises(state_db.StateDatabaseError, match="unsupported"):
        state_db.complete_cleanup_quarantine(
            history_id, "invalid", path=database, now=now
        )


def test_provider_health_handles_empty_missing_legacy_and_expired_rows(tmp_path):
    assert state_db.load_provider_health([], path=tmp_path / "unused.sqlite3") == {}
    assert (
        state_db.load_provider_health(
            ["tmdb:key"], path=tmp_path / "missing.sqlite3"
        )
        == {}
    )

    legacy = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(legacy)
    connection.execute("PRAGMA user_version = 1")
    connection.execute("CREATE TABLE placeholder(value TEXT)")
    connection.commit()
    connection.close()
    assert state_db.load_provider_health(["tmdb:key"], path=legacy) == {}

    database = tmp_path / "health.sqlite3"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state_db.save_provider_health(
        "tmdb:key",
        "tmdb",
        consecutive_failures=3,
        cooldown_seconds=10,
        path=database,
        now=now,
    )
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE provider_health SET open_until='not-a-time' WHERE provider_key='tmdb:key'"
    )
    connection.commit()
    connection.close()
    invalid = state_db.load_provider_health(
        ["tmdb:key"], path=database, now=now
    )["tmdb:key"]
    assert invalid["cooldown_seconds"] == 0
    assert invalid["consecutive_failures"] == 0

    state_db.save_provider_health(
        "plex:key",
        "plex",
        consecutive_failures=2,
        cooldown_seconds=5,
        path=database,
        now=now,
    )
    expired = state_db.load_provider_health(
        ["plex:key"], path=database, now=now + timedelta(seconds=6)
    )["plex:key"]
    assert expired["cooldown_seconds"] == 0
    assert expired["consecutive_failures"] == 0
    state_db.save_provider_health(
        "plex:key", "plex", successful=True, path=database, now=now
    )


def test_cleanup_quarantine_reader_handles_legacy_database(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 1")
    connection.execute("CREATE TABLE placeholder(value TEXT)")
    connection.commit()
    connection.close()
    assert state_db.load_cleanup_quarantine(path=database) == []


def test_quarantine_moves_and_restores_checksum_proven_artwork(
    monkeypatch, tmp_path
):
    kometa = tmp_path / "kometa"
    source = kometa / "assets" / "movie" / "Example" / "poster.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"managed-artwork")
    checksum = sha256_file(source)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(kometa))
    config["cleanup"]["quarantine_days"] = 14
    recorded = []
    monkeypatch.setattr(
        quarantine,
        "record_cleanup_quarantine",
        lambda *args, **kwargs: recorded.append((args, kwargs)) or 42,
    )

    history_id = quarantine.quarantine_managed_asset(
        config,
        source,
        {"title": "Example", "rating_key": "10", "media_type": "movie"},
        output_type="poster",
        checksum=checksum,
        base_dir=tmp_path / "config",
    )
    assert history_id == 42 and not source.exists()
    stored = next((tmp_path / "config" / "quarantine" / "cleanup").iterdir())
    assert sha256_file(stored) == checksum

    row = {
        "history_id": 42,
        "status": "active",
        "source_path": str(source),
        "quarantine_path": str(stored),
        "checksum": checksum,
        "output_type": "poster",
    }
    completed = []
    monkeypatch.setattr(
        quarantine, "load_cleanup_quarantine", lambda **kwargs: [row]
    )
    monkeypatch.setattr(
        quarantine,
        "complete_cleanup_quarantine",
        lambda *args, **kwargs: completed.append((args, kwargs)) or True,
    )
    restored = quarantine.restore_quarantined_asset(
        config, 42, base_dir=tmp_path / "config"
    )
    assert restored["status"] == "restored"
    assert source.read_bytes() == b"managed-artwork"
    assert not stored.exists() and completed


def test_quarantine_rejects_unsafe_or_changed_sources(monkeypatch, tmp_path):
    kometa = tmp_path / "kometa"
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(kometa))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    with pytest.raises(quarantine.QuarantineError, match="outside"):
        quarantine.quarantine_managed_asset(
            config,
            outside,
            {},
            output_type="poster",
            checksum=sha256_file(outside),
            base_dir=tmp_path,
        )

    managed = kometa / "assets" / "movie" / "Example" / "poster.jpg"
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"managed")
    link = managed.with_name("link.jpg")
    link.symlink_to(managed)
    with pytest.raises(quarantine.QuarantineError, match="regular"):
        quarantine.quarantine_managed_asset(
            config,
            link,
            {},
            output_type="poster",
            checksum=sha256_file(managed),
            base_dir=tmp_path,
        )
    with pytest.raises(quarantine.QuarantineError, match="changed"):
        quarantine.quarantine_managed_asset(
            config,
            managed,
            {},
            output_type="poster",
            checksum="0" * 64,
            base_dir=tmp_path,
        )

    plex = copy.deepcopy(DEFAULT_CONFIG)
    plex["settings"]["mode"] = "plex"
    plex["plex"]["path_mappings"] = [
        f"/media => {tmp_path / 'plex-media'}",
        "invalid mapping",
        "/empty =>  ",
    ]
    assert quarantine._inside(
        tmp_path / "plex-media" / "Movie" / "poster.jpg",
        quarantine._managed_roots(plex),
    )

    monkeypatch.setattr(quarantine, "sha256_file", lambda _path: "wrong")
    with pytest.raises(quarantine.QuarantineError, match="checksum"):
        quarantine._copy_verified(managed, tmp_path / "copy.jpg", "expected")


def test_quarantine_rolls_back_database_and_restore_failures(monkeypatch, tmp_path):
    kometa = tmp_path / "kometa"
    source = kometa / "assets" / "movie" / "Example" / "poster"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"managed-artwork")
    checksum = sha256_file(source)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(kometa))
    monkeypatch.setattr(
        quarantine,
        "record_cleanup_quarantine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database")),
    )
    with pytest.raises(RuntimeError, match="database"):
        quarantine.quarantine_managed_asset(
            config,
            source,
            {},
            output_type="poster",
            checksum=checksum,
            base_dir=tmp_path,
        )
    assert source.read_bytes() == b"managed-artwork"
    assert not list((tmp_path / "quarantine" / "cleanup").iterdir())

    stored = tmp_path / "quarantine" / "cleanup" / "stored.jpg"
    stored.write_bytes(b"managed-artwork")
    source.unlink()
    row = {
        "history_id": 9,
        "status": "active",
        "source_path": str(source),
        "quarantine_path": str(stored),
        "checksum": checksum,
        "output_type": "poster",
    }
    monkeypatch.setattr(quarantine, "load_cleanup_quarantine", lambda **_kwargs: [row])
    monkeypatch.setattr(
        quarantine,
        "complete_cleanup_quarantine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("commit")),
    )
    with pytest.raises(RuntimeError, match="commit"):
        quarantine.restore_quarantined_asset(config, 9, base_dir=tmp_path)
    assert stored.read_bytes() == b"managed-artwork"
    assert not source.exists()


def test_restore_quarantine_rejects_each_unsafe_state(monkeypatch, tmp_path):
    kometa = tmp_path / "kometa"
    root = tmp_path / "quarantine" / "cleanup"
    root.mkdir(parents=True)
    source = kometa / "assets" / "movie" / "Example" / "poster.jpg"
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(kometa))

    monkeypatch.setattr(quarantine, "load_cleanup_quarantine", lambda **_kwargs: [])
    with pytest.raises(quarantine.QuarantineError, match="one active"):
        quarantine.restore_quarantined_asset(config, 1, base_dir=tmp_path)

    def rejected(row, message):
        monkeypatch.setattr(
            quarantine, "load_cleanup_quarantine", lambda **_kwargs: [row]
        )
        with pytest.raises(quarantine.QuarantineError, match=message):
            quarantine.restore_quarantined_asset(config, 1, base_dir=tmp_path)

    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"art")
    rejected(
        {
            "source_path": str(source),
            "quarantine_path": str(outside),
            "checksum": sha256_file(outside),
        },
        "outside the quarantine root",
    )

    stored = root / "stored.jpg"
    stored.write_bytes(b"art")
    rejected(
        {
            "source_path": str(tmp_path / "outside-destination.jpg"),
            "quarantine_path": str(stored),
            "checksum": sha256_file(stored),
        },
        "outside current managed roots",
    )

    source.parent.mkdir(parents=True)
    source.write_bytes(b"existing")
    rejected(
        {
            "source_path": str(source),
            "quarantine_path": str(stored),
            "checksum": sha256_file(stored),
        },
        "already exists",
    )
    source.unlink()
    missing = root / "missing.jpg"
    rejected(
        {
            "source_path": str(source),
            "quarantine_path": str(missing),
            "checksum": "0" * 64,
        },
        "missing or is not a regular file",
    )
    rejected(
        {
            "source_path": str(source),
            "quarantine_path": str(stored),
            "checksum": "0" * 64,
        },
        "checksum no longer matches",
    )


def test_purge_expired_quarantine_protects_and_completes_records(
    monkeypatch, tmp_path
):
    root = tmp_path / "quarantine" / "cleanup"
    root.mkdir(parents=True)
    unsafe = tmp_path / "unsafe.jpg"
    unsafe.write_bytes(b"unsafe")
    symlink_target = root / "target.jpg"
    symlink_target.write_bytes(b"target")
    symlink = root / "link.jpg"
    symlink.symlink_to(symlink_target)
    directory = root / "directory"
    directory.mkdir()
    valid = root / "valid.jpg"
    valid.write_bytes(b"valid")
    records = [
        {
            "history_id": 1,
            "quarantine_path": str(unsafe),
            "checksum": sha256_file(unsafe),
        },
        {
            "history_id": 2,
            "quarantine_path": str(symlink),
            "checksum": sha256_file(symlink_target),
        },
        {
            "history_id": 3,
            "quarantine_path": str(root / "absent.jpg"),
            "checksum": "0" * 64,
        },
        {
            "history_id": 4,
            "quarantine_path": str(directory),
            "checksum": "0" * 64,
        },
        {
            "history_id": 5,
            "quarantine_path": str(valid),
            "checksum": sha256_file(valid),
        },
    ]
    monkeypatch.setattr(
        quarantine, "load_cleanup_quarantine", lambda **_kwargs: records
    )
    completed = []
    monkeypatch.setattr(
        quarantine,
        "complete_cleanup_quarantine",
        lambda *args, **kwargs: completed.append((args, kwargs)) or True,
    )
    results = quarantine.purge_expired_quarantine(
        copy.deepcopy(DEFAULT_CONFIG), base_dir=tmp_path, source="manual"
    )
    assert [record["status"] for record in results] == [
        "protected",
        "protected",
        "missing",
        "protected",
        "purged",
    ]
    assert not valid.exists()
    assert [call[0][0] for call in completed] == [3, 5]
    assert completed[0][1]["status"] == "missing"


def test_quarantine_report_handles_empty_and_populated_history(monkeypatch, tmp_path):
    monkeypatch.setattr(quarantine, "load_cleanup_quarantine", lambda: [])
    empty = quarantine.write_quarantine_report(base_dir=tmp_path)
    assert "- none" in empty.read_text(encoding="utf-8")

    monkeypatch.setattr(
        quarantine,
        "load_cleanup_quarantine",
        lambda: [
            {
                "history_id": 1,
                "status": "active",
                "library_name": None,
                "cache_key": "movie:plex:1",
                "output_type": "poster",
                "expires_at": "2026-01-01T00:00:00+00:00",
                "size_bytes": 12,
            }
        ],
    )
    populated = quarantine.write_quarantine_report(base_dir=tmp_path)
    assert "movie:plex:1" in populated.read_text(encoding="utf-8")


def test_adaptive_provider_health_is_loaded_saved_and_failure_tolerant(
    monkeypatch, caplog
):
    resources = concurrency.RuntimeResources(4, 4 * 1024**3)
    config = {"tmdb": {"api_key": "secret"}, "plex": {"url": "http://plex"}}
    monkeypatch.setattr(
        concurrency,
        "load_provider_health",
        lambda keys: {
            next(key for key in keys if key.startswith("tmdb:")): {
                "consecutive_failures": 2,
                "cooldown_seconds": 5,
            }
        },
    )
    saved = []
    monkeypatch.setattr(
        concurrency,
        "save_provider_health",
        lambda *args, **kwargs: saved.append((args, kwargs)) or True,
    )

    controller, token = concurrency.begin_adaptive_concurrency(
        config,
        resources=resources,
        clock=lambda: 100.0,
        persist_provider_health=True,
    )
    assert controller.lane("tmdb").consecutive_failures == 2
    controller.lane("tmdb").successes = 1
    concurrency.finish_adaptive_concurrency(controller, token)
    assert saved and saved[0][0][1] == "tmdb"

    monkeypatch.setattr(
        concurrency,
        "load_provider_health",
        lambda _keys: (_ for _ in ()).throw(state_db.StateDatabaseError("read")),
    )
    monkeypatch.setattr(
        concurrency,
        "save_provider_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            state_db.StateDatabaseError("write")
        ),
    )
    with caplog.at_level(logging.WARNING):
        controller, token = concurrency.begin_adaptive_concurrency(
            config, persist_provider_health=True
        )
        controller.lane("plex").failures = 1
        concurrency.finish_adaptive_concurrency(controller, token)
    assert "using fresh limits" in caplog.text
    assert "Unable to persist plex" in caplog.text

    dry = {"settings": {"dry_run": True}}
    controller, token = concurrency.begin_adaptive_concurrency(
        dry, resources=resources, persist_provider_health=True
    )
    concurrency.finish_adaptive_concurrency(controller, token)


class _FakeLock:
    def __init__(self, _path):
        self.acquired = False

    def acquire(self):
        self.acquired = True

    def release(self):
        self.acquired = False


def _args(**updates):
    args = metafusion.parse_cli_args([])
    for name, value in updates.items():
        setattr(args, name, value)
    return args


def test_operational_command_dispatches_reports_restore_and_purge(
    monkeypatch, tmp_path, capsys
):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(tmp_path / "kometa"))
    report = tmp_path / "report.txt"
    monkeypatch.setattr(metafusion, "JobRunLock", _FakeLock)
    monkeypatch.setattr(
        metafusion, "write_run_history_report", lambda *_args, **_kwargs: report
    )
    assert metafusion._handle_operator_command(_args(run_history=True), config) == 0
    assert (
        metafusion._handle_operator_command(_args(schedule_advice=True), config)
        == 0
    )

    monkeypatch.setattr(
        metafusion, "write_quarantine_report", lambda **_kwargs: report
    )
    assert (
        metafusion._handle_sqlite_only_command(
            _args(cleanup_quarantine_report=True)
        )
        == 0
    )
    monkeypatch.setattr(
        metafusion,
        "restore_quarantined_asset",
        lambda *_args, **_kwargs: {"history_id": 7},
    )
    assert (
        metafusion._handle_operator_command(_args(cleanup_restore=7), config) == 0
    )
    monkeypatch.setattr(
        metafusion,
        "purge_expired_quarantine",
        lambda *_args, **_kwargs: [
            {"status": "purged"},
            {"status": "missing"},
            {"status": "protected"},
        ],
    )
    assert metafusion._handle_operator_command(_args(cleanup_purge=True), config) == 0
    output = capsys.readouterr().out
    assert "Run history" in output
    assert "Schedule advice" in output
    assert "restored" in output
    assert "Purged 2" in output


def test_cleanup_restore_cli_rejects_nonpositive_history_id(capsys):
    assert metafusion.main(["--cleanup-restore", "0"]) == 2
    assert "positive history ID" in capsys.readouterr().err


def test_scheduler_reloads_valid_config_and_retains_invalid_replacement(
    monkeypatch, caplog
):
    initial = copy.deepcopy(DEFAULT_CONFIG)
    initial["metafusion_run"] = False
    initial["settings"].update(
        schedule=True,
        run_times=["23:59"],
        run_on_start=False,
        schedule_catch_up=False,
    )
    replacement = copy.deepcopy(initial)
    replacement["settings"].update(schedule=False, run_times=[])

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

    class Event:
        def __init__(self):
            self.waits = 0
            self.value = False

        def clear(self):
            self.value = False
            self.waits = 0

        def set(self):
            self.value = True

        def is_set(self):
            return self.value

        def wait(self, _seconds):
            self.waits += 1
            if self.waits >= 2:
                self.value = True
            return self.value

    class Reloader:
        def __init__(self, *_args, **_kwargs):
            self.calls = 0

        def reload_if_changed(self, current):
            self.calls += 1
            if self.calls == 1:
                return current, False, "invalid replacement"
            return replacement, True, None

    class FakeSchedule:
        class ScheduleValueError(ValueError):
            pass

        def __init__(self):
            self.callback = None

        def clear(self, _tag=None):
            self.callback = None

        def every(self):
            return self

        @property
        def day(self):
            return self

        def at(self, _value):
            return self

        def do(self, callback):
            self.callback = callback
            return self

        def tag(self, _tag):
            return self

        def run_pending(self):
            if self.callback is not None:
                callback, self.callback = self.callback, None
                callback()

    event = Event()
    completed = Event()
    runs = []
    monkeypatch.setattr(metafusion, "shutdown_requested", event)
    monkeypatch.setattr(metafusion, "shutdown_complete", completed)
    monkeypatch.setattr(metafusion, "schedule", FakeSchedule())
    monkeypatch.setattr(metafusion, "ScheduledConfigReloader", Reloader)
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **_kwargs: (copy.deepcopy(initial), {}),
    )
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_runtime_paths", lambda *_args: None)
    monkeypatch.setattr(
        metafusion,
        "get_setup_logging",
        lambda _config: logging.getLogger("reload-scheduler"),
    )
    monkeypatch.setattr(metafusion, "RuntimeStatus", Status)
    monkeypatch.setattr(
        metafusion,
        "run_metafusion_job",
        lambda *_args, **_kwargs: runs.append(True) or True,
    )
    with caplog.at_level(logging.INFO):
        assert metafusion.main([]) == 0
    assert runs == [True]
    assert "Reload rejected" in caplog.text
    assert "Reload accepted" in caplog.text
    assert "paused scheduled jobs" in caplog.text


def test_cleanup_reports_automatic_quarantine_purge_and_deferred_failure(
    monkeypatch, tmp_path, caplog
):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode="kometa", path=str(tmp_path))
    config["cleanup"].update(confirmation_scans=1, grace_hours=0)
    flags = {
        "dry_run": False,
        "metadata_basic": False,
        "metadata_enhanced": False,
        "poster": False,
        "season": False,
        "background": False,
    }
    monkeypatch.setattr(cleanup_module, "load_cache", lambda: {})
    monkeypatch.setattr(cleanup_module, "load_item_exceptions", lambda: [])
    monkeypatch.setattr(
        cleanup_module,
        "purge_expired_quarantine",
        lambda *_args, **_kwargs: [{"status": "purged"}],
    )
    with caplog.at_level(logging.INFO):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                config,
                flags,
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )
    assert "Purged expired artwork" in caplog.text

    caplog.clear()
    monkeypatch.setattr(
        cleanup_module,
        "purge_expired_quarantine",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("busy")),
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            cleanup_module.cleanup_title_orphans(
                config,
                flags,
                preloaded_plex_metadata={},
                safe_library_types={"movie"},
            )
        )
    assert "Expired purge deferred" in caplog.text
