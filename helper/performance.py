import threading
import time
from collections import Counter
from contextvars import ContextVar

from helper.logging import format_fields

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
        "[Performance] Run | %s",
        format_fields(
            ("Total duration", f"{data['elapsed_seconds']:.1f}s"),
            ("Plex inventory", f"{durations.get('plex_inventory', 0.0):.1f}s"),
            (
                "Library processing",
                f"{durations.get('library_processing', 0.0):.1f}s",
            ),
            ("Items/minute", f"{data['items_per_minute']:.1f}"),
        ),
    )
    logger.info(
        "[Performance] Provider: TMDb | %s",
        format_fields(
            ("Requests", int(counters.get("tmdb_requests", 0))),
            ("Cache hits", int(counters.get("tmdb_cache_hits", 0))),
            ("Cache hit rate", f"{data['tmdb_cache_hit_percent']:.1f}%"),
            ("Cache misses", int(counters.get("tmdb_cache_misses", 0))),
            ("Coalesced", int(counters.get("tmdb_coalesced_waits", 0))),
            ("Retries", int(counters.get("tmdb_retries", 0))),
            ("Rate limits", int(counters.get("tmdb_rate_limits", 0))),
            (
                "Rate-limit wait",
                f"{float(counters.get('tmdb_rate_limit_wait_seconds', 0.0)):.1f}s",
            ),
            (
                "Circuit rejections",
                int(counters.get("tmdb_circuit_rejections", 0)),
            ),
        ),
    )
    fanart_activity = sum(
        int(counters.get(name, 0))
        for name in (
            "fanart_requests",
            "fanart_cache_hits",
            "fanart_cache_misses",
            "fanart_coalesced_waits",
            "fanart_rate_limits",
            "fanart_circuit_rejections",
        )
    )
    if fanart_activity:
        logger.info(
            "[Performance] Provider: Fanart.tv | %s",
            format_fields(
                ("Requests", int(counters.get("fanart_requests", 0))),
                ("Cache hits", int(counters.get("fanart_cache_hits", 0))),
                ("Cache misses", int(counters.get("fanart_cache_misses", 0))),
                ("Coalesced", int(counters.get("fanart_coalesced_waits", 0))),
                ("Rate limits", int(counters.get("fanart_rate_limits", 0))),
                (
                    "Circuit rejections",
                    int(counters.get("fanart_circuit_rejections", 0)),
                ),
            ),
        )
    for seconds, library, rating_key in data["slow_items"]:
        logger.debug(
            "[Performance] Slow item | %s",
            format_fields(
                ("Library", library),
                ("Plex rating key", rating_key),
                ("Duration", f"{seconds:.1f}s"),
            ),
        )
