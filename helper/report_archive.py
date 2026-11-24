"""On-demand report snapshots and daily retention for core reports."""

from datetime import datetime, timezone
from pathlib import Path

from helper.report_retention import RECORDED_REPORTS as RECORDED_REPORTS
from helper.report_retention import trim_reports
from helper.reporting import write_diagnostic_report
from helper.state_db import load_application_record, save_application_record


def record_report(kind, text, data, *, path=None):
    save_application_record(
        "report_snapshot:" + kind,
        {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "text": text,
            "data": data,
        },
        path=path,
    )


def export_recorded_report(kind, base_dir, *, path=None):
    snapshot = load_application_record("report_snapshot:" + kind, path=path)
    if snapshot is None:
        return None
    now = datetime.now(timezone.utc)
    destination = Path(base_dir) / "reports" / f"{kind}-{now:%Y%m%d-%H%M%S%f}.txt"
    return write_diagnostic_report(
        destination,
        f"Evidence recorded: {snapshot['observed_at']}\n{snapshot['text']}",
        report_type=kind.replace("-", "_"),
        data={**snapshot["data"], "observed_at": snapshot["observed_at"]},
        generated_at=now,
    )


def sweep_reports(base_dir, keep, *, path=None, now=None):
    today = (now or datetime.now(timezone.utc)).date().isoformat()
    if load_application_record("report_sweep", path=path) == {"day": today}:
        return 0
    folder = Path(base_dir) / "reports"
    receipts = load_application_record("findings_audit_receipts", path=path) or {}
    removed = trim_reports(folder, keep, receipts)
    if receipts:
        save_application_record(
            "findings_audit_receipts",
            {k: True for k in receipts if (folder / k).is_file()},
            path=path,
        )
    save_application_record("report_sweep", {"day": today}, path=path)
    return removed
