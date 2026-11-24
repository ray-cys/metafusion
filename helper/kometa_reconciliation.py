"""Preserve absent generated definitions without taking ownership of manual YAML."""

import copy
import hashlib
import json
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


class InventoryIndex:
    """Lookup equivalent to checking the provider evidence in one inventory."""

    def __init__(self, records):
        self.matches = {}
        self.editions = {}
        self.complete = {"tmdb_id", "tvdb_id", "imdb_id"} if records else set()
        for record in records:
            for provider in ("tmdb_id", "tvdb_id", "imdb_id"):
                value = record.get(provider)
                if value:
                    identity = (provider, str(value))
                    self.matches.setdefault(identity, []).append(record)
                    self.editions.setdefault(identity, set()).add(record.get("edition") or None)
                else:
                    self.complete.discard(provider)

    def present(self, match, kind):
        identity = _identity(match, kind)
        if identity is None:
            return None
        matching = self.matches.get(identity)
        if matching:
            if match.get("edition"):
                return isinstance(match["edition"], str) and match["edition"] in self.editions[identity]
            if match.get("blank_edition"):
                return None in self.editions[identity]
            return True
        return False if identity[0] in self.complete else None


def reconcile_kometa_metadata(config, inventories, *, complete):
    """Run even on incremental jobs, but never infer absence from partial scans.

    Freshly generated entries establish ownership. Restore deferred definitions
    from the native catalogue and preserve unowned active entries.
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
        for name, claim in config.get("_kometa_generated_definitions", {}).get(kind, {}).items():
            if name in entries and entries[name].get("match") == claim["match"]:
                owned[name] = copy.deepcopy(claim)
        active = copy.deepcopy(entries)
        compact = copy.deepcopy(pending)

        def target(match, kind=kind):
            edition = match.get("edition")
            if isinstance(edition, (dict, list)):
                edition = (yaml.safe_dump(edition, sort_keys=True),)
            return (_identity(match, kind), edition, bool(match.get("blank_edition")))

        global_index = InventoryIndex(records)
        grouped = {}
        for record in records:
            grouped.setdefault(record.get("scope"), []).append(record)
        scoped_indexes = {key: InventoryIndex(group) for key, group in grouped.items()}
        current_matches = {
            target(v.get("match") or {}) for v in entries.values() if isinstance(v, dict)
        }
        for name, entry in pending.items():
            match = entry.get("match") or {}
            if name in active or target(match) in current_matches:
                continue  # Never overwrite a fresh/manual definition or alias.
            if global_index.present(match, kind) is True:
                active[name] = copy.deepcopy(entry)
                identity = _identity(match, kind)
                matches = global_index.matches[identity]
                libraries = {r.get("library_name") for r in matches}
                scopes = {r.get("scope") for r in matches}
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
            index = scoped_indexes.get(scope) if scope else None
            if name not in entries:
                continue
            if entries[name].get("match") != match:
                result["protected"] += 1
                continue
            held = scope in config.get("_held_inventory_scopes", ())
            present = index.present(match, kind) if index and not held else None
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
