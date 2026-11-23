import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from helper import findings
from helper.dashboard import collect_dashboard_snapshot, render_dashboard
from helper.state_db import record_item_failure, save_provider_health

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
ITEM = {
    "library": "Shows",
    "library_uuid": "tv",
    "server_id": "plex",
    "rating_key": "12",
    "title": "9-1-1",
    "tmdb_id": "75219",
    "season_number": 10,
}
ASSET = {**ITEM, "asset_type": "season 10 poster"}
GAP = {
    **ASSET,
    "category": "artwork_missing",
    "provider_attempts": [
        {"provider": "TMDb", "candidates": 0, "status": "no_candidates", "url": "private-token"}
    ],
}


def publish(tmp, day, cfg, ok=True):
    findings.publish_findings(
        cfg, ok, path=tmp / "state", base_dir=tmp, now=START + timedelta(days=day)
    )
    return json.loads((tmp / "reports/findings-latest.json").read_text())


def test_finding_lifecycle_retention_and_atomicity(tmp_path, monkeypatch):
    cfg = {"_artwork_gaps": [GAP, GAP]}
    p = publish(tmp_path, 0, cfg)
    (item,) = p["open"]
    assert item["observations"] == 1 and item["reason_code"] == "no_candidate_returned"
    assert "private-token" not in json.dumps(p)
    assert publish(tmp_path, 1, cfg)["run"]["changes"]["persistent"] == 1
    held = publish(tmp_path, 10, {"_execution": {"targeted": True}})
    assert held["open"][0]["last_checked_at"] == (START + timedelta(days=1)).isoformat()
    assert held["run"]["changes"]["not_rechecked"] == 1
    assert item["title"] in render_dashboard(collect_dashboard_snapshot(path=tmp_path / "state"))
    cfg = {
        "_artwork_evaluations": [{**ASSET, "destination_state": "present", "provider": "Plex"}],
        "_adoption_audit_records": [{**ASSET, "status": "filesystem_verified", "provider": "Plex"}],
    }
    closed = publish(tmp_path, 11, cfg)
    assert closed["open"] == []
    (resolved,) = closed["recently_resolved"]
    assert (
        resolved["resolution"] == "filesystem_verified"
        and resolved["recovery_evidence"]["provider"] == "Plex"
    )
    cfg = {"_artwork_gaps": [GAP]}
    assert publish(tmp_path, 12, cfg)["open"][0]["finding_id"] == item["finding_id"]
    folder = tmp_path / "reports"
    (folder / "other.json").write_text("keep")
    saved = (folder / "findings-latest.json").read_bytes()
    with monkeypatch.context() as m:

        def fail(*args):
            raise OSError("disk unavailable")

        m.setattr(findings, "atomic_write_json", fail)
        with pytest.raises(OSError):
            publish(tmp_path, 16, {})
    assert (folder / "findings-latest.json").read_bytes() == saved
    pruned = publish(tmp_path, 43, {})
    assert len(pruned["open"]) == 1 and pruned["recently_resolved"] == []
    assert len(pruned["run_history"]) == 2
    assert sorted(p.name for p in folder.glob("*.json")) == ["findings-latest.json", "other.json"]


def test_scoped_recovery_and_metadata(tmp_path):
    p = publish(
        tmp_path,
        0,
        {
            "_artwork_gaps": [
                {**ASSET, "category": "ownership_collision"},
                {
                    **GAP,
                    "season_number": 5,
                    "asset_type": "season 5 poster",
                    "provider_attempts": [{"candidates": 1}],
                },
            ],
            "_metadata_provenance_records": [
                {**ITEM, "action": "source_missing", "field_path": "summary"}
            ],
            "_job_library_results": {"Shows": {"status": "failed"}},
            "_findings_guard": {"Shows": {"status": "held", "missing": [{}]}},
        },
        False,
    )
    assert len(p["open"]) == 6
    p = publish(
        tmp_path,
        1,
        {
            "_artwork_evaluations": [{**ASSET, "destination_state": "present"}],
            "_metadata_provenance_records": [{**ITEM, "action": "filled", "field_path": "summary"}],
            "_job_library_results": {"Shows": {"status": "success"}},
            "_findings_guard": {"Shows": {"status": "trusted"}},
        },
    )
    assert len(p["open"]) == 2
    assert len(publish(tmp_path, 2, {"_findings_inventory": {("other", "tv"): set()}})["open"]) == 2
    assert len(publish(tmp_path, 3, {"_findings_inventory": {("plex", "tv"): set()}})["open"]) == 0


def test_provider_retry_audit_sources(tmp_path):
    start = START.isoformat()
    cfg = {"_findings_started": start}
    path = tmp_path / "state"
    record_item_failure(
        "plex",
        "tv",
        "12",
        PermissionError("secret text"),
        library_name="Shows",
        now=START,
        path=path,
    )
    save_provider_health("key", "TMDb", consecutive_failures=2, now=START, path=path)
    folder = tmp_path / "reports"
    folder.mkdir()

    def audit(index, status, **updates):
        payload = {
            "report_type": "kometa_application_audit",
            "generated_at": (START + timedelta(hours=index)).isoformat(),
            "data": {"metadata_items": [{"status": status, **ITEM}]},
            **updates,
        }
        (folder / f"kometa-application-audit-{index}.json").write_text(json.dumps(payload))

    audit(1, "partial")
    audit(2, "selected", report_type="other")
    audit(3, "selected", data=None)
    p = publish(tmp_path, 1, cfg)
    assert set(p["open_by_reason"]) == {"partial", "provider_unavailable", "retry_pending"}
    assert "secret text" not in json.dumps(p)
    assert p["run"]["unreadable_audits"]
    audit(4, "applied")
    audit(5, "applied")
    save_provider_health("key", "TMDb", successful=True, now=START + timedelta(days=2), path=path)
    cfg = {
        "_findings_started": (START + timedelta(days=1)).isoformat(),
        "_native_observations": [
            (ITEM | {"season_number": None}, "item_processing", "retry_succeeded", True, {})
        ],
    }
    assert publish(tmp_path, 2, cfg)["open"] == []
    assert publish(tmp_path, 3, {})["run"]["changes"]["resolved"] == 0


def test_job_findings_export_failure_is_visible(tmp_path, monkeypatch, caplog):
    from test_phase26_orchestration_matrix import _patch_job_services

    import metafusion

    _patch_job_services(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise OSError("read-only volume")

    monkeypatch.setattr(metafusion, "publish_findings", fail)
    assert metafusion.run_metafusion_job(
        {"settings": {"mode": "kometa"}}, logging.getLogger("findings")
    )
    assert "Export unavailable" in caplog.text


def test_empty_field_and_probe_are_not_recovery(tmp_path):
    p = publish(
        tmp_path,
        0,
        {
            "_artwork_gaps": [
                {
                    **ASSET,
                    "asset_type": "season 10 season",
                    "category": "post_write_checksum_mismatch",
                },
                {**ITEM, "asset_type": "metadata", "category": "policy_preserved_missing"},
            ],
            "_metadata_provenance_records": [
                {
                    **ITEM,
                    "target": "plex_api",
                    "field_path": "summary",
                    "action": "unchanged",
                    "value_fingerprint": None,
                }
            ],
        },
    )
    assert set(p["open_by_reason"]) == {
        "source_missing",
        "post_write_checksum_mismatch",
        "policy_preserved_missing",
    }
    p = publish(
        tmp_path, 1, {"_native_observations": [(ITEM, "metadata", "identity_verified", True, {})]}
    )
    assert len(p["open"]) == 3


def test_confirmed_season_removal_requires_matching_scope_and_season(tmp_path):
    from helper.state_db import record_cleanup_history

    cfg = {"_artwork_gaps": [GAP, {**GAP, "asset_type": "season 11 poster", "season_number": 11}]}
    publish(tmp_path, 0, cfg)
    t = START + timedelta(days=1)
    record_cleanup_history(
        "automated",
        "remove",
        "completed",
        ITEM,
        output_type="season_state",
        season_number=10,
        now=t,
        path=tmp_path / "state",
    )
    p = publish(
        tmp_path,
        1,
        {"_findings_started": t.isoformat(), "_findings_inventory": {("plex", "tv"): {"12"}}},
    )
    assert [r["season_number"] for r in p["open"]] == [11]
    assert p["recently_resolved"][0]["resolution"] == "season_removed"
