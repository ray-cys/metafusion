"""One-time, read-only runtime qualification for each published MetaFusion build."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from helper.build_info import build_info
from helper.compatibility import evaluate_compatibility, resolve_compatibility_profile
from helper.config import BASE_CONFIG_DIR
from helper.item_explanation import explain_item
from helper.plex import load_plex_library_inventory
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import (
    STATE_DATABASE,
    load_application_record,
    save_application_record,
)


class UpgradeCanaryError(RuntimeError):
    """Raised before output writes when a new build cannot pass its local canary."""


CANARY_HISTORY_KEY = "upgrade_canary_history_v1"
CANARY_HISTORY_LIMIT = 25


def _state_key(server_id, mode, profile):
    identity = f"{server_id}\0{mode}\0{profile}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"upgrade_canary_v1:{digest}"


def _published_build(current):
    return str(current.get("commit") or "").lower() not in {"", "unknown"} and str(
        current.get("version") or ""
    ).lower() not in {"", "development"}


def upgrade_canary_required(config, server_id, *, current=None, path=None):
    current = build_info() if current is None else dict(current)
    if not config.get("compatibility", {}).get("upgrade_canary", True):
        return False
    if config.get("settings", {}).get("dry_run", False) or not _published_build(current):
        return False
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    profile = resolve_compatibility_profile(config)
    saved = load_application_record(
        _state_key(server_id, mode, profile), path=path or STATE_DATABASE
    )
    return not (
        isinstance(saved, dict)
        and saved.get("passed") is True
        and str(saved.get("commit")) == str(current.get("commit"))
    )


def load_upgrade_canary_history(*, path=None):
    """Load recent detailed canary results from SQLite without creating state."""
    saved = load_application_record(CANARY_HISTORY_KEY, path=path or STATE_DATABASE)
    entries = saved.get("entries", []) if isinstance(saved, dict) else []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


def _store_upgrade_canary_result(result, *, path=None):
    """Persist a bounded detailed result history before any output write occurs."""
    recorded = dict(result)
    recorded["recorded_at"] = datetime.now(timezone.utc).isoformat()
    history = load_upgrade_canary_history(path=path)
    identity = (recorded.get("qualification_scope"), recorded.get("commit"))
    history = [
        entry
        for entry in history
        if (entry.get("qualification_scope"), entry.get("commit")) != identity
    ]
    history.insert(0, recorded)
    save_application_record(
        CANARY_HISTORY_KEY,
        {"entries": history[:CANARY_HISTORY_LIMIT]},
        path=path or STATE_DATABASE,
    )
    return recorded


def write_upgrade_canary_report(result, *, base_dir=None, retention=10):
    """Generate an on-demand report from one stored detailed canary result."""
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / f"upgrade-canary-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    lines = [
        "MetaFusion one-time upgrade canary",
        f"Generated: {generated.isoformat()}",
        f"Version: {result['version']}",
        f"Commit: {result['commit']}",
        f"Mode/profile: {result['mode']}/{result['profile']}",
        f"Result: {'passed' if result['passed'] else 'failed'}",
        "The canary read connectors, inventory, provider identity, policies, and mappings without changing media output.",
        "",
        "Compatibility checks",
    ]
    for check in result.get("checks", []):
        lines.append(
            f"- [{'pass' if check.get('passed') else 'fail'}] "
            f"{check.get('name')}: {check.get('detail')}"
        )
    lines.extend(("", "Deterministic samples"))
    if not result.get("samples"):
        lines.append("- No items were available; connector and contract checks passed.")
    for sample in result.get("samples", []):
        lines.append(
            f"- [{sample.get('status')}] {sample.get('library')} | "
            f"{sample.get('title')} | rating key={sample.get('plex_rating_key')}"
            + (f" | {sample.get('detail')}" if sample.get("detail") else "")
        )
    path = write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type="upgrade_canary",
        data=result,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "upgrade-canary", retention)
    return path


def write_upgrade_canary_report_from_state(*, base_dir=None, retention=10, path=None):
    """Generate a report for the newest detailed canary result stored in SQLite."""
    history = load_upgrade_canary_history(path=path)
    if not history:
        raise UpgradeCanaryError(
            "No stored upgrade canary result is available; run a published build first"
        )
    return write_upgrade_canary_report(
        history[0], base_dir=base_dir, retention=retention
    )


async def run_upgrade_canary(
    sections,
    inventory_by_library,
    config,
    *,
    session,
    server_id,
    plex_version,
    identity_counts=None,
    edition_counts=None,
    current=None,
    base_dir=None,
    path=None,
):
    """Exercise a deterministic sample before allowing a new image to write."""
    current = build_info() if current is None else dict(current)
    if not upgrade_canary_required(config, server_id, current=current, path=path or STATE_DATABASE):
        return None, None
    mode = str(config.get("settings", {}).get("mode", "kometa")).lower()
    profile = resolve_compatibility_profile(config)
    compatibility = evaluate_compatibility(
        config,
        {
            "available_count": len(sections),
            "plex_version": str(plex_version or "unknown"),
            "tmdb_available": True,
            "path_advice": {"records": []},
        },
    )
    samples = []
    library_sample_success = {}
    for section in sections:
        records = list(inventory_by_library.get(section.title, []))
        if not records:
            library_sample_success[section.title] = True
            continue
        chosen = sorted(
            records,
            key=lambda record: (
                str(record.get("media_type") or ""),
                str(record.get("title") or "").casefold(),
                str(record.get("year") or ""),
                str(record.get("rating_key") or ""),
            ),
        )[:2]
        chosen_keys = {str(record.get("rating_key")) for record in chosen}
        library_sample_success[section.title] = False
        try:
            inventory = await load_plex_library_inventory(section, config.get("runtime", {}))
            items = {
                str(getattr(item, "ratingKey", "")): item
                for item in inventory
                if str(getattr(item, "ratingKey", "")) in chosen_keys
            }
            for record in chosen:
                rating_key = str(record.get("rating_key"))
                item = items.get(rating_key)
                if item is None:
                    samples.append(
                        {
                            "library": section.title,
                            "title": record.get("title"),
                            "plex_rating_key": rating_key,
                            "status": "failed",
                            "detail": "sample disappeared during the canary inventory",
                        }
                    )
                    continue
                try:
                    explanation = await explain_item(
                        item,
                        config,
                        session=session,
                        identity_counts=identity_counts,
                        edition_counts=edition_counts,
                    )
                    samples.append(
                        {
                            "library": section.title,
                            "title": record.get("title"),
                            "plex_rating_key": rating_key,
                            "status": "passed",
                            "identity_status": explanation.get("status"),
                        }
                    )
                    library_sample_success[section.title] = True
                except Exception as error:
                    samples.append(
                        {
                            "library": section.title,
                            "title": record.get("title"),
                            "plex_rating_key": rating_key,
                            "status": "failed",
                            "detail": type(error).__name__,
                        }
                    )
        except Exception as error:
            samples.append(
                {
                    "library": section.title,
                    "title": "inventory sample",
                    "plex_rating_key": None,
                    "status": "failed",
                    "detail": type(error).__name__,
                }
            )

    passed = bool(compatibility.get("passed")) and all(library_sample_success.values())
    result = {
        "passed": passed,
        "version": str(current.get("version")),
        "commit": str(current.get("commit")),
        "mode": mode,
        "profile": profile,
        "qualification_scope": _state_key(server_id, mode, profile),
        "checks": compatibility.get("checks", []),
        "warnings": compatibility.get("warnings", []),
        "samples": samples,
    }
    _store_upgrade_canary_result(result, path=path or STATE_DATABASE)
    if not passed:
        raise UpgradeCanaryError(
            "Upgrade canary failed before output writes; run "
            "--upgrade-canary-report for the stored details"
        )
    return result, None


def commit_upgrade_canary(result, *, path=None):
    """Remember a pass only after the surrounding MetaFusion job succeeds."""
    if not isinstance(result, dict) or result.get("passed") is not True:
        return False
    key = result.get("qualification_scope")
    if not key:
        return False
    return save_application_record(
        key,
        {
            "passed": True,
            "version": result.get("version"),
            "commit": result.get("commit"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        path=path or STATE_DATABASE,
    )
