"""Runtime compatibility profiles for MetaFusion output contracts."""

PROFILES = {
    "kometa-2.4": {
        "mode": "kometa",
        "contract": "Kometa metadata schema 2.4.x",
        "capabilities": (
            "metadata YAML generation",
            "movie/show/season artwork trees",
            "season 0 and episode metadata",
            "pinned Kometa 2.4.8 CI validation",
        ),
    },
    "plex-api-v1": {
        "mode": "plex",
        "contract": "PlexAPI metadata and media-sidecar contract v1",
        "capabilities": (
            "media-sidecar artwork",
            "fill-missing/managed/overwrite metadata policies",
            "field-lock preservation and MetaFusion-owned locks",
            "Plex path-mapping discovery",
        ),
    },
}


def resolve_compatibility_profile(config, requested=None):
    configured = requested or config.get("compatibility", {}).get("profile", "auto")
    normalized = str(configured or "auto").strip().lower()
    if normalized == "auto":
        mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
        return "plex-api-v1" if mode == "plex" else "kometa-2.4"
    return normalized


def evaluate_compatibility(config, preflight=None, requested=None):
    """Return a value-safe compatibility assessment for the selected mode."""
    profile_name = resolve_compatibility_profile(config, requested=requested)
    profile = PROFILES.get(profile_name)
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    checks = []
    warnings = []

    def add(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    add("Known profile", profile is not None, profile_name)
    if profile is None:
        return {
            "profile": profile_name,
            "mode": mode,
            "contract": "unknown",
            "capabilities": (),
            "checks": checks,
            "warnings": warnings,
            "passed": False,
        }

    add(
        "Output mode",
        profile["mode"] == mode,
        f"profile={profile['mode']}, configured={mode}",
    )
    preflight = preflight or {}
    available = int(preflight.get("available_count") or 0)
    if preflight:
        add("Plex connector", available > 0, f"{available} supported libraries")
        add(
            "TMDb connector",
            bool(preflight.get("tmdb_available", True)),
            "configuration endpoint available",
        )

    if mode == "kometa":
        validate_schema = bool(config.get("output", {}).get("validate_schema", True))
        add("Kometa schema validation", validate_schema, "enabled" if validate_schema else "disabled")
        add(
            "Kometa output root",
            bool(str(config.get("settings", {}).get("path") or "").strip()),
            "configured",
        )
    else:
        plex_version = str(preflight.get("plex_version") or "unknown")
        add("Plex server identity", plex_version != "unknown", plex_version)
        path_advice = preflight.get("path_advice") or {}
        unresolved = sum(
            record.get("status") == "unresolved"
            for record in path_advice.get("records", [])
            if isinstance(record, dict)
        )
        assets_enabled = any(
            config.get("assets", {}).get(name, False)
            for name in ("run_poster", "run_season", "run_background")
        )
        add(
            "Plex artwork paths",
            not assets_enabled or unresolved == 0,
            "not required"
            if not assets_enabled
            else ("resolved" if unresolved == 0 else f"{unresolved} unresolved"),
        )
        if not config.get("plex_metadata", {}).get("enabled", False):
            warnings.append("Direct Plex metadata updates are disabled; artwork-only Plex mode remains supported.")

    return {
        "profile": profile_name,
        "mode": mode,
        "contract": profile["contract"],
        "capabilities": profile["capabilities"],
        "checks": checks,
        "warnings": warnings,
        "passed": all(check["passed"] for check in checks),
    }
