import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from test_phase26_orchestration_matrix import (
    _config,
    _item,
    _patch_runtime,
    _record,
    _Section,
)

import metafusion
from helper.incremental import child_inventory_fingerprint, config_fingerprint
from helper.scan_planner import LibraryObservation, quiet_opportunity, scan_deadline, schedule_after
from helper.state_db import save_application_record


def test_slot_choice_and_disappearance(tmp_path, monkeypatch):
    monkeypatch.setattr("helper.state_db.STATE_DATABASE", tmp_path / "meta_db")
    now = datetime(2026, 11, 28, 23, 35, tzinfo=timezone.utc)
    due = now + timedelta(hours=2)
    slots = ["03:35", "23:35"]
    samples = {"03:35": [20, 19], "23:35": [10, 11]}
    assert quiet_opportunity(samples, "23:35", slots, now, due, 72)
    assert not quiet_opportunity(samples, "23:35", slots, now, now, 72)
    assert not quiet_opportunity(samples, "23:35", slots, now, due + timedelta(hours=3), 72)
    assert not quiet_opportunity(samples, None, slots, now, due, 72)
    assert schedule_after(now, slots, inclusive=True) == now
    assert schedule_after(now, slots) == now + timedelta(hours=4)
    assert scan_deadline({"last_full": now.isoformat()}, 72) == now + timedelta(hours=72)
    assert scan_deadline({"last_full": "2026-11-28T23:35:00"}, 72) == now + timedelta(hours=72)
    assert scan_deadline({}, 72) is None
    scope = {"server_id": "server", "library_uuid": "movies"}

    def observe(*keys):
        return LibraryObservation(scope, [_record(k) for k in keys])

    observe("1", "2").finish(success=True, full=True, slot=None, elapsed=10)
    first = observe("1")
    assert first.missing == {"2"} and not first.confirm
    first.finish(success=True, full=False, slot="23:35", elapsed=7)
    assert observe("1").confirm
    assert not observe("1", "2").confirm
    observe("1").finish(success=False, full=True, slot="03:35", elapsed=2)
    latest = observe("1")
    assert latest.confirm and latest.history["03:35"] == [None]
    latest.finish(success=True, full=True, slot=None, elapsed=10)
    assert not observe("1").confirm


def test_mapping_changes_only_select_the_affected_show():
    inherited = SimpleNamespace(type="show", childCount=2, guid="tmdb://100")
    explicit = SimpleNamespace(type="show", childCount=2, guid="tmdb://99", showOrdering="tvdb")
    old = {"plex": {"_library_order": "tvdb"}}
    new = {"plex": {"_library_order": "tmdb"}}
    assert config_fingerprint(old) == config_fingerprint(new)
    assert child_inventory_fingerprint(explicit, old) == child_inventory_fingerprint(explicit, new)
    assert child_inventory_fingerprint(inherited, old) != child_inventory_fingerprint(
        inherited, new
    )
    custom = {
        **new,
        "tmdb": {
            "split_series_mappings": {"tmdb:100": {"seasons": {"1": {"tmdb_id": 100}}}},
            "episode_overrides": {"tmdb:100": {"S01E01": "S01E02"}},
        },
    }
    assert config_fingerprint(new) == config_fingerprint(custom)
    assert child_inventory_fingerprint(explicit, custom) == child_inventory_fingerprint(
        explicit, new
    )
    assert child_inventory_fingerprint(inherited, custom) != child_inventory_fingerprint(
        inherited, new
    )


def test_staggered_scan_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr("helper.state_db.STATE_DATABASE", tmp_path / "db")
    movies = _Section(items=[_item()])
    shows = _Section("Shows", "show", [_item("20", "show")])
    libraries = [{"title": s.title, "type": s.type, "uuid": s.uuid} for s in (movies, shows)]
    _patch_runtime(
        monkeypatch,
        [movies, shows],
        ["Movies", "Shows"],
        libraries,
        inventory={"Movies": [_record()], "Shows": [_record("20", "tv")]},
    )

    def decisions(config, *, scopes, **kwargs):
        for scope in scopes:
            scope["last_full"] = (datetime.now(timezone.utc) - timedelta(hours=71)).isoformat()
        return {(s["server_id"], s["library_uuid"]): False for s in scopes}

    monkeypatch.setattr(metafusion, "library_full_scan_decisions", decisions)
    monkeypatch.setattr(metafusion, "prepare_tmdb_change_plan", lambda *a, **kw: {})
    calls = []

    async def process(**kw):
        calls.append((kw["library_section"].title, kw["full_scan"]))

    async def cleanup(**kw):
        assert kw["safe_library_types"] == {"movie"}
        return metafusion.CleanupResult()

    monkeypatch.setattr(metafusion, "process_library", process)
    monkeypatch.setattr(metafusion, "cleanup_title_orphans", cleanup)
    save_application_record('adaptive_scan:["server", "uuid-Movies"]', {"seen": ["10", "11"]})
    cfg = _config(tmp_path)

    def run():
        asyncio.run(metafusion.metafusion_main(cfg, logging.getLogger("adaptive")))

    run()
    assert calls == [("Movies", False), ("Shows", False)]
    assert "queued" in cfg["_scan_summary"][0][1]
    calls.clear()
    run()
    assert calls == [("Movies", True), ("Shows", False)]
    assert cfg["_scan_summary"][0][-1] == "Full inventory"
    assert cfg["_partial_inventory_run"]
    monkeypatch.setattr(metafusion, "quiet_opportunity", lambda *args: args[1] and not calls)
    cfg["_scheduled_slot"] = "03:35"
    calls.clear()
    run()
    assert "quieter" in cfg["_scan_summary"][0][1]


def test_failed_sibling_blocks_shared_cleanup_and_records_slot_failure(monkeypatch, tmp_path):
    import pytest

    monkeypatch.setattr("helper.state_db.STATE_DATABASE", tmp_path / "db")
    a, b = _Section(items=[_item()]), _Section("Movies B", items=[_item("20")])
    libraries = [{"title": s.title, "type": s.type, "uuid": s.uuid} for s in (a, b)]
    _patch_runtime(
        monkeypatch,
        [a, b],
        [a.title, b.title],
        libraries,
        inventory={a.title: [_record()], b.title: [_record("21")]},
    )
    monkeypatch.setattr(metafusion, "prepare_tmdb_change_plan", lambda *a, **kw: {})

    async def process(**kw):
        pass

    async def scopes(plex, libs, scanned, complete, config):
        assert len(scanned) == 1 and not complete
        return {"movie": object()}, {"movie": "partially verified"}

    async def cleanup(**kw):
        assert not kw["safe_library_types"] and not kw["partial_scopes"]
        return metafusion.CleanupResult()

    monkeypatch.setattr(metafusion, "cleanup_scopes", scopes)
    monkeypatch.setattr(metafusion, "process_library", process)
    monkeypatch.setattr(metafusion, "cleanup_title_orphans", cleanup)
    cfg = _config(tmp_path, full_scan=True)
    cfg["_scheduled_slot"] = "03:35"
    with pytest.raises(RuntimeError, match="changed rating keys"):
        asyncio.run(metafusion.metafusion_main(cfg, logging.getLogger("adaptive-sibling")))
    assert "Deferred" in cfg["_cleanup_scopes"]["movie"]
    assert LibraryObservation({"server_id": "server", "library_uuid": b.uuid}, []).history == {
        "03:35": [None]
    }


def test_forced_provider_baseline_appears_in_final_summary(monkeypatch, tmp_path, caplog):
    from helper.logging import log_final_summary

    monkeypatch.setattr("helper.state_db.STATE_DATABASE", tmp_path / "db")
    section = _Section(items=[_item()])
    _patch_runtime(
        monkeypatch,
        [section],
        ["Movies"],
        [{"title": "Movies", "type": "movie"}],
        inventory={"Movies": [_record()]},
    )
    monkeypatch.setattr(metafusion, "log_final_summary", log_final_summary)
    monkeypatch.setattr(
        metafusion,
        "prepare_tmdb_change_plan",
        lambda *args, **kw: {"force_full_scan": True, "status": "checkpoint_stale"},
    )

    async def process(**kw):
        assert kw["full_scan"]

    monkeypatch.setattr(metafusion, "process_library", process)
    cfg = _config(tmp_path)
    cfg["cleanup"]["run_cleanup"] = False
    with caplog.at_level(logging.INFO):
        asyncio.run(metafusion.metafusion_main(cfg, logging.getLogger("summary")))
    assert "[Summary] Scan | Movies | Reason: TMDb baseline required" in caplog.text
    assert "Next eligible slot: manual" in caplog.text


def test_manual_modes_report_baseline(tmp_path):
    from helper.incremental import library_full_scan_decisions

    scope = {"server_id": "s", "library_uuid": "u"}
    for cfg, targeted, full, reason in [
        ({}, True, False, "targeted"),
        ({"incremental": {"enabled": False}}, False, True, "incremental disabled"),
    ]:
        assert (
            library_full_scan_decisions(
                cfg, targeted=targeted, scopes=[scope], path=tmp_path / "db"
            )[("s", "u")]
            is full
        )
        assert scope["scan_reason"] == reason


def test_large_drop_includes_titles_first_seen_between_full_scans(tmp_path):
    from helper.inventory_guard import inspect_inventory

    scope = {"server_id": "s", "library_uuid": "u"}
    path = tmp_path / "db"
    original = [_record("1")]
    grown = [_record(str(i)) for i in range(30)]
    inspect_inventory(scope, original, full=True, writable=True, path=path)
    assert inspect_inventory(scope, grown, writable=True, path=path)["status"] == "trusted"
    held = inspect_inventory(scope, original, writable=True, path=path)
    assert held["status"] == "held" and len(held["missing"]) == 29
    assert inspect_inventory(scope, grown, writable=True, path=path)["status"] == "trusted"
