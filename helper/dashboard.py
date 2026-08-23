"""Self-contained offline HTML diagnostics dashboard."""

from __future__ import annotations

import html
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from helper.build_info import build_info
from helper.config import BASE_CONFIG_DIR
from helper.database_maintenance import DATABASES, inspect_database
from helper.io import atomic_write_json, atomic_write_text
from helper.state_db import (
    STATE_DATABASE,
    load_cleanup_candidates,
    load_cleanup_history,
    load_identity_reviews,
    load_item_retries,
    load_metadata_provenance,
    load_unresolved_work,
    recent_job_runs,
)

DASHBOARD_SCHEMA_VERSION = 1
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|x-plex-token)(\s*[:=]\s*)([^\s&;,]+)"
)
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|token|x[_-]?plex[_-]?token)(?:$|[_-])"
)


def _safe_text(value, limit=500):
    text = _SECRET_PATTERN.sub(r"\1\2***", str(value or ""))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sanitize_snapshot(value):
    if isinstance(value, dict):
        return {
            str(key): (
                "***"
                if _SECRET_KEY_PATTERN.search(str(key))
                else _sanitize_snapshot(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_snapshot(child) for child in value]
    if isinstance(value, str):
        return _safe_text(value, limit=2_000)
    return value


def _readonly_rows(path, table, *, order_by=None, limit=10_000):
    database = Path(path)
    if not database.exists():
        return []
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return []
        query = f"SELECT * FROM {table}"
        if order_by:
            query += f" ORDER BY {order_by}"
        query += " LIMIT ?"
        return [
            dict(row)
            for row in connection.execute(query, (max(1, int(limit)),)).fetchall()
        ]
    finally:
        connection.close()


def collect_dashboard_snapshot(*, path=STATE_DATABASE):
    """Collect bounded recorded evidence without contacting Plex or providers."""
    generated = datetime.now(timezone.utc).isoformat()
    jobs = recent_job_runs(limit=20, path=path)
    libraries = _readonly_rows(
        path, "plex_library_inventory", order_by="library_name", limit=1_000
    )
    scans = _readonly_rows(
        path, "library_scan_state", order_by="library_name", limit=1_000
    )
    provider_health = _readonly_rows(
        path, "provider_health", order_by="provider", limit=100
    )
    unresolved = load_unresolved_work(statuses=["open"], path=path, limit=250)
    retries = load_item_retries(statuses=["pending", "parked", "running"], path=path)
    reviews = load_identity_reviews(statuses=["open"], path=path)
    cleanup_pending = load_cleanup_candidates(statuses=["pending"], path=path)
    cleanup_history = load_cleanup_history(limit=100, path=path)
    provenance = load_metadata_provenance(limit=500, path=path)
    databases = {}
    for name, (database, expected_schema) in DATABASES.items():
        inspected = inspect_database(path if name == "state" else database, expected_schema)
        inspected.pop("path", None)
        databases[name] = inspected
    latest = jobs[-1] if jobs else {}
    return _sanitize_snapshot({
        "schema": DASHBOARD_SCHEMA_VERSION,
        "generated_at": generated,
        "build": build_info(),
        "notice": (
            "Recorded SQLite evidence only; Plex, TMDb, Fanart.tv, Kometa YAML, "
            "and artwork files were not contacted."
        ),
        "overview": {
            "latest_job_status": latest.get("status") or "not recorded",
            "latest_job_finished": latest.get("finished_at"),
            "active_libraries": sum(bool(row.get("active")) for row in libraries),
            "open_unresolved": len(unresolved),
            "pending_retries": len(retries),
            "pending_cleanup": len(cleanup_pending),
            "field_provenance": len(provenance),
        },
        "databases": databases,
        "libraries": libraries,
        "scan_state": scans,
        "jobs": jobs,
        "unresolved_work": unresolved,
        "retries": retries[:250],
        "identity_reviews": reviews[:250],
        "cleanup_pending": cleanup_pending[:250],
        "cleanup_history": cleanup_history,
        "provider_health": provider_health,
        "metadata_provenance": provenance,
    })


def _display(value):
    if value in (None, ""):
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return _safe_text(value)


def _table(headers, rows, empty="No recorded entries."):
    head = "".join(f"<th>{html.escape(label)}</th>" for label in headers)
    if rows:
        body = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(_display(value))}</td>" for value in row)
            + "</tr>"
            for row in rows
        )
    else:
        body = f'<tr><td class="empty" colspan="{len(headers)}">{html.escape(empty)}</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _badge(value):
    normalized = _safe_text(value or "unknown").lower()
    tone = (
        "good"
        if normalized in {"ok", "success", "healthy", "active"}
        else "bad"
        if normalized in {"failed", "error", "unhealthy"}
        else "warn"
    )
    return f'<span class="badge {tone}">{html.escape(_display(value))}</span>'


def render_dashboard(snapshot):
    """Render a portable HTML file with inline styling and behavior."""
    overview = snapshot.get("overview") or {}
    build = snapshot.get("build") or {}
    latest_status = overview.get("latest_job_status")
    scan_index = {
        (str(row.get("server_id")), str(row.get("library_uuid"))): row
        for row in snapshot.get("scan_state") or []
    }
    library_rows = []
    for row in snapshot.get("libraries") or []:
        scan = scan_index.get(
            (str(row.get("server_id")), str(row.get("library_uuid"))), {}
        )
        library_rows.append(
            (
                row.get("library_name"),
                row.get("library_type"),
                bool(row.get("active")),
                row.get("last_seen"),
                scan.get("last_full_scan_completed") or "Never",
                scan.get("last_successful_incremental") or "Never",
            )
        )
    unresolved_rows = [
        (
            row.get("library_name"),
            row.get("title"),
            row.get("asset_type"),
            row.get("category"),
            row.get("detail"),
            row.get("last_seen"),
        )
        for row in snapshot.get("unresolved_work") or []
    ]
    retry_rows = [
        (
            row.get("library_name"),
            row.get("rating_key"),
            row.get("status"),
            row.get("failure_class"),
            row.get("error_type"),
            row.get("next_retry_at"),
        )
        for row in snapshot.get("retries") or []
    ]
    review_rows = [
        (
            row.get("library_name"),
            row.get("title"),
            row.get("category"),
            row.get("proposed_tmdb_id"),
            row.get("reason"),
            row.get("last_seen"),
        )
        for row in snapshot.get("identity_reviews") or []
    ]
    cleanup_rows = [
        (
            row.get("library_name"),
            row.get("title"),
            row.get("scope"),
            row.get("confirmations"),
            row.get("eligible_after"),
            row.get("reason"),
        )
        for row in snapshot.get("cleanup_pending") or []
    ]
    cleanup_history_rows = [
        (
            row.get("occurred_at"),
            row.get("source"),
            row.get("status"),
            row.get("library_name"),
            row.get("title") or row.get("cache_key"),
            row.get("action"),
            row.get("output_type"),
        )
        for row in snapshot.get("cleanup_history") or []
    ]
    provider_rows = [
        (
            row.get("provider"),
            row.get("consecutive_failures"),
            row.get("open_until") or "Closed",
            row.get("last_success_at"),
            row.get("updated_at"),
        )
        for row in snapshot.get("provider_health") or []
    ]
    provenance = snapshot.get("metadata_provenance") or []
    source_counts = Counter(row.get("source_provider") or "Unknown" for row in provenance)
    action_counts = Counter(row.get("action") or "unknown" for row in provenance)
    provenance_rows = [
        (
            row.get("library_name"),
            row.get("title"),
            row.get("target"),
            row.get("field_path"),
            row.get("source_provider"),
            row.get("action"),
            row.get("policy"),
            str(row.get("value_fingerprint") or "")[:12] or None,
            row.get("last_changed_at"),
        )
        for row in provenance
    ]
    database_cards = "".join(
        (
            '<article class="db-card">'
            f"<strong>{html.escape(name.title())}</strong>"
            f"{_badge(details.get('status'))}"
            f"<small>Schema {_display(details.get('schema'))}/{_display(details.get('expected_schema'))} · "
            f"{int(details.get('bytes') or 0) / (1024 * 1024):.2f} MiB</small>"
            "</article>"
        )
        for name, details in (snapshot.get("databases") or {}).items()
    )
    cards = (
        ("Latest job", latest_status, overview.get("latest_job_finished")),
        ("Active libraries", overview.get("active_libraries", 0), "Recorded inventory"),
        ("Open work", overview.get("open_unresolved", 0), "Unresolved ledger"),
        ("Retry queue", overview.get("pending_retries", 0), "Pending, parked, or running"),
        ("Cleanup pending", overview.get("pending_cleanup", 0), "Grace/confirmation protected"),
        ("Field provenance", overview.get("field_provenance", 0), "Latest 500 shown"),
    )
    card_html = "".join(
        '<article class="metric">'
        f"<span>{html.escape(str(label))}</span>"
        f"<strong>{html.escape(_display(value))}</strong>"
        f"<small>{html.escape(_display(detail))}</small>"
        "</article>"
        for label, value, detail in cards
    )
    jobs_rows = [
        (
            row.get("finished_at"),
            row.get("mode"),
            row.get("status"),
            row.get("error"),
        )
        for row in reversed(snapshot.get("jobs") or [])
    ]
    source_summary = ", ".join(
        f"{name}: {count}" for name, count in sorted(source_counts.items())
    ) or "None recorded"
    action_summary = ", ".join(
        f"{name}: {count}" for name, count in sorted(action_counts.items())
    ) or "None recorded"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MetaFusion Offline Diagnostics</title>
<style>
:root {{ color-scheme: dark; --bg:#07111f; --panel:#101c2d; --line:#26374d; --text:#e8eef7; --muted:#9fb0c5; --accent:#5eead4; --good:#34d399; --warn:#fbbf24; --bad:#fb7185; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font:14px/1.5 Inter,ui-sans-serif,system-ui,sans-serif; background:linear-gradient(145deg,#07111f,#0d1727 55%,#0a1322); color:var(--text); }}
header,main {{ width:min(1500px,calc(100% - 32px)); margin:auto; }} header {{ padding:38px 0 22px; display:flex; justify-content:space-between; gap:20px; align-items:end; }}
h1 {{ margin:0; font-size:clamp(28px,4vw,48px); letter-spacing:-.04em; }} h2 {{ margin:0 0 16px; font-size:22px; }} h3 {{ margin:20px 0 10px; }} p {{ color:var(--muted); }}
.eyebrow {{ color:var(--accent); font-weight:700; letter-spacing:.12em; text-transform:uppercase; }} .meta {{ text-align:right; color:var(--muted); }}
nav {{ position:sticky; top:0; z-index:4; border:1px solid var(--line); background:rgba(7,17,31,.94); backdrop-filter:blur(12px); border-radius:14px; padding:8px; display:flex; flex-wrap:wrap; gap:6px; }}
button,input {{ font:inherit; }} nav button {{ color:var(--muted); border:0; background:transparent; padding:9px 12px; border-radius:9px; cursor:pointer; }} nav button.active,nav button:hover {{ color:#04201d; background:var(--accent); }}
.toolbar {{ margin:16px 0; display:flex; gap:10px; }} input {{ width:min(430px,100%); color:var(--text); background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }}
section {{ display:none; margin:18px 0 30px; }} section.active {{ display:block; }} .panel {{ background:rgba(16,28,45,.92); border:1px solid var(--line); border-radius:16px; padding:20px; margin-bottom:16px; box-shadow:0 18px 50px rgba(0,0,0,.18); }}
.metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }} .metric,.db-card {{ background:#0b1727; border:1px solid var(--line); border-radius:13px; padding:16px; }} .metric span,.metric small,.db-card small {{ color:var(--muted); display:block; }} .metric strong {{ display:block; font-size:27px; margin:7px 0 2px; }}
.databases {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }} .db-card {{ display:grid; gap:8px; }} .badge {{ display:inline-flex; width:max-content; padding:3px 8px; border-radius:999px; font-size:12px; font-weight:700; }} .good {{ color:var(--good); background:#0b2d27; }} .warn {{ color:var(--warn); background:#33270b; }} .bad {{ color:var(--bad); background:#381420; }}
.table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:12px; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:#c9d6e6; background:#0b1727; position:sticky; top:0; }} td {{ color:#b7c5d8; }} tr:last-child td {{ border-bottom:0; }} .empty {{ text-align:center; color:var(--muted); padding:28px; }}
.notice {{ border-left:3px solid var(--accent); padding:10px 14px; background:#0b1d28; border-radius:0 10px 10px 0; }} footer {{ color:var(--muted); padding:10px 0 42px; }}
@media (max-width:700px) {{ header {{ align-items:start; flex-direction:column; }} .meta {{ text-align:left; }} }} @media print {{ nav,.toolbar {{ display:none; }} section {{ display:block; break-inside:avoid; }} body {{ background:white; color:#111; }} .panel {{ box-shadow:none; }} }}
</style>
</head>
<body>
<header><div><div class="eyebrow">Offline · read-only · self-contained</div><h1>MetaFusion Diagnostics</h1><p>{html.escape(_safe_text(snapshot.get('notice')))}</p></div><div class="meta">Version {html.escape(_display(build.get('version')))}<br>Commit {html.escape(_display(build.get('commit')))}<br>{html.escape(_display(snapshot.get('generated_at')))}</div></header>
<main>
<nav aria-label="Dashboard sections"><button class="active" data-section="overview">Overview</button><button data-section="libraries">Libraries</button><button data-section="problems">Problems</button><button data-section="cleanup">Cleanup</button><button data-section="providers">Providers</button><button data-section="provenance">Metadata provenance</button></nav>
<div class="toolbar"><input id="filter" type="search" placeholder="Filter visible table rows…"><button id="print">Print / Save PDF</button></div>
<section id="overview" class="active"><div class="panel"><h2>Run overview</h2><div class="metrics">{card_html}</div></div><div class="panel"><h2>SQLite health</h2><div class="databases">{database_cards}</div></div><div class="panel"><h2>Recent jobs</h2>{_table(('Finished','Mode','Status','Error'), jobs_rows)}</div></section>
<section id="libraries"><div class="panel"><h2>Recorded libraries</h2><p>Inventory and scan timestamps are historical SQLite evidence, not a live Plex query.</p>{_table(('Library','Type','Active','Last seen','Last full scan','Last incremental'), library_rows)}</div></section>
<section id="problems"><div class="panel"><h2>Unresolved work</h2>{_table(('Library','Title','Output','Category','Detail','Last seen'), unresolved_rows)}</div><div class="panel"><h2>Retry queue</h2>{_table(('Library','Rating key','Status','Class','Error type','Next retry'), retry_rows)}</div><div class="panel"><h2>Identity review queue</h2>{_table(('Library','Title','Category','Proposed TMDb','Reason','Last seen'), review_rows)}</div></section>
<section id="cleanup"><div class="panel"><h2>Pending cleanup</h2><p>These entries remain protected by confirmation and grace-period policy.</p>{_table(('Library','Title','Scope','Confirmations','Eligible after','Reason'), cleanup_rows)}</div><div class="panel"><h2>Recent cleanup history</h2>{_table(('Occurred','Source','Status','Library','Item','Action','Output'), cleanup_history_rows)}</div></section>
<section id="providers"><div class="panel"><h2>Recorded provider health</h2><p>No provider was contacted to create this dashboard.</p>{_table(('Provider','Consecutive failures','Circuit open until','Last success','Updated'), provider_rows)}</div></section>
<section id="provenance"><div class="panel"><h2>Field-level metadata provenance</h2><p class="notice">Values are never retained. Source summary: {html.escape(source_summary)}. Decision summary: {html.escape(action_summary)}.</p>{_table(('Library','Title','Target','Field','Effective source','Decision','Policy','Fingerprint','Recorded'), provenance_rows)}</div></section>
<footer>Open this file directly in any modern browser. It requires no server, network connection, external script, font, or stylesheet.</footer>
</main>
<script>
const buttons=[...document.querySelectorAll('nav button')];
buttons.forEach(button=>button.addEventListener('click',()=>{{buttons.forEach(item=>item.classList.remove('active'));document.querySelectorAll('main section').forEach(item=>item.classList.remove('active'));button.classList.add('active');document.getElementById(button.dataset.section).classList.add('active');document.getElementById('filter').dispatchEvent(new Event('input'));}}));
document.getElementById('filter').addEventListener('input',event=>{{const query=event.target.value.toLowerCase();document.querySelectorAll('section.active tbody tr').forEach(row=>{{row.hidden=query && !row.textContent.toLowerCase().includes(query);}});}});
document.getElementById('print').addEventListener('click',()=>window.print());
</script>
</body></html>"""


def _retain_dashboard_reports(report_dir, retention):
    reports = sorted(
        Path(report_dir).glob("metafusion-dashboard-[0-9]*.html"),
        key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        reverse=True,
    )
    for stale in reports[max(1, int(retention)) :]:
        for candidate in (stale, stale.with_suffix(".json")):
            try:
                candidate.unlink()
            except OSError:
                pass


def write_dashboard_report(
    *, base_dir=None, retention=10, path=STATE_DATABASE, snapshot=None
):
    """Write timestamped and stable-latest offline dashboard files atomically."""
    data = _sanitize_snapshot(snapshot or collect_dashboard_snapshot(path=path))
    report_dir = Path(base_dir or BASE_CONFIG_DIR) / "reports"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    report = report_dir / f"metafusion-dashboard-{timestamp}.html"
    latest = report_dir / "metafusion-dashboard-latest.html"
    rendered = render_dashboard(data)
    companion = {
        "schema": DASHBOARD_SCHEMA_VERSION,
        "report_type": "offline_dashboard",
        "generated_at": data.get("generated_at"),
        "html_report": report.name,
        "data": data,
    }
    latest_companion = {**companion, "html_report": latest.name}
    atomic_write_text(report, rendered)
    atomic_write_json(report.with_suffix(".json"), companion)
    atomic_write_text(latest, rendered)
    atomic_write_json(latest.with_suffix(".json"), latest_companion)
    _retain_dashboard_reports(report_dir, retention)
    return report
