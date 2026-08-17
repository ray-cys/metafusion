import logging

from helper.logging import (
    log_builder_event,
    log_processing_event,
    metadata_action_summary,
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
