"""Tests for validation.rule_versions (#5027)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from validation import deterministic
from validation.deterministic import run_deterministic_rules
from validation.rule_versions import (
    RULE_REASON_MARKERS,
    RULE_VERSION_LIVE_AT,
    RULE_VERSIONS,
    FailRow,
    fail_row_from_db,
    failed_rules,
    judged_versions,
    stale_rules,
    version_live_at,
)

LIVE_4793 = RULE_VERSION_LIVE_AT[("hearing_date_in_range", 2)]
BEFORE = datetime(2026, 9, 1, tzinfo=UTC)
AFTER = datetime(2026, 10, 1, tzinfo=UTC)
HEARING = "hearing_date (2025-01-01) is 300 days from captured_at (2026-09-01)"
TITLE = "ruling is almost certainly a bad-split fragment (see #2571, #4628)"


def _all_rules_fired() -> list[deterministic.DeterministicRuleResult]:
    return run_deterministic_rules(
        ruling_text="x",
        case_number="C",
        case_title="A v. B",
        hearing_date=None,
        captured_at=None,
        other_case_numbers=["D"],
    ).rules


def test_every_deterministic_rule_has_a_version() -> None:
    names = {r.rule for r in _all_rules_fired()} | {"no_duplicate_ruling_text"}
    assert names == set(RULE_VERSIONS)
    assert all(v >= 1 for v in RULE_VERSIONS.values())


def test_hearing_date_in_range_is_at_version_2_since_4793() -> None:
    assert RULE_VERSIONS["hearing_date_in_range"] == 2
    assert version_live_at("hearing_date_in_range", BEFORE) == 1
    assert version_live_at("hearing_date_in_range", LIVE_4793) == 2
    assert version_live_at("hearing_date_in_range", AFTER) == 2
    # Naive timestamps are UTC.
    assert version_live_at("hearing_date_in_range", AFTER.replace(tzinfo=None)) == 2
    assert version_live_at("no_html_in_ruling_text", AFTER) == 1


@pytest.mark.parametrize(
    ("rule", "result"),
    [
        (
            "hearing_date_in_range",
            deterministic.check_hearing_date_in_range(date(2020, 1, 1), date(2026, 9, 1), "llm"),
        ),
        ("no_html_in_ruling_text", deterministic.check_no_html_in_ruling_text("<html>x")),
        ("ruling_text_not_empty", deterministic.check_ruling_text_not_empty("")),
        (
            "unknown_and_null_title_fail",
            deterministic.check_unknown_and_null_title_fail("UNKNOWN-1", None),
        ),
        (
            "case_number_matches_county_format",
            deterministic.check_case_number_matches_county_format("23STCV01234", "Santa Clara"),
        ),
    ],
)
def test_reason_markers_match_each_failing_rules_reason(
    rule: str, result: deterministic.DeterministicRuleResult
) -> None:
    assert result.rule == rule and result.result == "fail"
    assert RULE_REASON_MARKERS[rule] in (result.reason or "")
    assert failed_rules(FailRow(BEFORE, result.reason)) == {rule}


def test_failed_rules_and_versions_prefer_the_recorded_columns() -> None:
    row = FailRow(BEFORE, HEARING, ("ruling_text_not_empty",), {"hearing_date_in_range": 2})
    assert failed_rules(row) == {"ruling_text_not_empty"}
    assert judged_versions(row) == {"hearing_date_in_range": 2}
    legacy = FailRow(BEFORE, f"{HEARING}; {TITLE}")
    assert failed_rules(legacy) == {"hearing_date_in_range", "unknown_and_null_title_fail"}
    assert judged_versions(legacy)["hearing_date_in_range"] == 1


def test_stale_when_a_relaxed_rule_failed_it_and_nothing_re_judged_it() -> None:
    assert stale_rules([FailRow(BEFORE, HEARING)]) == {"hearing_date_in_range"}
    # Another rule failing it as well does not hide the relaxed one.
    assert stale_rules([FailRow(BEFORE, f"{HEARING}; {TITLE}")]) == {"hearing_date_in_range"}


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="no-rows"),
        pytest.param([FailRow(BEFORE, TITLE)], id="unchanged-rule"),
        pytest.param([FailRow(AFTER, HEARING)], id="after-the-relaxation"),
        pytest.param([FailRow(BEFORE, HEARING), FailRow(AFTER, TITLE)], id="re-judged"),
        pytest.param([FailRow(BEFORE, "something else")], id="unknown-reason"),
        pytest.param(
            [FailRow(BEFORE, HEARING, ("hearing_date_in_range",), dict(RULE_VERSIONS))],
            id="recorded-current-version",
        ),
        pytest.param(
            [FailRow(BEFORE, HEARING, ("renamed_rule",), {"renamed_rule": 1})],
            id="rule-no-longer-exists",
        ),
    ],
)
def test_not_stale(rows: list[FailRow]) -> None:
    assert stale_rules(rows) == set()


def test_a_version_bump_reopens_failures_recorded_with_versions() -> None:
    row = FailRow(AFTER, TITLE, ("unknown_and_null_title_fail",), dict(RULE_VERSIONS))
    assert stale_rules([row]) == set()
    bumped = {**RULE_VERSIONS, "unknown_and_null_title_fail": 2}
    assert stale_rules([row], bumped) == {"unknown_and_null_title_fail"}
    rejudged = FailRow(AFTER, TITLE, ("unknown_and_null_title_fail",), bumped)
    assert stale_rules([row, rejudged], bumped) == set()


def test_fail_row_from_db() -> None:
    assert fail_row_from_db(BEFORE, "r", None, None) == FailRow(BEFORE, "r")
    assert fail_row_from_db(BEFORE, "r", ["a"], {"a": 1}) == FailRow(BEFORE, "r", ("a",), {"a": 1})
    # A malformed rule_versions value is treated as unrecorded.
    assert fail_row_from_db(BEFORE, "r", [], "garbage").rule_versions is None
