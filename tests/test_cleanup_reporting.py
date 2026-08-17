from helper.logging import log_final_summary
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
        CleanupResult(titles=0, seasons=1, episodes=3, assets=1)
    )

    assert "Cleanup - Removed: Titles: 0, Seasons: 1, Episodes: 3, Assets: 1" in report


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
        CleanupResult(titles=1, seasons=2, episodes=3, assets=4, dry_run=True),
        dry_run=True,
    )

    assert "Cleanup - Would remove: Titles: 1, Seasons: 2" in report


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
