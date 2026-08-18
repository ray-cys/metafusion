import asyncio
import logging
import math
import os
import time
import weakref
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import psutil


_current_controller = ContextVar("metafusion_concurrency_controller", default=None)
_loop_controllers = weakref.WeakKeyDictionary()

_ABSOLUTE_CEILINGS = {
    "item": 12,
    "tmdb": 8,
    "plex": 4,
    "nested": 4,
}
_HEALTHY_WINDOWS = {
    "item": 12,
    "tmdb": 20,
    "plex": 12,
    "nested": 12,
}


@dataclass(frozen=True)
class RuntimeResources:
    cpu_cores: float
    memory_limit_bytes: int
    memory_current_path: object = None

    @property
    def memory_gib(self):
        return self.memory_limit_bytes / (1024 ** 3)


class CircuitOpenError(RuntimeError):
    def __init__(self, kind, retry_after):
        self.kind = str(kind)
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(
            f"{self.kind} circuit is open for {self.retry_after:.1f} more second(s)"
        )


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _host_memory_bytes():
    try:
        return max(1, int(psutil.virtual_memory().total))
    except (AttributeError, OSError, ValueError):
        try:
            return max(
                1,
                int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")),
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return 4 * 1024 ** 3


def detect_runtime_resources(cgroup_root="/sys/fs/cgroup"):
    """Return effective CPU/memory capacity, honoring cgroup v2 or v1 limits."""
    host_cpus = float(os.cpu_count() or 1)
    try:
        host_cpus = min(host_cpus, float(len(os.sched_getaffinity(0))))
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    root = Path(cgroup_root)
    cpu_limit = None
    cpu_max = _read_text(root / "cpu.max")
    if cpu_max:
        parts = cpu_max.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                cpu_limit = float(parts[0]) / float(parts[1])
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    if cpu_limit is None:
        quota = _read_text(root / "cpu" / "cpu.cfs_quota_us")
        period = _read_text(root / "cpu" / "cpu.cfs_period_us")
        try:
            if quota is not None and period is not None and float(quota) > 0:
                cpu_limit = float(quota) / float(period)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    effective_cpus = max(0.25, min(host_cpus, cpu_limit or host_cpus))

    host_memory = _host_memory_bytes()
    memory_limit = host_memory
    memory_current_path = None
    v2_limit = _read_text(root / "memory.max")
    if v2_limit and v2_limit != "max":
        try:
            parsed = int(v2_limit)
            if 0 < parsed < memory_limit:
                memory_limit = parsed
                memory_current_path = root / "memory.current"
        except ValueError:
            pass
    else:
        v1_limit = _read_text(root / "memory" / "memory.limit_in_bytes")
        try:
            parsed = int(v1_limit) if v1_limit is not None else 0
            if 0 < parsed < memory_limit:
                memory_limit = parsed
                memory_current_path = root / "memory" / "memory.usage_in_bytes"
        except ValueError:
            pass
    return RuntimeResources(
        cpu_cores=effective_cpus,
        memory_limit_bytes=max(1, memory_limit),
        memory_current_path=memory_current_path,
    )


def _configured_ceiling(config):
    value = config.get("runtime", {}).get("max_concurrency", 0)
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, min(64, value))


def automatic_item_ceiling(resources):
    cpu_slots = max(1, int(math.floor(resources.cpu_cores * 2)))
    memory_slots = max(1, int(math.floor(resources.memory_gib / 0.75)))
    return max(1, min(_ABSOLUTE_CEILINGS["item"], cpu_slots, memory_slots))


def concurrency_ceiling(config, kind="item", resources=None):
    normalized = str(kind or "item").lower()
    current = _current_controller.get()
    if (
        resources is None
        and isinstance(current, AdaptiveConcurrencyController)
        and normalized in current.lanes
    ):
        return current.ceiling(normalized)
    resources = resources or detect_runtime_resources()
    item_ceiling = automatic_item_ceiling(resources)
    configured = _configured_ceiling(config)
    if configured:
        item_ceiling = min(item_ceiling, configured)
    if normalized == "item":
        return item_ceiling
    if normalized == "network":
        return min(12, max(2, item_ceiling + min(4, item_ceiling)))
    absolute = _ABSOLUTE_CEILINGS.get(normalized, _ABSOLUTE_CEILINGS["nested"])
    return max(1, min(absolute, item_ceiling))


class ResourcePressureProbe:
    def __init__(self, resources):
        self.resources = resources
        try:
            self.process = psutil.Process()
            self.process.cpu_percent(interval=None)
        except (AttributeError, OSError):
            self.process = None

    def __call__(self):
        cpu_percent = 0.0
        if self.process is not None:
            try:
                raw = max(0.0, float(self.process.cpu_percent(interval=None)))
                cpu_percent = raw / max(0.25, self.resources.cpu_cores)
            except (AttributeError, OSError, TypeError, ValueError):
                cpu_percent = 0.0
        memory_percent = 0.0
        current_path = self.resources.memory_current_path
        if current_path is not None:
            try:
                current = int(_read_text(current_path) or 0)
                memory_percent = current * 100.0 / self.resources.memory_limit_bytes
            except (TypeError, ValueError, ZeroDivisionError):
                memory_percent = 0.0
        else:
            try:
                memory_percent = float(psutil.virtual_memory().percent)
            except (AttributeError, OSError, TypeError, ValueError):
                memory_percent = 0.0
        return {"cpu_percent": cpu_percent, "memory_percent": memory_percent}


class LaneLease:
    def __init__(self, half_open=False):
        self.half_open = bool(half_open)
        self.failed = False
        self.reason = None
        self.cooldown = 0.0
        self.immediate_open = False

    def failure(
        self,
        reason="operation_failure",
        *,
        cooldown=0.0,
        immediate_open=False,
    ):
        self.failed = True
        self.reason = str(reason)
        self.cooldown = max(self.cooldown, float(cooldown or 0.0))
        self.immediate_open = self.immediate_open or bool(immediate_open)


class AdaptiveLane:
    def __init__(
        self,
        kind,
        initial,
        ceiling,
        *,
        clock=None,
        healthy_window=None,
        failure_threshold=5,
        circuit_cooldown=30.0,
        adjustment_callback=None,
    ):
        self.kind = str(kind)
        self.floor = 1
        self.ceiling = max(self.floor, int(ceiling))
        self.limit = max(self.floor, min(self.ceiling, int(initial)))
        self.clock = clock or time.monotonic
        self.healthy_window = max(
            1,
            int(healthy_window or _HEALTHY_WINDOWS.get(self.kind, 12)),
        )
        self.failure_threshold = max(1, int(failure_threshold))
        self.circuit_cooldown = max(1.0, float(circuit_cooldown))
        self.adjustment_callback = adjustment_callback
        self.condition = asyncio.Condition()
        self.active = 0
        self.healthy_count = 0
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.half_open_active = False
        self.successes = 0
        self.failures = 0
        self.rejections = 0
        self.increases = 0
        self.decreases = 0
        self.slow_responses = 0
        self.total_duration = 0.0

    def _notify_adjustment(self, previous, reason):
        if previous == self.limit:
            return
        if self.limit > previous:
            self.increases += 1
        else:
            self.decreases += 1
        if self.adjustment_callback:
            self.adjustment_callback(self.kind, previous, self.limit, reason)

    async def acquire(self):
        async with self.condition:
            now = self.clock()
            half_open = False
            if self.open_until:
                if now < self.open_until:
                    self.rejections += 1
                    raise CircuitOpenError(self.kind, self.open_until - now)
                if self.half_open_active:
                    self.rejections += 1
                    raise CircuitOpenError(self.kind, 0.5)
                self.half_open_active = True
                half_open = True
            while self.active >= self.limit:
                await self.condition.wait()
                now = self.clock()
                if self.open_until and now < self.open_until:
                    self.rejections += 1
                    raise CircuitOpenError(self.kind, self.open_until - now)
            self.active += 1
            return LaneLease(half_open=half_open)

    async def release(self, lease, duration, *, allow_increase=True):
        async with self.condition:
            self.active = max(0, self.active - 1)
            self.total_duration += max(0.0, float(duration))
            now = self.clock()
            if lease.failed:
                self.failures += 1
                self.healthy_count = 0
                self.consecutive_failures += 1
                previous = self.limit
                if lease.reason == "rate_limit":
                    self.limit = max(self.floor, int(math.ceil(self.limit / 2.0)))
                elif self.consecutive_failures >= 2:
                    self.limit = max(self.floor, self.limit - 1)
                self._notify_adjustment(previous, lease.reason or "failure")
                should_open = (
                    lease.immediate_open
                    or lease.half_open
                    or self.consecutive_failures >= self.failure_threshold
                )
                if should_open:
                    cooldown = lease.cooldown or self.circuit_cooldown
                    self.open_until = max(self.open_until, now + cooldown)
                    self.half_open_active = False
                    if self.adjustment_callback:
                        self.adjustment_callback(
                            self.kind,
                            self.limit,
                            self.limit,
                            f"circuit_open:{cooldown:.1f}s",
                        )
            else:
                self.successes += 1
                self.consecutive_failures = 0
                slow_response = self.kind == "plex" and float(duration) >= 5.0
                if slow_response:
                    self.slow_responses += 1
                    previous = self.limit
                    self.limit = max(self.floor, self.limit - 1)
                    self.healthy_count = 0
                    self._notify_adjustment(previous, "slow_response")
                if lease.half_open:
                    self.open_until = 0.0
                    self.half_open_active = False
                    if self.adjustment_callback:
                        self.adjustment_callback(
                            self.kind,
                            self.limit,
                            self.limit,
                            "circuit_closed",
                        )
                if allow_increase and not slow_response and self.limit < self.ceiling:
                    self.healthy_count += 1
                    if self.healthy_count >= self.healthy_window:
                        previous = self.limit
                        self.limit += 1
                        self.healthy_count = 0
                        self._notify_adjustment(previous, "healthy_window")
            self.condition.notify_all()

    async def reduce_for_pressure(self, reason):
        async with self.condition:
            previous = self.limit
            self.limit = max(self.floor, self.limit - 1)
            self.healthy_count = 0
            self._notify_adjustment(previous, reason)
            self.condition.notify_all()

    def snapshot(self):
        return {
            "ceiling": self.ceiling,
            "final_limit": self.limit,
            "active": self.active,
            "successes": self.successes,
            "failures": self.failures,
            "circuit_rejections": self.rejections,
            "increases": self.increases,
            "decreases": self.decreases,
            "slow_responses": self.slow_responses,
            "average_seconds": (
                self.total_duration / max(1, self.successes + self.failures)
            ),
        }


class AdaptiveConcurrencyController:
    def __init__(
        self,
        config,
        *,
        resources=None,
        clock=None,
        pressure_probe=None,
        pressure_interval=5.0,
        healthy_windows=None,
    ):
        self.config = config
        self.resources = resources or detect_runtime_resources()
        self.clock = clock or time.monotonic
        self.pressure_probe = pressure_probe or ResourcePressureProbe(self.resources)
        self.pressure_interval = max(0.0, float(pressure_interval))
        self.last_pressure_check = self.clock()
        self.last_pressure = {"cpu_percent": 0.0, "memory_percent": 0.0}
        self.adjustments = []
        windows = healthy_windows or {}
        self.lanes = {}
        for kind in ("item", "tmdb", "plex", "nested"):
            ceiling = concurrency_ceiling(config, kind, resources=self.resources)
            initial = min(4, ceiling)
            self.lanes[kind] = AdaptiveLane(
                kind,
                initial,
                ceiling,
                clock=self.clock,
                healthy_window=windows.get(kind),
                adjustment_callback=self._record_adjustment,
            )

    def _record_adjustment(self, kind, previous, current, reason):
        event = {
            "kind": str(kind),
            "previous": int(previous),
            "current": int(current),
            "reason": str(reason),
        }
        self.adjustments.append(event)
        logger = logging.getLogger(__name__)
        if str(reason).startswith("circuit_open"):
            logger.warning(
                "[Concurrency] %s circuit opened (%s); limit=%d.",
                kind,
                str(reason).split(":", 1)[-1],
                current,
            )
        elif reason == "circuit_closed":
            logger.info("[Concurrency] %s circuit closed; limit=%d.", kind, current)
        else:
            logger.info(
                "[Concurrency] %s limit %d -> %d (%s).",
                kind,
                previous,
                current,
                reason,
            )

    def lane(self, kind):
        return self.lanes.get(str(kind).lower(), self.lanes["nested"])

    def ceiling(self, kind):
        return self.lane(kind).ceiling

    def current_limit(self, kind):
        return self.lane(kind).limit

    async def release(self, kind, lease, duration):
        now = self.clock()
        check_pressure = (
            str(kind).lower() in {"item", "nested"}
            and now - self.last_pressure_check >= self.pressure_interval
        )
        pressure = self.last_pressure
        if check_pressure:
            self.last_pressure_check = now
            try:
                pressure = dict(self.pressure_probe() or {})
            except (OSError, TypeError, ValueError):
                pressure = {}
            self.last_pressure = pressure
        pressured = max(
            float(pressure.get("cpu_percent", 0.0) or 0.0),
            float(pressure.get("memory_percent", 0.0) or 0.0),
        ) >= 90.0
        lane = self.lane(kind)
        await lane.release(lease, duration, allow_increase=not pressured)
        if check_pressure and pressured:
            await lane.reduce_for_pressure("resource_pressure")

    def snapshot(self):
        return {
            "cpu_cores": self.resources.cpu_cores,
            "memory_gib": self.resources.memory_gib,
            "configured_ceiling": _configured_ceiling(self.config),
            "pressure": dict(self.last_pressure),
            "lanes": {kind: lane.snapshot() for kind, lane in self.lanes.items()},
            "adjustments": list(self.adjustments),
        }


def begin_adaptive_concurrency(config, **kwargs):
    controller = AdaptiveConcurrencyController(config, **kwargs)
    token = _current_controller.set(controller)
    logging.getLogger(__name__).info(
        "[Concurrency] Adaptive mode started: CPU %.2f, memory %.2f GiB; "
        "initial/ceiling item=%d/%d, TMDb=%d/%d, Plex=%d/%d, nested=%d/%d%s.",
        controller.resources.cpu_cores,
        controller.resources.memory_gib,
        controller.current_limit("item"),
        controller.ceiling("item"),
        controller.current_limit("tmdb"),
        controller.ceiling("tmdb"),
        controller.current_limit("plex"),
        controller.ceiling("plex"),
        controller.current_limit("nested"),
        controller.ceiling("nested"),
        (
            f"; configured ceiling={_configured_ceiling(config)}"
            if _configured_ceiling(config)
            else ""
        ),
    )
    return controller, token


def finish_adaptive_concurrency(controller, token=None):
    snapshot = controller.snapshot()
    lanes = snapshot["lanes"]
    logging.getLogger(__name__).info(
        "[Concurrency] Final limits item=%d, TMDb=%d, Plex=%d, nested=%d; "
        "adjustments=%d, circuit rejections=%d.",
        lanes["item"]["final_limit"],
        lanes["tmdb"]["final_limit"],
        lanes["plex"]["final_limit"],
        lanes["nested"]["final_limit"],
        len(snapshot["adjustments"]),
        sum(lane["circuit_rejections"] for lane in lanes.values()),
    )
    if token is not None:
        _current_controller.reset(token)
    return snapshot


def adaptive_controller(config):
    controller = _current_controller.get()
    if isinstance(controller, AdaptiveConcurrencyController):
        return controller
    loop = asyncio.get_running_loop()
    controller = _loop_controllers.get(loop)
    if controller is None:
        controller = AdaptiveConcurrencyController(config)
        _loop_controllers[loop] = controller
    return controller


@asynccontextmanager
async def runtime_slot(config, kind):
    controller = adaptive_controller(config)
    lane = controller.lane(kind)
    lease = await lane.acquire()
    started = time.monotonic()
    try:
        yield lease
    except BaseException as error:
        reason = (
            "timeout" if "timeout" in type(error).__name__.lower() else "exception"
        )
        lease.failure(reason)
        raise
    finally:
        await controller.release(kind, lease, time.monotonic() - started)


async def bounded_map(operation, items, limit=None, *, config=None, kind="nested"):
    """Map an async operation without creating one task for every input."""
    values = list(items)
    if not values:
        return []
    if limit is None:
        limit = concurrency_ceiling(config or {}, kind)
    results = [None] * len(values)
    iterator = iter(enumerate(values))

    async def worker():
        for index, value in iterator:
            if config is None:
                results[index] = await operation(value)
            else:
                async with runtime_slot(config, kind):
                    results[index] = await operation(value)

    workers = [
        asyncio.create_task(worker())
        for _ in range(min(max(1, int(limit)), len(values)))
    ]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        for worker_task in workers:
            worker_task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise
    return results


async def bounded_callables(operations, limit=None, *, config=None, kind="nested"):
    async def invoke(operation):
        return await operation()

    return await bounded_map(
        invoke,
        operations,
        limit,
        config=config,
        kind=kind,
    )
