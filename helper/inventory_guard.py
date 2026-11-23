"""Compare validated Plex inventories against the last trusted full scan."""

import hashlib
import json
from uuid import uuid4

from helper.state_db import load_application_record, save_application_record


def inspect_inventory(scope, records, *, accept=None, full=False, writable=False, path=None):
    key = "inventory_guard:" + json.dumps([scope["server_id"], scope["library_uuid"]])
    current = {str(r["rating_key"]): r.get("title") for r in records if r.get("rating_key")}
    if len(current) != len(records) or scope["server_id"] == "unknown":
        return {"status": "unverified", "reason": "missing or duplicate Plex identity"}
    previous = load_application_record(key, path=path)
    if previous is not None:
        baseline = previous["items"]
        missing = sorted(baseline.keys() - current.keys())
        if len(missing) >= 20 and len(missing) * 4 >= len(baseline):
            evidence = [key, previous["generation"], sorted(current)]
            token = hashlib.sha256(json.dumps(evidence).encode()).hexdigest()[:24]
            if not (full and writable and accept == token):
                return {
                    "status": "held",
                    "token": token,
                    "baseline": len(baseline),
                    "observed": len(current),
                    "missing": [{"rating_key": k, "title": baseline[k]} for k in missing],
                }
            status = "accepted"
        else:
            status = "trusted"
    else:
        status = "initializing"
    if full and writable and (previous is None or current != previous["items"]):
        save_application_record(key, {"generation": uuid4().hex, "items": current}, path=path)
    return {"status": status}


def evaluate_inventories(config, scopes, inventories, full_scans, logger, *, writable):
    reports = {}
    held = set()
    accept = config.get("_execution", {}).get("accept_inventory_drop")
    for scope in scopes:
        name = scope["library_name"]
        report = inspect_inventory(
            scope,
            inventories[name],
            accept=accept,
            full=full_scans.get((scope["server_id"], scope["library_uuid"]), True),
            writable=writable,
        )
        reports[name] = report
        if report["status"] in {"held", "unverified"}:
            held.add(name)
            logger.warning(
                "[Inventory] %s | Absence actions held | Reason: %s | Trusted: %s | Current: %s | "
                "Missing: %d | Review with --library-audit",
                name,
                report.get("reason", "large disappearance"),
                report.get("baseline"),
                report.get("observed"),
                len(report.get("missing", [])),
            )
    if accept and not any(r["status"] == "accepted" for r in reports.values()):
        raise ValueError(
            "Inventory acknowledgement did not match this full scan; review --library-audit"
        )
    return reports, held
