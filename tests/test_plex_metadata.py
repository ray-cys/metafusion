import asyncio
import logging
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from helper import plex_metadata as plex_metadata_module
from helper import state_db
from helper.config import DEFAULT_CONFIG, validate_config
from helper.plex_metadata import (
    PlexMetadataReporter,
    _apply_candidate,
    _media_index,
    _restore_candidate,
    apply_plex_metadata,
    begin_plex_metadata_run,
    finish_plex_metadata_run,
    get_plex_metadata_reporter,
    restore_plex_metadata,
)
from helper.state_db import MediaStateStore, load_plex_metadata_ownership


class EditableItem:
    def __init__(self, **values):
        self.ratingKey = values.pop("ratingKey", "10")
        self._locks = values.pop("locks", {})
        self.fields = [
            SimpleNamespace(name=name, locked=locked)
            for name, locked in self._locks.items()
        ]
        self.batch_count = 0
        for key, value in values.items():
            setattr(self, key, value)

    def isLocked(self, field):
        return self._locks.get(field, False)

    def batchEdits(self):
        self.batch_count += 1
        return self

    def editField(self, field, value, locked=True):
        setattr(self, field, value)
        self._locks[field] = locked
        return self

    def editTags(self, field, values, locked=True, remove=False):
        attribute = {"country": "countries"}.get(field, f"{field}s")
        current = list(getattr(self, attribute, []))
        if remove:
            removed = {str(value).casefold() for value in values}
            current = [
                value
                for value in current
                if str(getattr(value, "tag", value)).casefold() not in removed
            ]
        else:
            current.extend(
                SimpleNamespace(tag=value)
                for value in values
                if str(value).casefold()
                not in {str(getattr(item, "tag", item)).casefold() for item in current}
            )
        setattr(self, attribute, current)
        self._locks[field] = locked
        return self

    def saveEdits(self):
        return self

    def reload(self):
        return self


def plex_config(policy="fill_missing", dry_run=False):
    return {
        "settings": {"mode": "plex", "dry_run": dry_run},
        "plex_metadata": {
            "enabled": True,
            "policy": policy,
            "lock_writes": False,
            "lock_merged_tags": False,
            "allow_overwrite": policy == "overwrite",
            "max_writes_per_run": 10,
            "fields": [],
        },
    }


def identity():
    return {
        "server_id": "server-1",
        "library_uuid": "library-1",
        "library_name": "Movies",
        "ratingKey": "10",
        "library_type": "movie",
        "title": "Example",
    }


def test_fill_missing_preserves_existing_and_locked_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    item = EditableItem(
        summary="Manual summary",
        studio="",
        genres=[SimpleNamespace(tag="Drama")],
        locks={"studio": True},
    )
    candidate = {
        "root": {
            "fields": {"summary": "TMDb summary", "studio": "TMDb Studio"},
            "tags": {"genre": ["Drama", "Action"]},
        }
    }
    reporter = PlexMetadataReporter(plex_config())

    result = _apply_candidate(item, candidate, plex_config(), identity(), reporter)

    assert result == {"writes": 1, "failures": 0}
    assert item.summary == "Manual summary"
    assert item.studio == ""
    assert [genre.tag for genre in item.genres] == ["Drama", "Action"]
    ownership = load_plex_metadata_ownership("server-1", "library-1", "10")
    assert ("", "genre") in ownership
    assert ("", "summary") not in ownership
    assert reporter.counts["existing_skipped"] == 1
    assert reporter.counts["locked_skipped"] == 1


def test_managed_policy_does_not_readd_a_manually_removed_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    item = EditableItem(genres=[])
    candidate = {"root": {"tags": {"genre": ["Action"]}}}
    _apply_candidate(
        item,
        candidate,
        plex_config(),
        identity(),
        PlexMetadataReporter(plex_config()),
    )
    item.genres = []
    managed = plex_config("managed")
    reporter = PlexMetadataReporter(managed)

    result = _apply_candidate(item, candidate, managed, identity(), reporter)

    assert result == {"writes": 0, "failures": 0}
    assert item.genres == []
    assert reporter.counts["conflict"] == 1
    ownership = load_plex_metadata_ownership("server-1", "library-1", "10")
    assert ownership[("", "genre")]["owned_values"] == {
        "values": [],
        "relinquished": ["Action"],
    }

    second_reporter = PlexMetadataReporter(managed)
    _apply_candidate(item, candidate, managed, identity(), second_reporter)
    assert second_reporter.counts["conflict"] == 0
    assert second_reporter.counts["unchanged"] == 1


def test_restore_reverts_only_owned_field_and_removes_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    config["plex_metadata"]["lock_writes"] = True
    item = EditableItem(summary="")
    _apply_candidate(
        item,
        {"root": {"fields": {"summary": "TMDb summary"}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )
    assert item.summary == "TMDb summary"
    assert item.isLocked("summary") is True

    result = _restore_candidate(
        item,
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    assert result == {"writes": 1, "failures": 0}
    assert item.summary == ""
    assert item.isLocked("summary") is False
    assert load_plex_metadata_ownership("server-1", "library-1", "10") == {}


def test_manual_scalar_change_relinquishes_managed_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    item = EditableItem(summary="")
    candidate = {"root": {"fields": {"summary": "First summary"}}}
    _apply_candidate(
        item,
        candidate,
        plex_config(),
        identity(),
        PlexMetadataReporter(plex_config()),
    )
    item.summary = "Manual summary"
    managed = plex_config("managed")

    _apply_candidate(
        item,
        {"root": {"fields": {"summary": "New TMDb summary"}}},
        managed,
        identity(),
        PlexMetadataReporter(managed),
    )

    assert item.summary == "Manual summary"
    assert load_plex_metadata_ownership("server-1", "library-1", "10") == {}


def test_failed_ownership_commit_rolls_back_plex_write(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    monkeypatch.setattr(
        plex_metadata_module,
        "save_plex_metadata_ownership",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("database failed")),
    )
    item = EditableItem(summary="")
    config = plex_config()

    result = _apply_candidate(
        item,
        {"root": {"fields": {"summary": "TMDb summary"}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    assert result["failures"] == 1
    assert item.summary == ""
    assert item.isLocked("summary") is False


def test_overwrite_replaces_selected_locked_scalar_and_tags(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    item = EditableItem(
        summary="Manual",
        genres=[SimpleNamespace(tag="Drama"), SimpleNamespace(tag="Manual")],
        locks={"summary": True, "genre": True},
    )
    config = plex_config("overwrite")

    result = _apply_candidate(
        item,
        {
            "root": {
                "fields": {"summary": "TMDb"},
                "tags": {"genre": ["Action"]},
            }
        },
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    assert result == {"writes": 1, "failures": 0}
    assert item.summary == "TMDb"
    assert [genre.tag for genre in item.genres] == ["Action"]
    assert item.isLocked("summary") is False


def test_season_and_episode_candidates_use_one_write_per_object(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    episode = EditableItem(ratingKey="episode", index=2, summary="", writers=[])
    season = EditableItem(
        ratingKey="season", index=1, summary="", episodes=lambda: [episode]
    )
    show = EditableItem(ratingKey="10", summary="", seasons=lambda: [season])
    candidate = {
        "root": {"fields": {"summary": "Show summary"}},
        "seasons": {
            1: {
                "fields": {"summary": "Season summary"},
                "episodes": {
                    2: {
                        "fields": {"summary": "Episode summary"},
                        "tags": {"writer": ["Writer"]},
                    }
                },
            }
        },
    }
    config = plex_config()

    result = _apply_candidate(
        show,
        candidate,
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    assert result == {"writes": 3, "failures": 0}
    assert show.summary == "Show summary"
    assert season.summary == "Season summary"
    assert episode.summary == "Episode summary"
    assert [writer.tag for writer in episode.writers] == ["Writer"]


def test_item_ownership_is_committed_once_for_all_children(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    calls = []
    original_save = state_db.save_plex_metadata_ownership

    def capture_save(*args, **kwargs):
        calls.append((args, kwargs))
        return original_save(*args, **kwargs)

    monkeypatch.setattr(
        plex_metadata_module, "save_plex_metadata_ownership", capture_save
    )
    season = EditableItem(ratingKey="season", index=1, summary="", episodes=list)
    show = EditableItem(ratingKey="10", summary="", seasons=lambda: [season])

    result = _apply_candidate(
        show,
        {
            "root": {"fields": {"summary": "Show summary"}},
            "seasons": {1: {"fields": {"summary": "Season summary"}}},
        },
        plex_config(),
        identity(),
        PlexMetadataReporter(plex_config()),
    )

    assert result == {"writes": 2, "failures": 0}
    assert len(calls) == 1
    assert len(calls[0][0][0]) == 2
    assert calls[0][1]["prune_scope"] == ("server-1", "library-1", "10")


def test_unlock_keeps_value_and_clears_only_metafusion_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    config["plex_metadata"]["lock_writes"] = True
    item = EditableItem(summary="")
    _apply_candidate(
        item,
        {"root": {"fields": {"summary": "TMDb summary"}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    result = _restore_candidate(
        item,
        config,
        identity(),
        PlexMetadataReporter(config),
        unlock_only=True,
    )

    assert result == {"writes": 1, "failures": 0}
    assert item.summary == "TMDb summary"
    assert item.isLocked("summary") is False
    ownership = load_plex_metadata_ownership("server-1", "library-1", "10")
    assert ownership[("", "summary")]["metafusion_locked"] == 0


def test_write_limit_stops_after_first_plex_object(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    season = EditableItem(ratingKey="season", index=1, summary="", episodes=list)
    show = EditableItem(ratingKey="10", summary="", seasons=lambda: [season])
    config = plex_config()
    config["plex_metadata"]["max_writes_per_run"] = 1
    reporter = PlexMetadataReporter(config)

    result = _apply_candidate(
        show,
        {
            "root": {"fields": {"summary": "Show summary"}},
            "seasons": {1: {"fields": {"summary": "Season summary"}}},
        },
        config,
        identity(),
        reporter,
    )

    assert result == {"writes": 1, "failures": 0, "deferred": 1}
    assert show.summary == "Show summary"
    assert season.summary == ""
    assert reporter.counts["write_limit"] == 1


def test_dry_run_report_contains_actions_but_not_metadata_values(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config(dry_run=True)
    reporter = PlexMetadataReporter(config)
    item = EditableItem(summary="", genres=[])
    candidate = {
        "root": {
            "fields": {"summary": "Sensitive plot text"},
            "tags": {"genre": ["Rare private tag"]},
        }
    }

    _apply_candidate(item, candidate, config, identity(), reporter)
    report = reporter.write(base_dir=tmp_path)
    contents = report.read_text(encoding="utf-8")

    assert "would_fill" in contents
    assert "Sensitive plot text" not in contents
    assert "Rare private tag" not in contents
    assert item.summary == ""
    assert not (tmp_path / "meta_db.sqlite3").exists()


def test_successful_plex_api_update_detail_is_debug_to_avoid_duplicate_info(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    monkeypatch.setattr(
        plex_metadata_module, "_reporter", PlexMetadataReporter(config)
    )
    item = EditableItem(title="Example", summary="")

    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(
            apply_plex_metadata(
                item,
                {"root": {"fields": {"summary": "TMDb summary"}}},
                config,
                identity(),
            )
        )

    assert result == {"writes": 1, "failures": 0}
    assert "[Metadata] Plex | Example | Applied 1 API batch(es)" in caplog.text


def test_plex_report_logs_summary_and_safety_decisions(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    reporter = PlexMetadataReporter(config)
    item = EditableItem(
        summary="Manual summary",
        studio="",
        locks={"studio": True},
    )
    _apply_candidate(
        item,
        {
            "root": {
                "fields": {
                    "summary": "TMDb summary",
                    "studio": "TMDb Studio",
                }
            }
        },
        config,
        identity(),
        reporter,
    )

    with caplog.at_level(logging.INFO):
        report = reporter.write(base_dir=tmp_path)

    assert report.exists()
    assert "[Metadata] Plex report" not in caplog.text
    assert "Conflicts preserved: 0" in caplog.text
    assert "Locked fields: 1" in caplog.text


def test_plex_metadata_audit_records_locked_policy_and_unchanged_fields(tmp_path):
    config = plex_config(dry_run=True)
    config["_execution"] = {"metadata_audit": True}
    config["_metadata_audit_records"] = []
    config["plex_metadata"]["fields"] = ["summary", "studio"]
    reporter = PlexMetadataReporter(config)
    item = EditableItem(
        summary="Same summary",
        studio="Manual studio",
        tagline="Manual tagline",
        locks={"studio": True},
    )

    _apply_candidate(
        item,
        {
            "root": {
                "fields": {
                    "summary": "Same summary",
                    "studio": "TMDb Studio",
                    "tagline": "TMDb tagline",
                }
            }
        },
        config,
        identity(),
        reporter,
    )

    actions = {
        (record["field"], record["state"])
        for record in config["_metadata_audit_records"]
    }
    assert ("summary", "unchanged") in actions
    assert ("studio", "locked_skipped") in actions
    assert ("tagline", "policy_excluded") in actions
    assert reporter.write(base_dir=tmp_path) is None


def test_overwrite_policy_requires_explicit_acknowledgement():
    config = dict(DEFAULT_CONFIG)
    config["settings"] = {**DEFAULT_CONFIG["settings"], "mode": "plex"}
    config["plex"] = {
        "url": "http://plex:32400",
        "token": "token",
        "path_mappings": [],
    }
    config["tmdb"] = {**DEFAULT_CONFIG["tmdb"], "api_key": "key"}
    config["plex_metadata"] = {
        **DEFAULT_CONFIG["plex_metadata"],
        "enabled": True,
        "policy": "overwrite",
    }

    assert any("allow_overwrite" in error for error in validate_config(config))


def test_version_one_state_database_upgrades_in_place(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA user_version = 1")
    store = MediaStateStore(path=database)
    store.close()

    with closing(sqlite3.connect(database)) as connection, connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0]
            == state_db.SCHEMA_VERSION
        )
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "plex_metadata_ownership" in tables
    assert "asset_ownership" in tables


def test_reporter_empty_report_retention_and_run_lifecycle(tmp_path, monkeypatch):
    config = plex_config()
    config["output"] = {"report_retention": 1}
    reports = tmp_path / "reports"
    reports.mkdir()
    stale = reports / "plex-metadata-old.txt"
    stale.write_text("old", encoding="utf-8")

    reporter = begin_plex_metadata_run(config)
    assert get_plex_metadata_reporter(config) is reporter
    monkeypatch.setattr(plex_metadata_module, "BASE_CONFIG_DIR", tmp_path)
    report = finish_plex_metadata_run(config)
    contents = report.read_text(encoding="utf-8")

    assert "no eligible metadata fields" in contents
    assert "No items required metadata changes" in contents
    assert not stale.exists()
    replacement = get_plex_metadata_reporter(config)
    assert replacement is not reporter
    plex_metadata_module._reporter = None


def test_reporter_limits_detail_records_and_disabled_modes(tmp_path):
    config = plex_config()
    reporter = PlexMetadataReporter(config)
    reporter.max_details = 0
    reporter.record("Movies", "Example", "", "summary", "filled")
    assert reporter.counts["details_omitted"] == 1
    assert reporter.entries == []

    disabled = plex_config()
    disabled["settings"]["mode"] = "kometa"
    assert PlexMetadataReporter(disabled).write(base_dir=tmp_path) is None


@pytest.mark.parametrize(
    "item,names,expected",
    [
        (SimpleNamespace(index="2"), ("index",), 2),
        (SimpleNamespace(index="bad", seasonNumber="3"), ("index", "seasonNumber"), 3),
        (SimpleNamespace(index=None), ("index",), None),
    ],
)
def test_media_index_uses_valid_fallbacks(item, names, expected):
    assert _media_index(item, *names) == expected


def test_tag_restore_and_manual_tag_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    item = EditableItem(genres=[SimpleNamespace(tag="Manual")])
    _apply_candidate(
        item,
        {"root": {"tags": {"genre": ["Action"]}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )
    assert [value.tag for value in item.genres] == ["Manual", "Action"]

    result = _restore_candidate(
        item, config, identity(), PlexMetadataReporter(config)
    )
    assert result == {"writes": 1, "failures": 0}
    assert [value.tag for value in item.genres] == ["Manual"]

    _apply_candidate(
        item,
        {"root": {"tags": {"genre": ["Action"]}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )
    item.genres.append(SimpleNamespace(tag="User edit"))
    reporter = PlexMetadataReporter(config)
    result = _restore_candidate(item, config, identity(), reporter)
    assert result == {"writes": 0, "failures": 0}
    assert reporter.counts["conflict"] == 1


def test_restore_dry_run_and_write_limit_are_non_mutating(tmp_path, monkeypatch):
    monkeypatch.setattr(state_db, "STATE_DATABASE", tmp_path / "meta_db.sqlite3")
    config = plex_config()
    item = EditableItem(summary="")
    _apply_candidate(
        item,
        {"root": {"fields": {"summary": "TMDb"}}},
        config,
        identity(),
        PlexMetadataReporter(config),
    )

    dry_config = plex_config(dry_run=True)
    dry_reporter = PlexMetadataReporter(dry_config)
    assert _restore_candidate(item, dry_config, identity(), dry_reporter) == {
        "writes": 0,
        "failures": 0,
    }
    assert item.summary == "TMDb"
    assert dry_reporter.counts["would_restore"] == 1

    config["plex_metadata"]["max_writes_per_run"] = 1
    limited = PlexMetadataReporter(config)
    assert limited.claim_write("Movies")
    assert _restore_candidate(item, config, identity(), limited) == {
        "writes": 0,
        "failures": 0,
    }
    assert limited.counts["write_limit"] == 1


def test_apply_and_restore_retry_paths(monkeypatch):
    config = plex_config()
    config["runtime"] = {"plex_retries": 2, "plex_retry_delay": 0.001}
    attempts = []

    def apply_attempt(*_args, **_kwargs):
        attempts.append("apply")
        return (
            {"writes": 0, "failures": 1}
            if len(attempts) == 1
            else {"writes": 1, "failures": 0, "deferred": 2}
        )

    monkeypatch.setattr(plex_metadata_module, "_apply_candidate", apply_attempt)
    result = asyncio.run(
        apply_plex_metadata(
            EditableItem(title="Example"),
            {"root": {"fields": {"summary": "value"}}},
            config,
            identity(),
        )
    )
    assert result == {"writes": 1, "failures": 0, "deferred": 2}
    assert attempts == ["apply", "apply"]

    restore_attempts = []

    def restore_attempt(*_args, **_kwargs):
        restore_attempts.append("restore")
        return {"writes": 0, "failures": 1}

    monkeypatch.setattr(plex_metadata_module, "_restore_candidate", restore_attempt)
    result = asyncio.run(
        restore_plex_metadata(
            EditableItem(title="Example"), config, identity(), unlock_only=True
        )
    )
    assert result == {"writes": 0, "failures": 1}
    assert restore_attempts == ["restore", "restore"]


def test_apply_plex_metadata_noop_when_disabled():
    config = plex_config()
    config["plex_metadata"]["enabled"] = False
    assert asyncio.run(
        apply_plex_metadata(EditableItem(), {"root": {}}, config, identity())
    ) == {"writes": 0, "failures": 0}
