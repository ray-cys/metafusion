"""Read-only verification that generated Kometa output is visible in Plex."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from helper.config import BASE_CONFIG_DIR, report_retention
from helper.identity import metadata_key_for_meta
from helper.plex import get_plex_metadata, load_plex_library_inventory
from helper.plex_artwork_verification import verify_plex_artwork
from helper.plex_metadata import TAG_ATTRIBUTES, _existing_children
from helper.report_identity import item_report_record, item_report_records
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from modules.kometa import validate_metadata_document

SCALAR_ATTRIBUTES = {
    "title": "title",
    "sort_title": "titleSort",
    "original_title": "originalTitle",
    "originally_available": "originallyAvailableAt",
    "content_rating": "contentRating",
    "studio": "studio",
    "tagline": "tagline",
    "summary": "summary",
}
GENERATED_TAG_FIELDS = {
    "movie": {"country", "genre", "director", "writer", "producer"},
    "show": {"genre"},
    "season": set(),
    "episode": {"director", "writer"},
}


def _media_type(value):
    normalized = str(value or "").lower()
    return "show" if normalized in {"show", "shows", "tv"} else "movie"


def _scalar(value):
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value or "").split())


def _tags(values):
    if isinstance(values, str):
        values = [values]
    result = set()
    for value in values or []:
        name = _scalar(getattr(value, "tag", value)).casefold()
        if name:
            result.add(name)
    return result


def _field_expectations(entry, item_type, child_key=""):
    if not isinstance(entry, dict):
        return
    allowed_tags = GENERATED_TAG_FIELDS[item_type]
    for key, expected in entry.items():
        normalized = str(key).removesuffix(".sync")
        if normalized in SCALAR_ATTRIBUTES and expected not in (None, "", []):
            yield child_key, "scalar", normalized, expected
        elif normalized in allowed_tags and expected not in (None, "", []):
            yield child_key, "tag", normalized, expected
    if item_type != "show":
        return
    for season_number, season in (entry.get("seasons") or {}).items():
        try:
            number = int(season_number)
        except (TypeError, ValueError):
            continue
        season_key = f"season:{number}"
        yield from _field_expectations(season, "season", season_key)
        for episode_number, episode in (season.get("episodes") or {}).items():
            try:
                episode_index = int(episode_number)
            except (TypeError, ValueError):
                continue
            yield from _field_expectations(
                episode,
                "episode",
                f"episode:{number}:{episode_index}",
            )


def compare_kometa_entry(entry, item, item_type):
    """Compare generated fields without treating additional Plex values as errors."""
    children = _existing_children(item)
    expectations = list(_field_expectations(entry, item_type))
    for child_key in {expectation[0] for expectation in expectations}:
        target = children.get(child_key)
        if target is not None and hasattr(target, "reload"):
            target.reload()
    checked = matched = 0
    mismatches = []
    for child_key, field_kind, field, expected in expectations:
        checked += 1
        target = children.get(child_key)
        if target is None:
            mismatches.append(
                {
                    "child": child_key or "item",
                    "field": field,
                    "reason": "Plex child is unavailable",
                }
            )
            continue
        if field_kind == "tag":
            attribute = TAG_ATTRIBUTES.get(field, f"{field}s")
            expected_values = _tags(expected)
            actual_values = _tags(getattr(target, attribute, []))
            field_matches = expected_values <= actual_values
            reason = "one or more generated values are absent from Plex"
        else:
            attribute = SCALAR_ATTRIBUTES[field]
            field_matches = _scalar(expected) == _scalar(getattr(target, attribute, None))
            reason = "generated value differs from Plex"
        if field_matches:
            matched += 1
        else:
            mismatches.append(
                {
                    "child": child_key or "item",
                    "field": field,
                    "reason": reason,
                }
            )
    if checked == 0:
        status = "no_verifiable_fields"
    elif matched == checked:
        status = "applied"
    elif matched == 0:
        status = "not_applied"
    else:
        status = "partial"
    return {
        "status": status,
        "fields_checked": checked,
        "fields_matched": matched,
        "fields_missing_or_different": checked - matched,
        "mismatches": mismatches,
    }


def _load_documents(config):
    root = Path(config.get("settings", {}).get("path", "/kometa")) / "metadata"
    documents = {}
    for media_type, filename in (
        ("movie", "movie_metadata.yml"),
        ("show", "tv_metadata.yml"),
    ):
        path = root / filename
        if not path.is_file():
            documents[media_type] = {"path": str(path), "metadata": {}}
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"Unable to read generated Kometa metadata: {path}") from error
        validate_metadata_document(document, library_type=media_type)
        documents[media_type] = {
            "path": str(path),
            "metadata": document.get("metadata", {}),
        }
    return documents


async def verify_kometa_application(sections, config, rating_keys, session):
    """Compare generated YAML and managed artwork with current Plex selections."""
    requested = {str(value) for value in rating_keys or [] if str(value).strip()}
    documents = await asyncio.to_thread(_load_documents, config)
    identity_counts = Counter()
    edition_counts = Counter()
    lightweight = {}
    for section in sections:
        records = await load_plex_library_inventory(
            section, config.get("runtime", {}), records_only=True
        )
        lightweight[section.title] = records
        for record in records:
            kind = _media_type(record.get("media_type"))
            identity_counts[(kind, record.get("title"), record.get("year"))] += 1
            if kind == "movie":
                edition_counts[
                    (record.get("title"), record.get("year"), record.get("edition"))
                ] += 1

    found = set()
    metadata_records = []
    for section in sections:
        available = {
            str(record.get("rating_key"))
            for record in lightweight.get(section.title, [])
            if record.get("rating_key") is not None
        }
        if requested and not requested.intersection(available):
            continue
        inventory = await load_plex_library_inventory(section, config.get("runtime", {}))
        for item in inventory:
            rating_key = str(getattr(item, "ratingKey", ""))
            if requested and rating_key not in requested:
                continue
            found.add(rating_key)
            meta = await get_plex_metadata(
                item,
                _runtime_config=config.get("runtime", {}),
                _plex_config=config.get("plex", {}),
            )
            item_type = _media_type(meta.get("library_type"))
            group = (item_type, meta.get("title"), meta.get("year"))
            meta["requires_unique_key"] = identity_counts[group] > 1
            if item_type == "movie":
                edition_group = (
                    meta.get("title"),
                    meta.get("year"),
                    meta.get("edition_title"),
                )
                meta["edition_key_collision"] = edition_counts[edition_group] > 1
            entry_key = metadata_key_for_meta(meta)
            entry = documents[item_type]["metadata"].get(entry_key)
            identity = {
                "plex_rating_key": rating_key,
                "tmdb_id": meta.get("tmdb_id"),
                "imdb_id": meta.get("imdb_id"),
                "tvdb_id": meta.get("tvdb_id"),
                "edition": meta.get("edition_title"),
                "identity_source": meta.get("identity_source"),
            }
            if not isinstance(entry, dict):
                result = {
                    "status": "missing_yaml",
                    "fields_checked": 0,
                    "fields_matched": 0,
                    "fields_missing_or_different": 0,
                    "mismatches": [],
                }
            else:
                try:
                    result = await asyncio.to_thread(compare_kometa_entry, entry, item, item_type)
                except Exception as error:
                    result = {
                        "status": "unverifiable",
                        "fields_checked": 0,
                        "fields_matched": 0,
                        "fields_missing_or_different": 0,
                        "mismatches": [
                            {
                                "child": "item",
                                "field": "Plex readback",
                                "reason": type(error).__name__,
                            }
                        ],
                    }
            metadata_records.append(
                item_report_record(
                    {
                        "library": section.title,
                        "media_type": item_type,
                        "title": meta.get("title"),
                        "year": meta.get("year"),
                        "yaml_entry": entry_key,
                        "yaml_path": documents[item_type]["path"],
                        **result,
                    },
                    identity,
                )
            )
    for rating_key in sorted(requested - found):
        metadata_records.append(
            item_report_record(
                {
                    "library": "not found",
                    "media_type": "unknown",
                    "title": "Unknown title",
                    "status": "not_found",
                    "fields_checked": 0,
                    "fields_matched": 0,
                    "fields_missing_or_different": 0,
                    "mismatches": [],
                },
                {"plex_rating_key": rating_key},
            )
        )
    artwork_records = await verify_plex_artwork(sections, config, sorted(requested), session)
    return metadata_records, artwork_records


def write_kometa_application_report(
    metadata_records,
    artwork_records,
    *,
    base_dir=None,
    retention=10,
):
    metadata_records = item_report_records(metadata_records)
    artwork_records = item_report_records(artwork_records)
    generated = datetime.now(timezone.utc)
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    path = report_dir / (f"kometa-application-audit-{generated.strftime('%Y%m%d-%H%M%S%f')}.txt")
    metadata_counts = Counter(str(record.get("status") or "unknown") for record in metadata_records)
    artwork_counts = Counter(str(record.get("status") or "unknown") for record in artwork_records)
    lines = [
        "MetaFusion post-Kometa application verification",
        f"Generated: {generated.isoformat()}",
        "This command reads generated YAML, durable ownership, and live Plex. It does not invoke Kometa, refresh Plex, or change metadata or artwork.",
        "Additional Plex tags are allowed; generated tag values must be present.",
        "",
        "Metadata summary",
    ]
    lines.extend(f"- {status}: {count}" for status, count in sorted(metadata_counts.items()))
    if not metadata_counts:
        lines.append("- no items")
    lines.extend(("", "Artwork summary"))
    lines.extend(f"- {status}: {count}" for status, count in sorted(artwork_counts.items()))
    if not artwork_counts:
        lines.append("- no managed artwork")
    lines.extend(("", "Metadata items"))
    for record in metadata_records:
        lines.append(
            f"- [{record.get('status')}] {record.get('library')} | "
            f"{record.get('title')} | rating key={record.get('plex_rating_key')} | "
            f"fields={record.get('fields_matched', 0)}/{record.get('fields_checked', 0)}"
        )
        for mismatch in (record.get("mismatches") or [])[:10]:
            lines.append(
                f"  - {mismatch.get('child')} | {mismatch.get('field')}: {mismatch.get('reason')}"
            )
        if len(record.get("mismatches") or []) > 10:
            lines.append("  - additional mismatches are available in the JSON companion")
    lines.extend(("", "Artwork items"))
    for record in artwork_records:
        lines.append(
            f"- [{record.get('status')}] {record.get('library') or 'unknown library'} | "
            f"{record.get('title') or 'unknown item'} | "
            f"{record.get('asset_type') or 'artwork'} | {record.get('reason')}"
        )
    data = {
        "summary": {
            "metadata": dict(metadata_counts),
            "artwork": dict(artwork_counts),
        },
        "metadata_items": metadata_records,
        "artwork_items": artwork_records,
    }
    write_diagnostic_report(
        path,
        "\n".join(lines).rstrip() + "\n",
        report_type="kometa_application_audit",
        data=data,
        generated_at=generated,
    )
    retain_diagnostic_reports(report_dir, "kometa-application-audit", retention)
    return path


async def run_kometa_application_audit(
    sections,
    config,
    rating_keys,
    session,
    *,
    base_dir=None,
):
    metadata, artwork = await verify_kometa_application(sections, config, rating_keys, session)
    report = write_kometa_application_report(
        metadata,
        artwork,
        base_dir=base_dir,
        retention=report_retention(config),
    )
    return metadata, artwork, report
