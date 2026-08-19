"""Shared storage safety calculations without runtime/config import cycles."""


def storage_pressure_threshold(config, usage):
    configured_mb = max(
        0, int(config.get("runtime", {}).get("min_free_space_mb", 256))
    )
    configured_bytes = configured_mb * 1024 * 1024
    automatic_bytes = min(
        2 * 1024**3,
        max(256 * 1024**2, int(max(0, usage.total) * 0.01)),
    )
    return configured_mb, max(configured_bytes, automatic_bytes)
