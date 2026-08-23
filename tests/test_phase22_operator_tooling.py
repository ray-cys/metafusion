import copy
import sqlite3
from collections import namedtuple
from contextlib import closing
from types import SimpleNamespace

import pytest

import metafusion
from helper import database_maintenance
from helper.compatibility import (
    evaluate_compatibility,
    resolve_compatibility_profile,
)
from helper.config import DEFAULT_CONFIG, ENV_BINDINGS, validate_config
from helper.database_maintenance import (
    format_maintenance_results,
    inspect_database,
    maintain_databases,
    selected_databases,
)
from helper.diagnostics import (
    write_change_plan_report,
    write_compatibility_report,
    write_library_asset_audit_report,
)
from helper.state_db import load_item_retries, record_item_failure
from modules.cleanup import CleanupResult
from modules.utils import (
    artwork_provider_rating,
    artwork_quality_score,
    get_best_background,
    get_best_poster,
)


def complete_config(mode="kometa"):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["settings"]["mode"] = mode
    config["settings"]["path"] = "/kometa"
    config["plex"]["url"] = "http://plex:32400"
    config["plex"]["token"] = "token"
    config["tmdb"]["api_key"] = "key"
    config["plex_libraries"] = ["Movies"]
    return config


def create_database(path, schema):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample (value INTEGER)")
        connection.execute("INSERT INTO sample VALUES (1)")
        connection.execute(f"PRAGMA user_version = {int(schema)}")


def test_change_plan_and_target_overrides_are_explicitly_read_only():
    config = complete_config("plex")
    config["cleanup"]["run_cleanup"] = True
    args = metafusion.parse_cli_args(
        [
            "--plan",
            "--library",
            "Movies",
            "--rating-key",
            "10,11",
            "--tmdb-id",
            "550",
            "--tmdb-id",
            "551,552",
            "--media-type",
            "movie",
        ]
    )

    metafusion.override_config_with_cli(config, args)

    assert config["settings"]["dry_run"] is True
    assert config["metafusion_run"] is True
    assert config["plex_metadata"]["enabled"] is True
    assert config["cleanup"]["run_cleanup"] is False
    assert config["_execution"] == {
        "rating_keys": ["10", "11"],
        "targeted": True,
        "full_scan": True,
        "metadata_only": False,
        "asset_only": False,
        "asset_audit": True,
        "metadata_audit": True,
        "explain_selection": False,
        "tmdb_ids": ["550", "551", "552"],
        "media_types": ["movie"],
        "plan": True,
    }


def test_library_audit_and_selective_retry_overrides():
    audit = complete_config()
    metafusion.override_config_with_cli(
        audit,
        metafusion.parse_cli_args(["--library-audit", "--media-type", "show"]),
    )
    assert audit["settings"]["dry_run"] is True
    assert not any(audit["metadata"].values())
    assert audit["cleanup"]["run_cleanup"] is False
    assert audit["_execution"]["library_audit"] is True
    assert audit["_execution"]["asset_audit"] is True
    assert audit["_execution"]["media_types"] == ["show"]

    retry = complete_config()
    metafusion.override_config_with_cli(
        retry,
        metafusion.parse_cli_args(
            ["--retry-failed", "--retry-status", "parked", "--library", "Movies"]
        ),
    )
    assert retry["_execution"]["retry_failed"] is True
    assert retry["_execution"]["retry_status"] == "parked"
    assert retry["_execution"]["targeted"] is True
    assert retry["cleanup"]["run_cleanup"] is False


@pytest.mark.parametrize(
    "arguments,message",
    [
        (["--retry-status", "parked"], "requires --retry-failed"),
        (["--sqlite-target", "state"], "requires --sqlite-maintenance"),
        (["--plan", "--metadata-audit"], "choose only one"),
        (["--retry-failed", "--asset-audit"], "cannot be combined"),
        (
            ["--sqlite-maintenance", "check", "--preflight"],
            "standalone command",
        ),
    ],
)
def test_new_cli_rejects_ambiguous_combinations(arguments, message, capsys):
    assert metafusion.main(arguments) == 2
    assert message in capsys.readouterr().err


def test_tmdb_guid_targeting_uses_only_explicit_plex_guids():
    first = SimpleNamespace(
        guid="plex://movie/one",
        guids=[SimpleNamespace(id="tmdb://550"), "imdb://tt0137523"],
    )
    second = SimpleNamespace(guid="themoviedb:551", guids=[])
    third = SimpleNamespace(guid="tmdb://not-a-number", guids=[])

    assert metafusion.plex_item_tmdb_ids(first) == {"550"}
    assert metafusion.plex_item_tmdb_ids(second) == {"551"}
    selected, found = metafusion.target_items_by_tmdb(
        [first, second, third], ["551", "999"]
    )
    assert selected == [second]
    assert found == {"551"}
    assert metafusion.target_items_by_tmdb([first], [])[0] == [first]


def test_selective_retry_rows_support_library_key_and_status_filters(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    record_item_failure(
        "server",
        "movies-id",
        "10",
        TimeoutError("temporary"),
        library_name="Movies",
        media_type="movie",
        path=database,
    )
    record_item_failure(
        "server",
        "shows-id",
        "20",
        "identity mismatch",
        library_name="Shows",
        media_type="show",
        failure_class="permanent",
        path=database,
    )
    record_item_failure(
        "other",
        "movies-id",
        "30",
        TimeoutError("temporary"),
        library_name="Movies",
        path=database,
    )

    pending = load_item_retries(
        server_id="server",
        library_uuids=["movies-id"],
        statuses=["pending"],
        path=database,
    )
    assert [row["rating_key"] for row in pending] == ["10"]
    parked = load_item_retries(
        server_id="server",
        library_names=["shows"],
        rating_keys=["20"],
        statuses=["parked"],
        path=database,
    )
    assert parked[0]["failure_class"] == "permanent"
    assert load_item_retries(
        server_id="server", rating_keys=["missing"], path=database
    ) == []
    assert load_item_retries(path=tmp_path / "absent.sqlite3") == []


def test_artwork_quality_score_is_bounded_and_explains_components():
    config = complete_config()
    config["poster_set"].update({"max_width": 1000, "max_height": 1500})
    exact = artwork_quality_score(
        config,
        {
            "width": 1000,
            "height": 1500,
            "vote_average": 8,
            "iso_639_1": "en",
        },
        asset_type="poster",
        preferred_language="en",
    )
    assert exact == {
        "score": 93.0,
        "resolution": 45.0,
        "vote": 28.0,
        "aspect": 10.0,
        "language": 10.0,
        "content": 0.0,
        "blank_penalty": 0.0,
        "perceptual_hash": None,
        "validated_width": None,
        "validated_height": None,
        "provider_average": 8.0,
        "provider_count": 0,
        "provider_confidence": 0.0,
    }
    fallback = artwork_quality_score(
        config,
        {
            "width": 1000,
            "height": 1500,
            "vote_average": 99,
            "iso_639_1": "zh",
        },
        asset_type="poster",
        preferred_language="en",
    )
    assert fallback["score"] <= 100
    assert fallback["vote"] == 35
    assert fallback["provider_average"] == 10
    assert fallback["language"] == 7

    malformed = artwork_quality_score(
        config,
        {"width": 0, "height": 0, "vote_average": -1},
        asset_type="unknown",
        preferred_language="en",
    )
    assert malformed["score"] == 4


def test_provider_vote_average_remains_primary_over_vote_count():
    config = complete_config()
    sparse = artwork_quality_score(
        config,
        {
            "width": 1000,
            "height": 1500,
            "vote_average": 10,
            "vote_count": 1,
            "iso_639_1": "en",
        },
        preferred_language="en",
    )
    established = artwork_quality_score(
        config,
        {
            "width": 1000,
            "height": 1500,
            "vote_average": 8,
            "vote_count": 100,
            "iso_639_1": "en",
        },
        preferred_language="en",
    )
    assert sparse["vote"] > established["vote"]
    assert established["provider_confidence"] > sparse["provider_confidence"]
    config["poster_set"].update(
        {
            "prefer_vote": 5,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        }
    )
    sparse_candidate = {
        "file_path": "/sparse.jpg",
        "width": 1000,
        "height": 1500,
        "vote_average": 10,
        "vote_count": 1,
        "iso_639_1": "en",
    }
    established_candidate = {
        "file_path": "/established.jpg",
        "width": 1000,
        "height": 1500,
        "vote_average": 8,
        "vote_count": 100,
        "iso_639_1": "en",
    }
    assert get_best_poster(
        config,
        [established_candidate, sparse_candidate],
        preferred_language="en",
    ) == sparse_candidate
    assert artwork_provider_rating(
        {"vote_average": 7, "vote_count": object()}
    )["count"] == 0


def test_quality_score_breaks_candidate_ties_deterministically():
    config = complete_config()
    config["poster_set"].update(
        {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 1000,
            "max_height": 1500,
            "min_width": 500,
            "min_height": 750,
        }
    )
    candidates = [
        {
            "width": 1000,
            "height": 2000,
            "vote_average": 8,
            "iso_639_1": "en",
            "file_path": "/wrong-ratio.jpg",
        },
        {
            "width": 1000,
            "height": 1500,
            "vote_average": 8,
            "iso_639_1": "en",
            "file_path": "/balanced.jpg",
        },
    ]
    assert get_best_poster(config, candidates)["file_path"] == "/balanced.jpg"

    config["background_set"].update(
        {
            "prefer_vote": 5,
            "vote_relaxed": 3,
            "max_width": 1920,
            "max_height": 1080,
            "min_width": 1280,
            "min_height": 720,
        }
    )
    backgrounds = [
        {"width": 1920, "height": 1200, "vote_average": 8, "file_path": "/4x3.jpg"},
        {"width": 1920, "height": 1080, "vote_average": 8, "file_path": "/wide.jpg"},
    ]
    assert get_best_background(config, backgrounds)["file_path"] == "/wide.jpg"


def test_compatibility_profiles_auto_select_and_validate_modes():
    kometa = complete_config("kometa")
    plex = complete_config("plex")
    assert resolve_compatibility_profile(kometa) == "kometa-2.4"
    assert resolve_compatibility_profile(plex) == "plex-api-v1"
    assert ("compatibility", "profile") in {
        path for _name, path, _converter in ENV_BINDINGS
    }

    kometa_result = evaluate_compatibility(
        kometa,
        {
            "available_count": 1,
            "tmdb_available": True,
            "plex_version": "1.41",
        },
    )
    assert kometa_result["passed"] is True
    assert kometa_result["contract"] == "Kometa metadata schema 2.4.x"

    plex["assets"]["run_poster"] = True
    plex_result = evaluate_compatibility(
        plex,
        {
            "available_count": 1,
            "tmdb_available": True,
            "plex_version": "1.41",
            "path_advice": {"records": [{"status": "unresolved"}]},
        },
    )
    assert plex_result["passed"] is False
    assert plex_result["warnings"]

    unknown = evaluate_compatibility(kometa, requested="future-profile")
    assert unknown["passed"] is False
    assert unknown["contract"] == "unknown"


def test_config_validation_rejects_cross_mode_compatibility_profiles():
    config = complete_config("plex")
    config["compatibility"]["profile"] = "kometa-2.4"
    assert any("requires Kometa mode" in error for error in validate_config(config))
    config["compatibility"]["profile"] = "future"
    assert any("must be auto" in error for error in validate_config(config))


def test_report_writers_are_value_safe_and_bounded(tmp_path):
    metadata = [
        {
            "library": "Movies",
            "title": "Example",
            "field": "summary",
            "state": "missing",
            "proposed_action": "add",
        }
    ]
    assets = [
        {
            "library": "Movies",
            "title": "Example",
            "asset_type": "poster",
            "action": "would_download",
            "ownership": "missing",
            "candidate": {"quality_score": 91.5, "width": 2000, "height": 3000},
        }
    ]
    libraries = [
        {
            "library": "Movies",
            "type": "movie",
            "items": 1,
            "selected": True,
            "status": "loaded",
        }
    ]
    cleanup = CleanupResult(titles=2, assets=3, dry_run=True)

    first = write_change_plan_report(
        metadata,
        assets,
        libraries,
        [{"category": "identity_rejected"}],
        cleanup,
        mode="kometa",
        base_dir=tmp_path,
        retention=1,
    )
    second = write_change_plan_report(
        metadata,
        assets,
        libraries,
        cleanup_result={"titles": 2},
        mode="kometa",
        base_dir=tmp_path,
        retention=1,
    )
    assert not first.exists()
    plan_text = second.read_text(encoding="utf-8")
    assert "The report itself is the only deliberate output" in plan_text
    assert "score=91.5" in plan_text
    assert "secret metadata value" not in plan_text

    audit = write_library_asset_audit_report(
        libraries,
        assets,
        mode="plex",
        base_dir=tmp_path,
    )
    audit_text = audit.read_text(encoding="utf-8")
    assert "Mode: plex" in audit_text
    assert "would_download: 1" in audit_text
    assert "2000x3000" in audit_text

    compatibility = write_compatibility_report(
        {
            "passed": True,
            "profile": "kometa-2.4",
            "mode": "kometa",
            "contract": "Kometa metadata schema 2.4.x",
            "checks": [{"name": "Output mode", "passed": True, "detail": "kometa"}],
            "capabilities": ["metadata YAML generation"],
            "warnings": ["review this"],
        },
        base_dir=tmp_path,
    )
    text = compatibility.read_text(encoding="utf-8")
    assert "Result: PASS" in text
    assert "[PASS] Output mode" in text
    assert "review this" in text


def test_sqlite_inspection_and_all_explicit_maintenance_actions(monkeypatch, tmp_path):
    state = tmp_path / "meta_db.sqlite3"
    tmdb = tmp_path / "tmdb_cache.sqlite3"
    create_database(state, 4)
    create_database(tmdb, 1)
    monkeypatch.setattr(
        database_maintenance,
        "DATABASES",
        {"state": (state, 4), "tmdb": (tmdb, 1)},
    )

    checked = maintain_databases("check")
    assert all(item["healthy"] for item in checked)
    assert {item["database"] for item in checked} == {"state", "tmdb"}
    assert "[PASS] state check" in format_maintenance_results(checked)

    for action in ("optimize", "checkpoint", "vacuum"):
        result = maintain_databases(action, "state")
        assert result[0]["status"] == "completed"
        assert result[0]["healthy"] is True

    backup_dir = tmp_path / "backups"
    for _index in range(4):
        result = maintain_databases(
            "backup", "state", backup_dir=backup_dir, retention=2
        )
        assert result[0]["backup"].endswith(".sqlite3")
    assert len(list(backup_dir.glob("meta_db-*.sqlite3"))) == 2


def test_sqlite_maintenance_handles_missing_corrupt_and_invalid_targets(
    monkeypatch, tmp_path
):
    missing = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(
        database_maintenance,
        "DATABASES",
        {"state": (missing, 4)},
    )
    checked = maintain_databases("check", "state")
    assert checked[0]["healthy"] is True
    assert checked[0]["status"] == "missing"
    skipped = maintain_databases("vacuum", "state")
    assert skipped[0]["status"] == "skipped (database missing)"

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not sqlite", encoding="utf-8")
    inspected = inspect_database(corrupt, 4)
    assert inspected["healthy"] is False
    assert "DatabaseError" in inspected["status"]

    with pytest.raises(ValueError, match="Unsupported SQLite target"):
        selected_databases("other")
    with pytest.raises(ValueError, match="Unsupported SQLite maintenance"):
        maintain_databases("repair")


def test_vacuum_refuses_when_free_space_is_insufficient(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    create_database(database, 4)
    monkeypatch.setattr(
        database_maintenance,
        "DATABASES",
        {"state": (database, 4)},
    )
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(
        database_maintenance.shutil,
        "disk_usage",
        lambda _path: Usage(100, 100, 0),
    )
    result = maintain_databases("vacuum", "state")
    assert result[0]["healthy"] is False
    assert "insufficient free space" in result[0]["status"]


def test_sqlite_cli_and_compatibility_check_have_clear_exit_status(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        metafusion,
        "maintain_databases",
        lambda action, target: [
            {
                "database": target,
                "action": action,
                "healthy": True,
                "status": "ok",
                "schema": 4,
            }
        ],
    )
    assert metafusion.main(["--sqlite-maintenance", "check", "--sqlite-target", "state"]) == 0
    assert "[PASS] state check" in capsys.readouterr().out

    config = complete_config("kometa")
    calls = []
    monkeypatch.setattr(
        metafusion,
        "load_config_file",
        lambda **kwargs: (copy.deepcopy(config), {}),
    )
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)

    async def preflight(_config):
        return {
            "available_count": 1,
            "tmdb_available": True,
            "plex_version": "1.41",
        }

    monkeypatch.setattr(metafusion, "connector_preflight", preflight)
    monkeypatch.setattr(
        metafusion,
        "write_compatibility_report",
        lambda result, **_kwargs: calls.append(result) or (tmp_path / "compat.txt"),
    )
    assert metafusion.main(["--compatibility-check"]) == 0
    output = capsys.readouterr().out
    assert "Compatibility profile kometa-2.4 passed" in output
    assert calls[0]["passed"] is True
