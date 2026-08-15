import asyncio
import logging

import pytest

import metafusion


class Section:
    def __init__(self, title, library_type):
        self.title = title
        self.type = library_type


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
    monkeypatch.setattr(metafusion, "begin_cache_session", lambda: None)
    monkeypatch.setattr(metafusion, "flush_cache", lambda: flushed.append(True))

    successful = metafusion.run_metafusion_job(
        {"plex": {"token": "plex-secret"}, "tmdb": {"api_key": "tmdb-secret"}},
        logging.getLogger("orchestration-test"),
    )

    assert successful is False
    assert flushed == [True]


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
    monkeypatch.setattr(
        metafusion,
        "connect_plex_library",
        lambda _config: ([movie], ["Movies"], [{"title": "Movies", "type": "movie"}]),
    )
    monkeypatch.setattr(metafusion, "process_library", fail_library)
    monkeypatch.setattr(metafusion, "cleanup_title_orphans", capture_cleanup)
    monkeypatch.setattr(metafusion.aiohttp, "ClientSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(metafusion.aiohttp, "TCPConnector", lambda **_kwargs: object())

    config = {
        "settings": {"mode": "kometa", "path": str(tmp_path)},
        "runtime": {},
        "cleanup": {"run_process": True},
        "metadata": {},
        "assets": {},
        "plex": {},
        "tmdb": {},
    }
    with pytest.raises(RuntimeError, match="scan failed"):
        asyncio.run(metafusion.metafusion_main(config, logging.getLogger("main-test")))

    assert cleanup_scopes == [set()]
