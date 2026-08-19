import logging

from helper.logging import log_cleanup_event, log_final_summary
from modules.cleanup import CleanupResult


class CaptureLogger:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(message)


def render_summary(result, *, cleanup_enabled=True, dry_run=False):
    logger = CaptureLogger()
    log_final_summary(
        logger,
        1,
        {},
        {},
        result,
        None,
        [],
        [],
        {"settings": {"dry_run": dry_run}},
        {"cleanup": cleanup_enabled},
    )
    return "\n".join(logger.lines)


def test_cleanup_summary_reports_each_removed_scope():
    report = render_summary(
        CleanupResult(
            titles=0,
            seasons=1,
            episodes=3,
            assets=1,
            cache_entries=2,
            yaml_entries=4,
            assets_preserved=5,
            assets_skipped=6,
        )
    )

    assert "Cleanup - Completed | Mode: Kometa" in report
    assert "Cleanup stale inventory - Titles: 0, Seasons: 1, Episodes: 3" in report
    assert "Cleanup records - Removed: Cache: 2, Kometa YAML: 4" in report
    assert "Cleanup artwork - Removed: 1, Preserved: 5, Unchanged: 6" in report
    assert "Cleanup failures - Total: 0" in report


def test_cleanup_summary_reports_skipped_incremental_run():
    report = render_summary(
        CleanupResult(
            skipped_reason="incremental run; full reconciliation required"
        ),
        cleanup_enabled=False,
    )

    assert (
        "Cleanup - Skipped (incremental run; full reconciliation required)"
        in report
    )


def test_cleanup_summary_labels_dry_run_counts_as_proposed():
    report = render_summary(
        CleanupResult(
            titles=1,
            seasons=2,
            episodes=3,
            assets=4,
            cache_entries=5,
            yaml_entries=6,
            dry_run=True,
        ),
        dry_run=True,
    )

    assert "Cleanup - Preview | Mode: Kometa" in report
    assert "Cleanup records - Would remove: Cache: 5, Kometa YAML: 6" in report
    assert "Cleanup artwork - Would remove: 4" in report


def test_cleanup_summary_labels_plex_state_only_scope():
    report = render_summary(
        CleanupResult(titles=2, cache_entries=2, mode="plex")
    )

    assert "Cleanup - Completed | Mode: Plex" in report
    assert "Scope: State only; Kometa YAML" in report
    assert "artwork preserved" in report
    assert "Cleanup records - Removed: Cache: 2, Kometa YAML: 0" in report


def test_cleanup_summary_retains_confirmed_results_after_failure():
    report = render_summary(
        CleanupResult(
            titles=1,
            assets=2,
            cache_entries=1,
            yaml_entries=1,
            assets_preserved=3,
            failures=1,
            failed_reason="managed poster could not be removed",
        )
    )

    assert "Cleanup - Failed | Mode: Kometa" in report
    assert "Cleanup confirmed before failure - Titles: 1" in report
    assert "Cleanup records - Cache: 1, Kometa YAML: 1" in report
    assert "Cleanup artwork - Removed: 2, Preserved: 3" in report
    assert "Cleanup failures - Total: 1" in report


def test_cleanup_item_outcome_matches_component_action_format(caplog):
    logger = logging.getLogger("cleanup-item-outcome")

    with caplog.at_level(logging.INFO, logger=logger.name):
        log_cleanup_event(
            "cleanup_consolidated_removed",
            logger=logger,
            removed_summary={
                ("Example", 2024): {
                    "cache": True,
                    "yaml": True,
                    "asset": ["poster"],
                }
            },
        )

    assert (
        "[Cleanup] Inventory | Example (2024) | Removed cache entry, "
        "Kometa YAML entry, managed poster | "
        "Reason=Not present in complete Plex inventory"
    ) in caplog.text


def test_final_summary_reports_season_failures_without_other_season_actions():
    logger = CaptureLogger()
    log_final_summary(
        logger,
        1,
        {
            "Shows": {
                "complete": 0,
                "incomplete": 1,
                "total_items": 1,
                "percent_complete": 0,
                "percent_incomplete": 100,
                "library_type": "tv",
                "library_summary": {"season_poster_failed": 1},
            }
        },
        {"Shows": 0},
        None,
        None,
        ["Shows"],
        [{"title": "Shows"}],
        {"settings": {"dry_run": False}},
        {"season": True},
    )

    report = "\n".join(logger.lines)
    assert "Season - Downloaded: 0, Upgraded: 0, Adopted: 0" in report
    assert "Failed: 1" in report
