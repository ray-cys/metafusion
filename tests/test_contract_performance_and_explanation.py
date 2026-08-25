import asyncio
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import metafusion
from helper import item_explanation
from helper.config import DEFAULT_CONFIG
from helper.incremental import config_fingerprint, mark_library_scan_complete
from helper.state_db import MediaStateStore
from tools import performance_regression


def complete_config(tmp_path, mode="kometa"):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"].update(mode=mode, path=str(tmp_path / "kometa"))
    config["plex"].update(url="http://plex:32400", token="token")
    config["tmdb"]["api_key"] = "key"
    config["plex_libraries"] = ["Movies"]
    return config


def test_performance_baseline_and_small_workload_are_machine_readable(tmp_path):
    baseline = performance_regression.load_baseline()
    assert baseline["workload"]["movies"] == 2000
    assert baseline["workload"]["episodes"] == 8000

    workload = {
        "movies": 4,
        "shows": 2,
        "seasons": 3,
        "episodes": 8,
        "targeted_reads": 2,
        "corpus_render_iterations": 1,
    }
    metrics = performance_regression.run_workload(workload, tmp_path)
    assert metrics["state_items"] == 6
    assert metrics["state_seasons"] == 3
    assert metrics["state_episodes"] == 8
    assert metrics["targeted_rows"] == 2
    assert metrics["corpus_documents"] == 3
    assert performance_regression.evaluate(metrics, {"total_seconds": 999}) == []
    assert performance_regression.evaluate(metrics, {"total_seconds": 0})


def test_performance_cli_writes_report_and_summary(monkeypatch, tmp_path):
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "format": 1,
                "workload": {
                    "movies": 1,
                    "shows": 1,
                    "seasons": 1,
                    "episodes": 1,
                    "targeted_reads": 1,
                    "corpus_render_iterations": 1,
                },
                "maximum": {
                    "total_seconds": 1,
                    "state_write_seconds": 1,
                    "targeted_read_seconds": 1,
                    "corpus_render_seconds": 1,
                    "peak_memory_mib": 1,
                    "database_mib": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    measurements = {
        "total_seconds": 0.1,
        "state_write_seconds": 0.1,
        "targeted_read_seconds": 0.1,
        "corpus_render_seconds": 0.1,
        "peak_memory_mib": 0.1,
        "database_mib": 0.1,
        "items_per_second": 20,
    }
    monkeypatch.setattr(performance_regression, "run_workload", lambda *_args: measurements)
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.md"
    assert (
        performance_regression.main(
            [
                "--baseline",
                str(baseline),
                "--output",
                str(report),
                "--github-summary",
                str(summary),
            ]
        )
        == 0
    )
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is True
    assert "performance regression measurement" in summary.read_text(encoding="utf-8")


def test_selection_explanation_reads_missing_state_without_creating_it(
    monkeypatch, tmp_path
):
    database = tmp_path / "missing" / "meta_db.sqlite3"
    monkeypatch.setattr(item_explanation, "STATE_DATABASE", database)
    config = complete_config(tmp_path)
    item = SimpleNamespace(ratingKey="10", type="movie", updatedAt=None)
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "10",
    }

    result = item_explanation._selection_record(item, meta, config)

    assert result["normal_schedule_action"] == "process"
    assert result["full_scan_due"] is True
    assert result["causes"] == ["full_scan"]
    assert result["cache_record_present"] is False
    assert not database.exists()


def test_selection_explanation_skips_current_not_due_state(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    monkeypatch.setattr(item_explanation, "STATE_DATABASE", database)
    config = complete_config(tmp_path)
    now = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)
    updated_at = now.isoformat()
    fingerprint = config_fingerprint(config)
    scope = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "config_fingerprint": fingerprint,
        "item_count": 1,
    }
    store = MediaStateStore(database)
    store["movie:plex:10"] = {
        **scope,
        "rating_key": "10",
        "media_type": "movie",
        "plex_updated_at": updated_at,
        "poster_last_checked": updated_at,
        "background_last_checked": updated_at,
        "metadata_pending_count": 0,
    }
    store.flush()
    store.close()
    mark_library_scan_complete([scope], True, path=database, now=now)
    item = SimpleNamespace(ratingKey="10", type="movie", updatedAt=updated_at)
    meta = {
        "server_id": "server",
        "library_uuid": "library",
        "library_name": "Movies",
        "library_type": "movie",
        "ratingKey": "10",
    }

    result = item_explanation._selection_record(item, meta, config)

    assert result["normal_schedule_action"] == "skip"
    assert result["full_scan_due"] is False
    assert result["cache_record_present"] is True
    assert result["cached_config_matches"] is True


def test_unified_item_explanation_combines_identity_mapping_policy_and_state(
    monkeypatch, tmp_path
):
    config = complete_config(tmp_path, mode="plex")
    config["plex_metadata"].update(enabled=True, policy="managed")
    item = SimpleNamespace(ratingKey="20", type="show")
    meta = {
        "library_name": "TV Shows",
        "library_type": "show",
        "ratingKey": "20",
    }

    async def metadata(*_args, **_kwargs):
        return meta

    async def identity(*_args, **_kwargs):
        return {
            "status": "accepted",
            "library": "TV Shows",
            "rating_key": "20",
            "plex": {"localized_title": "Example", "year": 2024},
            "selection": {"tmdb_id": "200", "source": "plex_tmdb_guid"},
            "binding": {"status": "current", "history": []},
            "metadata_destination": {"path": "/library/metadata/20", "entry": "fields"},
            "artwork_destinations": {
                "poster": {"path": "/media/Example/poster.jpg"},
                "background": {"path": "/media/Example/fanart.jpg"},
                "seasons": [],
            },
        }

    async def mapping(*_args, **_kwargs):
        return {"status": "aligned", "explanation": "All episodes align."}

    monkeypatch.setattr(item_explanation, "get_plex_metadata", metadata)
    monkeypatch.setattr(item_explanation, "diagnose_identity", identity)
    monkeypatch.setattr(item_explanation, "diagnose_mapping", mapping)
    monkeypatch.setattr(
        item_explanation,
        "_selection_record",
        lambda *_args: {
            "normal_schedule_action": "skip",
            "full_scan_due": False,
            "work": [],
            "causes": [],
            "cache_record_present": True,
            "cached_config_matches": True,
        },
    )

    record = asyncio.run(item_explanation.explain_item(item, config, session=object()))
    assert record["status"] == "accepted"
    assert record["episode_mapping"]["status"] == "aligned"
    assert record["selection"]["normal_schedule_action"] == "skip"
    assert record["policies"]["metadata"]["plex_policy"] == "managed"

    report = item_explanation.write_item_explanation_report(
        [record], base_dir=tmp_path, retention=1
    )
    contents = report.read_text(encoding="utf-8")
    assert "Unified read-only item explanation" in contents
    assert "Normal scheduled-run decision" in contents
    assert "Plex API policy: managed" in contents
    assert "Episode mapping status: aligned" in contents

    kometa_policy = item_explanation._policy_record(complete_config(tmp_path), "movie")
    assert kometa_policy["metadata"]["target"] == "Kometa YAML"
    assert kometa_policy["metadata"]["kometa_tag_policy"] == "append"


def test_item_explanation_runner_handles_found_and_missing_keys(monkeypatch, tmp_path):
    item = SimpleNamespace(ratingKey="10")
    section = SimpleNamespace(title="Movies")

    async def inventory(_section, _runtime, records_only=False):
        if records_only:
            return [
                {
                    "rating_key": "10",
                    "media_type": "movie",
                    "title": "Example",
                    "year": 2020,
                    "edition": None,
                }
            ]
        return [item]

    async def explain(
        selected,
        _config,
        session=None,
        *,
        identity_counts=None,
        edition_counts=None,
    ):
        assert selected is item
        assert session is not None
        assert identity_counts[("movie", "Example", 2020)] == 1
        assert edition_counts[("Example", 2020, None)] == 1
        return {
            "status": "accepted",
            "library": "Movies",
            "rating_key": "10",
            "media_type": "movie",
            "identity": {"plex": {"localized_title": "Example", "year": 2020}},
            "selection": {},
            "policies": {},
            "episode_mapping": {},
        }

    monkeypatch.setattr(item_explanation, "load_plex_library_inventory", inventory)
    monkeypatch.setattr(item_explanation, "explain_item", explain)
    config = complete_config(tmp_path)
    config["output"]["report_retention"] = 1
    results, report = asyncio.run(
        item_explanation.run_item_explanation(
            [section],
            config,
            ["10", "99"],
            session=object(),
            base_dir=tmp_path,
        )
    )

    assert [record["status"] for record in results] == ["accepted", "not_found"]
    assert "Items: 2" in report.read_text(encoding="utf-8")


def test_explain_item_cli_requires_a_key_and_reports_connector_results(
    monkeypatch, tmp_path, capsys
):
    config = complete_config(tmp_path)
    monkeypatch.setattr(metafusion, "load_config_file", lambda **_kwargs: (config, {}))
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: True)

    async def explain(_config, rating_keys):
        assert rating_keys == ["10"]
        return [{"status": "accepted"}], tmp_path / "reports" / "item.txt"

    monkeypatch.setattr(metafusion, "item_explanation_connectors", explain)
    assert metafusion.main(["--explain-item", "--rating-key", "10"]) == 0
    assert "Item explanation completed for 1 item" in capsys.readouterr().out
    assert metafusion.main(["--explain-item"]) == 2
    assert "requires --rating-key" in capsys.readouterr().err
