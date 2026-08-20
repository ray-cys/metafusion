import asyncio
from pathlib import Path

from helper.io import sha256_file


def normalize_destination(path):
    if not path:
        return None
    return str(Path(path).expanduser().resolve(strict=False))


def canonical_asset_claim(
    media_type,
    tmdb_id,
    asset_type,
    source_path,
    season_number=None,
):
    media_type = str(media_type or "unknown").lower()
    if media_type == "show":
        media_type = "tv"
    season = "" if season_number is None else str(season_number)
    return (
        media_type,
        None if tmdb_id is None else str(tmdb_id),
        str(asset_type),
        season,
        None if source_path is None else str(source_path),
    )


class AssetDestinationRegistry:
    """Job-scoped, indexed ownership and writer coordination for artwork."""

    def __init__(self, records=None):
        self._entries = {}
        self._locks = {}
        for record in records or []:
            self.add_persisted(record)

    def _entry(self, destination):
        key = normalize_destination(destination)
        return key, self._entries.setdefault(
            key,
            {
                "owners": {},
                "runtime_claim": None,
                "runtime_owners": set(),
                "verified": {},
                "checksum_signature": None,
                "checksum": None,
            },
        )

    def add_persisted(self, record):
        if not isinstance(record, dict) or not record.get("destination"):
            return
        key, entry = self._entry(record["destination"])
        cache_key = str(record.get("cache_key") or "unknown")
        normalized = dict(record)
        normalized["cache_key"] = cache_key
        normalized["destination"] = key
        normalized["claim"] = canonical_asset_claim(
            normalized.get("media_type"),
            normalized.get("tmdb_id"),
            normalized.get("asset_type"),
            normalized.get("source_path"),
            normalized.get("season_number"),
        )
        entry["owners"][cache_key] = normalized

    def records_for(self, destination):
        key = normalize_destination(destination)
        entry = self._entries.get(key)
        if entry is None:
            return []
        return list(entry["owners"].values())

    @staticmethod
    def _shareable(candidate, record_claim):
        return (
            candidate[0] == "movie"
            and record_claim[0] == "movie"
            and candidate[1] is not None
            and candidate[4] is not None
            and record_claim[4] is not None
            and candidate[1] == record_claim[1]
            and candidate[2:] == record_claim[2:]
        )

    @staticmethod
    def _same_shared_asset(candidate, record_claim):
        """Allow one owner to advance a shared provider image source safely."""
        return (
            candidate[0] == "movie"
            and record_claim[0] == "movie"
            and candidate[1] is not None
            and candidate[4] is not None
            and record_claim[4] is not None
            and candidate[:3] == record_claim[:3]
            and candidate[3] == record_claim[3]
        )

    def claim(
        self,
        cache_key,
        destination,
        *,
        media_type,
        tmdb_id,
        asset_type,
        source_path,
        season_number=None,
    ):
        cache_key = str(cache_key)
        candidate = canonical_asset_claim(
            media_type,
            tmdb_id,
            asset_type,
            source_path,
            season_number,
        )
        _key, entry = self._entry(destination)
        owners = entry["owners"]
        same_owner = cache_key in owners

        runtime_claim = entry["runtime_claim"]
        if runtime_claim is not None and runtime_claim != candidate:
            owner = next(iter(entry["runtime_owners"]), str(runtime_claim))
            return "collision", owner

        other_records = [
            record for owner, record in owners.items() if owner != cache_key
        ]
        if other_records:
            if same_owner:
                compatible = all(
                    self._same_shared_asset(candidate, record["claim"])
                    for record in other_records
                )
            else:
                compatible = all(
                    self._shareable(candidate, record["claim"])
                    for record in other_records
                )
            if not compatible:
                return "collision", other_records[0]["cache_key"]

        entry["runtime_claim"] = candidate
        already_claimed = bool(entry["runtime_owners"])
        entry["runtime_owners"].add(cache_key)
        if same_owner:
            return "self", cache_key
        if owners or already_claimed:
            return "shared", next(iter(owners or entry["runtime_owners"]))
        return "new", cache_key

    def _current_checksum(self, destination):
        key, entry = self._entry(destination)
        path = Path(key)
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if entry["checksum_signature"] != signature:
                entry["checksum"] = sha256_file(path)
                entry["checksum_signature"] = signature
            return entry["checksum"]
        except OSError:
            entry["checksum"] = None
            entry["checksum_signature"] = None
            return None

    def shared_checksum(
        self,
        cache_key,
        destination,
        *,
        media_type,
        tmdb_id,
        asset_type,
        source_path,
        season_number=None,
    ):
        candidate = canonical_asset_claim(
            media_type,
            tmdb_id,
            asset_type,
            source_path,
            season_number,
        )
        key = normalize_destination(destination)
        entry = self._entries.get(key)
        if entry is None or not Path(key).exists():
            return None
        current = self._current_checksum(key)
        if not current:
            return None
        for owner, record in entry["owners"].items():
            if owner == str(cache_key):
                continue
            if (
                self._shareable(candidate, record["claim"])
                and record.get("checksum") == current
            ):
                return current
        for owner, verified in entry["verified"].items():
            if owner != str(cache_key) and verified == (candidate, current):
                return current
        return None

    def mark_verified(
        self,
        cache_key,
        destination,
        *,
        media_type,
        tmdb_id,
        asset_type,
        source_path,
        season_number=None,
        checksum=None,
    ):
        candidate = canonical_asset_claim(
            media_type,
            tmdb_id,
            asset_type,
            source_path,
            season_number,
        )
        _key, entry = self._entry(destination)
        checksum = checksum or self._current_checksum(destination)
        if checksum:
            entry["verified"][str(cache_key)] = (candidate, checksum)
        return checksum

    def lock_for(self, destination):
        key = normalize_destination(destination)
        loop = asyncio.get_running_loop()
        entry = self._locks.get(key)
        if entry is None or entry[0] is not loop:
            entry = (loop, asyncio.Lock())
            self._locks[key] = entry
        return entry[1]

    def __len__(self):
        return len(self._entries)
