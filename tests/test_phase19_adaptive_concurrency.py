import asyncio
from contextlib import suppress

import pytest

from helper import concurrency
from helper import tmdb as tmdb_module
from helper.plex import plex_operation


GIB = 1024 ** 3


def resources(cpus=8, memory_gib=8):
    return concurrency.RuntimeResources(
        cpu_cores=float(cpus),
        memory_limit_bytes=int(memory_gib * GIB),
    )


def test_cgroup_v2_limits_drive_automatic_ceiling(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    (tmp_path / "memory.max").write_text(str(2 * GIB), encoding="utf-8")
    (tmp_path / "memory.current").write_text(str(GIB), encoding="utf-8")
    monkeypatch.setattr(concurrency.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        concurrency.os,
        "sched_getaffinity",
        lambda _pid: set(range(8)),
        raising=False,
    )
    monkeypatch.setattr(concurrency, "_host_memory_bytes", lambda: 64 * GIB)

    detected = concurrency.detect_runtime_resources(tmp_path)

    assert detected.cpu_cores == 2
    assert detected.memory_limit_bytes == 2 * GIB
    assert detected.memory_current_path == tmp_path / "memory.current"
    assert concurrency.concurrency_ceiling(
        {"runtime": {"max_concurrency": 0}},
        "item",
        resources=detected,
    ) == 2


def test_positive_configuration_is_only_a_safety_ceiling():
    detected = resources(cpus=8, memory_gib=8)

    assert concurrency.concurrency_ceiling(
        {"runtime": {"max_concurrency": 0}},
        "item",
        resources=detected,
    ) == 10
    assert concurrency.concurrency_ceiling(
        {"runtime": {"max_concurrency": 3}},
        "item",
        resources=detected,
    ) == 3
    assert concurrency.concurrency_ceiling(
        {"runtime": {"max_concurrency": 64}},
        "plex",
        resources=detected,
    ) == 4


def test_healthy_work_grows_lane_one_slot_at_a_time():
    async def run():
        controller = concurrency.AdaptiveConcurrencyController(
            {"runtime": {"max_concurrency": 0}},
            resources=resources(),
            healthy_windows={"item": 2},
            pressure_interval=1000,
        )
        assert controller.current_limit("item") == 4
        for _ in range(2):
            lease = await controller.lane("item").acquire()
            await controller.release("item", lease, 0.1)
        return controller

    controller = asyncio.run(run())

    assert controller.current_limit("item") == 5
    assert controller.snapshot()["lanes"]["item"]["increases"] == 1


def test_rate_limit_reduces_only_tmdb_lane():
    async def run():
        controller = concurrency.AdaptiveConcurrencyController(
            {"runtime": {"max_concurrency": 0}},
            resources=resources(),
            pressure_interval=1000,
        )
        lease = await controller.lane("tmdb").acquire()
        lease.failure("rate_limit", cooldown=5)
        await controller.release("tmdb", lease, 0.1)
        return controller

    controller = asyncio.run(run())

    assert controller.current_limit("tmdb") == 2
    assert controller.current_limit("plex") == 4
    assert controller.current_limit("item") == 4


def test_repeated_failures_open_then_half_open_provider_circuit():
    clock = [100.0]

    async def run():
        lane = concurrency.AdaptiveLane(
            "tmdb",
            4,
            8,
            clock=lambda: clock[0],
            failure_threshold=3,
            circuit_cooldown=30,
        )
        for _ in range(3):
            lease = await lane.acquire()
            lease.failure("server_error")
            await lane.release(lease, 0.1)
        with pytest.raises(concurrency.CircuitOpenError):
            await lane.acquire()
        clock[0] += 31
        probe = await lane.acquire()
        assert probe.half_open is True
        await lane.release(probe, 0.1)
        normal = await lane.acquire()
        await lane.release(normal, 0.1)
        return lane

    lane = asyncio.run(run())

    assert lane.open_until == 0
    assert lane.half_open_active is False
    assert lane.rejections == 1


def test_resource_pressure_reduces_item_work_without_failing_it():
    async def run():
        controller = concurrency.AdaptiveConcurrencyController(
            {"runtime": {"max_concurrency": 0}},
            resources=resources(),
            pressure_probe=lambda: {
                "cpu_percent": 95,
                "memory_percent": 20,
            },
            pressure_interval=0,
            healthy_windows={"item": 1},
        )
        lease = await controller.lane("item").acquire()
        await controller.release("item", lease, 0.1)
        return controller

    controller = asyncio.run(run())

    assert controller.current_limit("item") == 3
    lane = controller.snapshot()["lanes"]["item"]
    assert lane["successes"] == 1
    assert lane["failures"] == 0


class BlockingResponse:
    status = 200
    headers = {}

    def __init__(self, release):
        self.release = release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        await self.release.wait()
        return {"id": 42}


class CountingSession:
    def __init__(self, release):
        self.release = release
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return BlockingResponse(self.release)


class UnlimitedRateLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def test_identical_tmdb_requests_share_one_inflight_response(monkeypatch):
    async def run():
        release = asyncio.Event()
        session = CountingSession(release)
        monkeypatch.setattr(
            tmdb_module,
            "get_tmdb_limiter",
            lambda: UnlimitedRateLimiter(),
        )
        requests = [
            asyncio.create_task(
                tmdb_module.tmdb_api_request(
                    {
                        "tmdb": {"api_key": "key"},
                        "runtime": {"max_concurrency": 2},
                    },
                    "movie/42",
                    cache=False,
                    session=session,
                )
            )
            for _ in range(10)
        ]
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(*requests), session.calls

    results, calls = asyncio.run(run())

    assert results == [{"id": 42}] * 10
    assert calls == 1


def test_cancelled_tmdb_waiter_does_not_cancel_shared_request(monkeypatch):
    async def run():
        release = asyncio.Event()
        session = CountingSession(release)
        monkeypatch.setattr(
            tmdb_module,
            "get_tmdb_limiter",
            lambda: UnlimitedRateLimiter(),
        )
        config = {
            "tmdb": {"api_key": "key"},
            "runtime": {"max_concurrency": 2},
        }
        owner = asyncio.create_task(
            tmdb_module.tmdb_api_request(
                config,
                "movie/42",
                cache=False,
                session=session,
            )
        )
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            tmdb_module.tmdb_api_request(
                config,
                "movie/42",
                cache=False,
                session=session,
            )
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with suppress(asyncio.CancelledError):
            await waiter
        release.set()
        return await owner, session.calls

    result, calls = asyncio.run(run())

    assert result == {"id": 42}
    assert calls == 1


class FailedResponse:
    status = 500
    headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return "temporary"


class FailingSession:
    def __init__(self):
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        return FailedResponse()


def test_repeated_tmdb_failures_open_provider_circuit(monkeypatch):
    async def run():
        config = {
            "tmdb": {"api_key": "key"},
            "runtime": {"max_concurrency": 4},
        }
        session = FailingSession()
        monkeypatch.setattr(
            tmdb_module,
            "get_tmdb_limiter",
            lambda: UnlimitedRateLimiter(),
        )
        controller, token = concurrency.begin_adaptive_concurrency(
            config,
            resources=resources(),
            pressure_interval=1000,
        )
        try:
            for index in range(5):
                assert await tmdb_module.tmdb_api_request(
                    config,
                    f"movie/{index}",
                    retries=1,
                    cache=False,
                    session=session,
                ) is None
            calls_before_rejection = session.calls
            assert await tmdb_module.tmdb_api_request(
                config,
                "movie/rejected",
                retries=1,
                cache=False,
                session=session,
            ) is None
            return controller.snapshot(), session.calls, calls_before_rejection
        finally:
            concurrency.finish_adaptive_concurrency(controller, token)

    snapshot, calls, calls_before_rejection = asyncio.run(run())

    assert calls_before_rejection == 5
    assert calls == calls_before_rejection
    assert snapshot["lanes"]["tmdb"]["circuit_rejections"] == 1


def test_repeated_plex_failures_fail_fast_after_circuit_opens():
    async def run():
        config = {"runtime": {"max_concurrency": 4}}
        calls = 0

        def fail():
            nonlocal calls
            calls += 1
            raise OSError("Plex unavailable")

        controller, token = concurrency.begin_adaptive_concurrency(
            config,
            resources=resources(),
            pressure_interval=1000,
        )
        try:
            with suppress(RuntimeError):
                await plex_operation(
                    fail,
                    {"plex_retries": 3, "plex_retry_delay": 0},
                    description="Read inventory",
                )
            with pytest.raises(RuntimeError, match="circuit is cooling down"):
                await plex_operation(
                    fail,
                    {"plex_retries": 3, "plex_retry_delay": 0},
                    description="Read inventory",
                )
            return controller.snapshot(), calls
        finally:
            concurrency.finish_adaptive_concurrency(controller, token)

    snapshot, calls = asyncio.run(run())

    assert calls == 5
    assert snapshot["lanes"]["plex"]["circuit_rejections"] == 1
