import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from helper import fanart, tmdb
from helper.concurrency import CircuitOpenError
from helper.performance import PerformanceTracker


class Cache:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def __setitem__(self, key, value):
        self.values[key] = value

    def set(self, key, value, **_kwargs):
        self.values[key] = value


class Lease:
    def __init__(self):
        self.failures = []

    def failure(self, *args, **kwargs):
        self.failures.append((args, kwargs))


@asynccontextmanager
async def slot(*_args, **_kwargs):
    yield Lease()


class Content:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, _size):
        yield self.body


class FanartResponse:
    def __init__(self, status=200, payload=None, headers=None, enter_error=None):
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = Content(json.dumps(payload or {}).encode())
        self.enter_error = enter_error

    async def __aenter__(self):
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *_args):
        return False


class TMDbResponse:
    def __init__(self, status=200, payload=None, headers=None, text="response"):
        self.status = status
        self.payload = payload or {}
        self.headers = headers or {}
        self._text = text
        self.content = Content(json.dumps(self.payload).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.payload

    async def text(self):
        return self._text


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)

    def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


def _fanart_config():
    return {
        "runtime": {"max_image_mb": 1},
        "tmdb_cache": {"negative_ttl_hours": 1},
        "_performance_tracker": PerformanceTracker(),
    }


def _tmdb_config():
    return {
        "tmdb": {"api_key": "key", "language": "en-US", "region": "US"},
        "runtime": {"max_image_mb": 1},
        "tmdb_cache": {"negative_ttl_hours": 4},
        "_performance_tracker": PerformanceTracker(),
    }


def test_fanart_cache_miss_invalid_value_and_owner_cancellation(monkeypatch):
    monkeypatch.setattr(fanart, "fanart_project_api_key", lambda: "key")
    monkeypatch.setattr(fanart, "fanart_response_cache", Cache())
    monkeypatch.setattr(fanart, "runtime_slot", slot)
    fanart._authorization_disabled.clear()

    assert asyncio.run(
        fanart.fanart_api_request(
            _fanart_config(),
            "movies",
            "1",
            session=Session(FanartResponse(payload={"movieposter": []})),
            _coalesced_owner=True,
        )
    ) == {"movieposter": []}

    async def invalid(*_args, **_kwargs):
        raise ValueError("unexpected stream failure")

    monkeypatch.setattr(fanart, "_read_limited", invalid)
    assert asyncio.run(
        fanart.fanart_api_request(
            _fanart_config(),
            "movies",
            "2",
            session=Session(FanartResponse()),
            cache=False,
            _coalesced_owner=True,
        )
    ) is None

    cancelled = FanartResponse(enter_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            fanart.fanart_api_request(
                _fanart_config(),
                "movies",
                "3",
                session=Session(cancelled),
                cache=False,
                _coalesced_owner=True,
            )
        )


def test_tmdb_cache_miss_authorization_season_404_and_circuit(monkeypatch):
    cache = Cache()
    monkeypatch.setattr(tmdb, "tmdb_response_cache", cache)
    monkeypatch.setattr(tmdb, "runtime_slot", slot)
    config = _tmdb_config()
    assert asyncio.run(
        tmdb.tmdb_api_request(
            config,
            "movie/1",
            session=Session(TMDbResponse(200, {"id": 1})),
            _coalesced_owner=True,
        )
    ) == {"id": 1}
    assert asyncio.run(
        tmdb.tmdb_api_request(
            config,
            "movie/2",
            session=Session(TMDbResponse(401)),
            cache=False,
            retries=1,
            _coalesced_owner=True,
        )
    ) is None
    assert asyncio.run(
        tmdb.tmdb_api_request(
            config,
            "tv/2/season/1",
            session=Session(TMDbResponse(404)),
            retries=1,
            _coalesced_owner=True,
        )
    ) is None

    @asynccontextmanager
    async def open_circuit(*_args, **_kwargs):
        raise CircuitOpenError("tmdb", 3)
        yield

    monkeypatch.setattr(tmdb, "runtime_slot", open_circuit)
    assert asyncio.run(
        tmdb.tmdb_api_request(
            config,
            "movie/3",
            session=Session(TMDbResponse()),
            cache=False,
            retries=1,
            _coalesced_owner=True,
        )
    ) is None


def test_tmdb_coalesced_wait_tracking(monkeypatch):
    monkeypatch.setattr(tmdb, "tmdb_response_cache", Cache())
    monkeypatch.setattr(tmdb, "runtime_slot", slot)
    entered = asyncio.Event()
    released = asyncio.Event()

    class SlowResponse(TMDbResponse):
        async def __aenter__(self):
            entered.set()
            await released.wait()
            return self

    async def run():
        config = _tmdb_config()
        session = Session(SlowResponse(200, {"id": 4}))
        owner = asyncio.create_task(
            tmdb.tmdb_api_request(config, "movie/4", session=session, cache=False)
        )
        await entered.wait()
        waiter = asyncio.create_task(
            tmdb.tmdb_api_request(config, "movie/4", session=session, cache=False)
        )
        await asyncio.sleep(0)
        released.set()
        assert await owner == {"id": 4}
        assert await waiter == {"id": 4}
        return config["_performance_tracker"].snapshot()["counters"]

    counters = asyncio.run(run())
    assert counters["tmdb_coalesced_waits"] == 1


def test_tmdb_title_hint_and_duplicate_episode_group(monkeypatch):
    assert tmdb._title_year_hint("(2026)") == ("(2026)", None)
    responses = iter(
        [
            {"results": [{"id": "group"}]},
            {
                "groups": [
                    {
                        "order": 1,
                        "episodes": [
                            {"id": 1, "order": 1},
                            {"id": 2, "order": 1},
                        ],
                    }
                ]
            },
        ]
    )

    async def request(*_args, **_kwargs):
        return next(responses)

    monkeypatch.setattr(tmdb, "tmdb_api_request", request)
    assert asyncio.run(
        tmdb.resolve_episode_group_mapping(
            {"tmdb": {"episode_group_fallback": True}},
            "1",
            {1: [1]},
        )
    ) is None
