import logging

from helper.logging import log_builder_event


def test_unchanged_metadata_log_labels_completeness_percentages(caplog):
    with caplog.at_level(logging.INFO):
        log_builder_event(
            "builder_no_metadata_changes",
            media_type="Movie",
            full_title="1917 (2019)",
            percent=100,
            incomplete_percent=0,
        )

    assert (
        "No metadata changes detected: 1917 (2019). "
        "Metadata completeness: 100% present, 0% missing. Skipping updates..."
        in caplog.text
    )
    assert "(100%/0%) completed" not in caplog.text
