import asyncio
import json
import shutil
import sqlite3
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

import metafusion
from helper import database_maintenance, state_db
from helper import logging as logging_module
from helper import tmdb as tmdb_module
from helper import tmdb_cache as cache_module
from helper.database_maintenance import maintain_databases
from helper.plex import plex_operation
from helper.state_db import MediaStateStore, StateDatabaseError
from helper.tmdb_cache import PersistentTTLCache
from modules import builder


def test_tmdb_cache_memory_mapping_and_automatic_limit_edges(monkeypatch, tmp_path):
    def unavailable_usage(_path):
        raise OSError("disk usage unavailable")

    monkeypatch.setattr(cache_module.shutil, "disk_usage", unavailable_usage)
    cache = PersistentTTLCache()
    cache.configure(
        tmp_path / "missing" / "cache.sqlite3",
        writable=False,
        max_entries=1,
        max_mb=1,
    )
    assert cache.max_entries == 1
    assert cache.max_bytes == 1024 * 1024
    assert cache.stats()["health"] == "memory_only"

    cache["one"] = {"value": 1}
    cache["two"] = {"value": 2}
    assert set(cache) == {"two"}
    assert cache.stats()["evictions"] == 1
    del cache["two"]
    with pytest.raises(KeyError):
        del cache["missing"]
    cache.clear()
    assert len(cache) == 0


def test_tmdb_cache_database_delete_duplicate_length_and_read_only_clear(tmp_path):
    path = tmp_path / "tmdb_cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, max_entries=10, max_mb=1)
    cache["one"] = {"value": 1}
    cache["two"] = {"value": 2}
    cache.flush()

    cache._store_memory("one", {"value": "memory"}, cache_module.time.time())
    assert len(cache) == 2
    del cache["one"]
    assert cache["one"] == {"value": 1}
    del cache["one"]
    with pytest.raises(KeyError):
        del cache["one"]
    cache.clear()
    assert cache.flush() is True
    assert len(cache) == 0
    cache.reset_memory()

    read_only = PersistentTTLCache()
    read_only.configure(path, writable=False)
    read_only.clear()
    assert read_only._ignore_database is True
    assert len(read_only) == 0


def test_tmdb_cache_recovers_missing_metadata_and_prunes_old_quarantines(tmp_path):
    path = tmp_path / "tmdb_cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path)
    cache.reset_memory()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    for index in range(7):
        old = tmp_path / f"tmdb_cache.sqlite3.corrupt-20260101-00000{index}"
        old.write_text("old", encoding="utf-8")

    recovered = PersistentTTLCache()
    recovered.configure(path)

    assert recovered.stats()["health"] == "recovered"
    assert len(list(tmp_path.glob("tmdb_cache.sqlite3*.corrupt-*"))) == 4
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT entry_count FROM tmdb_cache_meta WHERE singleton = 1"
        ).fetchone() == (0,)


def test_tmdb_cache_relieve_space_and_bounded_maintenance(monkeypatch, tmp_path):
    path = tmp_path / "tmdb_cache.sqlite3"
    cache = PersistentTTLCache()
    cache.configure(path, max_entries=20, max_mb=10)
    for index in range(6):
        cache[str(index)] = {"payload": "x" * 2000, "index": index}
    cache.flush()

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        cache_module.shutil,
        "disk_usage",
        lambda _path: Usage(100, 100, 0),
    )
    removed = cache.relieve_space(tmp_path, required_free_bytes=1)

    assert removed == 6
    assert len(cache) == 0
    maintenance = cache.maintain(wal_threshold_mb=1024)
    assert maintenance["optimized"] is True
    assert maintenance["checkpointed"] is False


def test_provider_rate_limit_is_not_cached_and_a_later_run_recovers(
    monkeypatch, tmp_path
):
    class Response:
        content = None

        def __init__(self, status, payload=None):
            self.status = status
            self.payload = payload
            self.headers = {"Retry-After": "120"} if status == 429 else {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return "rate limited"

    class Session:
        def __init__(self, response):
            self.response = response

        def get(self, *_args, **_kwargs):
            return self.response

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tmdb_module, "get_tmdb_limiter", lambda: Limiter())
    tmdb_module.tmdb_response_cache.configure(tmp_path / "tmdb_cache.sqlite3")
    config = {
        "tmdb": {"api_key": "secret", "language": "en-US", "region": "US"},
        "runtime": {"max_concurrency": 1},
    }

    limited = asyncio.run(
        tmdb_module.tmdb_api_request(
            config,
            "configuration",
            retries=1,
            session=Session(Response(429)),
        )
    )
    assert limited is None
    assert len(tmdb_module.tmdb_response_cache) == 0

    recovered = asyncio.run(
        tmdb_module.tmdb_api_request(
            config,
            "configuration",
            retries=1,
            session=Session(Response(200, {"ok": True})),
        )
    )
    assert recovered == {"ok": True}
    assert len(tmdb_module.tmdb_response_cache) == 1


def test_temporary_plex_disconnect_retries_without_duplicate_mutation(monkeypatch):
    attempts = []
    sleeps = []

    def operation():
        attempts.append("called")
        if len(attempts) < 3:
            raise ConnectionError("Plex temporarily disconnected")
        return "updated-once"

    async def no_wait(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("helper.plex.asyncio.sleep", no_wait)
    result = asyncio.run(
        plex_operation(
            operation,
            runtime={"plex_retries": 3, "plex_retry_delay": 0.5},
            description="temporary disconnect test",
        )
    )

    assert result == "updated-once"
    assert attempts == ["called", "called", "called"]
    assert sleeps == [0.5, 1.0]


def test_state_backup_integrity_and_offline_restoration(monkeypatch, tmp_path):
    monkeypatch.setattr(state_db, "_integrity_checked_databases", set())
    monkeypatch.setattr(state_db, "_initialized_databases", set())
    monkeypatch.setattr(state_db, "_backed_up_databases", set())
    database = tmp_path / "meta_db.sqlite3"
    store = MediaStateStore(database)
    store["movie:1"] = {
        "server_id": "server",
        "library_uuid": "movies",
        "rating_key": "1",
        "media_type": "movie",
        "tmdb_id": "100",
        "title": "Example",
        "year": 2020,
    }
    assert store.flush() is True
    store.close()

    monkeypatch.setattr(
        database_maintenance,
        "DATABASES",
        {"state": (database, state_db.SCHEMA_VERSION)},
    )
    result = maintain_databases(
        "backup", "state", backup_dir=tmp_path / "backups"
    )[0]
    backup = Path(result["backup"])
    restored = tmp_path / "restored" / "meta_db.sqlite3"
    restored.parent.mkdir()
    shutil.copy2(backup, restored)

    restored_store = MediaStateStore(restored, writable=False)
    assert restored_store["movie:1"]["tmdb_id"] == "100"
    restored_store.close()

    database.write_bytes(b"not a sqlite database")
    state_db._integrity_checked_databases.clear()
    state_db._initialized_databases.clear()
    with pytest.raises(StateDatabaseError, match="Unable to open durable"):
        MediaStateStore(database, writable=False)


def test_state_memory_scope_reconciliation_preserves_only_requested_items(tmp_path):
    store = MediaStateStore(tmp_path / "missing.sqlite3", writable=False)
    store["movie:1"] = {
        "server_id": "server",
        "library_uuid": "movies",
        "rating_key": "1",
        "title": "One",
    }
    store["movie:2"] = {
        "server_id": "server",
        "library_uuid": "movies",
        "rating_key": "2",
        "title": "Two",
    }
    store["other:3"] = {
        "server_id": "other",
        "library_uuid": "movies",
        "rating_key": "3",
    }
    store._deleted.add("movie:2")

    assert store.entries_for_scope("server", "movies", ["1", "2"]) == {
        "movie:1": {
            "server_id": "server",
            "library_uuid": "movies",
            "rating_key": "1",
            "title": "One",
        }
    }
    assert list(store.items())
    assert list(store.values())


def test_state_read_only_legacy_schema_without_asset_ownership_is_safe(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(state_db, "_integrity_checked_databases", set())
    monkeypatch.setattr(state_db, "_initialized_databases", set())
    database = tmp_path / "legacy-state.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version = {state_db.SCHEMA_VERSION}")

    store = MediaStateStore(database, writable=False)
    assert store.asset_destination_records() == []
    store.close()


def test_logging_setup_system_checks_and_full_summaries(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_module, "LOG_FILE", tmp_path / "metafusion.log")
    config = {
        "settings": {
            "dry_run": False,
            "log_level": "INFO",
            "log_max_mb": 1,
            "log_backup_count": 2,
        },
        "plex": {"url": "http://plex", "token": "plex-secret"},
        "tmdb": {"api_key": "tmdb-secret"},
    }
    logger = logging_module.get_setup_logging(config)
    logger.info("tokens plex-secret tmdb-secret")
    for handler in logger.handlers:
        handler.flush()
    text = (tmp_path / "metafusion.log").read_text(encoding="utf-8")
    assert "plex-secret" not in text
    assert "tmdb-secret" not in text

    memory = SimpleNamespace(
        total=8 * 1024**3,
        used=2 * 1024**3,
        available=6 * 1024**3,
    )
    monkeypatch.setattr(logging_module.psutil, "virtual_memory", lambda: memory)
    monkeypatch.setattr(logging_module.psutil, "cpu_percent", lambda interval=None: 5)
    monkeypatch.setattr(logging_module.os, "cpu_count", lambda: 8)
    replies = iter([SimpleNamespace(status_code=200), SimpleNamespace(status_code=200)])
    monkeypatch.setattr(logging_module.requests, "get", lambda *_a, **_k: next(replies))
    assert logging_module.check_sys_requirements(logger, config) is True

    counts = {
        "meta_downloaded": 1,
        "meta_upgraded": 1,
        "meta_skipped": 1,
        "meta_failed": 0,
        "poster_downloaded": 1,
        "background_upgraded": 1,
        "season_poster_skipped": 1,
        "incremental_skipped": 2,
    }
    flags = {
        "metadata_basic": True,
        "poster": True,
        "background": True,
        "season": True,
        "cleanup": True,
    }
    logging_module.log_library_summary(
        "TV Shows",
        3,
        1,
        4,
        75,
        25,
        poster_size=1024,
        background_size=2048,
        season_poster_size=512,
        feature_flags=flags,
        library_filesize={"TV Shows": 3584},
        run_metadata=True,
        library_summary=counts,
        logger=logger,
        library_type="show",
        season_count=2,
        episode_count=12,
    )
    logging_module.log_final_summary(
        logger,
        65,
        {
            "TV Shows": {
                "library_summary": counts,
                "library_type": "show",
                "total_items": 4,
                "library_items": 6,
                "complete": 3,
                "incomplete": 1,
                "percent_complete": 75,
                "season_count": 2,
                "episode_count": 12,
            },
            "Skipped": None,
        },
        {"TV Shows": 3584},
        0,
        0,
        ["TV Shows"],
        [{"title": "TV Shows"}, {"title": "Movies"}],
        {"settings": {"dry_run": True}},
        feature_flags=flags,
    )

    assert "Artwork season posters | Downloaded=" in (
        tmp_path / "metafusion.log"
    ).read_text(
        encoding="utf-8"
    )
    for handler in list(logger.handlers):
        handler.close()
    logger.handlers.clear()


def test_logging_upgrade_reason_and_asset_status_maps(caplog):
    context = {
        "new_votes": 8,
        "cached_votes": 6,
        "vote_threshold": 7,
        "vote_relaxed": 4,
        "new_width": 2000,
        "new_height": 3000,
        "existing_width": 1000,
        "existing_height": 1500,
    }
    with caplog.at_level(logging_module.logging.DEBUG):
        for status in (
            "UPGRADE_VOTES",
            "UPGRADE_STRICT",
            "UPGRADE_THRESHOLD",
            "UPGRADE_RELAXED",
            "UPGRADE_DIMENSIONS",
            "OTHER",
        ):
            logging_module.log_builder_event(
                "builder_asset_upgraded",
                media_type="Movie",
                asset_type="poster",
                full_title="Example",
                status_code=status,
                context=context,
                filesize=1024,
            )
        for status in (
            "UPGRADE_VOTES_SEASON",
            "UPGRADE_ZERO_VOTE_SEASON",
            "UPGRADE_STRICT_SEASON",
            "UPGRADE_THRESHOLD_SEASON",
            "UPGRADE_RELAXED_SEASON",
            "UPGRADE_DIMENSIONS_SEASON",
            "OTHER",
        ):
            logging_module.log_builder_event(
                "builder_asset_upgraded_season",
                media_type="TV Show",
                asset_type="poster",
                full_title="Example",
                season_number=1,
                status_code=status,
                context=context,
                filesize=1024,
            )
        for status in (
            "FORCE_UPGRADE_STALE",
            "ALREADY_UP_TO_DATE",
            "NO_UPGRADE_NEEDED",
            "STALE_CANDIDATE_DOWNGRADE",
            "NO_IMAGE_FOR_COMPARE",
            "ERROR_IMAGE_COMPARE",
            "FORCE_UPGRADE_STALE_SEASON",
            "ALREADY_UP_TO_DATE_SEASON",
            "NO_UPGRADE_NEEDED_SEASON",
            "STALE_CANDIDATE_DOWNGRADE_SEASON",
            "NO_IMAGE_FOR_COMPARE_SEASON",
            "ERROR_IMAGE_COMPARE_SEASON",
            "UNKNOWN",
        ):
            logging_module.log_asset_status(
                status,
                media_type="Movie",
                asset_type="poster",
                full_title="Example",
                filesize=1024,
                error="test",
                extra="",
                season_number=1,
            )

    assert "Example" in caplog.text


def test_builder_and_orchestration_noop_and_normalization_edges(monkeypatch):
    real_movie_builder = builder._build_movie
    real_tv_builder = builder._build_tv

    async def fake_build(*_args, **_kwargs):
        return {"ok": True}

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(builder, "_build_movie", fake_build)
    monkeypatch.setattr(builder, "_build_tv", fake_build)
    monkeypatch.setattr(builder, "_media_asset_lock", lambda *_args: Lock())
    assert asyncio.run(builder.build_movie({}, {}, {}, meta={})) == {"ok": True}
    assert asyncio.run(builder.build_tv({}, {}, {}, meta={})) == {"ok": True}

    empty_movie = asyncio.run(
        real_movie_builder(
            {},
            {},
            feature_flags={
                "metadata_basic": False,
                "poster": False,
                "background": False,
            },
        )
    )
    empty_tv = asyncio.run(
        real_tv_builder(
            {},
            {},
            feature_flags={
                "metadata_basic": False,
                "poster": False,
                "background": False,
                "season": False,
            },
        )
    )
    assert empty_movie["is_complete"] is True
    assert empty_tv["is_complete"] is True
    assert metafusion.cli_media_type("unsupported") == "unsupported"
    assert metafusion.normalize_library_type("SHOW") == "tv"
    assert metafusion.complete_inventory_types(
        [{"type": "movie"}, {"type": "show"}],
        [SimpleNamespace(type="movie", title=None)],
    ) == {"movie"}


def test_orchestration_override_covers_audit_profile_and_restore_modes():
    config = {
        "metafusion_run": False,
        "settings": {"mode": "plex", "dry_run": False},
        "metadata": {"run_basic": True, "run_enhanced": True},
        "assets": {"run_poster": True, "run_season": True, "run_background": True},
        "cleanup": {"run_cleanup": True},
        "plex_metadata": {"enabled": False},
        "compatibility": {},
        "plex_libraries": [],
    }
    args = metafusion.parse_cli_args(
        [
            "--asset-audit",
            "--compatibility-profile",
            "plex-api-v1",
            "--plex-metadata-restore",
            "--rating-key",
            "10",
        ]
    )

    metafusion.override_config_with_cli(config, args)

    assert config["settings"]["dry_run"] is True
    assert config["metadata"] == {"run_basic": False, "run_enhanced": False}
    assert config["compatibility"]["profile"] == "plex-api-v1"
    assert config["_execution"]["plex_metadata_maintenance"] == "restore"


def test_status_command_reports_state_history_and_retry_queue(
    monkeypatch, tmp_path, capsys
):
    status_path = tmp_path / "status.json"
    status_path.write_text('{"state": "idle"}', encoding="utf-8")
    monkeypatch.setenv("STATUS_FILE", str(status_path))
    monkeypatch.setattr(
        metafusion,
        "recent_job_runs",
        lambda path=None: [{"status": "completed", "path": str(path)}],
    )
    monkeypatch.setattr(
        metafusion,
        "retry_queue_summary",
        lambda path=None: {"pending": 2, "path": str(path)},
    )

    assert metafusion.main(["--status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "idle"
    assert output["recent_jobs"][0]["status"] == "completed"
    assert output["retry_queue"]["pending"] == 2


def test_sqlite_mutating_maintenance_holds_the_run_lock(monkeypatch, capsys):
    calls = []

    class Lock:
        def __init__(self, path):
            calls.append(("created", Path(path).name))

        def acquire(self):
            calls.append(("acquired", None))

        def release(self):
            calls.append(("released", None))

    monkeypatch.setattr(metafusion, "JobRunLock", Lock)
    monkeypatch.setattr(
        metafusion,
        "maintain_databases",
        lambda action, target: [
            {"database": target, "action": action, "healthy": True}
        ],
    )
    monkeypatch.setattr(
        metafusion,
        "format_maintenance_results",
        lambda results: f"{results[0]['database']}: healthy",
    )

    assert (
        metafusion.main(
            ["--sqlite-maintenance", "optimize", "--sqlite-target", "state"]
        )
        == 0
    )
    assert calls == [
        ("created", ".metafusion-run.lock"),
        ("acquired", None),
        ("released", None),
    ]
    assert capsys.readouterr().out.strip() == "state: healthy"


def test_sqlite_maintenance_reports_lock_acquisition_failure(monkeypatch, capsys):
    calls = []

    class FailingLock:
        def __init__(self, _path):
            pass

        def acquire(self):
            raise OSError("lock storage unavailable")

        def release(self):
            calls.append("released")

    monkeypatch.setattr(metafusion, "JobRunLock", FailingLock)

    assert metafusion.main(["--sqlite-maintenance", "optimize"]) == 1
    assert calls == ["released"]
    assert "lock storage unavailable" in capsys.readouterr().err
