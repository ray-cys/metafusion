import threading
import time
from collections import Counter
from contextvars import ContextVar

_current_tracker = ContextVar("metafusion_performance_tracker", default=None)


class PerformanceTracker:
    """In-process, value-safe performance counters for one MetaFusion job."""

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._lock = threading.Lock()
        self._counters = Counter()
        self._durations = Counter()
        self._slow_items = []

    def increment(self, name, amount=1):
        with self._lock:
            self._counters[str(name)] += amount

    def add_duration(self, name, seconds):
        with self._lock:
            self._durations[str(name)] += max(0.0, float(seconds))

    def record_item(self, library, rating_key, seconds):
        duration = max(0.0, float(seconds))
        with self._lock:
            self._counters["items"] += 1
            self._slow_items.append(
                (duration, str(library or "Unknown"), str(rating_key or "unknown"))
            )
            self._slow_items = sorted(self._slow_items, reverse=True)[:5]

    def snapshot(self):
        with self._lock:
            counters = dict(self._counters)
            durations = dict(self._durations)
            slow_items = list(self._slow_items)
        elapsed = max(0.0, self._clock() - self._started)
        hits = int(counters.get("tmdb_cache_hits", 0))
        misses = int(counters.get("tmdb_cache_misses", 0))
        lookups = hits + misses
        return {
            "elapsed_seconds": elapsed,
            "items_per_minute": (
                float(counters.get("items", 0)) * 60.0 / elapsed if elapsed else 0.0
            ),
            "tmdb_cache_hit_percent": hits * 100.0 / lookups if lookups else 0.0,
            "counters": counters,
            "durations": durations,
            "slow_items": slow_items,
        }


def tracker_for(config):
    tracker = config.get("_performance_tracker") if isinstance(config, dict) else None
    if isinstance(tracker, PerformanceTracker):
        return tracker
    current = _current_tracker.get()
    return current if isinstance(current, PerformanceTracker) else None


def begin_performance_tracking(tracker=None):
    return _current_tracker.set(tracker or PerformanceTracker())


def reset_performance_tracking(token):
    _current_tracker.reset(token)


def log_performance_summary(logger, tracker):
    if tracker is None:
        return
    data = tracker.snapshot()
    counters = data["counters"]
    durations = data["durations"]
    logger.info(
        "[Performance] Total %.1fs; Plex inventory %.1fs; library processing %.1fs; "
        "items/min %.1f; TMDb requests %d; cache hits %d (%.1f%%); misses %d; "
        "coalesced %d; retries %d; rate limits %d (waited %.1fs); "
        "circuit rejections %d.",
        data["elapsed_seconds"],
        durations.get("plex_inventory", 0.0),
        durations.get("library_processing", 0.0),
        data["items_per_minute"],
        int(counters.get("tmdb_requests", 0)),
        int(counters.get("tmdb_cache_hits", 0)),
        data["tmdb_cache_hit_percent"],
        int(counters.get("tmdb_cache_misses", 0)),
        int(counters.get("tmdb_coalesced_waits", 0)),
        int(counters.get("tmdb_retries", 0)),
        int(counters.get("tmdb_rate_limits", 0)),
        float(counters.get("tmdb_rate_limit_wait_seconds", 0.0)),
        int(counters.get("tmdb_circuit_rejections", 0)),
    )
    for seconds, library, rating_key in data["slow_items"]:
        logger.info(
            "[Performance] Slow item: library %s, rating key %s, %.1fs.",
            library,
            rating_key,
            seconds,
        )
