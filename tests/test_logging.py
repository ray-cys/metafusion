import logging

from helper.logging import (
    PlexMetadataProgress,
    _format_event_message,
    _metadata_change_details,
    artwork_schedule_line,
    format_fields,
    get_meta_banner,
    log_builder_event,
    log_fanart_event,
    log_item_outcomes,
    log_processing_event,
    log_tmdb_event,
    metadata_action_summary,
    plex_progress_item_interval,
    schedule_reconciliation_warning,
)


def test_unchanged_metadata_log_labels_completeness_percentages(caplog):
    with caplog.at_level(logging.DEBUG):
        log_builder_event(
            "builder_no_metadata_changes",
            media_type="Movie",
            full_title="1917 (2019)",
            percent=100,
            incomplete_percent=0,
        )

    assert (
        "[Metadata] Kometa | Movie | 1917 (2019) | Unchanged | "
        "Field coverage: 100% | Missing fields: 0%"
        in caplog.text
    )
    assert "(100%/0%) completed" not in caplog.text


def test_item_outcome_visibility_separates_changes_from_field_coverage(caplog):
    flags = {"mode": "kometa", "plex_metadata": False}
    with caplog.at_level(logging.INFO):
        log_item_outcomes(
            "Movies",
            "Changed and complete (2024)",
            {
                "metadata_action": "upgraded",
                "metadata_changes": ["['summary']", "['producer'][0]"],
                "percent": 100,
                "incomplete_percent": 0,
                "is_complete": True,
            },
            flags,
        )
        log_item_outcomes(
            "TV Shows",
            "Incomplete but stable (2024)",
            {
                "metadata_action": "skipped",
                "percent": 79,
                "incomplete_percent": 21,
                "is_complete": True,
            },
            flags,
        )
        log_item_outcomes(
            "TV Shows",
            "Complete and stable (2024)",
            {
                "metadata_action": "skipped",
                "percent": 100,
                "incomplete_percent": 0,
                "is_complete": True,
            },
            flags,
        )

    assert "Changed and complete (2024) | Updated" in caplog.text
    assert "Changed fields: summary, producer | Field changes: 2" in caplog.text
    assert "Field coverage: 100% | Missing fields: 0%" in caplog.text
    assert "Incomplete but stable (2024) | Unchanged" in caplog.text
    assert "Status: Incomplete but unchanged" in caplog.text
    assert "Complete and stable (2024)" not in caplog.text


def test_metadata_change_details_are_bounded_and_collapse_nested_fields():
    details = _metadata_change_details(
        [
            "plain fallback",
            "['seasons']['1']['episodes']['2']['summary']",
            "['seasons']['1']['episodes']['3']['summary']",
            "['seasons']['1']['title']",
            "['studio']",
            "['tagline']",
        ],
        limit=4,
    )

    assert "plain fallback" in details
    assert "episode summary" in details
    assert "season title" in details
    assert "+1 more" in details
    assert "Field changes: 6" in details


def test_builder_asset_details_are_debug_to_avoid_duplicate_item_outcomes(caplog):
    with caplog.at_level(logging.INFO):
        log_builder_event(
            "builder_downloading_asset",
            media_type="Movie",
            asset_type="poster",
            full_title="Example (2024)",
            filesize=2048,
        )
        log_builder_event(
            "builder_already_up_to_date",
            media_type="Movie",
            asset_type="poster",
            full_title="Unchanged (2024)",
            filesize=2048,
        )

    assert "Example (2024)" not in caplog.text


def test_split_series_policy_outcomes_distinguish_present_from_missing(caplog):
    with caplog.at_level(logging.DEBUG):
        log_item_outcomes(
            "TV Shows",
            "Split Show (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "policy_preserved",
                "background_action": "policy_missing",
            },
            {"mode": "kometa"},
        )

    assert "Poster policy preserved | Source: Existing" in caplog.text
    assert "Background policy-preserved missing | Source: None" in caplog.text
    assert "Unchanged (2024)" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        log_builder_event(
            "builder_downloading_asset",
            media_type="Movie",
            asset_type="poster",
            full_title="Example (2024)",
            filesize=2048,
        )
        log_builder_event(
            "builder_already_up_to_date",
            media_type="Movie",
            asset_type="poster",
            full_title="Unchanged (2024)",
            filesize=2048,
        )
    assert "Downloaded poster" in caplog.text
    assert "Unchanged (2024) | Unchanged poster" in caplog.text


def test_successful_kometa_yaml_write_is_info(caplog):
    with caplog.at_level(logging.INFO):
        log_processing_event(
            "processing_metadata_saved",
            library_name="Movies",
            output_path="/config/metadata/movie.yml",
            changed_items=2,
            normalized_entries=0,
        )

    assert "[Metadata] Movies | Saved Kometa YAML" in caplog.text
    assert "Changed items: 2 | Normalized entries: 0" in caplog.text


def test_provider_cache_statistics_share_one_debug_format(caplog):
    statistics = {
        "entries": 10,
        "stored_mib": 2.5,
        "disk_mib": 3.0,
        "hits": 7,
        "misses": 3,
        "evictions": 1,
        "recoveries": 0,
    }

    with caplog.at_level(logging.DEBUG):
        log_tmdb_event("tmdb_cache_stats", **statistics)
        log_fanart_event("fanart_cache_stats", **statistics)

    assert "[Cache] Provider: TMDb | Entries: 10" in caplog.text
    assert "[Cache] Provider: Fanart.tv | Entries: 10" in caplog.text
    assert "Compressed: 2.5 MiB | Disk: 3.0 MiB" in caplog.text


def test_routine_identity_and_mapping_outcomes_are_debug(caplog):
    with caplog.at_level(logging.INFO):
        log_builder_event(
            "builder_tmdb_identity_alias",
            media_type="TV Show",
            full_title="Example (2024)",
            reason="trusted external ID",
        )
        log_builder_event(
            "builder_split_series_mapping",
            media_type="TV Show",
            full_title="Example (2024)",
            seasons="1, 2",
        )
    assert "Example (2024)" not in caplog.text

    with caplog.at_level(logging.DEBUG):
        log_builder_event(
            "builder_tmdb_identity_alias",
            media_type="TV Show",
            full_title="Example (2024)",
            reason="trusted external ID",
        )
    assert "[Identity] TV Show | Example (2024) | Accepted alias" in caplog.text


def test_artwork_failures_always_use_artwork_component(caplog):
    with caplog.at_level(logging.ERROR):
        log_builder_event(
            "builder_asset_destination_collision",
            media_type="Movie",
            full_title="Example (2024)",
            asset_type="poster",
            destination="/assets/poster.jpg",
            owner="movie:2",
        )

    assert "[Artwork] Movie | Example (2024)" in caplog.text
    assert "Refused poster destination collision" in caplog.text


def test_metadata_summaries_are_mode_specific():
    counts = {
        "meta_downloaded": 2,
        "meta_upgraded": 3,
        "meta_skipped": 4,
        "meta_failed": 1,
        "plex_metadata_writes": 5,
    }

    assert metadata_action_summary(
        counts, {"metadata_basic": True, "plex_metadata": False}
    ) == (
        "Metadata result | Target: Kometa YAML | Created: 2 | Updated: 3 | "
        "Unchanged: 4 | Failed: 1"
    )
    assert metadata_action_summary(
        counts, {"metadata_basic": True, "plex_metadata": True}
    ) == (
        "Metadata result | Target: Plex | Changed: 3 | API batches: 5 | "
        "Unchanged: 4 | Failed: 1"
    )


def test_schedule_reconciliation_and_unknown_season_inventory_are_explicit():
    counts = {
        "metadata_schedule_destinations": 5,
        "metadata_schedule_due": 1,
        "metadata_schedule_required": 1,
        "metadata_schedule_forced": 1,
        "metadata_schedule_not_due": 2,
        "season_poster_schedule_destinations": 3,
        "season_poster_schedule_due": 0,
        "season_poster_schedule_required": 1,
        "season_poster_schedule_forced": 0,
        "season_poster_schedule_not_due": 2,
        "season_poster_schedule_inventory_unknown": 1,
    }

    assert schedule_reconciliation_warning(
        counts, "metadata", "Movies", "Metadata"
    ) is None
    counts["metadata_schedule_not_due"] = 1
    warning = schedule_reconciliation_warning(
        counts, "metadata", "Movies", "Metadata"
    )
    assert "Status: Mismatch" in warning
    assert "Destinations: 5 | Accounted: 4 | Difference: 1" in warning
    assert artwork_schedule_line(counts, "season_poster", "Season poster").endswith(
        "Season inventories unavailable: 1"
    )


def test_plex_progress_intervals_adapt_to_library_size():
    assert plex_progress_item_interval(50) == 5
    assert plex_progress_item_interval(100) == 10
    assert plex_progress_item_interval(101) == 25
    assert plex_progress_item_interval(500) == 25
    assert plex_progress_item_interval(1000) == 50
    assert plex_progress_item_interval(1001) == 100
    assert plex_progress_item_interval(2000) == 100
    assert plex_progress_item_interval(5000) == 250


def test_plex_progress_rate_limits_items_and_emits_heartbeat_and_final():
    class CaptureLogger:
        def __init__(self):
            self.lines = []

        def info(self, message, *args):
            self.lines.append(message % args)

    now = [0.0]
    logger = CaptureLogger()
    progress = PlexMetadataProgress(
        "Movies", 50, logger=logger, clock=lambda: now[0]
    )

    assert progress.start() is True
    now[0] = 10
    assert progress.update(
        5, changed=1, api_batches=1, unchanged=4, failed=0
    ) is False
    now[0] = 31
    assert progress.update(
        5, changed=1, api_batches=1, unchanged=4, failed=0
    ) is True
    now[0] = 92
    assert progress.update(
        5, changed=1, api_batches=1, unchanged=4, failed=0
    ) is True
    now[0] = 93
    assert progress.update(
        50,
        changed=2,
        api_batches=3,
        unchanged=47,
        failed=1,
        force=True,
    ) is True

    assert "Movies | Checked: 0/50 (0.0%)" in logger.lines[0]
    assert "Movies | Checked: 5/50 (10.0%)" in logger.lines[1]
    assert "Movies | Checked: 5/50 (10.0%)" in logger.lines[2]
    assert "Movies | Checked: 50/50 (100.0%)" in logger.lines[3]
    assert (
        "Changed: 2 | API batches: 3 | Unchanged: 47 | Failed: 1"
        in logger.lines[3]
    )


def test_banner_supports_logger_and_console_and_malformed_events_are_safe(capsys):
    class CaptureLogger:
        def __init__(self):
            self.lines = []
            self.debug_lines = []

        def info(self, line):
            self.lines.append(line)

        def debug(self, message, *args):
            self.debug_lines.append(message % args)

    logger = CaptureLogger()
    get_meta_banner(logger)
    assert logger.lines == ["[Startup] ── M E T A F U S I O N ──"]

    get_meta_banner()
    assert "M E T A F U S I O N" in capsys.readouterr().out

    template = "Missing {required}"
    assert _format_event_message(template, {}, logger, "Test") == template
    assert "Unable to format log event" in logger.debug_lines[0]


def test_shared_field_formatter_uses_human_readable_labels():
    assert format_fields(
        ("RAM used", "12.39 GB"),
        ("CPU cores", 28),
        ("Dry run", False),
    ) == "RAM used: 12.39 GB | CPU cores: 28 | Dry run: Disabled"
