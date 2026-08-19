"""Stable identity fields shared by machine-readable item reports."""

IDENTITY_REPORT_FIELDS = (
    "plex_rating_key",
    "tmdb_id",
    "imdb_id",
    "tvdb_id",
    "edition",
    "season_number",
    "identity_source",
)


def _first(source, *names):
    for name in names:
        value = source.get(name)
        if value not in (None, ""):
            return value
    return None


def _identifier(value):
    if value in (None, ""):
        return None
    return str(value)


def report_identity(source=None, **overrides):
    """Return the normalized, nullable identity contract for one report item."""
    source = source if isinstance(source, dict) else {}
    values = {
        "plex_rating_key": _first(source, "plex_rating_key", "rating_key", "ratingKey"),
        "tmdb_id": _first(source, "tmdb_id"),
        "imdb_id": _first(source, "imdb_id"),
        "tvdb_id": _first(source, "tvdb_id"),
        "edition": _first(source, "edition", "edition_title", "editionTitle"),
        "season_number": _first(source, "season_number", "season"),
        "identity_source": _first(
            source,
            "identity_source",
            "identity_match_source",
            "tmdb_id_source",
        ),
    }
    for name, value in overrides.items():
        if name in IDENTITY_REPORT_FIELDS:
            values[name] = value
    for name in ("plex_rating_key", "tmdb_id", "imdb_id", "tvdb_id"):
        values[name] = _identifier(values[name])
    values["edition"] = None if values["edition"] in (None, "") else str(values["edition"])
    values["identity_source"] = (
        None if values["identity_source"] in (None, "") else str(values["identity_source"])
    )
    season = values["season_number"]
    if season in (None, ""):
        values["season_number"] = None
    else:
        try:
            values["season_number"] = int(season)
        except (TypeError, ValueError):
            values["season_number"] = str(season)
    return values


def item_report_record(record, source=None, **overrides):
    """Copy an item record and guarantee every normalized identity field."""
    record = dict(record or {})
    identity_source = source if isinstance(source, dict) else record
    identity_overrides = {
        name: record[name] for name in IDENTITY_REPORT_FIELDS if record.get(name) not in (None, "")
    }
    identity_overrides.update(overrides)
    return {
        **record,
        **report_identity(identity_source, **identity_overrides),
    }


def item_report_records(records):
    """Normalize a sequence of item records while dropping non-object values."""
    return [item_report_record(record) for record in (records or []) if isinstance(record, dict)]
