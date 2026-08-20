import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from helper import plex_metadata


class Editable:
    def __init__(self, **values):
        self.ratingKey = values.pop("ratingKey", "1")
        self.type = values.pop("type", "movie")
        self._locks = values.pop("locks", {})
        for key, value in values.items():
            setattr(self, key, value)

    def reload(self):
        return self

    def isLocked(self, field):
        return self._locks.get(field, False)

    def batchEdits(self):
        return self

    def editField(self, field, value, locked=False):
        setattr(self, field, value)
        self._locks[field] = locked
        return self

    def editTags(self, field, values, locked=False, remove=False):
        attribute = plex_metadata.TAG_ATTRIBUTES.get(field, f"{field}s")
        current = list(getattr(self, attribute, []))
        if remove:
            removed = {str(value).casefold() for value in values}
            current = [
                value
                for value in current
                if str(getattr(value, "tag", value)).casefold() not in removed
            ]
        else:
            existing = {
                str(getattr(value, "tag", value)).casefold() for value in current
            }
            current.extend(
                SimpleNamespace(tag=value)
                for value in values
                if str(value).casefold() not in existing
            )
        setattr(self, attribute, current)
        self._locks[field] = locked
        return self

    def saveEdits(self):
        return self


def _config(policy="managed", *, dry_run=False, limit=100):
    return {
        "settings": {"mode": "plex", "dry_run": dry_run},
        "plex_metadata": {
            "enabled": True,
            "policy": policy,
            "lock_writes": False,
            "lock_merged_tags": False,
            "max_writes_per_run": limit,
            "fields": [],
        },
        "runtime": {"plex_retries": 2, "plex_retry_delay": 0},
    }


def _identity():
    return {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "rating_key": "1",
        "media_type": "movie",
    }


def _owner(field, kind="scalar", *, applied="old", original="original", owned=None):
    return plex_metadata._record_payload(
        _identity(),
        "",
        field,
        kind,
        original,
        applied,
        owned or ([applied] if applied else []),
        False,
        False,
    )


def test_scalar_tag_helpers_reporter_lifecycle_and_child_mapping(monkeypatch, tmp_path):
    assert plex_metadata._clean_scalar(date(2026, 1, 2)) == "2026-01-02"
    assert plex_metadata._clean_tags(["Drama", "drama", "", SimpleNamespace(tag="Action")]) == [
        "Drama",
        "Action",
    ]
    assert plex_metadata._missing_tags(["Drama"], ["drama", "Action"]) == ["Action"]
    assert plex_metadata._media_index(SimpleNamespace(index="bad", seasonNumber="2"), "index", "seasonNumber") == 2
    assert plex_metadata._media_index(SimpleNamespace(index="bad"), "index") is None

    config = _config()
    monkeypatch.setattr(plex_metadata, "write_diagnostic_report", lambda *_a, **_k: tmp_path / "report.txt")
    monkeypatch.setattr(plex_metadata, "retain_diagnostic_reports", lambda *_a, **_k: None)
    plex_metadata._reporter = None
    assert plex_metadata.finish_plex_metadata_run(config).name.startswith("plex-metadata-")
    reporter = plex_metadata.get_plex_metadata_reporter(config)
    assert reporter is plex_metadata.get_plex_metadata_reporter(config)
    assert plex_metadata.begin_plex_metadata_run(config) is not reporter

    episode = Editable(index=1)
    season = Editable(index=1)
    season.episodes = lambda: [episode]
    root = Editable()
    root.seasons = lambda: [season]
    candidate = {
        "root": {},
        "seasons": {
            "1": {"episodes": {"1": {"fields": {"title": "Episode"}}, "2": {}}},
            "2": {},
        },
    }
    children = list(plex_metadata._children_for_candidate(root, candidate))
    assert [key for key, *_rest in children] == ["", "season:1", "episode:1:1"]


def test_apply_object_scalar_and_tag_policy_matrix():
    config = _config("managed")
    reporter = plex_metadata.PlexMetadataReporter(config)
    item = Editable(
        summary="old",
        title="manual",
        originalTitle="manual original",
        genres=[SimpleNamespace(tag="Drama"), SimpleNamespace(tag="Manual")],
        locks={"title": True, "country": True},
    )
    ownership = {
        ("", "summary"): _owner("summary", applied="old", original="original"),
        ("", "title"): _owner("title", applied="provider", original="original"),
        ("", "originalTitle"): _owner(
            "originalTitle", applied="provider original", original="original"
        ),
        ("", "genre"): _owner(
            "genre", "tag", applied=["Drama", "Action"], original=["Drama"], owned=["Drama", "Action"]
        ),
    }
    ownership[("", "genre")]["owned_values"]["relinquished"] = ["Rejected"]
    candidate = {
        "fields": {
            "summary": "",
            "title": "new",
            "originalTitle": "new original",
            "tagline": "",
            "contentRating": "R",
        },
        "tags": {
            "genre": ["Drama", "Action", "Rejected"],
            "country": ["US"],
            "writer": [],
        },
    }
    writes, records = plex_metadata._apply_object(
        item, candidate, "", _identity(), ownership,
        config["plex_metadata"], reporter, "Example", False
    )
    assert writes == 1
    assert item.summary == "original"
    assert item.contentRating == "R"
    assert {entry["field_name"] for entry in records if "field_name" in entry} >= {
        "summary",
        "genre",
        "contentRating",
    }
    assert any("_delete_key" in entry for entry in records)
    assert reporter.counts["conflict"] >= 1
    assert reporter.counts["locked_skipped"] >= 1
    assert reporter.counts["source_missing"] == 0


def test_apply_object_dry_run_limits_and_retention_failures():
    dry = _config("overwrite", dry_run=True)
    reporter = plex_metadata.PlexMetadataReporter(dry)
    item = Editable(summary="old", genres=[SimpleNamespace(tag="Old")])
    writes, records = plex_metadata._apply_object(
        item,
        {"fields": {"summary": "new"}, "tags": {"genre": ["New"]}},
        "", _identity(), {}, dry["plex_metadata"], reporter, "Example", True
    )
    assert writes == 0 and records == []
    assert reporter.counts["would_fill"] and reporter.counts["would_remove"]

    limited = _config("overwrite", limit=1)
    reporter = plex_metadata.PlexMetadataReporter(limited)
    assert reporter.claim_write("Movies", 1)
    writes, records = plex_metadata._apply_object(
        Editable(summary="old"), {"fields": {"summary": "new"}}, "",
        _identity(), {}, limited["plex_metadata"], reporter, "Example", False
    )
    assert writes == 0 and records == [] and reporter.counts["write_limit"] == 1

    class RejectScalar(Editable):
        def editField(self, field, value, locked=False):
            self._locks[field] = locked
            return self

    with pytest.raises(RuntimeError, match="retain"):
        plex_metadata._apply_object(
            RejectScalar(summary="old"), {"fields": {"summary": "new"}}, "",
            _identity(), {}, _config("overwrite")["plex_metadata"],
            plex_metadata.PlexMetadataReporter(_config("overwrite")), "Example", False
        )

    class RejectTags(Editable):
        def editTags(self, field, values, locked=False, remove=False):
            self._locks[field] = locked
            return self

    with pytest.raises(RuntimeError, match="retain"):
        plex_metadata._apply_object(
            RejectTags(genres=[]), {"tags": {"genre": ["New"]}}, "",
            _identity(), {}, _config("overwrite")["plex_metadata"],
            plex_metadata.PlexMetadataReporter(_config("overwrite")), "Example", False
        )


def test_rollback_and_apply_candidate_failure_paths(monkeypatch):
    item = Editable(summary="new", genres=[SimpleNamespace(tag="New")])
    records = [
        _owner("summary", applied="new", original="old"),
        _owner("genre", "tag", applied=["New"], original=["Old"], owned=["New"]),
    ]
    plex_metadata._rollback_untracked_write(item, records)
    assert item.summary == "old"
    assert plex_metadata._clean_tags(item.genres) == ["Old"]

    config = _config("overwrite")
    reporter = plex_metadata.PlexMetadataReporter(config)
    monkeypatch.setattr(plex_metadata, "load_plex_metadata_ownership", lambda *_a: {})
    monkeypatch.setattr(
        plex_metadata,
        "_existing_children",
        lambda _item: (_ for _ in ()).throw(RuntimeError("children")),
    )
    assert plex_metadata._apply_candidate(item, {}, config, {}, reporter)["failures"] == 1

    monkeypatch.setattr(plex_metadata, "_existing_children", lambda root: {"": root})
    monkeypatch.setattr(
        plex_metadata,
        "save_plex_metadata_ownership",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ledger")),
    )
    result = plex_metadata._apply_candidate(
        Editable(summary="old"), {"root": {"fields": {"summary": "new"}}},
        config, {"title": "Example"}, reporter
    )
    assert result["writes"] == 1 and result["failures"] == 1


def test_apply_and_restore_retry_wrappers(monkeypatch):
    config = _config()
    item = Editable(title="Example")
    assert asyncio.run(plex_metadata.apply_plex_metadata(item, {}, config, {})) == {
        "writes": 0,
        "failures": 0,
    }
    attempts = []

    def apply(*_args):
        attempts.append(True)
        return {"writes": 1, "failures": 1 if len(attempts) == 1 else 0, "deferred": 2}

    monkeypatch.setattr(plex_metadata, "_apply_candidate", apply)
    result = asyncio.run(
        plex_metadata.apply_plex_metadata(
            item, {"root": {"fields": {"summary": "new"}}}, config, {"title": "Example"}
        )
    )
    assert result == {"writes": 2, "failures": 0, "deferred": 2}

    attempts.clear()
    monkeypatch.setattr(
        plex_metadata,
        "_restore_candidate",
        lambda *_a, **_k: (
            attempts.append(True) or {"writes": 0, "failures": 1}
        ),
    )
    assert asyncio.run(plex_metadata.restore_plex_metadata(item, config, {}))["failures"] == 1


def test_restore_tag_unlock_lock_only_and_unowned_child(monkeypatch):
    config = _config()
    meta = {
        "server_id": "server",
        "library_uuid": "uuid",
        "library_name": "Movies",
        "ratingKey": "1",
        "library_type": "movie",
        "title": "Example",
    }
    reporter = plex_metadata.PlexMetadataReporter(config)
    saved = []
    monkeypatch.setattr(
        plex_metadata,
        "save_plex_metadata_ownership",
        lambda records, **kwargs: saved.append((records, kwargs)),
    )

    unlock_record = _owner(
        "genre", "tag", applied=["Drama"], original=["Drama"], owned=["Drama"]
    )
    unlock_record["metafusion_locked"] = 1
    monkeypatch.setattr(
        plex_metadata,
        "load_plex_metadata_ownership",
        lambda *_args: {("", "genre"): unlock_record},
    )
    season = Editable(index=1)
    item = Editable(genres=[SimpleNamespace(tag="Drama")], locks={"genre": True})
    item.seasons = lambda: [season]
    result = plex_metadata._restore_candidate(
        item, config, meta, reporter, unlock_only=True
    )
    assert result == {"writes": 1, "failures": 0}
    assert item.isLocked("genre") is False

    restore_record = _owner(
        "genre", "tag", applied=["Drama"], original=["Drama"], owned=["Drama"]
    )
    restore_record["original_locked"] = 1
    monkeypatch.setattr(
        plex_metadata,
        "load_plex_metadata_ownership",
        lambda *_args: {("", "genre"): restore_record},
    )
    item = Editable(genres=[SimpleNamespace(tag="Drama")], locks={"genre": False})
    result = plex_metadata._restore_candidate(item, config, meta, reporter)
    assert result == {"writes": 1, "failures": 0}
    assert item.isLocked("genre") is True
