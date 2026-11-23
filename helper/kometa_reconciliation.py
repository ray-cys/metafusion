"""Preserve absent generated definitions without taking ownership of manual YAML."""

import copy
import hashlib
import json
import logging
from pathlib import Path

import yaml

from helper.io import atomic_write_json
from modules.kometa import validate_metadata_document, write_kometa_metadata

CAPABILITY_VERSION = 1


def record_generated_definition(config, media_type, key, entry):
    if config.get("settings", {}).get("mode", "kometa").lower() != "kometa":
        return
    kind = "movie" if media_type == "movie" else "tv"
    config.setdefault("_kometa_generated_definitions", {}).setdefault(kind, {})[key] = {
        "match": copy.deepcopy(entry.get("match") or {}),
        "library": config.get("_kometa_reconciliation_library"),
        "scope": config.get("_kometa_reconciliation_scope"),
    }


def _identity(match, kind):
    value = str(match.get("mapping_id") or "")
    if value.isdigit():
        return ("tmdb_id" if kind == "movie" else "tvdb_id", value)
    if value.startswith("tt") and value[2:].isdigit():
        return "imdb_id", value
    return None


def _present(match, kind, records):
    identity = _identity(match, kind)
    if identity is None:
        return None
    provider, value = identity
    # Missing identifiers anywhere make an absence claim unsafe.
    matching = [r for r in records if str(r.get(provider) or "") == value]
    if matching:
        if match.get("edition"):
            return any(r.get("edition") == match["edition"] for r in matching)
        if match.get("blank_edition"):
            return any(not r.get("edition") for r in matching)
        return True
    if not records or any(not r.get(provider) for r in records):
        return None
    return False


def _import_legacy_catalogue(root, kind, markers, entries, pending):
    if kind in markers:
        return 0
    path = root / ".preflight" / f"{kind}_metadata.yml.deferred.yml"
    try:
        source = path.read_bytes()
    except FileNotFoundError:
        return 0
    except OSError as error:
        logging.getLogger(__name__).warning(
            "[Kometa] Legacy import pending | %s | %s | Native reconciliation continues; "
            "will retry on the next eligible run.", path, error,
        )
        return 0

    imported = yaml.safe_load(source) or {}
    if not isinstance(imported, dict):
        raise TypeError("Invalid legacy deferred catalogue; originals preserved")
    additions = {}
    for name, entry in imported.items():
        if name not in entries and name not in pending and isinstance(entry, dict):
            validate_metadata_document({"metadata": {name: entry}})
            additions[name] = entry
    pending.update(additions)
    markers[kind] = hashlib.sha256(source).hexdigest()
    return len(additions)


def reconcile_kometa_metadata(config, inventories, *, complete):
    """Run even on incremental jobs, but never infer absence from partial scans.

    Freshly generated entries establish ownership. The old preflight's deferred
    catalogue is imported non-destructively; unowned active entries stay intact.
    Persist a catalogue superset before replacing YAML, then compact afterwards.
    """
    result = {
        "schema": 1,
        "deferred": 0,
        "restored": 0,
        "protected": 0,
        "imported": 0,
        "actions": [],
    }
    config["_kometa_reconciliation"] = result
    if config.get("settings", {}).get("mode", "kometa").lower() != "kometa":
        return {**result, "status": "not_applicable"}
    if not complete or config.get("settings", {}).get("dry_run", False):
        result["status"] = "deferred"
        return result
    root = Path(config.get("settings", {}).get("path", "."))
    state_path = root / ".metafusion" / "metadata-reconciliation.json"
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = {"schema": 1, "owned": {}, "deferred": {}}
    if state.get("schema") != 1 or not all(
        isinstance(state.get(k), dict) for k in ("owned", "deferred")
    ):
        raise ValueError("Unsupported Kometa reconciliation catalogue; originals preserved")
    state = copy.deepcopy(state)
    state.setdefault("legacy_imports", {})
    saved_state = copy.deepcopy(state)

    def persist_state():
        nonlocal saved_state
        if state != saved_state:
            atomic_write_json(state_path, state, backup=True)
            saved_state = copy.deepcopy(state)

    for kind in ("movie", "tv"):
        records = [r for r in inventories if r.get("media_type") == kind]
        path = root / "metadata" / f"{kind}_metadata.yml"
        if not records:
            continue
        try:
            source = path.read_bytes()
        except FileNotFoundError:
            continue
        document = yaml.safe_load(source) or {}
        validate_metadata_document(document)
        snapshot = (True, hashlib.sha256(source).hexdigest())
        entries = document.get("metadata") or {}
        owned = state["owned"].setdefault(kind, {})
        pending = state["deferred"].setdefault(kind, {})
        result["imported"] += _import_legacy_catalogue(
            root, kind, state["legacy_imports"], entries, pending
        )
        for name, claim in config.get("_kometa_generated_definitions", {}).get(kind, {}).items():
            if name in entries and entries[name].get("match") == claim["match"]:
                owned[name] = copy.deepcopy(claim)
        active = copy.deepcopy(entries)
        compact = copy.deepcopy(pending)

        def target(match, kind=kind):
            return (_identity(match, kind), match.get("edition"), bool(match.get("blank_edition")))

        current_matches = [
            target(v.get("match") or {}) for v in entries.values() if isinstance(v, dict)
        ]
        for name, entry in pending.items():
            match = entry.get("match") or {}
            if name in active or target(match) in current_matches:
                continue  # Never overwrite a fresh/manual definition or alias.
            if _present(match, kind, records) is True:
                active[name] = copy.deepcopy(entry)
                identity = _identity(match, kind)
                libraries = {
                    r.get("library_name")
                    for r in records
                    if identity and str(r.get(identity[0]) or "") == identity[1]
                }
                scopes = {
                    r.get("scope")
                    for r in records
                    if identity and str(r.get(identity[0]) or "") == identity[1]
                }
                owned[name] = {
                    "match": copy.deepcopy(match),
                    "library": next(iter(libraries)) if len(libraries) == 1 else None,
                    "scope": next(iter(scopes)) if len(scopes) == 1 else None,
                }
                compact.pop(name, None)
                result["restored"] += 1
                result["actions"].append(f"Restored {kind} definition: {name}")
        for name, claim in list(owned.items()):
            match = claim["match"]
            scope = claim.get("scope")
            scoped_records = [r for r in records if r.get("scope") == scope] if scope else []
            if name not in entries:
                continue
            if entries[name].get("match") != match:
                result["protected"] += 1
                continue
            present = _present(match, kind, scoped_records)
            if present is False:
                compact[name] = copy.deepcopy(entries[name])
                active.pop(name, None)
                result["deferred"] += 1
                result["actions"].append(f"Deferred absent {kind} definition: {name}")
            elif present is None:
                result["protected"] += 1
        # Durable preservation precedes YAML removal; restoration precedes prune.
        pending.update(compact)
        state["deferred"][kind] = pending
        persist_state()
        if active != entries:
            document["metadata"] = active
            output = config.get("output", {})
            write_kometa_metadata(
                path,
                document,
                library_type=kind,
                backup_count=max(1, output.get("backup_count", 3)),
                expected_snapshot=snapshot,
            )
        state["deferred"][kind] = compact
        persist_state()
    result["status"] = "ok"
    result["catalogue_sha256"] = hashlib.sha256(
        json.dumps(state, sort_keys=True).encode()
    ).hexdigest()
    result["actions"] = result["actions"][:50]
    return result
