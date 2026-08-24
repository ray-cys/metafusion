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
    metadata_coverage_line,
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
            "First incomplete observation (2024)",
            {
                "metadata_action": "skipped",
                "percent": 79,
                "incomplete_percent": 21,
                "is_complete": True,
                "metadata_coverage_transition": "first_incomplete",
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
                "metadata_coverage_transition": "unchanged",
                "metadata_previous_percent": 79,
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
    assert "First incomplete observation (2024) | Unchanged" in caplog.text
    assert "Status: First incomplete observation" in caplog.text
    assert "Incomplete but stable (2024)" not in caplog.text
    assert "Complete and stable (2024)" not in caplog.text


def test_metadata_coverage_transitions_choose_action_driven_levels(caplog):
    flags = {"mode": "kometa", "plex_metadata": False}
    with caplog.at_level(logging.DEBUG):
        log_item_outcomes(
            "Movies",
            "Stable incomplete (2024)",
            {
                "metadata_action": "skipped",
                "percent": 75,
                "incomplete_percent": 25,
                "metadata_previous_percent": 75,
                "metadata_coverage_transition": "unchanged",
            },
            flags,
        )
        log_item_outcomes(
            "Movies",
            "Improved (2024)",
            {
                "metadata_action": "skipped",
                "percent": 90,
                "incomplete_percent": 10,
                "metadata_previous_percent": 75,
                "metadata_coverage_transition": "improved",
            },
            flags,
        )
        log_item_outcomes(
            "Movies",
            "Regressed (2024)",
            {
                "metadata_action": "upgraded",
                "percent": 70,
                "incomplete_percent": 30,
                "metadata_previous_percent": 90,
                "metadata_coverage_transition": "regressed",
            },
            flags,
        )
        log_item_outcomes(
            "Movies",
            "Malformed history (2024)",
            {
                "metadata_action": "skipped",
                "percent": 80,
                "incomplete_percent": 20,
                "metadata_previous_percent": "invalid",
                "metadata_coverage_transition": "improved",
            },
            flags,
        )

    assert "Stable incomplete (2024) | Unchanged" in caplog.text
    assert "Improved (2024) | Unchanged" in caplog.text
    assert "Coverage change: 75% → 90%" in caplog.text
    assert "Regressed (2024) | Updated" in caplog.text
    assert "Coverage change: 90% → 70%" in caplog.text
    assert "Status: Field coverage regressed" in caplog.text
    assert "Malformed history (2024) | Unchanged" in caplog.text
    regression = next(
        record for record in caplog.records if "Regressed (2024)" in record.message
    )
    assert regression.levelno == logging.WARNING


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


def test_season_outcomes_name_meaningful_seasons_and_count_quiet_states(caplog):
    actions = {
        1: "downloaded",
        2: "upgraded",
        3: "adopted",
        4: "skipped",
        5: "not_due",
        6: "preserved",
        7: "policy_preserved",
        8: "policy_missing",
        9: "missing",
        10: "deferred",
        11: "failed",
    }
    with caplog.at_level(logging.INFO):
        log_item_outcomes(
            "TV Shows",
            "Traceable Seasons (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "not_due",
                "background_action": "not_due",
                "season_poster_actions": actions,
                "season_artwork_providers": {
                    number: "tmdb" for number in actions if number != 5
                },
            },
            {"mode": "kometa"},
        )

    text = caplog.text
    assert "Downloaded: 1 [S01]" in text
    assert "Upgraded: 1 [S02]" in text
    assert "Adopted: 1 [S03]" in text
    assert "Unchanged: 1" in text
    assert "Unchanged: 1 [" not in text
    assert "Not due: 1" in text
    assert "Not due: 1 [" not in text
    assert "Preserved: 1 [S06]" in text
    assert "Policy preserved: 1" in text
    assert "Policy preserved: 1 [" not in text
    assert "Policy-preserved missing: 1 [S08]" in text
    assert "Missing: 1 [S09]" in text
    assert "Deferred: 1 [S10]" in text
    assert "Failed: 1 [S11]" in text


def test_all_not_due_seasons_remain_summary_only(caplog):
    with caplog.at_level(logging.DEBUG):
        log_item_outcomes(
            "TV Shows",
            "Quiet Seasons (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "not_due",
                "background_action": "not_due",
                "season_poster_actions": {1: "not_due", 2: "not_due"},
            },
            {"mode": "kometa"},
        )

    assert "Quiet Seasons (2024)" not in caplog.text


def test_season_references_are_bounded(caplog):
    with caplog.at_level(logging.INFO):
        log_item_outcomes(
            "TV Shows",
            "Long Runner (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "not_due",
                "background_action": "not_due",
                "season_poster_actions": dict.fromkeys(
                    range(1, 11), "downloaded"
                ),
                "season_artwork_providers": dict.fromkeys(range(1, 11), "tmdb"),
            },
            {"mode": "plex"},
        )

    assert (
        "Downloaded: 10 [S01, S02, S03, S04, S05, S06, S07, S08, +2 more]"
        in caplog.text
    )


def test_season_references_preserve_non_numeric_provider_labels(caplog):
    with caplog.at_level(logging.INFO):
        log_item_outcomes(
            "TV Shows",
            "Provider Label (2024)",
            {
                "metadata_action": "not_due",
                "poster_action": "not_due",
                "background_action": "not_due",
                "season_poster_actions": {"bonus": "downloaded"},
                "season_artwork_providers": {"bonus": "tmdb"},
            },
            {"mode": "kometa"},
        )

    assert "Downloaded: 1 [bonus]" in caplog.text


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
    assert metadata_coverage_line(
        {
            "metadata_complete": 8,
            "metadata_incomplete": 2,
            "metadata_coverage_improved": 3,
            "metadata_coverage_regressed": 1,
            "metadata_coverage_first_incomplete": 2,
        }
    ) == (
        "Metadata coverage | Evaluated: 10 | Meets threshold: 8 (80%) | "
        "Below threshold: 2 (20%) | Improved: 3 | Regressed: 1 | "
        "First incomplete: 2"
    )
    assert "Evaluated: 0 | Meets threshold: 0 (100%)" in metadata_coverage_line({})
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
