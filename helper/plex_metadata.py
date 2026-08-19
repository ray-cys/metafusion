import asyncio
import logging
import threading
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from helper.concurrency import runtime_slot
from helper.config import BASE_CONFIG_DIR, mode_check, report_retention
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.state_db import (
    load_plex_metadata_ownership,
    save_plex_metadata_ownership,
    utc_now,
)

TAG_ATTRIBUTES = {
    "country": "countries",
    "genre": "genres",
    "director": "directors",
    "writer": "writers",
    "producer": "producers",
}


def _clean_scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "").strip()


def _clean_tags(values):
    cleaned = []
    seen = set()
    for value in values or []:
        name = _clean_scalar(getattr(value, "tag", value))
        folded = name.casefold()
        if name and folded not in seen:
            cleaned.append(name)
            seen.add(folded)
    return cleaned


def _missing_tags(current, desired):
    current_folded = {value.casefold() for value in current}
    return [value for value in desired if value.casefold() not in current_folded]


class PlexMetadataReporter:
    def __init__(self, config):
        settings = config.get("plex_metadata", {})
        self.audit_mode = bool(
            config.get("_execution", {}).get("metadata_audit", False)
        )
        self.audit_records = config.setdefault("_metadata_audit_records", [])
        override_settings = [
            override.get("plex_metadata", {})
            for override in config.get("library_overrides", {}).values()
            if isinstance(override, dict)
            and isinstance(override.get("plex_metadata", {}), dict)
        ]
        self.enabled = mode_check(config, "plex") and (
            self.audit_mode
            or settings.get("enabled", False)
            or any(override.get("enabled", False) for override in override_settings)
        ) and not config.get("_execution", {}).get("asset_audit", False)
        self.dry_run = config.get("settings", {}).get("dry_run", False)
        self.policy = (
            "per-library"
            if any(
                override.get("policy")
                and override.get("policy") != settings.get("policy")
                for override in override_settings
            )
            else str(settings.get("policy", "fill_missing"))
        )
        self.max_writes = max(1, int(settings.get("max_writes_per_run", 100)))
        self.retention = report_retention(config)
        self.counts = Counter()
        self.entries = []
        self.max_details = 10000
        self._writes = 0
        self._library_writes = Counter()
        self._lock = threading.Lock()

    def record(self, library, title, child_key, field, action, detail=""):
        with self._lock:
            self.counts[action] += 1
            if self.audit_mode:
                proposed = {
                    "would_fill": "add/update",
                    "would_remove": "remove",
                    "locked_skipped": "preserve_locked",
                    "existing_skipped": "preserve_existing",
                    "source_missing": "none",
                    "unchanged": "none",
                    "conflict": "preserve_conflict",
                    "policy_excluded": "none",
                    "failed": "none",
                }.get(action, action)
                self.audit_records.append(
                    {
                        "library": str(library),
                        "media_type": "Plex item",
                        "title": str(title),
                        "child": str(child_key or "item"),
                        "field": str(field),
                        "state": str(action),
                        "policy": self.policy,
                        "proposed_action": proposed,
                        "detail": str(detail or ""),
                        "target": "Plex API",
                    }
                )
            if action in {"unchanged", "source_missing"} and not self.audit_mode:
                return
            if len(self.entries) >= self.max_details:
                self.counts["details_omitted"] += 1
                return
            self.entries.append(
                (
                    str(library),
                    str(title),
                    str(child_key or "item"),
                    str(field),
                    action,
                    detail,
                )
            )

    def claim_write(self, library, limit=None):
        with self._lock:
            effective_limit = (
                self.max_writes
                if limit is None
                else min(self.max_writes, max(1, int(limit)))
            )
            if (
                self._writes >= self.max_writes
                or self._library_writes[str(library)] >= effective_limit
            ):
                return False
            self._writes += 1
            self._library_writes[str(library)] += 1
            return True

    @property
    def writes(self):
        return self._writes

    def write(self, base_dir=None):
        if not self.enabled:
            return None
        if self.audit_mode:
            return None
        report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
        path = report_dir / f"plex-metadata-{timestamp}.txt"
        lines = [
            "MetaFusion Plex metadata report",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            f"Policy: {self.policy}",
            f"Dry run: {self.dry_run}",
            f"Plex API writes: {self.writes}/{self.max_writes}",
            "",
            "Summary",
        ]
        if self.counts:
            lines.extend(
                f"- {name}: {count}" for name, count in sorted(self.counts.items())
            )
        else:
            lines.append("- no eligible metadata fields")
        lines.extend(("", "Items"))
        if not self.entries:
            lines.append("- No items required metadata changes.")
        else:
            for library, title, child, field, action, detail in self.entries:
                suffix = f" ({detail})" if detail else ""
                lines.append(
                    f"- [{library}] {title} | {child} | {field}: {action}{suffix}"
                )
        write_diagnostic_report(
            path,
            "\n".join(lines),
            report_type="plex_metadata",
            data={
                "policy": self.policy,
                "dry_run": self.dry_run,
                "writes": self.writes,
                "write_limit": self.max_writes,
                "summary": dict(self.counts),
                "items": [
                    {
                        "library": library,
                        "title": title,
                        "child": child,
                        "field": field,
                        "action": action,
                        "detail": detail,
                    }
                    for library, title, child, field, action, detail in self.entries
                ],
            },
        )
        retain_diagnostic_reports(report_dir, "plex-metadata", self.retention)
        logger = logging.getLogger(__name__)
        logger.info(
            "[Metadata] Plex summary | API batches: %d/%d, fields filled: %d, "
            "tags added: %d, values removed: %d, unchanged: %d, "
            "existing values preserved: %d, source missing: %d, failed: %d.",
            self.writes,
            self.max_writes,
            self.counts.get("filled", 0),
            self.counts.get("tags_added", 0),
            self.counts.get("removed", 0),
            self.counts.get("unchanged", 0),
            self.counts.get("existing_skipped", 0),
            self.counts.get("source_missing", 0),
            self.counts.get("failed", 0),
        )
        safety_counts = {
            "locked fields": self.counts.get("locked_skipped", 0),
            "conflicts preserved": self.counts.get("conflict", 0),
            "write-limit skips": self.counts.get("write_limit", 0),
        }
        if any(safety_counts.values()):
            logger.warning(
                "[Metadata] Plex safety | %s | Report: %s",
                ", ".join(
                    f"{name}: {count}" for name, count in safety_counts.items()
                ),
                path,
            )
        if self.dry_run:
            logger.info(
                "[Dry Run] [Metadata] Plex | Would fill: %d, would remove: %d",
                self.counts.get("would_fill", 0),
                self.counts.get("would_remove", 0),
            )
        return path


_reporter = None


def begin_plex_metadata_run(config):
    global _reporter
    _reporter = PlexMetadataReporter(config)
    return _reporter


def finish_plex_metadata_run(config):
    global _reporter
    reporter = _reporter
    _reporter = None
    if reporter is None:
        reporter = PlexMetadataReporter(config)
    return reporter.write()


def get_plex_metadata_reporter(config):
    global _reporter
    if _reporter is None:
        _reporter = PlexMetadataReporter(config)
    return _reporter


def _field_enabled(field, settings):
    selected = {
        str(value).strip() for value in settings.get("fields", []) if str(value).strip()
    }
    return not selected or field in selected


def _media_index(item, *names):
    for name in names:
        value = getattr(item, name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _record_payload(
    identity,
    child_key,
    field,
    kind,
    original,
    applied,
    owned,
    locked,
    meta_locked,
    relinquished=None,
):
    now = utc_now()
    return {
        **identity,
        "child_key": child_key,
        "field_name": field,
        "field_kind": kind,
        "original_value": {"value": original},
        "applied_value": {"value": applied},
        "owned_values": {
            "values": owned,
            "relinquished": list(relinquished or []),
        },
        "original_locked": locked,
        "metafusion_locked": meta_locked,
        "last_checked": now,
        "last_updated": now,
    }


def _children_for_candidate(item, candidate, existing=None):
    existing = existing or _existing_children(item)
    yield "", existing[""], candidate.get("root", {})
    season_candidates = candidate.get("seasons", {})
    if not season_candidates:
        return
    for number, season_candidate in season_candidates.items():
        season = existing.get(f"season:{int(number)}")
        if season is None:
            continue
        yield f"season:{int(number)}", season, season_candidate
        episode_candidates = season_candidate.get("episodes", {})
        if not episode_candidates:
            continue
        for episode_number, episode_candidate in episode_candidates.items():
            episode = existing.get(f"episode:{int(number)}:{int(episode_number)}")
            if episode is not None:
                yield (
                    f"episode:{int(number)}:{int(episode_number)}",
                    episode,
                    episode_candidate,
                )


def _apply_object(
    obj,
    candidate,
    child_key,
    identity,
    ownership,
    settings,
    reporter,
    title,
    dry_run,
):
    if hasattr(obj, "reload"):
        obj.reload()
    policy = str(settings.get("policy", "fill_missing")).lower()
    lock_writes = bool(settings.get("lock_writes", False))
    lock_tags = bool(settings.get("lock_merged_tags", False))
    changes = []
    records = []
    library = identity.get("library_name")

    for field, desired_raw in candidate.get("fields", {}).items():
        if not _field_enabled(field, settings):
            reporter.record(
                library, title, child_key, field, "policy_excluded"
            )
            continue
        desired = _clean_scalar(desired_raw)
        current = _clean_scalar(getattr(obj, field, ""))
        owner = ownership.get((child_key, field))
        locked = bool(obj.isLocked(field)) if hasattr(obj, "isLocked") else False
        if current == desired:
            reporter.record(library, title, child_key, field, "unchanged")
            continue
        if (
            locked
            and not (owner and owner.get("metafusion_locked"))
            and policy != "overwrite"
        ):
            reporter.record(library, title, child_key, field, "locked_skipped")
            continue
        allowed = False
        target_value = desired
        if policy == "overwrite":
            allowed = True
        elif owner:
            previous = _clean_scalar(owner["applied_value"].get("value"))
            if current == previous:
                allowed = policy == "managed"
                if allowed and not desired:
                    target_value = _clean_scalar(owner["original_value"].get("value"))
            else:
                reporter.record(library, title, child_key, field, "conflict")
                records.append(
                    {
                        "_delete_key": (
                            str(identity["server_id"]),
                            str(identity["library_uuid"]),
                            str(identity["rating_key"]),
                            str(child_key),
                            str(field),
                        )
                    }
                )
                continue
        else:
            allowed = bool(desired and not current)
        if not allowed:
            action = "source_missing" if not desired else "existing_skipped"
            reporter.record(library, title, child_key, field, action)
            continue
        target_lock = bool(lock_writes or (owner and owner.get("metafusion_locked")))
        changes.append(("field", field, target_value, target_lock, False))
        records.append(
            _record_payload(
                identity,
                child_key,
                field,
                "scalar",
                owner["original_value"].get("value") if owner else current,
                target_value,
                [target_value] if target_value else [],
                owner.get("original_locked") if owner else locked,
                target_lock,
            )
        )

    for field, desired_raw in candidate.get("tags", {}).items():
        if not _field_enabled(field, settings):
            reporter.record(
                library, title, child_key, field, "policy_excluded"
            )
            continue
        desired = _clean_tags(desired_raw)
        attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
        current = _clean_tags(getattr(obj, attribute, []))
        owner = ownership.get((child_key, field))
        relinquished = _clean_tags(
            (owner or {}).get("owned_values", {}).get("relinquished", [])
        )
        locked = bool(obj.isLocked(field)) if hasattr(obj, "isLocked") else False
        if (
            locked
            and not (owner and owner.get("metafusion_locked"))
            and policy != "overwrite"
        ):
            reporter.record(library, title, child_key, field, "locked_skipped")
            continue
        additions = _missing_tags(current, desired)
        if relinquished:
            rejected = {value.casefold() for value in relinquished}
            additions = [
                value for value in additions if value.casefold() not in rejected
            ]
        removals = []
        if policy == "overwrite":
            removals = _missing_tags(desired, current)
        elif policy == "managed" and owner:
            previously_owned = _clean_tags(owner["owned_values"].get("values", []))
            current_folded = {value.casefold() for value in current}
            manually_removed = [
                value
                for value in previously_owned
                if value.casefold() not in current_folded
            ]
            if manually_removed:
                relinquished.extend(_missing_tags(relinquished, manually_removed))
                removed_folded = {value.casefold() for value in manually_removed}
                additions = [
                    value
                    for value in additions
                    if value.casefold() not in removed_folded
                ]
                reporter.record(
                    library,
                    title,
                    child_key,
                    field,
                    "conflict",
                    "manually removed managed tag retained",
                )
            retained_owned = [
                value
                for value in previously_owned
                if value.casefold() in current_folded
            ]
            removals = _missing_tags(desired, retained_owned)
        if not additions and not removals:
            reporter.record(library, title, child_key, field, "unchanged")
            if owner and policy == "managed" and manually_removed:
                records.append(
                    _record_payload(
                        identity,
                        child_key,
                        field,
                        "tag",
                        owner["original_value"].get("value"),
                        current,
                        retained_owned,
                        owner.get("original_locked"),
                        owner.get("metafusion_locked"),
                        relinquished=relinquished,
                    )
                )
            continue
        target_lock = bool(
            (lock_tags if policy != "overwrite" else lock_writes)
            or (owner and owner.get("metafusion_locked"))
        )
        if additions:
            changes.append(("tag", field, additions, target_lock, False))
        if removals:
            changes.append(("tag", field, removals, target_lock, True))
        final = [
            value
            for value in current
            if value.casefold() not in {v.casefold() for v in removals}
        ]
        final.extend(_missing_tags(final, additions))
        owned = desired if policy == "overwrite" else _missing_tags(current, additions)
        if owner and policy == "managed":
            owned = _clean_tags(owner["owned_values"].get("values", []))
            owned = [
                value
                for value in owned
                if value.casefold() not in {v.casefold() for v in removals}
            ]
            owned.extend(_missing_tags(owned, additions))
        records.append(
            _record_payload(
                identity,
                child_key,
                field,
                "tag",
                owner["original_value"].get("value") if owner else current,
                final,
                owned,
                owner.get("original_locked") if owner else locked,
                target_lock,
                relinquished=relinquished,
            )
        )

    if not changes:
        return 0, records
    if dry_run:
        for kind, field, values, _locked, remove in changes:
            action = "would_remove" if remove else "would_fill"
            reporter.record(
                library,
                title,
                child_key,
                field,
                action,
                f"{len(values) if kind == 'tag' else 1} value(s)",
            )
        return 0, []
    if not reporter.claim_write(
        library, settings.get("max_writes_per_run", reporter.max_writes)
    ):
        for _kind, field, _values, _locked, _remove in changes:
            reporter.record(library, title, child_key, field, "write_limit")
        return 0, []

    obj.batchEdits()
    for kind, field, value, locked, remove in changes:
        if kind == "field":
            obj.editField(field, value, locked=locked)
        else:
            obj.editTags(field, value, locked=locked, remove=remove)
    obj.saveEdits()
    if hasattr(obj, "reload"):
        obj.reload()
    for kind, field, _value, _locked, remove in changes:
        if kind == "field":
            if _clean_scalar(getattr(obj, field, "")) != _clean_scalar(_value):
                raise RuntimeError(f"Plex did not retain the {field} field update")
            continue
        attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
        current = {name.casefold() for name in _clean_tags(getattr(obj, attribute, []))}
        expected = {name.casefold() for name in _clean_tags(_value)}
        if (remove and current & expected) or (not remove and not expected <= current):
            raise RuntimeError(f"Plex did not retain the {field} tag update")
    for kind, field, _value, _locked, remove in changes:
        action = "removed" if remove else ("tags_added" if kind == "tag" else "filled")
        reporter.record(library, title, child_key, field, action)
    return 1, records


def _rollback_untracked_write(obj, records):
    """Best-effort rollback when the ownership ledger cannot be committed."""
    obj.batchEdits()
    for record in records:
        field = record["field_name"]
        locked = bool(record.get("original_locked"))
        original = record["original_value"].get("value")
        if record["field_kind"] == "scalar":
            obj.editField(field, _clean_scalar(original), locked=locked)
            continue
        attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
        current = _clean_tags(getattr(obj, attribute, []))
        wanted = _clean_tags(original)
        remove = _missing_tags(wanted, current)
        add = _missing_tags(current, wanted)
        if remove:
            obj.editTags(field, remove, locked=locked, remove=True)
        if add or not remove:
            obj.editTags(field, add, locked=locked)
    obj.saveEdits()
    if hasattr(obj, "reload"):
        obj.reload()


def _apply_candidate(item, candidate, config, meta, reporter):
    write_limit_before = reporter.counts.get("write_limit", 0)
    settings = config.get("plex_metadata", {})
    identity = {
        "server_id": meta.get("server_id") or "unknown",
        "library_uuid": meta.get("library_uuid")
        or meta.get("library_name")
        or "unknown",
        "library_name": meta.get("library_name") or "Unknown",
        "rating_key": meta.get("ratingKey") or getattr(item, "ratingKey", "unknown"),
        "media_type": meta.get("library_type") or getattr(item, "type", "unknown"),
    }
    ownership = load_plex_metadata_ownership(
        identity["server_id"], identity["library_uuid"], identity["rating_key"]
    )
    writes = 0
    failures = 0
    try:
        existing_children = _existing_children(item)
        children = list(
            _children_for_candidate(item, candidate, existing=existing_children)
        )
    except Exception as error:
        reporter.record(
            identity["library_name"],
            meta.get("title") or getattr(item, "title", "Unknown"),
            "item",
            "children",
            "failed",
            type(error).__name__,
        )
        return {"writes": 0, "failures": 1}
    pending_records = []
    pending_deleted = []
    rollback_batches = []
    for child_key, child, child_candidate in children:
        try:
            child_writes, child_records = _apply_object(
                child,
                child_candidate,
                child_key,
                identity,
                ownership,
                settings,
                reporter,
                meta.get("title") or getattr(item, "title", "Unknown"),
                config.get("settings", {}).get("dry_run", False),
            )
            writes += child_writes
            if child_records and not config.get("settings", {}).get("dry_run", False):
                pending_deleted.extend(
                    record["_delete_key"]
                    for record in child_records
                    if "_delete_key" in record
                )
                persisted = [
                    record for record in child_records if "_delete_key" not in record
                ]
                pending_records.extend(persisted)
                if child_writes and persisted:
                    rollback_batches.append((child, persisted))
        except Exception as error:
            failures += 1
            reporter.record(
                identity["library_name"],
                meta.get("title") or getattr(item, "title", "Unknown"),
                child_key,
                "item",
                "failed",
                type(error).__name__,
            )
            logging.getLogger(__name__).error(
                "[Metadata] Plex | %s | Failed %s | Error type: %s",
                meta.get("title") or getattr(item, "title", "Unknown"),
                child_key or "item",
                type(error).__name__,
            )
    if not config.get("settings", {}).get("dry_run", False):
        try:
            save_plex_metadata_ownership(
                pending_records,
                deleted=pending_deleted,
                prune_scope=(
                    identity["server_id"],
                    identity["library_uuid"],
                    identity["rating_key"],
                ),
                valid_child_keys=existing_children,
            )
        except Exception as error:
            for child, records in reversed(rollback_batches):
                try:
                    _rollback_untracked_write(child, records)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "[Metadata] Plex | Failed to roll back an untracked write"
                    )
            failures += 1
            reporter.record(
                identity["library_name"],
                meta.get("title") or getattr(item, "title", "Unknown"),
                "item",
                "ownership",
                "failed",
                type(error).__name__,
            )
    result = {"writes": writes, "failures": failures}
    deferred = max(
        0, reporter.counts.get("write_limit", 0) - write_limit_before
    )
    if deferred:
        result["deferred"] = deferred
    return result


async def apply_plex_metadata(item, candidate, config, meta):
    settings = config.get("plex_metadata", {})
    if not (
        mode_check(config, "plex") and settings.get("enabled", False) and candidate
    ):
        return {"writes": 0, "failures": 0}
    reporter = get_plex_metadata_reporter(config)
    runtime = config.get("runtime", {})
    retries = max(1, int(runtime.get("plex_retries", 3)))
    delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    total_writes = 0
    result = {"writes": 0, "failures": 0}
    title = meta.get("title") or getattr(item, "title", "Unknown")
    for attempt in range(1, retries + 1):
        async with runtime_slot(config, "plex") as concurrency:
            result = await asyncio.to_thread(
                _apply_candidate, item, candidate, config, meta, reporter
            )
            if result.get("failures"):
                concurrency.failure("operation_failure")
        total_writes += result.get("writes", 0)
        if not result.get("failures"):
            if total_writes:
                logging.getLogger(__name__).debug(
                    "[Metadata] Plex | %s | Applied %d API batch(es) | "
                    "Field details are recorded in the run report",
                    title,
                    total_writes,
                )
            elif config.get("settings", {}).get("dry_run", False):
                logging.getLogger(__name__).debug(
                    "[Dry Run] [Metadata] Plex | %s | Evaluation completed; "
                    "planned field actions are recorded in the run report.",
                    title,
                )
            else:
                logging.getLogger(__name__).debug(
                    "[Metadata] Plex | %s | No API changes required",
                    title,
                )
            response = {"writes": total_writes, "failures": 0}
            if result.get("deferred", 0):
                response["deferred"] = result["deferred"]
            return response
        if attempt < retries and delay:
            await asyncio.sleep(delay * attempt)
    logging.getLogger(__name__).debug(
        "[Metadata] Plex | %s | API update attempts exhausted | Attempts: %d",
        title,
        retries,
    )
    response = {
        "writes": total_writes,
        "failures": result.get("failures", 0),
    }
    if result.get("deferred", 0):
        response["deferred"] = result["deferred"]
    return response


def _existing_children(item):
    children = {"": item}
    if not hasattr(item, "seasons"):
        return children
    for season in item.seasons():
        season_number = _media_index(season, "index", "seasonNumber")
        if season_number is None:
            continue
        children[f"season:{season_number}"] = season
        if hasattr(season, "episodes"):
            for episode in season.episodes():
                episode_number = _media_index(episode, "index", "episodeNumber")
                if episode_number is None:
                    continue
                children[f"episode:{season_number}:{episode_number}"] = episode
    return children


def _restore_candidate(item, config, meta, reporter, unlock_only=False):
    identity = {
        "server_id": meta.get("server_id") or "unknown",
        "library_uuid": meta.get("library_uuid")
        or meta.get("library_name")
        or "unknown",
        "rating_key": str(
            meta.get("ratingKey") or getattr(item, "ratingKey", "unknown")
        ),
    }
    ownership = load_plex_metadata_ownership(
        identity["server_id"], identity["library_uuid"], identity["rating_key"]
    )
    children = _existing_children(item)
    updates = []
    deleted = []
    writes = failures = 0
    for child_key, child in children.items():
        records = [
            record
            for (record_child, _field), record in ownership.items()
            if record_child == child_key
        ]
        if not records:
            continue
        if hasattr(child, "reload"):
            child.reload()
        changes = []
        successful_records = []
        for record in records:
            field = record["field_name"]
            original_locked = bool(record.get("original_locked"))
            if record["field_kind"] == "scalar":
                current = _clean_scalar(getattr(child, field, ""))
                applied = _clean_scalar(record["applied_value"].get("value"))
                if current != applied:
                    reporter.record(
                        meta.get("library_name"),
                        meta.get("title"),
                        child_key,
                        field,
                        "conflict",
                        "manual value retained",
                    )
                    continue
                value = (
                    current
                    if unlock_only
                    else _clean_scalar(record["original_value"].get("value"))
                )
                if unlock_only and not record.get("metafusion_locked"):
                    reporter.record(
                        meta.get("library_name"),
                        meta.get("title"),
                        child_key,
                        field,
                        "unchanged",
                    )
                    continue
                changes.append(("field", field, value, original_locked, False))
            else:
                attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
                current = _clean_tags(getattr(child, attribute, []))
                applied = _clean_tags(record["applied_value"].get("value", []))
                if {value.casefold() for value in current} != {
                    value.casefold() for value in applied
                }:
                    reporter.record(
                        meta.get("library_name"),
                        meta.get("title"),
                        child_key,
                        field,
                        "conflict",
                        "manual tag change retained",
                    )
                    continue
                if unlock_only:
                    if not record.get("metafusion_locked"):
                        reporter.record(
                            meta.get("library_name"),
                            meta.get("title"),
                            child_key,
                            field,
                            "unchanged",
                        )
                        continue
                    changes.append(("tag", field, [], original_locked, False))
                else:
                    original = _clean_tags(record["original_value"].get("value", []))
                    remove = _missing_tags(original, current)
                    add = _missing_tags(current, original)
                    if remove:
                        changes.append(("tag", field, remove, original_locked, True))
                    if add:
                        changes.append(("tag", field, add, original_locked, False))
                    if (
                        not remove
                        and not add
                        and bool(child.isLocked(field)) != original_locked
                    ):
                        changes.append(("tag", field, [], original_locked, False))
            successful_records.append(record)
        if not changes:
            continue
        if config.get("settings", {}).get("dry_run", False):
            for record in successful_records:
                reporter.record(
                    meta.get("library_name"),
                    meta.get("title"),
                    child_key,
                    record["field_name"],
                    "would_unlock" if unlock_only else "would_restore",
                )
            continue
        if not reporter.claim_write(
            meta.get("library_name"),
            config.get("plex_metadata", {}).get("max_writes_per_run", 100),
        ):
            for record in successful_records:
                reporter.record(
                    meta.get("library_name"),
                    meta.get("title"),
                    child_key,
                    record["field_name"],
                    "write_limit",
                )
            continue
        try:
            child.batchEdits()
            for kind, field, value, locked, remove in changes:
                if kind == "field":
                    child.editField(field, value, locked=locked)
                else:
                    child.editTags(field, value, locked=locked, remove=remove)
            child.saveEdits()
            if hasattr(child, "reload"):
                child.reload()
            for record in successful_records:
                field = record["field_name"]
                expected_lock = bool(record.get("original_locked"))
                if (
                    hasattr(child, "isLocked")
                    and bool(child.isLocked(field)) != expected_lock
                ):
                    raise RuntimeError(f"Plex did not retain the {field} lock update")
                expected = (
                    record["applied_value"].get("value")
                    if unlock_only
                    else record["original_value"].get("value")
                )
                if record["field_kind"] == "scalar":
                    retained = _clean_scalar(getattr(child, field, ""))
                    if retained != _clean_scalar(expected):
                        raise RuntimeError(f"Plex did not retain the {field} restore")
                else:
                    attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
                    retained = {
                        value.casefold()
                        for value in _clean_tags(getattr(child, attribute, []))
                    }
                    wanted = {value.casefold() for value in _clean_tags(expected)}
                    if retained != wanted:
                        raise RuntimeError(
                            f"Plex did not retain the {field} tag restore"
                        )
            writes += 1
            for record in successful_records:
                reporter.record(
                    meta.get("library_name"),
                    meta.get("title"),
                    child_key,
                    record["field_name"],
                    "unlocked" if unlock_only else "restored",
                )
                key = (
                    record["server_id"],
                    record["library_uuid"],
                    record["rating_key"],
                    record["child_key"],
                    record["field_name"],
                )
                if unlock_only:
                    record["metafusion_locked"] = 0
                    record["last_checked"] = utc_now()
                    record["last_updated"] = utc_now()
                    updates.append(record)
                else:
                    deleted.append(key)
        except Exception as error:
            failures += 1
            reporter.record(
                meta.get("library_name"),
                meta.get("title"),
                child_key,
                "item",
                "failed",
                type(error).__name__,
            )
    if updates or deleted:
        save_plex_metadata_ownership(updates, deleted=deleted)
    return {"writes": writes, "failures": failures}


async def restore_plex_metadata(item, config, meta, unlock_only=False):
    reporter = get_plex_metadata_reporter(config)
    runtime = config.get("runtime", {})
    retries = max(1, int(runtime.get("plex_retries", 3)))
    delay = max(0.0, float(runtime.get("plex_retry_delay", 1.0)))
    total_writes = 0
    result = {"writes": 0, "failures": 0}
    for attempt in range(1, retries + 1):
        async with runtime_slot(config, "plex") as concurrency:
            result = await asyncio.to_thread(
                _restore_candidate, item, config, meta, reporter, unlock_only
            )
            if result.get("failures"):
                concurrency.failure("operation_failure")
        total_writes += result.get("writes", 0)
        if not result.get("failures"):
            return {"writes": total_writes, "failures": 0}
        if attempt < retries and delay:
            await asyncio.sleep(delay * attempt)
    return {"writes": total_writes, "failures": result.get("failures", 0)}
