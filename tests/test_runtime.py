import asyncio
import io
import logging

from helper import tmdb as tmdb_module
from helper import logging as logging_module
from helper.plex import get_plex_metadata
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
