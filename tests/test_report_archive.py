import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import metafusion
from helper import diagnostics, plex_metadata, report_archive, reporting, state_db
from helper.findings import publish_findings

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


def test_daily_sweep_groups_and_protects_evidence(tmp_path):
    db = tmp_path / "state"
    folder = tmp_path / "reports"
    folder.mkdir()
    for i in range(3):
        for kind in ("metadata-audit", "kometa-application-audit", "metafusion-dashboard"):
            for ext in ("html", "json") if kind == "metafusion-dashboard" else ("txt", "json"):
                (folder / f"{kind}-20261124-12000{i}.{ext}").write_text("{}")
    keep = [
        "findings-latest.json",
        "findings.json",
        "my-report-20260101-120000.txt",
        "metadata-audit-notes.txt",
        "metafusion-dashboard-latest.html",
    ]
    for name in keep:
        (folder / name).write_text("mine")
    link = folder / "metadata-audit-20251124-120000.txt"
    link.symlink_to(folder / keep[0])
    (folder / "formula1").mkdir()
    target = folder / "formula1/metadata-audit-20260101-120000.txt"
    target.touch()
    assert report_archive.sweep_reports(tmp_path, 1, path=db, now=NOW) == 8
    assert len(list(folder.glob("kometa-application*"))) == 6
    audit = folder / "kometa-application-audit-20261124-120000.json"
    audit.write_text(
        json.dumps(
            {
                "report_type": "kometa_application_audit",
                "generated_at": NOW.isoformat(),
                "data": {"metadata_items": []},
            }
        )
    )
    publish_findings({}, True, path=db, base_dir=tmp_path, now=NOW)
    assert report_archive.sweep_reports(tmp_path, 1, path=db, now=NOW) == 0
    assert report_archive.sweep_reports(tmp_path, 1, path=db, now=NOW + timedelta(days=1)) == 2
    assert not audit.exists() and not audit.with_suffix(".txt").exists()
    assert link.is_symlink() and target.exists() and all((folder / n).exists() for n in keep)
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "reports").symlink_to(folder)
    with pytest.raises(OSError, match="symbolic"):
        report_archive.sweep_reports(outside, 1, path=db, now=NOW + timedelta(days=2))


def test_recorded_exports_no_live_writes(tmp_path, monkeypatch, capsys):
    db = tmp_path / "state"
    monkeypatch.setattr(state_db, "STATE_DATABASE", db)
    monkeypatch.setattr(metafusion, "STATE_DATABASE", db)
    monkeypatch.setattr(metafusion, "BASE_CONFIG_DIR", tmp_path)

    def export(kind):
        args = metafusion.parse_cli_args(["--export-report", kind])
        assert metafusion._handle_sqlite_only_command(args) == 0
        return capsys.readouterr().out

    assert "No recorded evidence" in export("artwork-gaps")
    diagnostics.write_artwork_gap_report([], base_dir=tmp_path, record_only=True)
    diagnostics.write_adoption_audit_report(
        [{"status": "missing_after_write"}], base_dir=tmp_path, record_only=True
    )
    cache = {
        "x": {
            "title": "Movie",
            "destination_history": [
                {"asset_type": "poster", "new_destination": "new", "previous_destination": "old"}
            ],
        }
    }
    diagnostics.write_destination_history_report(cache, base_dir=tmp_path, record_only=True)
    config = {"settings": {"mode": "plex"}, "plex_metadata": {"enabled": True}}
    reporter = plex_metadata.begin_plex_metadata_run(config)
    assert (
        reporter.enabled
        and plex_metadata.finish_plex_metadata_run(config, record_only=True) is None
    )
    assert not (tmp_path / "reports").exists()
    monkeypatch.setattr(
        diagnostics,
        "_reconcile_managed_destination",
        Mock(side_effect=AssertionError("export touched artwork")),
    )
    for kind in report_archive.RECORDED_REPORTS:
        message = export(kind)
        file = Path(message.strip().removeprefix("Report saved to "))
        assert file.is_file()
        assert json.loads(file.with_suffix(".json").read_text())["data"]["observed_at"]
    assert "No recorded evidence" in export("unresolved-work")
    reporting.configure_reporting(report_format="json")
    try:
        assert ".json" in export("artwork-gaps")
    finally:
        reporting.configure_reporting()


def test_failed_sweep_does_not_mask_findings(tmp_path, monkeypatch, caplog):
    from test_phase26_orchestration_matrix import _patch_job_services

    _patch_job_services(monkeypatch, tmp_path)
    monkeypatch.setattr(
        metafusion,
        "publish_findings",
        lambda *a, **k: dict.fromkeys(["new", "persistent", "resolved", "not_rechecked"], 0),
    )
    sweep = Mock(side_effect=PermissionError("readonly"))
    monkeypatch.setattr(metafusion, "sweep_reports", sweep)
    assert metafusion.run_metafusion_job({}, logging.getLogger("test"))
    assert "Report cleanup deferred" in caplog.text
    sweep.reset_mock()
    assert metafusion.run_metafusion_job({"settings": {"dry_run": True}}, logging.getLogger("test"))
    sweep.assert_not_called()


def test_historical_verification_expires_without_reopening_old_findings(tmp_path):
    db = tmp_path / "state"
    folder = tmp_path / "reports"
    folder.mkdir()
    name = "plex-artwork-verification-20200101-120000.json"
    (folder / name).write_text(
        json.dumps(
            {
                "report_type": "plex_artwork_verification",
                "generated_at": (NOW - timedelta(days=60)).isoformat(),
                "data": {"entries": [{"status": "local_missing"}]},
            }
        )
    )
    publish_findings({}, True, path=db, base_dir=tmp_path, now=NOW)
    assert state_db.load_application_record("findings_audit_receipts", path=db) == {name: True}
    assert json.loads((folder / "findings-latest.json").read_text())["open"] == []
