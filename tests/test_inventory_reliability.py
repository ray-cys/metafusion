import asyncio
import json
import logging
import threading
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

import metafusion
from helper import episode_order_audit, inventory_guard
from helper.diagnostics import write_library_asset_audit_report
from helper.runtime import JobRunLock, RuntimeStatus

LOG = logging.getLogger("guard")


def test_inventory_guard_baseline_restart_review_and_exact_ack(tmp_path, monkeypatch):
    original = inventory_guard.inspect_inventory
    path = tmp_path / "meta_db"
    monkeypatch.setattr(
        inventory_guard, "inspect_inventory", lambda *a, **kw: original(*a, **kw, path=path)
    )
    scope = {"server_id": "machine", "library_uuid": "a", "library_name": "Movies"}
    records = [{"rating_key": str(i), "title": f"Film {i}"} for i in range(100)]

    def evaluate(items, *, full=True, write=True, token=None):
        return inventory_guard.evaluate_inventories(
            {"_execution": {"accept_inventory_drop": token}},
            [scope],
            {"Movies": items},
            {("machine", "a"): full},
            LOG,
            writable=write,
        )

    assert evaluate(records, write=False)[0]["Movies"]["status"] == "initializing"
    assert not path.exists()
    evaluate(records)
    assert evaluate(records)[0]["Movies"]["status"] == "trusted"
    assert evaluate(records[24:], write=False)[1] == set()
    assert evaluate(records[:1] * 2)[1] == {"Movies"}
    reports, held = evaluate(records[25:])
    preview = reports["Movies"]
    assert held == {"Movies"} and len(preview["missing"]) == 25
    token = preview["token"]
    assert evaluate(records[25:], full=False)[0]["Movies"]["token"] == token
    assert evaluate(records)[1] == set()
    with pytest.raises(ValueError, match="acknowledgement"):
        evaluate(records[:-25], token=token)
    assert evaluate(records[25:], token=token)[0]["Movies"]["status"] == "accepted"
    assert evaluate(records[25:])[1] == set()
    audit = write_library_asset_audit_report(
        [{"library": "Movies", "inventory_guard": preview}], [], mode="kometa", base_dir=tmp_path
    )
    assert token in audit.read_text() and "Film 24 (Plex 24)" in audit.read_text()


def test_stop_during_real_output_lock_wait(tmp_path, monkeypatch):
    status = RuntimeStatus(tmp_path / "status.json", state_database=tmp_path / "meta_db")
    config = {"settings": {"path": str(tmp_path)}}
    event = threading.Event()
    monkeypatch.setattr(metafusion, "BASE_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(metafusion, "shutdown_requested", event)
    consumer = JobRunLock(tmp_path / ".metafusion/output.lock").acquire()
    waiting = threading.Event()
    original = status.waiting_for_kometa

    def notify():
        original()
        waiting.set()

    monkeypatch.setattr(status, "waiting_for_kometa", notify)
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(metafusion.run_metafusion_job(config, LOG, status))
    )
    try:
        thread.start()
        assert waiting.wait(5)
        assert json.loads(status.path.read_text())["state"] == "waiting_for_kometa_output_lock"
    finally:
        event.set()
        thread.join(3)
        consumer.release()
    assert outcomes == [False]
    assert json.loads(status.path.read_text())["last_run_status"] == "interrupted"
    JobRunLock(tmp_path / ".metafusion-run.lock").acquire().release()


def test_ack_cli():
    args = metafusion.parse_cli_args(["--accept-inventory-drop", "abc"])
    config = {"settings": {"schedule": True}, "cleanup": {}}
    metafusion.override_config_with_cli(config, args)
    assert config["metafusion_run"] and config["_execution"]["full_scan"]
    assert not config["settings"]["schedule"]
    with pytest.raises(SystemExit):
        metafusion.parse_cli_args(["--dry_run", "--accept-inventory-drop", "abc"])


def test_order_audit_samples_split_ids_and_reports(tmp_path, monkeypatch):
    section = Obj(settings=lambda: [Obj(id="showOrdering", value="tmdbAiring")])

    def episodes(*, maxresults):
        assert maxresults == 3
        return [
            Obj(index=i, parentIndex=2, title="Sample", originallyAvailableAt="2026-03-10")
            for i in (1, 2, 3)
        ]

    shows = [Obj(ratingKey=1, showOrdering=None, episodes=episodes), Obj(ratingKey=2)]
    records = [
        {"rating_key": str(s.ratingKey), "title": "Split", "tvdb_id": "345246"} for s in shows
    ]
    fetch = AsyncMock(
        return_value={
            "episodes": [
                {"episode_number": 1, "name": "Sample"},
                {"episode_number": 2, "name": "Other"},
            ]
        }
    )
    monkeypatch.setattr(episode_order_audit, "tmdb_api_request", fetch)
    result = asyncio.run(episode_order_audit.audit_episode_order(section, shows, records, {}, None))
    first, second = result["shows"]
    assert first["effective"] == "tmdbAiring" and first["split_mapping"] == "tvdb:345246"
    assert second["effective"] == "unknown" and second["unavailable"] == "AttributeError"
    assert [s["status"] for s in first["samples"]] == ["match", "review", "unknown"]
    fetch.assert_awaited_once_with({}, "tv/109958/season/1", session=None, cache=False)
    report = write_library_asset_audit_report(
        [{"library": "TV", "episode_order": result}], [], mode="plex", base_dir=tmp_path
    )
    assert "review | Plex [2, 2]" in report.read_text()
    assert (
        asyncio.run(episode_order_audit.audit_episode_order(Obj(), [], [], {}, None))[
            "library_default"
        ]
        == "unknown"
    )
