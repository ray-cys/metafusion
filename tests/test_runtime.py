import asyncio
import io
import logging
from types import SimpleNamespace

import pytest

from helper import logging as logging_module
from helper import plex as plex_module
from helper import tmdb as tmdb_module
from helper.plex import get_plex_metadata
from helper.runtime import JobAlreadyRunningError, JobRunLock, RuntimeStatus
from modules.utils import get_best_background


def test_tmdb_request_without_session_does_not_raise_or_log_secret(monkeypatch):
    events = []
    monkeypatch.setattr(
        tmdb_module,
        "log_tmdb_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    result = asyncio.run(
        tmdb_module.tmdb_api_request(
            {"tmdb": {"api_key": "super-secret", "language": "en", "region": "US"}},
            "movie/1",
            session=None,
        )
    )

    assert result == {}
    assert events[-1][1]["query"]["api_key"] == "***"
    assert "super-secret" not in repr(events)


def test_tmdb_id_resolution_uses_external_ids_and_active_session(monkeypatch):
    calls = []
    active_session = object()

    async def request(_config, endpoint, params=None, session=None, **_kwargs):
        calls.append((endpoint, params, session))
        return {"tv_results": [{"id": 321}]}

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)
    resolved = asyncio.run(
        tmdb_module.resolve_tmdb_id(
            {}, "tv", tvdb_id="123", session=active_session
        )
    )

    assert resolved == "321"
    assert calls == [
        ("find/123", {"external_source": "tvdb_id"}, active_session)
    ]


def test_tmdb_title_search_fallback_accepts_only_one_exact_title_and_year(monkeypatch):
    async def request(_config, endpoint, params=None, **_kwargs):
        assert endpoint == "search/movie"
        assert params["year"] == 2020
        return {
            "results": [
                {"id": 1, "title": "Example!", "release_date": "2020-01-01"},
                {"id": 2, "title": "Example", "release_date": "2019-01-01"},
                {"id": 3, "title": "Different", "release_date": "2020-01-01"},
            ]
        }

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)
    resolved = asyncio.run(
        tmdb_module.resolve_tmdb_id(
            {"tmdb": {"title_search_fallback": True}},
            "movie",
            title="Example",
            year=2020,
            session=object(),
        )
    )

    assert resolved == "1"


def test_tmdb_title_search_prefers_terminal_title_year_over_conflicting_plex_year(
    monkeypatch,
):
    async def request(_config, endpoint, params=None, **_kwargs):
        assert endpoint == "search/movie"
        assert params["query"] == "Monster"
        assert params["year"] == 2022
        return {
            "results": [
                {"id": 10, "title": "Monster", "release_date": "2022-06-01"},
                {"id": 11, "title": "Monster", "release_date": "2024-06-01"},
            ]
        }

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)

    resolved = asyncio.run(
        tmdb_module.resolve_tmdb_id(
            {"tmdb": {"title_search_fallback": True}},
            "movie",
            title="Monster (2022)",
            year=2024,
            session=object(),
        )
    )

    assert resolved == "10"


def test_tmdb_identity_accepts_matching_title_year_over_conflicting_plex_year():
    accepted, reason = tmdb_module.tmdb_identity_consistent(
        "movie",
        "Monster (2022)",
        2024,
        {
            "title": "Monster",
            "original_title": "Monster",
            "release_date": "2022-05-01",
        },
    )

    assert accepted is True
    assert reason == "matched title year 2022; ignored conflicting Plex year 2024"


def test_tmdb_identity_accepts_trusted_anthology_alias_and_title_year():
    accepted, reason = tmdb_module.tmdb_identity_consistent(
        "tv",
        "Monster (2022)",
        2024,
        {
            "name": "DAHMER - Monster: The Jeffrey Dahmer Story",
            "original_name": "Dahmer – Monster: The Jeffrey Dahmer Story",
            "first_air_date": "2022-09-21",
        },
        trusted_external_id=True,
    )

    assert accepted is True
    assert reason == (
        "trusted external ID matched title year 2022; "
        "ignored conflicting Plex year 2024"
    )


def test_tmdb_identity_keeps_untrusted_anthology_alias_rejected():
    accepted, reason = tmdb_module.tmdb_identity_consistent(
        "tv",
        "Monster (2022)",
        2024,
        {
            "name": "DAHMER - Monster: The Jeffrey Dahmer Story",
            "first_air_date": "2022-09-21",
        },
    )

    assert accepted is False
    assert reason == "year mismatch (2024 vs 2022)"


@pytest.mark.parametrize(
    ("title", "details", "reason"),
    [
        (
            "Different (2022)",
            {"title": "Monster", "release_date": "2022-05-01"},
            "year mismatch (2024 vs 2022)",
        ),
        (
            "Monster (2025)",
            {"title": "Monster", "release_date": "2022-05-01"},
            "title year mismatch (2025 vs 2022)",
        ),
        (
            "Monster",
            {"title": "Monster", "release_date": "2022-05-01"},
            "year mismatch (2024 vs 2022)",
        ),
    ],
)
def test_tmdb_identity_does_not_weaken_genuine_year_rejections(
    title, details, reason
):
    accepted, actual_reason = tmdb_module.tmdb_identity_consistent(
        "movie", title, 2024, details
    )

    assert accepted is False
    assert actual_reason == reason


def test_tmdb_external_id_consensus_trusts_match_and_rejects_conflict():
    details = {"external_ids": {"imdb_id": "tt123", "tvdb_id": 456}}

    assert tmdb_module.tmdb_external_id_consensus(
        "tv", details, imdb_id="TT123", tvdb_id="456"
    ) == (True, True, "matched IMDb, TVDB")

    accepted, trusted, reason = tmdb_module.tmdb_external_id_consensus(
        "tv", details, imdb_id="tt999", tvdb_id="456"
    )
    assert accepted is False
    assert trusted is False
    assert "IMDb tt999 vs tt123" in reason


def test_tmdb_external_id_consensus_allows_explicit_split_tvdb_mapping():
    assert tmdb_module.tmdb_external_id_consensus(
        "tv",
        {"external_ids": {"tvdb_id": 999}},
        tvdb_id="456",
        allow_tvdb_mismatch=True,
    ) == (True, True, "accepted configured split-series TVDB mapping")


def test_tmdb_id_resolution_excludes_a_rejected_external_match(monkeypatch):
    async def request(_config, endpoint, **_kwargs):
        assert endpoint == "find/tt123"
        return {"movie_results": [{"id": 100}, {"id": 101}]}

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)
    resolved = asyncio.run(
        tmdb_module.resolve_tmdb_id(
            {},
            "movie",
            imdb_id="tt123",
            excluded_ids={"100"},
            session=object(),
        )
    )

    assert resolved == "101"


def test_unfiltered_image_request_disables_metadata_locale(monkeypatch):
    calls = []

    async def request(_config, endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return {"posters": []}

    monkeypatch.setattr(tmdb_module, "tmdb_api_request", request)
    asyncio.run(
        tmdb_module.tmdb_unfiltered_images(
            {}, "tv", "10", season_number=2, session=object()
        )
    )

    assert calls[0][0] == "tv/10/season/2/images"
    assert calls[0][1]["include_locale"] is False


def test_job_run_lock_rejects_overlapping_writer(tmp_path):
    first = JobRunLock(tmp_path / ".metafusion-run.lock").acquire()
    try:
        with pytest.raises(JobAlreadyRunningError):
            JobRunLock(tmp_path / ".metafusion-run.lock").acquire()
    finally:
        first.release()

    with JobRunLock(tmp_path / ".metafusion-run.lock"):
        assert (tmp_path / ".metafusion-run.lock").read_text().strip()


def test_heartbeat_retries_after_transient_write_failure(tmp_path, caplog):
    status = RuntimeStatus(tmp_path / "status.json", heartbeat_seconds=0)

    class StopAfterRetry:
        def __init__(self):
            self.calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    attempts = []

    def fail_once(**_values):
        attempts.append(True)
        raise OSError("temporary")

    status._stop = StopAfterRetry()
    status._update = fail_once
    with caplog.at_level(logging.WARNING):
        status._heartbeat_loop()

    assert attempts == [True]
    assert "retrying" in caplog.text


def test_plex_show_inventory_uses_one_episode_request_and_includes_specials():
    calls = []

    class Show:
        title = "Example"
        year = 2020
        type = "show"
        ratingKey = "phase9-show"
        librarySection = SimpleNamespace(title="TV Shows", type="show")
        guids = [SimpleNamespace(id="tmdb://123")]

        def episodes(self):
            calls.append("episodes")
            part = SimpleNamespace(file="/tv/Example/Season 00/Special.mkv")
            media = SimpleNamespace(parts=[part])
            return [
                SimpleNamespace(seasonNumber=0, episodeNumber=1, media=[media]),
                SimpleNamespace(seasonNumber=1, episodeNumber=2, media=[]),
            ]

        def seasons(self):
            raise AssertionError("season inventory should be derived from episodes")

    plex_module._plex_cache.clear()
    metadata = asyncio.run(
        plex_module.get_plex_metadata(
            Show(), _runtime_config={"plex_retries": 1}
        )
    )

    assert calls == ["episodes"]
    assert metadata["seasons_episodes"] == {0: [1], 1: [2]}


def test_plex_operation_retries_transient_failures(monkeypatch):
    attempts = []

    def operation():
        attempts.append(True)
        if len(attempts) < 3:
            raise OSError("temporary")
        return "ok"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(plex_module.asyncio, "sleep", no_sleep)
    result = asyncio.run(
        plex_module.plex_operation(
            operation,
            {"plex_retries": 3, "plex_retry_delay": 1},
            description="test operation",
        )
    )

    assert result == "ok"
    assert len(attempts) == 3


def test_tmdb_redacts_secrets_embedded_in_external_url(monkeypatch):
    events = []
    monkeypatch.setattr(
        tmdb_module,
        "log_tmdb_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    asyncio.run(
        tmdb_module.tmdb_api_request(
            {},
            "https://example.test/image?api_key=url-secret",
            session=None,
        )
    )

    assert "url-secret" not in repr(events)
    assert "%2A%2A%2A" in events[-1][1]["url"]


def test_plex_metadata_error_path_has_initialized_context(monkeypatch):
    events = []
    monkeypatch.setattr(
        "helper.plex.log_plex_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    class FlakyItem:
        title = "Example"
        year = 2020
        type = "movie"
        librarySection = None
        guids = []

        def __init__(self):
            self.rating_key_reads = 0

        @property
        def ratingKey(self):
            self.rating_key_reads += 1
            if self.rating_key_reads == 1:
                raise RuntimeError("temporary rating key failure")
            return "123"

    metadata = asyncio.run(get_plex_metadata(FlakyItem()))

    assert metadata["title"] == "Example"
    assert any(event == "plex_failed_extract_item_id" for event, _ in events)


def test_plex_connection_uses_configured_request_timeout(monkeypatch):
    calls = []

    class Library:
        def sections(self):
            return []

    class Server:
        version = "test"
        library = Library()

    def fake_server(url, token, timeout):
        calls.append((url, token, timeout))
        return Server()

    monkeypatch.setattr(plex_module, "PlexServer", fake_server)
    plex_module.connect_plex_library(
        {
            "plex": {"url": "http://plex:32400", "token": "token"},
            "plex_libraries": ["Movies"],
            "runtime": {"plex_timeout": 7.5},
        }
    )

    assert calls == [("http://plex:32400", "token", 7.5)]


def test_plex_connector_retries_then_reuses_preflight_connection(monkeypatch):
    attempts = []
    sleeps = []

    class Library:
        def sections(self):
            return []

    class Server:
        version = "test"
        library = Library()

    server = Server()

    def flaky_server(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) < 3:
            raise OSError("temporary Plex failure")
        return server

    monkeypatch.setattr(plex_module, "PlexServer", flaky_server)
    monkeypatch.setattr(plex_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    config = {
        "plex": {"url": "http://plex:32400", "token": "token"},
        "plex_libraries": ["Movies"],
        "runtime": {"plex_retries": 3, "plex_retry_delay": 0.5},
    }

    connected = plex_module.connect_plex_server(config)
    result = plex_module.connect_plex_library(config, plex=connected)

    assert connected is server
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]
    assert result == ([], ["Movies"], [])


def test_background_selection_accepts_builder_input_without_extra_arguments():
    config = {
        "background_set": {
            "prefer_vote": 7.0,
            "vote_relaxed": 5.0,
            "max_width": 1920,
            "max_height": 1080,
            "min_width": 1280,
            "min_height": 720,
        }
    }
    image = {"vote_average": 8.0, "width": 1920, "height": 1080}

    assert get_best_background(config, [image]) == image


def test_dry_run_logging_does_not_create_log_directory(monkeypatch, tmp_path):
    log_file = tmp_path / "missing" / "logs" / "metafusion.log"
    monkeypatch.setattr(logging_module, "LOG_FILE", log_file)

    logger = logging_module.get_setup_logging(
        {"settings": {"dry_run": True, "log_level": "INFO"}}
    )
    try:
        assert not log_file.parent.exists()
        assert all(not isinstance(handler, logging.FileHandler) for handler in logger.handlers)
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_logging_filter_redacts_configured_secrets(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(logging_module, "LOG_FILE", tmp_path / "unused.log")
    logger = logging_module.get_setup_logging(
        {
            "settings": {"dry_run": True, "log_level": "INFO"},
            "plex": {"token": "plex-secret"},
            "tmdb": {"api_key": "tmdb-secret"},
        }
    )
    try:
        logger.error("tokens: %s %s", "plex-secret", "tmdb-secret")
        output = capsys.readouterr().out
        assert "plex-secret" not in output
        assert "tmdb-secret" not in output
        assert "tokens: *** ***" in output
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_tmdb_connectivity_error_redacts_api_key(monkeypatch):
    secret = "tmdb-super-secret"
    output = io.StringIO()
    logger = logging.getLogger("metafusion-secret-test")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    monkeypatch.setattr(logging_module.psutil, "cpu_percent", lambda interval: 0)
    monkeypatch.setattr(logging_module, "MIN_PYTHON", (3, 9))

    def fail_request(*args, **kwargs):
        raise RuntimeError(f"request failed for api_key={secret}")

    monkeypatch.setattr(logging_module.requests, "get", fail_request)
    logging_module.check_sys_requirements(
        logger,
        {"plex": {}, "tmdb": {"api_key": secret}},
    )

    assert secret not in output.getvalue()
    assert "api_key=***" in output.getvalue()


def test_plex_connectivity_error_redacts_token(monkeypatch):
    secret = "plex-super-secret"
    output = io.StringIO()
    logger = logging.getLogger("metafusion-plex-secret-test")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(output))
    monkeypatch.setattr(logging_module, "MIN_PYTHON", (3, 9))
    monkeypatch.setattr(logging_module.psutil, "cpu_percent", lambda interval: 0)

    def fail_request(*_args, **kwargs):
        token = kwargs.get("headers", {}).get("X-Plex-Token", "")
        raise RuntimeError(f"request failed with token={token}")

    monkeypatch.setattr(logging_module.requests, "get", fail_request)
    logging_module.check_sys_requirements(
        logger,
        {
            "plex": {"url": "http://plex:32400", "token": secret},
            "tmdb": {},
        },
    )

    assert secret not in output.getvalue()
    assert "token=***" in output.getvalue()


def test_raw_tmdb_response_is_bounded(monkeypatch):
    events = []

    class Content:
        async def iter_chunked(self, _size):
            yield b"12345"

    class Response:
        status = 200
        headers = {"Content-Length": "5"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(
        tmdb_module,
        "log_tmdb_event",
        lambda event, **kwargs: events.append((event, kwargs)),
    )
    result = asyncio.run(
        tmdb_module.tmdb_api_request(
            {"runtime": {"max_image_mb": 1}},
            "https://image.tmdb.org/test.jpg",
            raw=True,
            session=Session(),
            max_response_bytes=4,
        )
    )

    assert result is None
    assert any(event == "tmdb_response_too_large" for event, _ in events)


def test_tmdb_rate_limit_retries_and_recovers(monkeypatch):
    sleeps = []

    class Response:
        headers = {}
        content = None

        def __init__(self, status, payload=None, retry_after=None):
            self.status = status
            self.payload = payload
            if retry_after is not None:
                self.headers = {"Retry-After": str(retry_after)}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self):
            return self.payload

        async def text(self):
            return "rate limited"

    class Session:
        def __init__(self):
            self.responses = [Response(429, retry_after=1), Response(200, {"ok": True})]

        def get(self, *_args, **_kwargs):
            return self.responses.pop(0)

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tmdb_module, "get_tmdb_limiter", lambda: Limiter())
    monkeypatch.setattr(tmdb_module.asyncio, "sleep", lambda seconds: _record_sleep(sleeps, seconds))

    result = asyncio.run(
        tmdb_module.tmdb_api_request(
            {"tmdb": {"api_key": "key", "language": "en", "region": "US"}},
            "configuration",
            retries=2,
            session=Session(),
            cache=False,
        )
    )

    assert result == {"ok": True}
    assert sleeps == [1]


def test_tmdb_not_found_is_not_retried(monkeypatch):
    calls = []
    sleeps = []

    class Response:
        status = 404
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"status_code":34}'

    class Session:
        def get(self, *_args, **_kwargs):
            calls.append(True)
            return Response()

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tmdb_module, "get_tmdb_limiter", lambda: Limiter())
    monkeypatch.setattr(
        tmdb_module.asyncio,
        "sleep",
        lambda seconds: _record_sleep(sleeps, seconds),
    )

    result = asyncio.run(
        tmdb_module.tmdb_api_request(
            {"tmdb": {"api_key": "key"}},
            "movie/1306363",
            retries=3,
            session=Session(),
            cache=False,
        )
    )

    assert result is None
    assert len(calls) == 1
    assert sleeps == []


def test_tmdb_not_found_uses_short_negative_cache(monkeypatch, tmp_path):
    calls = []

    class Response:
        status = 404
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"status_code":34}'

    class Session:
        def get(self, *_args, **_kwargs):
            calls.append(True)
            return Response()

    class Limiter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    cache = tmdb_module.tmdb_response_cache
    cache.configure(tmp_path / "tmdb.sqlite3", ttl_hours=24)
    monkeypatch.setattr(tmdb_module, "get_tmdb_limiter", lambda: Limiter())
    config = {
        "tmdb": {"api_key": "key"},
        "tmdb_cache": {"negative_ttl_hours": 12},
    }
    try:
        first = asyncio.run(
            tmdb_module.tmdb_api_request(
                config, "movie/1306363", retries=3, session=Session()
            )
        )
        second = asyncio.run(
            tmdb_module.tmdb_api_request(
                config, "movie/1306363", retries=3, session=Session()
            )
        )
    finally:
        cache.reset_memory()

    assert first is None
    assert second is None
    assert len(calls) == 1


def test_combined_log_rotation_enforces_size_and_backup_count(tmp_path):
    log_file = tmp_path / "metafusion.log"
    handler = logging_module.SizeAndTimeRotatingFileHandler(
        log_file, max_bytes=80, backup_count=2
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("metafusion-rotation-test")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for index in range(5):
            logger.info("entry-%d-%s", index, "x" * 70)
        handler.next_rollover_at = 0
        logger.info("daily-boundary")
    finally:
        handler.close()
        logger.handlers.clear()

    assert log_file.exists()
    assert len(list(tmp_path.glob("metafusion.log.*"))) == 2

    unlimited = logging_module.SizeAndTimeRotatingFileHandler(
        tmp_path / "unlimited.log", max_bytes=0, backup_count=1
    )
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "line", (), None)
    try:
        assert unlimited.shouldRollover(record) is False
        unlimited.next_rollover_at = 0
        assert unlimited.shouldRollover(record) is True
    finally:
        unlimited.close()


async def _record_sleep(calls, seconds):
    calls.append(seconds)
