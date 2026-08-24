import asyncio
import json
import logging
from pathlib import Path

import pytest

from helper import fanart, provider_credentials
from helper.logging import log_item_outcomes
from modules import builder, utils


def artwork_config():
    return {
        "plex": {"url": "http://plex:32400", "token": "plex-secret"},
        "tmdb": {
            "language": "en-US",
            "fallback": [],
            "artwork_allow_any_language": False,
        },
        "poster_set": {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 2000,
            "max_height": 3000,
            "min_width": 1000,
            "min_height": 1500,
        },
        "season_set": {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 2000,
            "max_height": 3000,
            "min_width": 1000,
            "min_height": 1500,
        },
        "background_set": {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 3840,
            "max_height": 2160,
            "min_width": 1920,
            "min_height": 1080,
        },
        "runtime": {"max_concurrency": 2, "max_image_mb": 2},
    }


@pytest.fixture(autouse=True)
def bundled_project_key(monkeypatch):
    monkeypatch.setattr(
        fanart, "fanart_project_api_key", lambda: "project-secret"
    )


def test_metafusion_bundles_application_project_key():
    assert provider_credentials.fanart_project_api_key()


class ChunkedBody:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, _size):
        yield self.body


class Response:
    def __init__(self, status, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = ChunkedBody(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class MemoryCache:
    SCHEMA_VERSION = 1

    def __init__(self):
        self.values = {}
        self.enabled = True
        self.configured = None

    def get(self, key, default=None):
        return self.values.get(key, default)

    def __setitem__(self, key, value):
        self.values[key] = value

    def set(self, key, value, **_kwargs):
        self.values[key] = value

    def configure(self, path, **kwargs):
        self.configured = (path, kwargs)
        self.enabled = bool(kwargs.get("enabled", True))

    def flush(self):
        return True

    def stats(self):
        return {
            "entries": len(self.values),
            "stored_mib": 0,
            "disk_mib": 0,
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "recoveries": 0,
            "health": "ok",
        }

    def maintain(self):
        return {"checkpointed": False}

    def reset_memory(self):
        self.values.clear()


def candidate(path, width, height, provider=None):
    result = {
        "file_path": path,
        "iso_639_1": "en",
        "vote_average": 6,
        "width": width,
        "height": height,
    }
    if provider:
        result["provider"] = provider
    return result


def test_fanart_adapter_uses_project_header_and_normalizes_movie_candidate():
    payload = {
        "movieposter": [
            {
                "id": "44",
                "url": "https://assets.fanart.tv/poster.jpg",
                "lang": "en",
                "likes": "8",
                "width": "1000",
                "height": "1500",
            }
        ]
    }
    session = Session(Response(200, payload))

    async def run():
        response = await fanart.fanart_api_request(
            artwork_config(), "movies", 550, session=session, cache=False
        )
        normalized = await fanart.fanart_artwork_candidates(
            artwork_config(), "movie", tmdb_id=550, session=Session(Response(200, payload))
        )
        return response, normalized

    response, normalized = asyncio.run(run())

    assert response == payload
    assert session.calls[0][0].endswith("/v3.2/movies/550")
    assert session.calls[0][1]["headers"]["api-key"] == "project-secret"
    assert session.calls[0][1]["allow_redirects"] is False
    assert normalized[0]["provider"] == "fanart"
    assert normalized[0]["vote_count"] == 8
    assert normalized[0]["provider_image_id"] == "44"
    assert normalized[0]["width"] == 1000


def test_fanart_404_is_negative_cached_without_retry(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(fanart, "fanart_response_cache", cache)
    session = Session(Response(404, {"status": "not found"}))

    async def run():
        first = await fanart.fanart_api_request(
            artwork_config(), "movies", 404, session=session
        )
        second = await fanart.fanart_api_request(
            artwork_config(), "movies", 404, session=session
        )
        return first, second

    assert asyncio.run(run()) == (None, None)
    assert len(session.calls) == 1
    assert next(iter(cache.values.values()))[fanart._NEGATIVE_STATUS_KEY] == 404


def test_fanart_authorization_failure_disables_provider_for_run(monkeypatch, caplog):
    monkeypatch.setattr(fanart, "fanart_response_cache", MemoryCache())
    session = Session(Response(401, {"status": "invalid key"}))

    async def run():
        first = await fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=session, cache=False
        )
        second = await fanart.fanart_api_request(
            artwork_config(), "movies", 2, session=session, cache=False
        )
        return first, second

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) == (None, None)
    assert len(session.calls) == 1
    assert "disabling Fanart.tv fallback for this run" in caplog.text


def test_fanart_rate_limit_honors_retry_after(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_response_cache", MemoryCache())
    session = SequenceSession(
        [
            Response(429, {}, {"Retry-After": "1"}),
            Response(200, {"movieposter": []}),
        ]
    )
    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(fanart.asyncio, "sleep", no_wait)
    result = asyncio.run(
        fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=session, cache=False
        )
    )

    assert result == {"movieposter": []}
    assert waits == [1]
    assert len(session.calls) == 2


def test_fanart_rejects_malformed_oversized_and_untrusted_identifiers(
    monkeypatch, caplog
):
    monkeypatch.setattr(fanart, "fanart_response_cache", MemoryCache())
    malformed = Session(
        Response(200, b"not-json", {"Content-Type": "application/json"})
    )
    oversized = Session(
        Response(
            200,
            {},
            {"Content-Type": "application/json", "Content-Length": "9999999"},
        )
    )

    async def run():
        bad_json = await fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=malformed, cache=False
        )
        too_large = await fanart.fanart_api_request(
            artwork_config(), "movies", 2, session=oversized, cache=False
        )
        invalid_id = await fanart.fanart_api_request(
            artwork_config(), "movies", "../latest", session=malformed, cache=False
        )
        return bad_json, too_large, invalid_id

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) == (None, None, None)
    assert "malformed response" in caplog.text
    assert "oversized response" in caplog.text
    assert len(malformed.calls) == 1


def test_fanart_network_errors_redact_project_key(monkeypatch, caplog):
    monkeypatch.setattr(fanart, "fanart_response_cache", MemoryCache())

    class BrokenSession:
        def get(self, *_args, **_kwargs):
            raise OSError("request exposed project-secret")

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            fanart.fanart_api_request(
                artwork_config(),
                "movies",
                1,
                session=BrokenSession(),
                retries=1,
                cache=False,
            )
        )
    assert result is None
    assert "project-secret" not in caplog.text
    assert "request exposed ***" in caplog.text


def test_fanart_cache_lifecycle_respects_dry_run_and_missing_key(monkeypatch):
    cache = MemoryCache()
    monkeypatch.setattr(fanart, "fanart_response_cache", cache)
    monkeypatch.setattr(fanart, "CACHE_DIR", Path("/config/cache"))
    config = artwork_config()
    config["settings"] = {"dry_run": True}
    config["tmdb_cache"] = {"enabled": True, "ttl_hours": 12}

    fanart.begin_fanart_cache(config)
    assert cache.configured[0].name == "fanart_cache.sqlite3"
    assert cache.configured[1]["writable"] is False
    assert fanart.flush_fanart_cache() is True

    monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "")
    fanart.begin_fanart_cache(config)
    assert cache.enabled is False
    assert fanart.flush_fanart_cache() is False


def test_fanart_optional_and_unexpected_responses_fail_closed(monkeypatch, caplog):
    monkeypatch.setattr(fanart, "fanart_response_cache", MemoryCache())
    wrong_type = Session(Response(200, {}, {"Content-Type": "text/html"}))
    unexpected = Session(Response(418, {}))

    async def run():
        monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "")
        disabled = await fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=wrong_type
        )
        monkeypatch.setattr(
            fanart, "fanart_project_api_key", lambda: "project-secret"
        )
        no_session = await fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=None
        )
        invalid_type = await fanart.fanart_api_request(
            artwork_config(), "movies", 1, session=wrong_type, cache=False
        )
        unexpected_status = await fanart.fanart_api_request(
            artwork_config(), "movies", 2, session=unexpected, cache=False
        )
        return disabled, no_session, invalid_type, unexpected_status

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) == (None, None, None, None)
    assert "unexpected content type" in caplog.text
    assert "HTTP 418" in caplog.text
    assert fanart._safe_int("invalid") == 0
    assert fanart._safe_float(object()) == 0


def test_fanart_tv_season_candidates_require_tvdb_id_and_matching_season(monkeypatch):
    async def response(*_args, **_kwargs):
        return {
            "seasonposter": [
                {
                    "id": "one",
                    "url": "https://assets.fanart.tv/s1.jpg",
                    "season": "1",
                    "lang": "00",
                    "width": "1000",
                    "height": "1500",
                },
                {
                    "id": "two",
                    "url": "https://assets.fanart.tv/s2.jpg",
                    "season": "2",
                    "lang": "en",
                    "width": "1000",
                    "height": "1500",
                },
            ]
        }

    monkeypatch.setattr(fanart, "fanart_api_request", response)
    selected = asyncio.run(
        fanart.fanart_artwork_candidates(
            artwork_config(),
            "tv",
            tmdb_id=999,
            tvdb_id=81189,
            asset_type="season",
            season_number=2,
            session=object(),
        )
    )

    assert [item["provider_image_id"] for item in selected] == ["two"]


def test_selector_falls_through_fanart_plex_and_best_available(monkeypatch):
    config = artwork_config()

    async def no_unfiltered(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(builder, "tmdb_unfiltered_images", no_unfiltered)

    async def fanart_candidate(*_args, **_kwargs):
        return [candidate("https://assets.fanart.tv/good.jpg", 1000, 1500, "fanart")]

    monkeypatch.setattr(builder, "fanart_artwork_candidates", fanart_candidate)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {"plex_artwork": {"poster": "/library/metadata/1/thumb"}},
            [candidate("/small.jpg", 500, 750)],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected["provider"] == "fanart"
    assert [step["provider"] for step in selected["provider_attempts"]] == [
        "TMDb",
        "Fanart.tv",
    ]

    async def no_fanart(*_args, **_kwargs):
        return []

    monkeypatch.setattr(builder, "fanart_artwork_candidates", no_fanart)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {"plex_artwork": {"poster": "/library/metadata/1/thumb"}},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected["provider"] == "plex"
    assert selected["file_path"] == "http://plex:32400/library/metadata/1/thumb"

    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [candidate("/small.jpg", 500, 750)],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected["provider"] == "tmdb"
    assert "best available" in selected["selection_reason"]


def test_selector_prefers_valid_tmdb_canonical_and_rejects_invalid_canonical():
    config = artwork_config()
    canonical = candidate("/canonical.jpg", 1000, 1500)
    canonical["vote_average"] = 2
    ranked = candidate("/ranked.jpg", 2000, 3000)
    ranked["vote_average"] = 10
    assert builder._tmdb_canonical_candidate([ranked], "/missing.jpg") is None

    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [ranked, canonical],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            canonical_path="/canonical.jpg",
        )
    )

    assert selected["file_path"] == "/canonical.jpg"
    assert selected["tmdb_canonical"] is True
    assert selected["selection_stage"] == "tmdb_canonical"
    assert selected["provider_attempts"][0]["status"] == "selected_canonical"

    invalid = candidate("/invalid.jpg", 500, 750)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [ranked, invalid],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            canonical_path="/invalid.jpg",
        )
    )

    assert selected["file_path"] == "/ranked.jpg"
    assert not selected.get("tmdb_canonical")


def test_season_selector_uses_source_number_for_providers_and_plex_target(
    monkeypatch,
):
    config = artwork_config()
    config["tmdb"]["artwork_allow_any_language"] = True
    observed = []

    async def no_unfiltered(*_args, **kwargs):
        observed.append(("tmdb", kwargs.get("season_number")))
        return {}

    async def no_fanart(*_args, **kwargs):
        observed.append(("fanart", kwargs.get("season_number")))
        return []

    monkeypatch.setattr(builder, "tmdb_unfiltered_images", no_unfiltered)
    monkeypatch.setattr(builder, "fanart_artwork_candidates", no_fanart)
    attempts = []

    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {"plex_artwork": {"seasons": {5: "/library/metadata/5/thumb"}}},
            [],
            asset_type="season",
            media_type="tv",
            tmdb_id=99,
            season_number=2,
            plex_season_number=5,
            session=object(),
            attempts_out=attempts,
        )
    )

    assert observed == [("tmdb", 2), ("fanart", 2)]
    assert selected["provider"] == "plex"
    assert selected["provider_image_id"] == "/library/metadata/5/thumb"
    assert [attempt["provider"] for attempt in attempts] == [
        "TMDb",
        "Fanart.tv",
        "Plex",
    ]


def test_primary_tmdb_candidate_avoids_unneeded_fanart_request(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("Fanart.tv must remain a fallback")

    monkeypatch.setattr(builder, "fanart_artwork_candidates", unexpected)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            artwork_config(),
            {},
            [candidate("/primary.jpg", 1000, 1500)],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected["provider"] == "tmdb"


def test_fanart_candidates_follow_artwork_language_policy(monkeypatch):
    config = artwork_config()

    async def japanese_only(*_args, **_kwargs):
        image = candidate(
            "https://assets.fanart.tv/ja.jpg", 1000, 1500, "fanart"
        )
        image["iso_639_1"] = "ja"
        return [image]

    monkeypatch.setattr(builder, "fanart_artwork_candidates", japanese_only)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected is None

    config["tmdb"]["fallback"] = ["ja"]
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            {},
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )
    assert selected["provider"] == "fanart"


def test_missing_destination_uses_automatic_language_relaxation(
    monkeypatch, tmp_path
):
    config = artwork_config()
    config["settings"] = {"mode": "kometa", "path": str(tmp_path)}
    meta = {"library_type": "movie", "movie_path": "Example (2024)"}

    async def no_tmdb(*_args, **_kwargs):
        return {}

    async def japanese_only(*_args, **_kwargs):
        image = candidate(
            "https://assets.fanart.tv/ja.jpg", 1000, 1500, "fanart"
        )
        image["iso_639_1"] = "ja"
        return [image]

    monkeypatch.setattr(builder, "tmdb_unfiltered_images", no_tmdb)
    monkeypatch.setattr(builder, "fanart_artwork_candidates", japanese_only)

    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            meta,
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )

    assert selected["provider"] == "fanart"
    assert selected["automatic_relaxed"] is True
    assert selected["selection_stage"] == "missing_only_relaxed"
    assert selected["provider_attempts"][-1]["status"] == (
        "selected_missing_only_relaxed"
    )


def test_automatic_relaxation_never_selects_for_existing_artwork(
    monkeypatch, tmp_path
):
    config = artwork_config()
    config["settings"] = {"mode": "kometa", "path": str(tmp_path)}
    meta = {"library_type": "movie", "movie_path": "Example (2024)"}
    destination = (
        tmp_path / "assets" / "movie" / "Example (2024)" / "poster.jpg"
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"manual-artwork")

    async def japanese_only(*_args, **_kwargs):
        image = candidate(
            "https://assets.fanart.tv/ja.jpg", 1000, 1500, "fanart"
        )
        image["iso_639_1"] = "ja"
        return [image]

    monkeypatch.setattr(builder, "fanart_artwork_candidates", japanese_only)
    selected = asyncio.run(
        builder._select_artwork_with_fallback(
            config,
            meta,
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
            session=object(),
        )
    )

    assert selected is None
    assert destination.read_bytes() == b"manual-artwork"


def test_external_download_rejects_redirects_and_untrusted_provider_urls():
    session = Session(Response(302, b"", {"Location": "https://example.com/image.jpg"}))
    result = asyncio.run(
        utils._download_external_artwork(
            artwork_config(),
            "https://assets.fanart.tv/image.jpg",
            provider="fanart",
            session=session,
            retries=1,
        )
    )
    assert result[0] is None
    assert session.calls[0][1]["allow_redirects"] is False

    rejected = asyncio.run(
        utils._download_external_artwork(
            artwork_config(),
            "https://example.com/image.jpg",
            provider="fanart",
            session=session,
        )
    )
    assert rejected[2] == "Rejected untrusted Fanart.tv artwork URL"


def test_aligned_item_logging_identifies_metadata_and_artwork_sources(caplog):
    stats = {
        "metadata_action": "downloaded",
        "percent": 80,
        "poster_action": "downloaded",
        "background_action": "missing",
        "artwork_providers": {"poster": "fanart"},
    }
    flags = {"plex_metadata": False, "mode": "kometa"}

    with caplog.at_level(logging.INFO):
        log_item_outcomes("Movies", "Example (2024)", stats, flags)

    assert "Created | Source: TMDb | Target: Kometa YAML" in caplog.text
    assert "Poster downloaded | Source: Fanart.tv | Target: Kometa assets" in caplog.text
    assert "Background missing | Source: None | Target: Kometa assets" in caplog.text


def test_season_artwork_warning_names_missing_season_and_provider_attempts(caplog):
    stats = {
        "metadata_action": "not_due",
        "poster_action": "not_due",
        "background_action": "not_due",
        "season_poster_actions": {0: "missing", 1: "skipped"},
        "season_artwork_providers": {1: "tmdb"},
        "season_artwork_attempts": {
            0: [
                {"provider": "TMDb", "status": "no_candidates"},
                {"provider": "Fanart.tv", "status": "no_candidates"},
                {"provider": "Plex", "status": "no_candidate"},
            ]
        },
    }

    with caplog.at_level(logging.WARNING):
        log_item_outcomes(
            "TV Shows",
            "Example (2024)",
            stats,
            {"mode": "plex"},
        )

    assert "Unchanged: 1 | Missing: 1 [S00]" in caplog.text
    assert "Sources: None: 1 | TMDb: 1" in caplog.text
    assert "Target: Plex local media" in caplog.text
    assert (
        "Missing seasons: S00 (TMDb:no candidates, Fanart.tv:no candidates, "
        "Plex:no explicit season thumb)"
        in caplog.text
    )
