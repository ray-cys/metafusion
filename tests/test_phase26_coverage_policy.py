from copy import deepcopy

from tools import check_targeted_coverage


def _summary(*, lines=100, covered_lines=100, branches=20, covered_branches=20):
    return {
        "num_statements": lines,
        "covered_lines": covered_lines,
        "num_branches": branches,
        "covered_branches": covered_branches,
    }


def _passing_report():
    return {
        "totals": _summary(),
        "files": {
            filename: {"summary": _summary()}
            for filename in check_targeted_coverage.TARGETS
        },
    }


def test_coverage_policy_accepts_report_at_or_above_every_floor():
    results = check_targeted_coverage.evaluate(_passing_report())

    assert results
    assert all(result["passed"] for result in results)


def test_coverage_policy_rejects_missing_branch_instrumentation():
    report = _passing_report()
    report["totals"]["num_branches"] = 0
    report["totals"]["covered_branches"] = 0

    results = check_targeted_coverage.evaluate(report)

    assert results[0]["branch_percent"] == 0.0
    assert results[0]["passed"] is False


def test_coverage_policy_rejects_global_and_target_regressions():
    report = _passing_report()
    report["totals"].update(num_statements=1000, covered_lines=949)
    first_target = next(iter(check_targeted_coverage.TARGETS))
    del report["files"][first_target]

    results = check_targeted_coverage.evaluate(deepcopy(report))

    assert results[0]["passed"] is False
    assert results[1]["filename"] == first_target
    assert results[1]["passed"] is False
