import asyncio
import json
import re
from collections import namedtuple
from io import BytesIO

from PIL import Image, ImageDraw

import metafusion
from helper import concurrency as concurrency_module
from helper import logging as logging_module
from helper import state_db
from helper.diagnostics import (
    write_adoption_audit_report,
    write_artwork_gap_report,
    write_destination_history_report,
)
from helper.io import sha256_file
from helper.provider_replay import provider_replay_issues, write_sanitized_replay_capture
from helper.reporting import retain_diagnostic_reports, write_diagnostic_report
from helper.storage import storage_pressure_threshold
from modules import builder, processing, utils


def _image_bytes(*, size=(120, 180), blank=False):
    image = Image.new("RGB", size, "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        for offset in range(0, size[1], 12):
            draw.rectangle(
                (0, offset, size[0], min(size[1], offset + 5)),
                fill=(offset % 255, 30, 190),
            )
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=92)
    return stream.getvalue()


def test_effective_storage_threshold_drives_final_warning(monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    usage = Usage(100 * 1024**3, 99_500 * 1024**2, 500 * 1024**2)
    config = {
        "settings": {"mode": "kometa", "path": "/kometa", "dry_run": False},
        "runtime": {"min_free_space_mb": 256},
        "plex_libraries": [],
    }
    configured_mb, effective = storage_pressure_threshold(config, usage)
    assert configured_mb == 256
    assert effective == 1024**3

    monkeypatch.setattr(
        logging_module,
        "_storage_mounts",
        lambda _config: [{"labels": ["Config"], "path": "/config", "usage": usage}],
    )
    monkeypatch.setattr(logging_module, "_runtime_storage_bytes", lambda: {})

    class Logger:
        def __init__(self):
            self.info_lines = []
            self.warning_lines = []

        def info(self, message, *args):
            self.info_lines.append(message % args if args else message)

        def warning(self, message, *args):
            self.warning_lines.append(message % args if args else message)

    logger = Logger()
    logging_module.log_final_summary(
        logger,
        1,
        {},
        {},
        None,
        0,
        [],
        [],
        config,
        feature_flags={},
    )
    assert "Required: 1.00 GB" in logger.warning_lines[0]


def test_downloaded_image_properties_are_validated_before_install(
    monkeypatch, tmp_path, caplog
):
    saved = []
    monkeypatch.setattr(utils, "save_artwork_analysis", lambda *args, **kwargs: saved.append(args))
    config = {"settings": {"dry_run": False}}
    destination = tmp_path / "poster.jpg"
    content = _image_bytes()

    result, error = asyncio.run(
        utils.save_poster(
            content,
            destination,
            config=config,
            provider="tmdb",
            source_path="/poster.jpg",
            expected_image={"width": 120, "height": 180},
        )
    )
    assert result is True
    assert error is None
    assert destination.is_file()
    assert saved

    blank, blank_error = asyncio.run(
        utils.save_poster(
            _image_bytes(blank=True),
            tmp_path / "blank.jpg",
            config=config,
            provider="tmdb",
            source_path="/blank.jpg",
        )
    )
    assert blank is False
    assert "appears blank" in blank_error
    assert not (tmp_path / "blank.jpg").exists()

    mismatch, mismatch_error = asyncio.run(
        utils.save_poster(
            content,
            tmp_path / "mismatch.jpg",
            config=config,
            provider="tmdb",
            source_path="/mismatch.jpg",
            expected_image={"width": 1000, "height": 1500},
        )
    )
    assert mismatch is False
    assert "provider metadata" in mismatch_error

    below_minimum, minimum_error = asyncio.run(
        utils.save_poster(
            content,
            tmp_path / "below-minimum.jpg",
            config={
                "settings": {"dry_run": False},
                "poster_set": {"min_width": 150, "min_height": 200},
            },
            provider="tmdb",
            source_path="/below-minimum.jpg",
        )
    )
    assert below_minimum is False
    assert "below configured minimum dimensions" in minimum_error

    fallback, fallback_error = asyncio.run(
        utils.save_poster(
            content,
            tmp_path / "validated-best-available.jpg",
            config={
                "settings": {"dry_run": False},
                "tmdb": {"fallback": []},
                "poster_set": {"min_width": 150, "min_height": 200},
            },
            provider="tmdb",
            source_path="/validated-best-available.jpg",
            expected_image={"width": 120, "height": 180},
        )
    )
    assert fallback is True
    assert fallback_error is None

    def unavailable_cache(*_args, **_kwargs):
        raise state_db.StateDatabaseError("analysis cache unavailable")

    monkeypatch.setattr(utils, "save_artwork_analysis", unavailable_cache)
    uncached, uncached_error = asyncio.run(
        utils.save_poster(
            content,
            tmp_path / "uncached-valid.jpg",
            config=config,
            provider="tmdb",
            source_path="/uncached-valid.jpg",
            expected_image={"width": 120, "height": 180},
        )
    )
    assert uncached is True
    assert uncached_error is None
    assert "Content analysis cache | Status: Degraded" in caplog.text


def test_content_analysis_is_persistent_and_affects_scoring(monkeypatch, tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    analysis = utils.analyze_image_content(_image_bytes())
    assert state_db.save_artwork_analysis(
        "tmdb", "/poster.jpg", analysis, path=database
    )
    restored = state_db.load_artwork_analysis("tmdb", "/poster.jpg", path=database)
    assert restored["content_sha256"] == analysis["content_sha256"]
    assert restored["perceptual_hash"] == analysis["perceptual_hash"]
    assert restored["sharpness"] > 0

    utils._ARTWORK_ANALYSIS_MEMORY.clear()
    monkeypatch.setattr(utils, "load_artwork_analysis", lambda *_args, **_kwargs: restored)
    config = {
        "poster_set": {"max_width": 1000, "max_height": 1500},
        "tmdb": {"fallback": ["en"]},
    }
    candidate = {
        "provider": "tmdb",
        "file_path": "/poster.jpg",
        "width": 800,
        "height": 1200,
        "vote_average": 7,
        "iso_639_1": "en",
    }
    score = utils.artwork_quality_score(
        config, candidate, preferred_language="en"
    )
    assert score["content"] > 0
    assert score["perceptual_hash"] == analysis["perceptual_hash"]

    blank = dict(candidate, file_path="/blank.jpg", content_analysis={**analysis, "blank": True})
    assert utils.artwork_quality_score(config, blank, preferred_language="en")["score"] == 0
    assert not utils.artwork_candidate_acceptable(config, blank, asset_type="poster")

    duplicate = dict(candidate, file_path="/duplicate.jpg", content_analysis=analysis)
    selected = dict(candidate, content_analysis=analysis)
    explanations = utils.artwork_candidate_explanations(
        config, [selected, duplicate], selected, preferred_language="en"
    )
    assert "visually duplicates the selected artwork" in explanations[0]["reasons"]


def test_missing_only_download_failover_stops_when_destination_exists(
    monkeypatch, tmp_path, caplog
):
    calls = []
    selected = []
    initial = {"provider": "tmdb", "file_path": "/first.jpg"}
    fanart = {"provider": "fanart", "file_path": "https://assets.fanart.tv/next.jpg"}

    async def fake_download(_config, _source, _temp, **kwargs):
        calls.append(kwargs["provider"])
        return (kwargs["provider"] == "fanart", 503, "provider unavailable")

    async def fake_select(*_args, excluded_providers=None, **_kwargs):
        selected.append(set(excluded_providers or []))
        return fanart

    monkeypatch.setattr(builder, "download_poster", fake_download)
    monkeypatch.setattr(builder, "_select_artwork_with_fallback", fake_select)
    destination = tmp_path / "poster.jpg"
    result = asyncio.run(
        builder._download_with_missing_failover(
            {},
            {},
            initial,
            tmp_path / "temporary.jpg",
            destination,
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
        )
    )
    assert result[0]["provider"] == "fanart"
    assert result[0]["selection_stage"] == "missing_only_download_failover"
    assert calls == ["tmdb", "fanart"]
    assert selected == [{"tmdb"}]

    with caplog.at_level(logging_module.logging.INFO):
        logging_module.log_item_outcomes(
            "Movies",
            "Example (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "downloaded",
                "artwork_providers": {"poster": "fanart"},
                "artwork_selection_stages": {
                    "poster": "missing_only_download_failover"
                },
            },
            {"mode": "kometa"},
        )
    assert "Selection: Missing-only download failover" in caplog.text

    calls.clear()
    selected.clear()
    destination.write_bytes(b"manual artwork")
    result = asyncio.run(
        builder._download_with_missing_failover(
            {},
            {},
            initial,
            tmp_path / "temporary.jpg",
            destination,
            [],
            asset_type="poster",
            media_type="movie",
            tmdb_id=1,
        )
    )
    assert result[1] is False
    assert calls == ["tmdb"]
    assert selected == []


def test_unresolved_ledger_only_resolves_after_successful_full_scan(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    problem = {
        "library": "Movies",
        "media_type": "Movie",
        "title": "Example (2024)",
        "asset_type": "poster",
        "category": "artwork_missing",
        "detail": "No provider candidate",
    }
    state_db.reconcile_unresolved_work([problem], path=database)
    repeated = state_db.reconcile_unresolved_work([problem], path=database)
    assert repeated[0]["status"] == "open"
    assert repeated[0]["occurrences"] == 2

    state_db.reconcile_unresolved_work([], resolved_libraries=[], path=database)
    assert state_db.load_unresolved_work(path=database)[0]["status"] == "open"

    state_db.reconcile_unresolved_work(
        [], resolved_libraries=["Movies"], path=database
    )
    resolved = state_db.load_unresolved_work(path=database)[0]
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"]


def test_unresolved_ledger_resolves_only_work_evaluated_by_full_scan(tmp_path):
    database = tmp_path / "meta_db.sqlite3"
    common = {
        "library": "Movies",
        "media_type": "Movie",
        "title": "Example (2024)",
        "category": "artwork_missing",
        "detail": "No provider candidate",
    }
    state_db.reconcile_unresolved_work(
        [
            {**common, "asset_type": "poster"},
            {**common, "asset_type": "background"},
        ],
        path=database,
    )

    state_db.reconcile_unresolved_work(
        [], resolved_work={"Movies": ["poster"]}, path=database
    )

    records = {
        row["asset_type"]: row
        for row in state_db.load_unresolved_work(path=database)
    }
    assert records["poster"]["status"] == "resolved"
    assert records["background"]["status"] == "open"


def test_managed_destination_reconciliation_is_checksum_and_root_bounded(tmp_path):
    kometa = tmp_path / "kometa"
    assets = kometa / "assets" / "movie" / "Example"
    assets.mkdir(parents=True)
    old = assets / "old-poster.jpg"
    current = assets / "poster.jpg"
    old.write_bytes(b"owned old artwork")
    current.write_bytes(b"current artwork")
    cache = {
        "movie:1": {
            "title": "Example",
            "year": 2024,
            "poster_checksum": sha256_file(current),
            "destination_history": [
                {
                    "asset_type": "poster",
                    "previous_destination": str(old),
                    "new_destination": str(current),
                    "previous_checksum": sha256_file(old),
                }
            ],
        }
    }
    config = {
        "settings": {"mode": "kometa", "path": str(kometa)},
        "assets": {"update_policy": "managed"},
    }
    report = write_destination_history_report(
        cache, base_dir=tmp_path / "config", config=config
    )
    assert report.is_file()
    assert report.with_suffix(".json").is_file()
    assert not old.exists()
    assert cache["movie:1"]["destination_history"][0]["reconciliation_status"] == "removed"

    modified = assets / "modified.jpg"
    modified.write_bytes(b"manual replacement")
    modified_cache = {
        "movie:2": {
            "title": "Protected",
            "year": 2024,
            "poster_checksum": sha256_file(current),
            "destination_history": [
                {
                    "asset_type": "poster",
                    "previous_destination": str(modified),
                    "new_destination": str(current),
                    "previous_checksum": "0" * 64,
                }
            ],
        }
    }
    write_destination_history_report(
        modified_cache, base_dir=tmp_path / "config", config=config
    )
    assert modified.exists()
    assert (
        modified_cache["movie:2"]["destination_history"][0]["reconciliation_status"]
        == "preserved"
    )

    shared = assets / "shared.jpg"
    replacement = assets / "replacement.jpg"
    shared.write_bytes(b"shared managed artwork")
    replacement.write_bytes(b"replacement artwork")
    shared_cache = {
        "movie:3": {
            "title": "Moved owner",
            "poster_path": str(replacement),
            "poster_checksum": sha256_file(replacement),
            "destination_history": [
                {
                    "asset_type": "poster",
                    "previous_destination": str(shared),
                    "new_destination": str(replacement),
                    "previous_checksum": sha256_file(shared),
                }
            ],
        },
        "movie:4": {
            "title": "Remaining owner",
            "poster_path": str(shared),
            "poster_checksum": sha256_file(shared),
        },
    }
    write_destination_history_report(
        shared_cache, base_dir=tmp_path / "config", config=config
    )
    assert shared.exists()
    assert (
        shared_cache["movie:3"]["destination_history"][0][
            "reconciliation_status"
        ]
        == "preserved"
    )


def test_post_application_adoption_audit_verifies_installed_bytes(monkeypatch, tmp_path):
    artwork = tmp_path / "poster.jpg"
    artwork.write_bytes(b"installed artwork")
    checksum = sha256_file(artwork)

    class Registry:
        def mark_verified(self, *_args, **_kwargs):
            return checksum

    monkeypatch.setattr(builder, "_asset_registry", lambda _config: Registry())
    config = {
        "settings": {"mode": "plex"},
        "_library_name": "Movies",
        "_adoption_audit_records": [],
    }
    builder._mark_asset_verified(
        config,
        "movie:1",
        artwork,
        media_type="movie",
        tmdb_id=1,
        asset_type="poster",
        source_path="/poster.jpg",
        full_title="Example (2024)",
        provider="tmdb",
    )
    record = config["_adoption_audit_records"][0]
    assert record["status"] == "filesystem_verified"
    assert record["plex_visibility"] == "pending_normal_plex_discovery"
    report = write_adoption_audit_report(
        config["_adoption_audit_records"], base_dir=tmp_path / "config"
    )
    assert report.with_suffix(".json").is_file()


def test_diagnostic_json_companions_are_retained_as_pairs(tmp_path):
    report_dir = tmp_path / "reports"
    old = report_dir / "sample-old.txt"
    current = report_dir / "sample-20260819-120000.txt"
    orphan = report_dir / "sample-orphan.json"
    write_diagnostic_report(old, "old\n", report_type="sample", data={"value": 1})
    write_diagnostic_report(
        current, "current\n", report_type="sample", data={"value": 2}
    )
    orphan.write_text("{}", encoding="utf-8")
    retain_diagnostic_reports(report_dir, "sample", 1)
    assert current.is_file()
    assert current.with_suffix(".json").is_file()
    assert not old.exists()
    assert not old.with_suffix(".json").exists()
    assert not orphan.exists()

    gap_report = write_artwork_gap_report(
        [
            {
                "library": "Movies",
                "media_type": "Movie",
                "title": "Example",
                "asset_type": "poster",
                "category": "artwork_missing",
            }
        ],
        base_dir=tmp_path / "config",
    )
    payload = json.loads(gap_report.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["report_type"] == "artwork_gaps"
    assert payload["data"]["entries"][0]["title"] == "Example"


def test_sanitized_replay_capture_removes_secrets_paths_and_rating_keys(tmp_path):
    report = write_sanitized_replay_capture(
        [
            {
                "rating_key": "12345",
                "server_id": "private-machine-id",
                "library_uuid": "private-library-id",
                "token": "private-token",
                "destination": "/mnt/media/Movies/Example/poster.jpg",
                "file_path": "/mnt/media/Movies/Example/fanart.jpg",
                "provider_url": "https://private.plex.local/library/metadata/12345?token=secret",
                "title": "Example",
            }
        ],
        base_dir=tmp_path / "config",
    )
    payload = json.loads(report.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert "private-token" not in serialized
    assert "/mnt/media" not in serialized
    assert "private.plex.local" not in serialized
    assert "private-machine-id" not in serialized
    assert "private-library-id" not in serialized
    assert "/metadata/12345" not in serialized
    assert payload["data"]["items"][0]["rating_key"].startswith("replay-")
    assert provider_replay_issues(payload) == []


def test_unresolved_problem_and_replay_cli_paths_are_read_only(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(
        metafusion,
        "load_unresolved_work",
        lambda **_kwargs: [{"status": "open", "title": "Example"}],
    )
    assert metafusion.main(["--problems"]) == 0
    problem_output = json.loads(capsys.readouterr().out)
    assert problem_output["open"] == 1
    assert problem_output["items"][0]["title"] == "Example"

    assert metafusion.main(["--capture-replay"]) == 2
    assert "requires --rating-key" in capsys.readouterr().err
    assert (
        metafusion.main(
            ["--capture-replay", "--rating-key", "10", "--metafusion_run"]
        )
        == 2
    )
    assert "standalone diagnostic" in capsys.readouterr().err

    config = {
        "settings": {"mode": "kometa", "dry_run": False},
        "metadata": {},
        "assets": {},
        "cleanup": {},
        "plex": {"token": "secret-plex"},
        "tmdb": {"api_key": "secret-tmdb"},
        "output": {"report_retention": 2},
        "plex_libraries": [],
    }
    monkeypatch.setattr(metafusion, "load_config_file", lambda **_kwargs: (config, {}))
    monkeypatch.setattr(metafusion, "validate_config", lambda _config: [])
    monkeypatch.setattr(metafusion, "validate_preflight_paths", lambda *_args: None)
    monkeypatch.setattr(metafusion.tmdb_response_cache, "reset_memory", lambda: None)

    async def capture(_config, rating_keys, *, write_report):
        assert rating_keys == ["10"]
        assert write_report is False
        return [{"status": "accepted"}], None

    destination = tmp_path / "provider-replay.json"
    monkeypatch.setattr(metafusion, "item_explanation_connectors", capture)
    monkeypatch.setattr(
        metafusion,
        "write_sanitized_replay_capture",
        lambda *_args, **_kwargs: destination,
    )
    assert metafusion.main(["--capture-replay", "--rating-key", "10"]) == 0
    output = capsys.readouterr().out
    assert "captured for 1 item" in output
    assert str(destination) in output


def test_log_labels_and_sections_reject_joined_legacy_names():
    fields = logging_module.format_fields(
        ("RAM used", "12.39 GB"),
        ("CPU cores", 28),
        ("Server version", "1.0"),
        ("Dry run", False),
    )
    assert fields == (
        "RAM used: 12.39 GB | CPU cores: 28 | Server version: 1.0 | "
        "Dry run: Disabled"
    )
    for component, title in (
        ("Startup", "M E T A F U S I O N"),
        ("System", "Runtime environment"),
        ("Configuration", "Effective run configuration"),
        ("Processing", "Library processing"),
        ("Summary", "Final run summary"),
    ):
        assert logging_module.log_section(None, component, title) == (
            f"[{component}] ── {title} ──"
        )

    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            logging_module.Path(logging_module.__file__),
            logging_module.Path(concurrency_module.__file__),
            logging_module.Path(builder.__file__),
            logging_module.Path(processing.__file__),
            logging_module.Path(metafusion.__file__),
        )
    )
    joined = re.compile(
        r"\b(?:RAMUsed|RAMTotal|RAMFree|CPUCores|CPUUsage|StartedAt|"
        r"ServerVersion|FieldCoverage|MissingFields|APIBatches|LibraryCount)\b"
    )
    assert joined.search(sources) is None
