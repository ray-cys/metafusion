import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from helper import fanart, tmdb
from helper.concurrency import CircuitOpenError


class Body:
    def __init__(self, value, *, chunks=None):
        self.value = value
        self.chunks = chunks

    async def iter_chunked(self, _size):
        if self.chunks is not None:
            for chunk in self.chunks:
                yield chunk
        else:
            yield self.value


class Response:
    def __init__(self, status=200, payload=None, headers=None, *, chunks=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = Body(raw, chunks=chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return self.responses.pop(0)


class Cache:
    enabled = True

    def __init__(self, value=...):
        self.value = value
        self.saved = {}

    def get(self, _key, default=None):
        return default if self.value is ... else self.value

    def __setitem__(self, key, value):
        self.saved[key] = value

    def set(self, key, value, **_kwargs):
        self.saved[key] = value

    def flush(self):
        return True

    def stats(self):
        return {"health": "degraded", "last_error": "locked"}


class Tracker:
    def __init__(self):
        self.events = []

    def increment(self, *args):
        self.events.append(args)


def _config():
    return {
        "settings": {"dry_run": False},
        "runtime": {"max_image_mb": 1},
        "tmdb_cache": {"enabled": True, "negative_ttl_hours": 1},
    }


@pytest.fixture(autouse=True)
def provider_key(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "key")
    fanart._authorization_disabled.clear()
    fanart._missing_key_logged.clear()


def test_fanart_cache_degraded_and_positive_cache(monkeypatch):
    cache = Cache({"movieposter": []})
    monkeypatch.setattr(fanart, "fanart_response_cache", cache)
    tracker = Tracker()
    monkeypatch.setattr(fanart, "tracker_for", lambda _config: tracker)

    assert fanart.flush_fanart_cache() is True
    result = asyncio.run(
        fanart.fanart_api_request(_config(), "movies", "1", session=Session())
    )
    assert result == {"movieposter": []}
    assert ("fanart_cache_hits",) in tracker.events


def test_fanart_missing_key_session_and_invalid_identifiers(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "")

    async def run():
        first = await fanart.fanart_api_request(_config(), "movies", "1", session=Session())
        second = await fanart.fanart_api_request(_config(), "movies", "1", session=Session())
        return first, second

    assert asyncio.run(run()) == (None, None)
    monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "key")
    assert asyncio.run(fanart.fanart_api_request(_config(), "movies", "x", session=Session())) is None
    assert asyncio.run(fanart.fanart_api_request(_config(), "movies", "1", session=None)) is None


def test_fanart_rejects_large_malformed_and_non_object_responses(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_response_cache", Cache())
    too_large = Response(
        payload={}, headers={"Content-Type": "application/json", "Content-Length": str(2 * 1024 * 1024)}
    )
    chunked = Response(payload=b"", chunks=[b"x" * (1024 * 1024), b"x"])
    malformed = Response(payload=b"not-json")
    non_object = Response(payload=[])

    async def run():
        results = []
        for response in (too_large, chunked, malformed, non_object):
            results.append(
                await fanart.fanart_api_request(
                    _config(), "movies", "1", session=Session(response), cache=False
                )
            )
        return results

    assert asyncio.run(run()) == [None, None, None, None]


def test_fanart_retry_invalid_header_server_failure_and_circuit(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_response_cache", Cache())
    tracker = Tracker()
    monkeypatch.setattr(fanart, "tracker_for", lambda _config: tracker)
    sleeps = []

    async def no_sleep(value):
        sleeps.append(value)

    monkeypatch.setattr(fanart.asyncio, "sleep", no_sleep)
    session = Session(
        Response(429, {}, headers={"Retry-After": "invalid"}),
        Response(503, {}),
    )
    assert asyncio.run(
        fanart.fanart_api_request(
            _config(), "movies", "1", session=session, retries=2, delay=2, cache=False
        )
    ) is None
    assert sleeps == [2]
    assert ("fanart_rate_limits",) in tracker.events

    @asynccontextmanager
    async def open_circuit(*_args, **_kwargs):
        raise CircuitOpenError("fanart", 4)
        yield

    monkeypatch.setattr(fanart, "runtime_slot", open_circuit)
    assert asyncio.run(
        fanart.fanart_api_request(
            _config(), "movies", "2", session=Session(Response(200, {})), cache=False
        )
    ) is None
    assert ("fanart_circuit_rejections",) in tracker.events


def test_fanart_cancel_propagates_and_coalesces_waiters(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_response_cache", Cache())
    tracker = Tracker()
    monkeypatch.setattr(fanart, "tracker_for", lambda _config: tracker)
    started = asyncio.Event()
    release = asyncio.Event()

    class WaitingResponse(Response):
        async def __aenter__(self):
            started.set()
            await release.wait()
            return self

    async def run():
        session = Session(WaitingResponse(200, {}))
        owner = asyncio.create_task(
            fanart.fanart_api_request(_config(), "movies", "9", session=session, cache=False)
        )
        await started.wait()
        waiter = asyncio.create_task(
            fanart.fanart_api_request(_config(), "movies", "9", session=session, cache=False)
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release.set()
        assert await owner == {}
        return session.calls

    assert asyncio.run(run()) == 1
    assert ("fanart_coalesced_waits",) in tracker.events


def test_fanart_candidate_normalization_and_selection_edges(monkeypatch):
    assert fanart._normalize_candidate(None, asset_type="poster") is None
    assert fanart._normalize_candidate({"url": "http://bad"}, asset_type="poster") is None
    normalized = fanart._normalize_candidate(
        {"url": "https://image", "lang": "00", "likes": "bad", "width": "bad"},
        asset_type="poster",
    )
    assert normalized["iso_639_1"] is None
    assert normalized["vote_average"] == 0
    assert normalized["width"] == 0

    async def fake_request(*_args, **_kwargs):
        return {
            "seasonposter": [
                {"url": "https://s1", "season": "1"},
                {"url": "https://s2", "season": "2"},
            ],
            "tvposter": "invalid",
        }

    monkeypatch.setattr(fanart, "fanart_api_request", fake_request)

    async def run():
        return (
            await fanart.fanart_artwork_candidates(_config(), "tv", asset_type="poster", tvdb_id=1),
            await fanart.fanart_artwork_candidates(_config(), "tv", asset_type="logo", tvdb_id=1),
            await fanart.fanart_artwork_candidates(_config(), "tv", asset_type="season", tvdb_id=1, season_number=2),
            await fanart.fanart_artwork_candidates(_config(), "movie", asset_type="poster"),
        )

    poster, invalid, season, missing = asyncio.run(run())
    assert poster == []
    assert invalid == []
    assert [value["file_path"] for value in season] == ["https://s2"]
    assert missing == []


def test_tmdb_cache_language_and_limited_body_edges(monkeypatch):
    class TMDbCache(Cache):
        def stats(self):
            return {"health": "degraded", "last_error": "locked"}

    cache = TMDbCache({"cached": True})
    monkeypatch.setattr(tmdb, "tmdb_response_cache", cache)
    tracker = Tracker()
    monkeypatch.setattr(tmdb, "tracker_for", lambda _config: tracker)
    assert tmdb.flush_tmdb_cache() is True
    assert tmdb.artwork_language_codes(
        {"tmdb": {"language": "en-US", "fallback": "zh-CN"}}
    ) == "en,zh,null"

    invalid_length = Response(
        payload=b"body",
        headers={"Content-Length": "invalid"},
    )
    assert asyncio.run(tmdb._read_limited(invalid_length, 100)) == b"body"
    oversized = Response(payload=b"", chunks=[b"123", b"456"])
    with pytest.raises(tmdb.ResponseTooLargeError):
        asyncio.run(tmdb._read_limited(oversized, 5))

    result = asyncio.run(
        tmdb.tmdb_api_request(
            {"tmdb": {"api_key": "key"}},
            "movie/1",
            session=Session(),
        )
    )
    assert result == {"cached": True}
    assert ("tmdb_cache_hits",) in tracker.events


def test_tmdb_episode_group_rejects_malformed_and_ambiguous_layouts(monkeypatch):
    responses = iter(
        [
            {
                "results": [
                    {"type": 1},
                    {"id": "bad-group", "type": 1},
                    {"id": "duplicate", "type": 1},
                ]
            },
            {"groups": [{"order": "bad", "episodes": []}]},
            {
                "groups": [
                    {
                        "order": 1,
                        "episodes": [
                            {"order": "bad", "id": 1},
                            {"order": 1, "id": 2},
                        ],
                    },
                    {"order": 1, "episodes": [{"order": 1, "id": 3}]},
                ]
            },
        ]
    )

    async def request(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(tmdb, "tmdb_api_request", request)
    result = asyncio.run(
        tmdb.resolve_episode_group_mapping(
            {"tmdb": {"episode_group_fallback": True}},
            "1",
            {1: [1]},
            episode_ordering="tmdb_aired",
        )
    )
    assert result is None
    assert asyncio.run(
        tmdb.resolve_episode_group_mapping(
            {"tmdb": {"episode_group_fallback": False}}, "1", {1: [1]}
        )
    ) is None
    assert asyncio.run(
        tmdb.resolve_episode_group_mapping({}, "1", {})
    ) is None
