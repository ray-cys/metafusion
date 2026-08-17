import logging

from helper.logging import (
    PlexMetadataProgress,
    log_builder_event,
    log_processing_event,
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
        "[Kometa Metadata] No changes for Movie: 1917 (2019). "
        "Completeness: 100% present, 0% missing."
        in caplog.text
    )
    assert "(100%/0%) completed" not in caplog.text


def test_successful_asset_mutations_are_info_and_noops_are_debug(caplog):
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

    assert "Downloading TMDb poster: Example (2024)" in caplog.text
    assert "Unchanged (2024)" not in caplog.text

    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        log_builder_event(
            "builder_already_up_to_date",
            media_type="Movie",
            asset_type="poster",
            full_title="Unchanged (2024)",
            filesize=2048,
        )
    assert "No poster changes detected: Unchanged (2024)" in caplog.text


def test_successful_kometa_yaml_write_is_info(caplog):
    with caplog.at_level(logging.INFO):
        log_processing_event(
            "processing_metadata_saved", output_path="/config/metadata/movie.yml"
        )

    assert "YAML successfully saved" in caplog.text


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
        "Kometa Metadata - Created: 2, Updated: 3, Unchanged: 4, Failed: 1"
    )
    assert metadata_action_summary(
        counts, {"metadata_basic": True, "plex_metadata": True}
    ) == (
        "Plex Metadata - Items changed: 3, API batches: 5, "
        "Unchanged: 4, Failed: 1"
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

    assert "Movies: 0/50 checked (0.0%)" in logger.lines[0]
    assert "Movies: 5/50 checked (10.0%)" in logger.lines[1]
    assert "Movies: 5/50 checked (10.0%)" in logger.lines[2]
    assert "Movies: 50/50 checked (100.0%)" in logger.lines[3]
    assert "items changed: 2, API batches: 3, unchanged: 47, failed: 1" in logger.lines[3]
