"""Future-tolerant Formula 1 programme and provider-session matching."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

DEFAULT_DATE_FIELDS = {
    "warmup": "FirstPractice",
    "practice1": "FirstPractice",
    "practice2": "SecondPractice",
    "practice3": "ThirdPractice",
    "sprint_qualifying": "SprintQualifying",
    "pre_sprint": "Sprint",
    "sprint": "Sprint",
    "post_sprint": "Sprint",
    "pre_qualifying": "Qualifying",
    "qualifying": "Qualifying",
    "post_qualifying": "Qualifying",
}


def normalize_session(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value).strip()


def session_date(episode, race, config):
    """Resolve a configured or newly published provider session without guessing."""
    configured = config.get("sessions", {}).get("date_fields", DEFAULT_DATE_FIELDS)
    field = configured.get(episode.program_kind)
    if field and race.session_dates.get(field):
        return race.session_dates[field], field

    programme = normalize_session(episode.program_title)
    candidates = []
    for provider_field, date in race.session_dates.items():
        identity = normalize_session(provider_field)
        if not identity or not date:
            continue
        score = SequenceMatcher(None, programme, identity).ratio()
        programme_tokens = set(programme.split())
        identity_tokens = set(identity.split())
        overlap = len(programme_tokens & identity_tokens) / max(len(identity_tokens), 1)
        if score >= 0.82 or overlap >= 0.8:
            candidates.append((max(score, overlap), provider_field, date))
    candidates.sort(reverse=True)
    if candidates and (len(candidates) == 1 or candidates[0][0] > candidates[1][0]):
        return candidates[0][2], candidates[0][1]
    return race.race_date, "Race"
