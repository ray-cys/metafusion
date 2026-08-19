import logging

from helper.logging import (
    PlexMetadataProgress,
    _format_event_message,
    get_meta_banner,
    log_builder_event,
    log_fanart_event,
    log_processing_event,
    log_tmdb_event,
    metadata_action_summary,
    plex_progress_item_interval,
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
        "FieldCoverage=100% | MissingFields=0%"
        in caplog.text
    )
    assert "(100%/0%) completed" not in caplog.text


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
    assert "ChangedItems=2 | NormalizedEntries=0" in caplog.text


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

    assert "[Cache] Provider=TMDb | Entries=10" in caplog.text
    assert "[Cache] Provider=Fanart.tv | Entries=10" in caplog.text
    assert "Compressed=2.5 MiB | Disk=3.0 MiB" in caplog.text


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
        "Metadata | Target=Kometa YAML | Created=2 | Updated=3 | "
        "Unchanged=4 | Failed=1"
    )
    assert metadata_action_summary(
        counts, {"metadata_basic": True, "plex_metadata": True}
    ) == (
        "Metadata | Target=Plex | Changed=3 | APIBatches=5 | "
        "Unchanged=4 | Failed=1"
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

    assert "Movies | Checked=0/50 (0.0%)" in logger.lines[0]
    assert "Movies | Checked=5/50 (10.0%)" in logger.lines[1]
    assert "Movies | Checked=5/50 (10.0%)" in logger.lines[2]
    assert "Movies | Checked=50/50 (100.0%)" in logger.lines[3]
    assert "Changed=2 | APIBatches=3 | Unchanged=47 | Failed=1" in logger.lines[3]


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
    assert logger.lines == ["[Startup] M E T A F U S I O N"]

    get_meta_banner()
    assert "M E T A F U S I O N" in capsys.readouterr().out

    template = "Missing {required}"
    assert _format_event_message(template, {}, logger, "Test") == template
    assert "Unable to format log event" in logger.debug_lines[0]
