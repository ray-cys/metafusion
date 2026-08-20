import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import docker_entrypoint
from helper import (
    asset_registry,
    cache,
    identity,
    identity_diagnostics,
    mapping_diagnostics,
    plex_paths,
    provider_mappings,
    report_identity,
    reporting,
    runtime,
)
from helper import io as app_io


def test_asset_registry_invalid_persisted_records_and_checksum_paths(tmp_path):
    registry = asset_registry.AssetDestinationRegistry(
        [None, {}, {"destination": str(tmp_path / "poster.jpg"), "cache_key": "one"}]
    )
    assert asset_registry.normalize_destination(None) is None
    assert asset_registry.canonical_asset_claim("show", 1, "poster", "/a")[0] == "tv"
    assert registry.records_for(tmp_path / "missing") == []
    assert len(registry) == 1

    missing = tmp_path / "missing.jpg"
    assert registry._current_checksum(missing) is None
    assert registry.shared_checksum(
        "other", missing, media_type="movie", tmdb_id=1,
        asset_type="poster", source_path="/a"
    ) is None

    image = tmp_path / "image.jpg"
    image.write_bytes(b"image")
    checksum = registry.mark_verified(
        "one", image, media_type="movie", tmdb_id=1,
        asset_type="poster", source_path="/a"
    )
    assert checksum
    assert registry.mark_verified(
        "two", image, media_type="movie", tmdb_id=1,
        asset_type="poster", source_path="/a", checksum=checksum
    ) == checksum
    assert registry.shared_checksum(
        "three", image, media_type="movie", tmdb_id=1,
        asset_type="poster", source_path="/a"
    ) == checksum

    async def locks():
        first = registry.lock_for(image)
        second = registry.lock_for(image)
        assert first is second

    asyncio.run(locks())


def test_plex_path_validation_mount_discovery_and_advice(tmp_path, monkeypatch):
    with pytest.raises(plex_paths.PlexPathError, match="destination must be absolute"):
        plex_paths.parse_path_mappings(["/plex=>relative"])
    with pytest.raises(plex_paths.PlexPathError, match="unsafe traversal"):
        plex_paths.parse_path_mappings(["/plex/../bad=>/media"])
    with pytest.raises(plex_paths.PlexPathError, match="Duplicate"):
        plex_paths.parse_path_mappings(["/plex=>/one", "/plex=>/two"])
    with pytest.raises(plex_paths.PlexPathError, match="empty"):
        plex_paths.translate_plex_path("")
    with pytest.raises(plex_paths.PlexPathError, match="non-absolute"):
        plex_paths.translate_plex_path("relative")

    mount = tmp_path / "Media Files"
    mount.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "bad\n1 2 3 4 /proc/x extra\n1 2 3 4 "
        + str(mount).replace(" ", "\\040")
        + " extra\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plex_paths.Path, "is_dir", lambda self: self == mount)
    assert plex_paths.visible_mount_roots(mountinfo) == [mount]
    assert plex_paths.visible_mount_roots(tmp_path / "missing") == []

    target = mount / "Movies" / "Example.mkv"
    result = plex_paths.advise_path_mappings(
        ["/volume/Movies/Example.mkv", "/unresolved/file.mkv"],
        mount_roots=[mount],
        exists=lambda path: str(path) == str(target),
    )
    assert result["suggestions"] == [f"/volume=>{mount}"]
    assert {record["status"] for record in result["records"]} == {
        "suggested", "unresolved"
    }


def test_runtime_storage_guards_path_validation_and_status_failure(tmp_path, monkeypatch, caplog):
    config = {"runtime": {"min_free_space_mb": 1}, "settings": {"mode": "plex"}}
    file_path = tmp_path / "file"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unavailable"):
        runtime.ensure_storage_available(config, file_path)

    monkeypatch.setattr(runtime.os, "access", lambda *_args: False)
    with pytest.raises(RuntimeError, match="not writable"):
        runtime.ensure_storage_available(config, tmp_path)
    monkeypatch.setattr(runtime.os, "access", lambda *_args: True)
    monkeypatch.setattr(
        runtime.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=99, free=1),
    )
    monkeypatch.setattr(runtime, "storage_pressure_threshold", lambda *_args: (1, 10))
    with pytest.raises(runtime.DiskPressureError) as caught:
        runtime.ensure_storage_available(config, tmp_path)
    assert caught.value.free_bytes == 1
    assert runtime.ensure_storage_available(
        config, tmp_path, require_space=False
    ) == tmp_path

    monkeypatch.setattr(runtime.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(RuntimeError, match="refuses to run as root"):
        runtime.validate_runtime_paths(config, tmp_path)
    monkeypatch.setattr(runtime.os, "geteuid", lambda: 99, raising=False)
    assert runtime.validate_runtime_paths(
        {"settings": {"mode": "plex", "dry_run": True}}, tmp_path
    ) is None

    status = runtime.RuntimeStatus(
        tmp_path / "status.json", state_database=tmp_path / "state.sqlite3"
    )
    monkeypatch.setattr(
        runtime,
        "record_job_run",
        lambda **_kwargs: (_ for _ in ()).throw(runtime.StateDatabaseError("locked")),
    )
    with caplog.at_level(logging.WARNING):
        status.run_finished(False, error="failed", library_results={"Movies": {}})
    status.idle()
    status.stopping()
    status.stop()
    assert "Unable to persist completed job history" in caplog.text


def test_cache_scope_destination_history_and_upgrade_observations(tmp_path, monkeypatch):
    entry = {}
    cache._record_destination_change(entry, "poster", None, "/new", "now")
    cache._record_destination_change(entry, "poster", "/same", "/same", "now")
    cache._record_destination_change(entry, "poster", "/old", "/new", "now")
    cache._record_destination_change(entry, "poster", "/old", "/new", "later")
    assert len(entry["destination_history"]) == 1
    cache._record_artwork_observation(entry, "poster", "candidate")
    cache._record_artwork_observation(entry, "poster", "candidate")
    assert entry["poster_unchanged_checks"] == 1

    class Store(dict):
        path = tmp_path / "db"

        def flush(self):
            return True

        def replace_all(self, values):
            self.clear()
            self.update(values)

    store = Store()
    monkeypatch.setattr(cache, "_cache_store", store)
    token = cache.set_cache_scope("server", "library", "Movies")
    try:
        asyncio.run(
            cache.meta_cache_async(
                "movie:1", "1", "Movie", 2020, "movie",
                asset_upgraded=True, poster_upgraded=True,
                background_upgraded=True, poster_checked=True,
                background_checked=True, plex_metadata_checked=True,
                metadata_pending_count=2,
            )
        )
    finally:
        cache.reset_cache_scope(token)
    assert store["movie:1"]["server_id"] == "server"
    assert store["movie:1"]["poster_missing_checks"] == 1
    assert cache.flush_cache() is True
    cache.save_cache({"movie:2": {"title": "Other"}})
    assert "movie:2" in store
    assert cache.mark_cache_dirty() is None


def test_identity_destination_and_report_edge_paths(tmp_path):
    assert identity_diagnostics._provider_guids(
        SimpleNamespace(guid="plex://1", guids=[SimpleNamespace(id="tmdb://2"), "tvdb://3"])
    ) == ["plex://1", "tmdb://2", "tvdb://3"]
    assert identity_diagnostics._destination_record(None, "poster.jpg")["path"] is None
    plex_config = {"settings": {"mode": "plex"}}
    tv_meta = {
        "library_type": "tv", "ratingKey": "1", "show_dir": str(tmp_path),
        "season_dirs": {"bad": "/bad", "0": str(tmp_path), 2: str(tmp_path)}
    }
    artwork = identity_diagnostics._artwork_destinations(plex_config, tv_meta)
    assert [record["season"] for record in artwork["seasons"]] == [0, 2]

    kometa_config = {"settings": {"mode": "kometa", "path": str(tmp_path)}}
    artwork = identity_diagnostics._artwork_destinations(
        kometa_config,
        {"library_type": "tv", "show_path": "Show", "seasons_episodes": {0: [], "bad": []}},
    )
    assert artwork["seasons"][0]["season"] == 0
    assert identity_diagnostics._tmdb_names(
        {"name": "Localized", "original_name": "Original", "first_air_date": "2020-01-01"},
        "tv",
    )["year"] == "2020"

    report = identity_diagnostics.write_identity_inspection_report(
        [
            {
                "status": "accepted", "library": "Shows", "rating_key": "1",
                "media_type": "tv",
                "plex": {"localized_title": "Show", "year": 2020, "guids": []},
                "selection": {}, "tmdb": {},
                "binding": {
                    "status": "stale", "active": {},
                    "history": [{"occurred_at": "now", "event_type": "invalidated", "reason_code": "changed"}],
                },
                "metadata_destination": {},
                "artwork_destinations": {"poster": {}, "background": {}, "seasons": [{"season": 0}]},
            }
        ],
        base_dir=tmp_path,
    )
    text = report.read_text(encoding="utf-8")
    assert "Binding invalidation" in text
    assert "season 0" in text


def test_identity_fallbacks_and_report_normalization(tmp_path):
    path_meta = {"library_type": "movie", "movie_dir": tmp_path / "Movie"}
    assert identity.item_identity(path_meta).startswith("path:")
    assert identity.item_identity({"edition_title": "Director's Cut"}) == (
        "edition:director-s-cut"
    )
    assert identity.cache_key_for_meta(
        {"library_type": "show", "title": "Show", "year": 2020}
    ) == "tv:Show:2020"
    assert "Shows - plex:1" in identity.metadata_key_for_meta(
        {
            "library_type": "tv",
            "title": "Show",
            "year": 2020,
            "ratingKey": "1",
            "library_name": "Shows",
            "requires_unique_key": True,
        }
    )
    assert "Director's Cut - plex:1" in identity.metadata_key_for_meta(
        {
            "library_type": "movie",
            "title": "Movie",
            "year": 2020,
            "ratingKey": "1",
            "edition_title": "Director's Cut",
            "edition_key_collision": True,
        }
    )
    assert report_identity.report_identity({"season": "special"})["season_number"] == (
        "special"
    )


def test_atomic_replace_reports_ownership_mismatch(monkeypatch, tmp_path):
    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.txt"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    original_stat = Path.stat

    def mismatched_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == source:
            return SimpleNamespace(
                st_uid=result.st_uid + 1,
                st_gid=result.st_gid,
                st_mode=result.st_mode,
            )
        return result

    monkeypatch.setattr(Path, "stat", mismatched_stat)
    monkeypatch.setattr(
        app_io.os,
        "chown",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(PermissionError, match="Cannot preserve ownership"):
        app_io.atomic_replace_file(source, destination)
    assert destination.read_text(encoding="utf-8") == "old"


def test_mapping_and_provider_mapping_edge_paths(tmp_path, monkeypatch):
    assert mapping_diagnostics._inventory_pairs(
        {"bad": [1], 1: ["bad", 2]}
    ) == {(1, 2)}
    assert mapping_diagnostics._offset_override_proposal({(1, 1)}, {(0, 0), (2, 2)}) == {}

    async def tmdb_request(_config, _endpoint, **_kwargs):
        return {"episodes": [{}, {"episode_number": "bad"}, {"episode_number": 1}]}

    monkeypatch.setattr(mapping_diagnostics, "tmdb_api_request", tmdb_request)
    pairs = asyncio.run(
        mapping_diagnostics._standard_episode_pairs({}, "1", {1: [1]}, None)
    )
    assert pairs == {(1, 1)}
    pairs = asyncio.run(
        mapping_diagnostics._split_series_pairs(
            {}, "1", {1: [1]}, {"seasons": {1: {"tmdb_id": 2, "season_number": 3}}}, None
        )
    )
    assert pairs == {(1, 1)}

    report = mapping_diagnostics.write_mapping_diagnosis_report(
        [
            {
                "title": "Show", "year": 2020, "library": "Shows",
                "rating_key": "1", "tmdb_id": "2", "status": "episode_group",
                "explanation": "matched", "missing_standard": ["S01E01"],
                "episode_group_id": "group",
                "proposed_configuration": {"tmdb": {"episode_overrides": {}}},
            }
        ],
        base_dir=tmp_path,
    )
    assert "Unique episode group" in report.read_text(encoding="utf-8")

    assert set(provider_mappings.provider_identity_keys(1, 2, "tt3")) == {
        "tmdb:1", "tvdb:2", "imdb:tt3"
    }
    assert provider_mappings._episode_pair("S01E02") == (1, 2)
    assert provider_mappings._episode_pair("bad") is None
    assert provider_mappings._normalized_split_mapping([]) is None
    errors = provider_mappings.validate_provider_mapping_config(
        {
            "split_series_mappings": {"bad": "value"},
            "episode_overrides": {"tmdb:1": {"bad": "also bad"}},
        }
    )
    assert errors


def test_entrypoint_symlink_template_and_return_edges(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    managed_link = tmp_path / "managed-link"
    managed_link.symlink_to(target)
    with pytest.raises(RuntimeError, match="Managed runtime path"):
        docker_entrypoint._prepare_managed_directory(managed_link, 1, 1)
    with pytest.raises(RuntimeError, match="Configuration directory"):
        docker_entrypoint.prepare_runtime_paths(managed_link, 1, 1)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(docker_entrypoint, "_set_owner", lambda *_args: None)
    with pytest.raises(RuntimeError, match="packaged config template"):
        docker_entrypoint.sync_config_template(
            config_dir, 1, 1, tmp_path / "missing-template.yml"
        )

    source = tmp_path / "source.yml"
    source.write_bytes(b"template")
    destination = config_dir / docker_entrypoint.CONFIG_TEMPLATE_NAME
    destination.write_bytes(b"old")
    real_read = docker_entrypoint.Path.read_bytes

    def denied_read(path):
        if path == destination:
            raise OSError("denied")
        return real_read(path)

    monkeypatch.setattr(docker_entrypoint.Path, "read_bytes", denied_read)
    with pytest.raises(RuntimeError, match="managed config template"):
        docker_entrypoint.sync_config_template(config_dir, 1, 1, source)
    monkeypatch.setattr(docker_entrypoint.Path, "read_bytes", real_read)
    monkeypatch.setattr(
        docker_entrypoint.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("read only")),
    )
    with pytest.raises(RuntimeError, match="maintain config template"):
        docker_entrypoint.sync_config_template(config_dir, 1, 1, source)

    monkeypatch.setattr(docker_entrypoint.os, "geteuid", lambda: 99)
    monkeypatch.setattr(docker_entrypoint.os, "getegid", lambda: 100)
    monkeypatch.setattr(
        docker_entrypoint,
        "sync_config_template",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(docker_entrypoint.os, "execvp", lambda *_args: None)
    assert docker_entrypoint.main(["python", "metafusion.py"]) == 0


def test_report_retention_tolerates_races_and_orphan_failures(tmp_path, monkeypatch):
    first = reporting.write_diagnostic_report(
        tmp_path / "report-1.txt", "one", report_type="test"
    )
    second = reporting.write_diagnostic_report(
        tmp_path / "report-2.txt", "two", report_type="test"
    )
    orphan = tmp_path / "report-orphan.json"
    orphan.write_text("{}", encoding="utf-8")
    real_unlink = reporting.Path.unlink

    def guarded_unlink(path, *args, **kwargs):
        if path == first:
            raise FileNotFoundError(path)
        if path == orphan:
            raise OSError("busy")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(reporting.Path, "unlink", guarded_unlink)
    reporting.retain_diagnostic_reports(tmp_path, "report", 1)
    assert second.exists()
