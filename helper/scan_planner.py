"""Learn from completed library inventories and scheduled processing times."""

import json
from datetime import datetime, timedelta, timezone
from statistics import median

from helper.state_db import load_application_record, save_application_record


def scan_deadline(scope, hours):
    try:
        last = datetime.fromisoformat(str(scope.get("last_full")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone() + timedelta(hours=hours)


def schedule_after(moment, slots, *, inclusive=False):
    candidates = []
    for slot in slots:
        hour, minute = map(int, slot.split(":"))
        candidate = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < moment or (candidate == moment and not inclusive):
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates, default=None)


def quiet_opportunity(history, slot, slots, now, deadline, interval_hours):
    if not slot or not deadline or slot not in slots:
        return False
    remaining = deadline - now
    if not timedelta(0) < remaining <= timedelta(hours=min(6, interval_hours / 10)):
        return False
    following = schedule_after(now, slots)
    if following < deadline:
        return False
    current = history.get(slot, [])
    later = history.get(following.strftime("%H:%M"), [])
    return (
        len(current) >= 2
        and len(later) >= 2
        and None not in current
        and (None in later or median(current) < 0.8 * median(later))
    )


class LibraryObservation:
    def __init__(self, scope, records):
        identity = json.dumps([scope["server_id"], scope["library_uuid"]])
        self.key = "adaptive_scan:" + identity
        self.previous = load_application_record(self.key) or {}
        self.history = load_application_record(self.key + ":slots") or {}
        self.current = {str(r["rating_key"]) for r in records if r.get("rating_key")}
        self.valid = len(self.current) == len(records) and scope["server_id"] != "unknown"
        if "seen" in self.previous:
            known = self.previous["seen"]
        else:
            guard = load_application_record("inventory_guard:" + identity) or {}
            known = guard.get("items", {})
        self.missing = set(known) - self.current
        self.confirm = bool(self.missing.intersection(self.previous.get("pending", [])))

    def finish(self, *, success, full, slot, elapsed):
        state = dict(self.previous)
        if success and self.valid:
            state["seen"] = sorted(self.current if full else self.current | self.missing)
            state["pending"] = [] if full else sorted(self.missing)
        if state != self.previous:
            save_application_record(self.key, state)
        if slot and (not success or not full):
            sample = round(elapsed, 2) if success else None
            history = {**self.history, slot: [*self.history.get(slot, [])[-3:], sample]}
            if history != self.history:
                save_application_record(self.key + ":slots", history)
