"""Versioned findings export. Only fresh observations may change incident state."""

import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import chain
from pathlib import Path
from uuid import uuid4

from helper.build_info import build_info
from helper.config import get_feature_flags
from helper.io import atomic_write_json
from helper.metadata_provenance import value_fingerprint
from helper.report_identity import report_identity
from helper.state_db import _connect

RETENTION_DAYS = 30
EMPTY_VALUES = {None, value_fingerprint(""), value_fingerprint([])}


def _event(record, operation, code, *, resolved=False, **evidence):
    return record, operation, code, resolved, evidence


def _events(config, success):
    targeted = config.get("_execution", {}).get("targeted")
    for gap in config.get("_artwork_gaps", []):
        category = gap.get("category") or "unknown"
        asset = gap.get("asset_type") or "metadata"
        if asset.endswith(" season") and gap.get("season_number") is not None:
            asset = f"season {gap['season_number']} poster"
        attempts = [
            {k: a[k] for k in ("provider", "status", "candidates") if k in a}
            for a in gap.get("provider_attempts", [])
        ]
        code = category
        if category == "artwork_missing":
            code = (
                "no_eligible_candidate"
                if any(a.get("candidates") for a in attempts)
                else "no_candidate_returned"
                if attempts
                else "artwork_missing"
            )
        yield _event(
            gap,
            asset,
            code,
            provider_attempts=attempts,
            destination=gap.get("destination"),
            conflicting_owner=gap.get("conflicting_owner"),
        )
    for record in config.get("_artwork_evaluations", []):
        if record.get("destination_state") == "present":
            yield _event(
                record,
                record["asset_type"],
                "destination_present",
                resolved=True,
                provider=record.get("provider"),
                action=record.get("action"),
            )
    for record in config.get("_adoption_audit_records", []):
        if record.get("status") == "filesystem_verified":
            season = record.get("season_number")
            asset = f"season {season} poster" if season is not None else record["asset_type"]
            yield _event(
                record, asset, "filesystem_verified", resolved=True, provider=record.get("provider")
            )
    for record in config.get("_metadata_provenance_records", []):
        action = record.get("action")
        if (
            record.get("target") == "plex_api"
            and action == "unchanged"
            and record.get("value_fingerprint") in EMPTY_VALUES
        ):
            action = "source_missing"
        if action in {
            "source_missing",
            "locked_skipped",
            "filled",
            "tags_added",
            "created",
            "updated",
            "unchanged",
        }:
            yield _event(
                record,
                "field/" + record["field_path"],
                action,
                resolved=action not in {"source_missing", "locked_skipped"},
                policy=record.get("policy"),
                target_value_present=record.get("value_fingerprint") not in EMPTY_VALUES,
                provider=record.get("source_provider"),
            )
    for name, result in config.get("_job_library_results", {}).items():
        passed = result.get("status") == "success"
        yield _event(
            {"library": name},
            "targeted_library_processing" if targeted else "library_processing",
            "library_checked" if passed else "library_processing_failed",
            resolved=passed,
        )
    for name, guard in config.get("_findings_guard", {}).items():
        held = guard["status"] in {"held", "unverified"}
        yield _event(
            {"library": name},
            "inventory",
            guard["status"],
            resolved=not held,
            baseline=guard.get("baseline"),
            observed=guard.get("observed"),
            missing=len(guard.get("missing", [])),
        )
    yield from config.get("_native_observations", [])
    yield _event(
        {},
        "targeted_job" if targeted else "job",
        "completed" if success else "incomplete_coverage",
        resolved=success,
        **config.get("_findings_job_error", {}),
    )


def _finding(config, record, operation):
    library = record.get("library") or record.get("library_name")
    scope = config.get("_findings_scopes", {}).get(library, {})
    server = record.get("server_id") or scope.get("server_id")
    library_id = record.get("library_uuid") or scope.get("library_uuid")
    identity = report_identity(record)
    key = identity["plex_rating_key"]
    mode = config.get("settings", {}).get("mode", "kometa")
    component = record.get("component", "core")
    subject = key or (identity["tmdb_id"], identity["edition"], record.get("title"))
    parts = [
        mode,
        component,
        server,
        library_id or library,
        subject,
        operation,
        identity["season_number"],
        record.get("episode_number"),
    ]
    fingerprint = hashlib.sha256(json.dumps(parts).encode()).hexdigest()
    return {
        "finding_id": fingerprint,
        "library": library,
        "library_uuid": library_id,
        "server_id": server,
        "component": component,
        "episode_number": record.get("episode_number"),
        "mode": mode,
        "operation": operation,
        "title": record.get("title"),
        "media_type": record.get("media_type"),
        **identity,
    }


def _apply(db, opened, item, code, resolved, evidence, stamp, run_id, changes):
    key = item["finding_id"]
    old = opened.get(key)
    if resolved and old is None:
        return
    if resolved:
        # A healthy file can coexist with a policy conflict, failed upgrade or
        # verification error. File presence proves only a previous missing file.
        if code == "destination_present" and old["reason_code"] not in {
            "no_eligible_candidate",
            "no_candidate_returned",
            "artwork_missing",
        }:
            return
        if code == "identity_verified" and old["reason_code"] not in {
            "identity_rejected",
            "tmdb_missing",
            "tmdb_failure",
        }:
            return
        row = dict(
            old,
            status="resolved",
            resolved_at=stamp,
            resolution=code,
            recovery_evidence=evidence,
            last_checked_at=stamp,
        )
        del opened[key]
        changes["resolved"] += 1
    else:
        row = dict(
            item,
            reason_code=code,
            status="open",
            next_action=(
                "review_ownership"
                if code in {"protected_output", "ownership_collision", "locked_skipped"}
                else "verify_application"
                if item["operation"].startswith("application/")
                else "inspect_retry_queue"
                if code.startswith("retry_")
                else "check_storage_permissions"
                if evidence.get("error_type") == "PermissionError"
                else "review_inventory"
                if item["operation"] == "inventory"
                else "recheck_on_eligible_scan"
            ),
            evidence=evidence,
            first_seen=(old or {}).get("first_seen", stamp),
            last_seen=stamp,
            last_checked_at=stamp,
            last_run_id=run_id,
            observations=(old or {}).get("observations", 0) + 1,
        )
        opened[key] = row
        changes["persistent" if old else "new"] += 1
    db.execute(
        """INSERT INTO native_findings(id,first_seen,resolved_at,payload) VALUES(?,?,?,?)
                  ON CONFLICT(id,first_seen) DO UPDATE SET resolved_at=excluded.resolved_at,payload=excluded.payload""",
        (key, row["first_seen"], row.get("resolved_at"), json.dumps(row)),
    )


def _audit_events(folder, after, cutoff, stem, report_type, fields, receipts):
    # Read only the existing retained JSON reports; explicit diagnostic commands
    # keep their read-only database contract.
    for path in sorted(folder.glob(f"{stem}-[0-9]*.json")):
        if path.name in receipts:
            continue
        try:
            audit = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(audit, dict)
                or not isinstance(audit.get("data"), dict)
                or not isinstance(audit.get("generated_at"), str)
            ):
                raise TypeError("invalid audit envelope")
        except (TypeError, ValueError, OSError):
            # An unreadable audit supplies no evidence about its subjects.
            yield path.name, None, []
            continue
        if audit.get("report_type") != report_type:
            continue
        if path.name <= after or audit["generated_at"] < cutoff:
            # Validate historical input without replaying stale decisions over newer observations.
            yield path.name, audit["generated_at"], []
            continue
        events = []
        for field in fields:
            for row in (
                audit["data"].get(field, []) if isinstance(audit["data"].get(field), list) else []
            ):
                status = row.get("status") if isinstance(row, dict) else None
                if (
                    isinstance(status, str)
                    and isinstance(row.get("asset_type") or "", str)
                    and status
                    in {
                        "applied",
                        "partial",
                        "not_applied",
                        "unverifiable",
                        "plex_unavailable",
                        "missing_yaml",
                        "not_selected",
                        "selected",
                        "modified",
                        "local_missing",
                    }
                ):
                    events.append(
                        _event(
                            row,
                            "application/" + (row.get("asset_type") or "metadata"),
                            status,
                            resolved=status in {"applied", "selected"},
                            audited_at=audit["generated_at"],
                        )
                    )
        yield path.name, audit["generated_at"], events


def _database_events(db, started, stamp):
    rows = db.execute(
        """SELECT server_id,library_uuid,library_name,rating_key,media_type,
                          status,failure_class,error_type,attempts,next_retry_at FROM item_retry_queue
                          WHERE last_failed_at BETWEEN ? AND ?""",
        (started, stamp),
    )
    for row in rows:
        yield _event(
            dict(row),
            "item_processing",
            "retry_" + row["status"],
            **{k: row[k] for k in ("failure_class", "error_type", "attempts", "next_retry_at")},
        )

    for row in db.execute(
        "SELECT * FROM provider_health WHERE updated_at BETWEEN ? AND ?",
        (started, stamp),
    ):
        if row["consecutive_failures"] or row["last_success_at"] == row["updated_at"]:
            yield _event(
                {"component": "provider", "title": row["provider"]},
                "connection/" + hashlib.sha256(row["provider_key"].encode()).hexdigest()[:16],
                "provider_unavailable" if row["consecutive_failures"] else "provider_responded",
                resolved=not row["consecutive_failures"],
                failures=row["consecutive_failures"],
                retry_after=row["open_until"],
            )


def publish_findings(config, success, *, path, base_dir, now=None):
    current = now or datetime.now(timezone.utc)
    stamp = current.isoformat()
    cutoff = (current - timedelta(days=RETENTION_DAYS)).isoformat()
    run_id = uuid4().hex
    folder = Path(base_dir) / "reports"
    db = _connect(path, writable=True)
    try:
        with db:
            db.execute("DELETE FROM native_findings WHERE resolved_at < ?", (cutoff,))
            db.execute("DELETE FROM findings_runs WHERE finished_at < ?", (cutoff,))
            opened = {
                r["id"]: json.loads(r["payload"])
                for r in db.execute(
                    "SELECT id,payload FROM native_findings WHERE resolved_at IS NULL"
                )
            }
            changes = {"new": 0, "persistent": 0, "resolved": 0}
            audit_errors = []
            previous = db.execute(
                "SELECT state_value FROM application_state WHERE state_key='findings_audit_receipts'"
            ).fetchone()
            receipts = json.loads(previous[0]) if previous else {}
            initial_count = len(receipts)
            for mode, stem, report_type, fields in (
                (
                    "kometa",
                    "kometa-application-audit",
                    "kometa_application_audit",
                    ("metadata_items", "artwork_items"),
                ),
                ("plex", "plex-artwork-verification", "plex_artwork_verification", ("entries",)),
            ):
                cursor_key = "findings_audit_cursor:" + mode
                cursor = db.execute(
                    "SELECT state_value FROM application_state WHERE state_key=?", (cursor_key,)
                ).fetchone()
                for name, checked, events in _audit_events(
                    folder, cursor[0] if cursor else "", cutoff, stem, report_type, fields, receipts
                ):
                    if checked is None:
                        audit_errors.append(name)
                        continue
                    receipts[name] = True
                    for record, operation, code, resolved, evidence in events:
                        item = _finding({"settings": {"mode": mode}}, record, operation)
                        _apply(db, opened, item, code, resolved, evidence, checked, run_id, changes)
                    db.execute(
                        "INSERT OR REPLACE INTO application_state(state_key,state_value,updated_at) VALUES(?,?,?)",
                        (cursor_key, max(name, cursor[0]) if cursor else name, stamp),
                    )
            if len(receipts) != initial_count:
                db.execute(
                    "INSERT OR REPLACE INTO application_state VALUES(?,?,?)",
                    ("findings_audit_receipts", json.dumps(receipts), stamp),
                )
            observed = set()
            recoveries = {}

            for record, operation, code, resolved, evidence in chain(
                _events(config, success),
                _database_events(db, config.get("_findings_started", stamp), stamp),
            ):
                if resolved and not opened:
                    continue
                item = _finding(config, record, operation)
                key = item["finding_id"]
                if key in observed:
                    continue
                if resolved:
                    if key in opened and (key not in recoveries or code == "filesystem_verified"):
                        recoveries[key] = (item, code, evidence)
                else:
                    _apply(db, opened, item, code, False, evidence, stamp, run_id, changes)
                    observed.add(key)
            for key in recoveries.keys() - observed:
                item, code, evidence = recoveries[key]
                _apply(db, opened, item, code, True, evidence, stamp, run_id, changes)
            removed_seasons = {
                tuple(row)
                for row in db.execute(
                    """SELECT server_id, library_uuid, rating_key, season_number FROM cleanup_history
                   WHERE source='automated' AND action='remove' AND status='completed'
                   AND output_type='season_state' AND occurred_at BETWEEN ? AND ?""",
                    (config.get("_findings_started", stamp), stamp),
                )
            }
            for key, old in list(opened.items()):
                inventory = config.get("_findings_inventory", {}).get(
                    (old["server_id"], old["library_uuid"])
                )
                if (
                    success
                    and old["mode"] == config.get("settings", {}).get("mode", "kometa")
                    and inventory is not None
                    and old["component"] == "core"
                    and not old["operation"].startswith("cleanup/")
                    and old["plex_rating_key"]
                    and key not in observed
                ):
                    if old["plex_rating_key"] not in inventory:
                        _apply(db, opened, old, "item_removed", True, {}, stamp, run_id, changes)
                    elif (
                        old["server_id"],
                        old["library_uuid"],
                        old["plex_rating_key"],
                        old["season_number"],
                    ) in removed_seasons:
                        _apply(db, opened, old, "season_removed", True, {}, stamp, run_id, changes)
            cleanup = config.get("_cleanup_result")
            results = config.get("_job_library_results", {})
            run = {
                "run_id": run_id,
                "started_at": config.get("_findings_started"),
                "artwork_present_by_action": dict(
                    Counter(
                        r.get("action", "unknown")
                        for r in config.get("_artwork_evaluations", [])
                        if r.get("destination_state") == "present"
                    )
                ),
                "fields_by_action": dict(
                    Counter(
                        r.get("action", "unknown")
                        for r in config.get("_metadata_provenance_records", [])
                    )
                ),
                "finished_at": stamp,
                "build": build_info(),
                "cleanup": {
                    k: getattr(cleanup, k, 0)
                    for k in (
                        "titles",
                        "seasons",
                        "episodes",
                        "assets",
                        "ownership_deferred",
                        "assets_preserved",
                        "candidates_pending",
                        "failures",
                    )
                },
                "status": "completed" if success else "incomplete",
                "mode": config.get("settings", {}).get("mode", "kometa"),
                "unreadable_audits": audit_errors,
                "targeted": bool(config.get("_execution", {}).get("targeted")),
                "enabled_operations": get_feature_flags(config),
                "libraries": results,
                "full_scan_libraries": config.get("_successful_full_scan_libraries", []),
                "changes": {
                    **changes,
                    "not_rechecked": sum(r["last_run_id"] != run_id for r in opened.values()),
                },
            }
            db.execute("INSERT INTO findings_runs VALUES(?,?,?)", (run_id, stamp, json.dumps(run)))
            recent = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM native_findings WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC"
                )
            ]
            history = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM findings_runs ORDER BY finished_at DESC")
            ]
        groups = {}
        for row in opened.values():
            group_code = row["evidence"].get("error_type") or row["reason_code"]
            group = groups.setdefault(group_code, {"count": 0, "examples": []})
            group["count"] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(row["finding_id"])
        payload = {
            "schema_version": 1,
            "generated_at": stamp,
            "resolved_retention_days": RETENTION_DAYS,
            "run": run,
            "run_history": history,
            "open": sorted(opened.values(), key=lambda r: r["finding_id"]),
            "recently_resolved": recent,
            "issue_groups": groups,
            "open_by_reason": dict(Counter(r["reason_code"] for r in opened.values())),
        }
        atomic_write_json(folder / "findings-latest.json", payload)
        return run["changes"]
    finally:
        db.close()
