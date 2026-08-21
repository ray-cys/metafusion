"""Value-free field-level metadata provenance records."""

from __future__ import annotations

import hashlib
import json

from helper.identity import cache_key_for_meta


def value_fingerprint(value):
    """Return a stable one-way fingerprint without retaining metadata values."""
    if value is None:
        return None
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _flatten_fields(value, prefix=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _flatten_fields(child, (*prefix, str(key)))
        return
    yield ".".join(prefix), value


def _path_value(document, field_path):
    current = document
    for part in field_path.split(".") if field_path else []:
        if not isinstance(current, dict):
            return None, False
        candidates = (part, int(part)) if part.isdigit() else (part,)
        for candidate in candidates:
            if candidate in current:
                current = current[candidate]
                break
        else:
            return None, False
    return current, True


def _child_and_field(field_path):
    parts = field_path.split(".")
    if len(parts) >= 5 and parts[0] == "seasons" and parts[2] == "episodes":
        return f"episode:{parts[1]}:{parts[3]}", ".".join(parts[4:])
    if len(parts) >= 3 and parts[0] == "seasons":
        return f"season:{parts[1]}", ".".join(parts[2:])
    return "item", field_path


def _kometa_source(field_path, action):
    if action == "preserved":
        return "Kometa YAML"
    if field_path in {"match.title", "match.year", "sort_title"}:
        source = "Plex"
    elif field_path == "match.mapping_id":
        source = "Identity resolver"
    else:
        source = "TMDb"
    return f"{source} / existing" if action == "unchanged" else source


def provenance_record(
    identity,
    *,
    target,
    child_key,
    field_name,
    field_path=None,
    source_provider,
    source_id=None,
    action,
    policy,
    reason,
    value=None,
):
    """Build the normalized record accepted by the durable provenance ledger."""
    identity = identity if isinstance(identity, dict) else {}
    normalized_child = str(child_key or "item")
    normalized_field = str(field_name or "unknown")
    return {
        "cache_key": str(identity.get("cache_key") or cache_key_for_meta(identity)),
        "server_id": str(identity.get("server_id") or "unknown"),
        "library_uuid": str(
            identity.get("library_uuid")
            or identity.get("library_name")
            or "unknown"
        ),
        "library_name": str(identity.get("library_name") or "Unknown library"),
        "rating_key": str(
            identity.get("rating_key")
            or identity.get("ratingKey")
            or "unknown"
        ),
        "media_type": str(
            identity.get("media_type")
            or identity.get("library_type")
            or "unknown"
        ),
        "title": str(identity.get("title") or "Unknown title"),
        "tmdb_id": (
            None if identity.get("tmdb_id") in (None, "")
            else str(identity.get("tmdb_id"))
        ),
        "target": str(target),
        "child_key": normalized_child,
        "field_name": normalized_field,
        "field_path": str(
            field_path
            or (
                normalized_field
                if normalized_child == "item"
                else f"{normalized_child}.{normalized_field}"
            )
        ),
        "source_provider": str(source_provider or "Unknown"),
        "source_id": None if source_id in (None, "") else str(source_id),
        "action": str(action),
        "policy": str(policy),
        "reason": str(reason or ""),
        "value_fingerprint": value_fingerprint(value),
    }


def kometa_provenance_records(
    identity,
    *,
    existing,
    generated,
    merged,
    policy="kometa_merge",
):
    """Explain the effective source and action for every generated Kometa field."""
    records = []
    for field_path, desired in _flatten_fields(generated):
        current, present = _path_value(existing or {}, field_path)
        final, final_present = _path_value(merged or {}, field_path)
        desired_available = desired not in (None, "", [])
        current_available = present and current not in (None, "", [])
        if not desired_available:
            if current_available and final_present and final == current:
                action = "preserved"
                reason = "TMDb source missing; existing Kometa value preserved"
                effective_value = current
            else:
                action = "source_missing"
                reason = "TMDb source did not provide a usable value"
                effective_value = None
        elif not current_available:
            action = "created"
            reason = "Target field was missing"
            effective_value = final if final_present else desired
        elif current == desired:
            action = "unchanged"
            reason = "Existing value matches the selected source"
            effective_value = current
        elif final_present and final == desired:
            action = "updated"
            reason = "Selected source differs from the existing value"
            effective_value = final
        else:
            action = "preserved"
            reason = "Merge policy retained the existing value"
            effective_value = current
        child_key, field_name = _child_and_field(field_path)
        source = _kometa_source(field_path, action)
        source_id = (
            identity.get("ratingKey")
            if source.startswith("Plex")
            else identity.get("tmdb_id")
        )
        records.append(
            provenance_record(
                identity,
                target="kometa_yaml",
                child_key=child_key,
                field_name=field_name,
                field_path=field_path,
                source_provider=source,
                source_id=source_id,
                action=action,
                policy=policy,
                reason=reason,
                value=effective_value,
            )
        )
    return records
