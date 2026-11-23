"""Retire only verified, extension-owned outputs of a missing championship."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from extensions.formula1.state import utc_now
from helper.io import sha256_file


def section_identity(sections):
    if len(sections) == 1:
        section = sections[0]
        server = getattr(getattr(section, "_server", None), "machineIdentifier", None)
        library = getattr(section, "uuid", None)
        if server and library:
            return json.dumps([str(server), str(library)])
    return None


def remember_championship(state, year, scope, rating_key, metadata_dir, yaml_digest):
    if scope:
        with state.connection as db:
            db.execute(
                """INSERT INTO championship_outputs(year, scope, rating_key, metadata_dir, yaml_digest)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(year) DO UPDATE SET
                   scope=excluded.scope, rating_key=excluded.rating_key, metadata_dir=excluded.metadata_dir, yaml_digest=excluded.yaml_digest,
                   missing_since=NULL, missing_scans=0
                   WHERE scope != excluded.scope OR rating_key != excluded.rating_key
                      OR metadata_dir != excluded.metadata_dir OR yaml_digest IS NOT excluded.yaml_digest
                      OR missing_scans != 0""",
                (year, scope, rating_key, str(metadata_dir), yaml_digest),
            )


def _safe_remove(path, root, digest, dry_run):
    path = Path(path)
    if not path.is_relative_to(root) or path.resolve() != path or path == root:
        return "protected"
    if not path.exists():
        return "absent"
    if not digest or not path.is_file() or sha256_file(path) != digest:
        return "protected"
    if not dry_run:
        path.unlink()
    return "removed"


def _retire(state, config, row, issues):
    year = row["year"]
    paths = config["paths"]
    dry = config["dry_run"]
    metadata = paths["metadata"] / f"formula1_{year}.yml"
    outcomes = {str(metadata): _safe_remove(metadata, paths["metadata"], row["yaml_digest"], dry)}
    if outcomes[str(metadata)] == "protected":
        issues.append(
            f"Championship {year}: preserving unverified or edited YAML and its artwork: {metadata}"
        )
        return 0

    prefix = f"{year}:r"
    round_root = paths["assets"] / str(year)
    show_root = paths["show_assets"] / str(year)
    db = state.connection
    outputs = {
        r["destination"]: (round_root, r["checksum"])
        for r in db.execute(
            "SELECT destination,checksum FROM artwork_state WHERE logical_key LIKE ?",
            (prefix + "%",),
        )
    }
    for rotation in state.show_rotation_history():
        if rotation["season_year"] == year:
            checksums = rotation["source"].get("generated_checksums", {})
            for kind in ("poster", "background"):
                outputs[rotation[kind + "_destination"]] = (show_root, checksums.get(kind))
    rotation = state.show_rotation(f"show:{year}")
    if rotation:
        for kind in ("poster", "background"):
            outputs[rotation[kind + "_destination"]] = (show_root, rotation[kind + "_checksum"])
    for path, (root, checksum) in outputs.items():
        outcomes[path] = _safe_remove(path, root, checksum, dry)
        if outcomes[path] == "protected":
            issues.append(f"Championship {year}: preserving unverified or edited artwork: {path}")

    with db:
        for table in ("artwork_state", "item_bindings", "cleanup_candidates"):
            db.execute(f"DELETE FROM {table} WHERE logical_key LIKE ?", (prefix + "%",))
        for table in (
            "show_rotation_state",
            "show_rotation_history",
            "show_bindings",
            "application_verification_queue",
            "event_identity_bindings",
            "episode_round_sources",
        ):
            db.execute(f"DELETE FROM {table} WHERE season_year=?", (year,))
        db.execute("DELETE FROM championship_outputs WHERE year=?", (year,))
        details = {
            "year": year,
            "plex_rating_key": row["rating_key"],
            "plex_scope": json.loads(row["scope"]),
            "outputs": outcomes,
        }
        db.execute(
            "INSERT INTO cleanup_history(logical_key,action,details,created_at) VALUES (?,?,?,?)",
            (f"show:{year}", "automatic", json.dumps(details), utc_now().isoformat()),
        )
    return sum(value == "removed" for value in outcomes.values())


def reconcile_championships(
    state, config, scope, years, rating_keys, issues, *, logger=None, now=None
):
    """Called after complete discovery. Dry runs use a disposable database snapshot."""
    current = now or utc_now()
    cleanup = config["cleanup"]
    removed = 0
    for row in state.connection.execute("SELECT * FROM championship_outputs").fetchall():
        year = row["year"]
        if (
            row["scope"] != scope
            or row["metadata_dir"] != str(config["paths"]["metadata"])
            or year >= current.year
        ):
            continue
        present = None in years or year in years or row["rating_key"] in rating_keys
        if present or not cleanup["enabled"]:
            if row["missing_scans"]:
                with state.connection as db:
                    db.execute(
                        "UPDATE championship_outputs SET missing_since=NULL, missing_scans=0 WHERE year=?",
                        (year,),
                    )
            continue
        since = datetime.fromisoformat(row["missing_since"]) if row["missing_since"] else current
        scans = row["missing_scans"] + 1
        with state.connection as db:
            db.execute(
                "UPDATE championship_outputs SET missing_since=?, missing_scans=? WHERE year=?",
                (since.isoformat(), scans, year),
            )
        if logger:
            logger.info(
                "[Cleanup] Missing championship | Year: %d | Observations: %d | First absent: %s | Dry run: %s",
                year,
                scans,
                since.isoformat(),
                config["dry_run"],
            )
        if scans >= cleanup["confirmation_scans"] and current - since >= timedelta(
            hours=cleanup["grace_hours"]
        ):
            removed += _retire(state, config, row, issues)
    return removed
