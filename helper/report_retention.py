"""Restrict deletion to regular, timestamped MetaFusion report files."""

import re
from collections import defaultdict

RECORDED_REPORTS = ("artwork-gaps", "adoption-audit", "destination-history", "plex-metadata")
AUDITS = {"plex-artwork-verification", "kometa-application-audit"}
REPORT_TYPES = (
    AUDITS
    | set(RECORDED_REPORTS)
    | {
        "asset-audit",
        "change-plan",
        "cleanup-history",
        "cleanup-quarantine",
        "compatibility",
        "configuration-impact",
        "identity-inspection",
        "identity-review",
        "item-explanation",
        "library-asset-audit",
        "library-rebinding",
        "mapping-diagnosis",
        "metadata-audit",
        "metafusion-dashboard",
        "output-management",
        "provider-replay-capture",
        "release-qualification",
        "run-history",
        "schedule-advice",
        "state-report",
        "support-report",
        "unresolved-work",
        "upgrade-canary",
    }
)
NAME = re.compile(r"([a-z-]+)-(\d{8}-\d{6}(?:\d{6})?)\.(txt|json|html)")


def trim_reports(folder, keep, receipts, *, stem=None):
    if folder.is_symlink():
        raise OSError("Refusing to sweep a symbolic-link reports directory")
    groups = defaultdict(lambda: defaultdict(list))
    for file in folder.glob(f"{stem}-*" if stem else "*"):
        match = NAME.fullmatch(file.name)
        if (
            match
            and match[1] in REPORT_TYPES
            and (stem is None or match[1] == stem)
            and not file.is_symlink()
            and file.is_file()
            and (match[3] != "html" or match[1] == "metafusion-dashboard")
        ):
            groups[match[1]][match[2]].append(file)
    removed = 0
    for kind, timestamps in groups.items():
        for stamp in sorted(timestamps, reverse=True)[max(1, keep) :]:
            files = timestamps[stamp]
            if kind in AUDITS and any(
                f.suffix == ".json" and f.name not in receipts for f in files
            ):
                continue
            for file in files:
                file.unlink(missing_ok=True)
                removed += 1
    return removed
