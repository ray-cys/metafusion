"""Read-only comparison of two effective MetaFusion configurations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from helper.config import BASE_CONFIG_DIR, SECRET_PATHS
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import find_media_state


def _leaves(value, prefix=()):
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).startswith("_"):
                continue
            yield from _leaves(nested, (*prefix, key))
    else:
        yield prefix, value


def _safe_value(path, value):
    return "<redacted>" if tuple(path) in SECRET_PATHS else value


def _impact(path, before, after):
    dotted = ".".join(path)
    severity = "information"
    effect = "Effective value changes; affected items may be selected again."
    if dotted in {"settings.mode", "settings.path", "plex.path_mappings"}:
        severity = "high"
        effect = "Output destinations or ownership scope change; run a plan and verify mappings."
    elif dotted in {"plex_libraries", "cleanup.run_cleanup"}:
        severity = "high"
        effect = "Library or deletion scope changes; cleanup remains full-scan guarded."
    elif dotted in {"assets.update_policy", "plex_metadata.policy", "plex_metadata.allow_overwrite"}:
        severity = "high"
        effect = "Existing output replacement eligibility changes."
    elif dotted.startswith(("assets.run_", "metadata.run_", "plex_metadata.enabled")):
        severity = "medium"
        effect = "A processing lane is enabled or disabled."
    elif dotted.startswith(("poster_set.", "season_set.", "background_set.", "tmdb.")):
        severity = "medium"
        effect = "Provider selection or identity behavior changes; artwork or metadata may be reconsidered."
    elif dotted.startswith(("incremental.", "image_upgrades.", "library_overrides.")):
        severity = "medium"
        effect = "Selection cadence changes without changing existing output ownership."
    elif dotted.startswith("cleanup."):
        severity = "high"
        effect = "Cleanup confirmation, grace, or managed-output eligibility changes."
    return {
        "path": dotted,
        "before": _safe_value(path, before),
        "after": _safe_value(path, after),
        "severity": severity,
        "effect": effect,
    }


def compare_configurations(current, proposed):
    before = dict(_leaves(current))
    after = dict(_leaves(proposed))
    changes = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            changes.append(_impact(path, before.get(path), after.get(path)))
    state_items = find_media_state()
    high = sum(change["severity"] == "high" for change in changes)
    medium = sum(change["severity"] == "medium" for change in changes)
    fingerprint_change = any(
        change["path"].startswith(
            (
                "settings.mode", "metadata.", "kometa.", "assets.",
                "plex_metadata.", "plex.path_mappings", "image_upgrades.",
                "tmdb.", "poster_set.", "season_set.", "background_set.",
            )
        )
        for change in changes
    )
    return {
        "changes": changes,
        "summary": {
            "changed_values": len(changes),
            "high_risk": high,
            "medium_risk": medium,
            "recorded_items_potentially_reselected": (
                len(state_items) if fingerprint_change else 0
            ),
            "cleanup_requires_live_plan": any(
                change["path"].startswith(("cleanup.", "plex_libraries", "settings.mode"))
                for change in changes
            ),
        },
    }


def write_config_impact_report(
    current,
    proposed,
    *,
    proposed_path=None,
    base_dir=None,
    retention=10,
):
    result = compare_configurations(current, proposed)
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / (
        f"configuration-impact-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt"
    )
    summary = result["summary"]
    lines = [
        "MetaFusion configuration impact comparison",
        f"Generated: {generated.isoformat()}",
        f"Proposed configuration: {proposed_path or 'provided configuration'}",
        "This is a local comparison. Plex, providers, YAML, and artwork files were not contacted.",
        f"Changed values: {summary['changed_values']}",
        f"High-risk changes: {summary['high_risk']}",
        f"Medium-risk changes: {summary['medium_risk']}",
        "Recorded items potentially selected again: "
        f"{summary['recorded_items_potentially_reselected']}",
        "",
        "Changes",
    ]
    if not result["changes"]:
        lines.append("- none; the effective configurations are equivalent")
    for change in result["changes"]:
        lines.append(
            f"- [{change['severity']}] {change['path']}: "
            f"{change['before']!r} -> {change['after']!r} | {change['effect']}"
        )
    if summary["cleanup_requires_live_plan"]:
        lines.extend(
            (
                "",
                "Cleanup note",
                "- This report cannot prove current Plex absence. Run --plan with the proposed configuration before enabling cleanup.",
            )
        )
    path = write_diagnostic_report(
        path,
        "\n".join(lines) + "\n",
        report_type="configuration_impact",
        data=result,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "configuration-impact", retention)
    return result, path
