"""Which recorded validation failures were judged by a since-relaxed rule (#5027).

A key whose rulings all failed deterministic validation is skipped: no
``derived.documents`` row, one failed ``telemetry.validation_results`` row
per ruling.  The raw-key reconciler (``scripts/reconcile_raw_keys.py``)
counts such a key as explained.  When a rule is later relaxed (#4793:
``hearing_date_in_range`` flags a structured-source date it used to fail),
the key would now be written, but nothing re-ingests it.

Each rule has a version (:data:`RULE_VERSIONS`), bumped on a relaxation.
The worker records the versions that judged a row and the rules that failed
it (``rule_versions`` / ``failed_rules``, migration 73).
:func:`stale_rules` names the rules whose relaxation a key has not been
re-judged under: a failure recorded under an older version of a rule that
failed it, and no row of the key recorded under the current version since.
The reconciler re-ingests those keys through ``telemetry.ingest_incomplete``
(reason ``stale_validation``) and its attempt cap.

Rows written before migration 73 carry neither column.  Their failing rules
are read from the reason text (:data:`RULE_REASON_MARKERS`) and their
versions from ``created_at`` (:data:`RULE_VERSION_LIVE_AT`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Standard library only: the reconciler imports this module without pulling
# in the worker (``validation.deterministic`` imports ``framework``).

#: Semantic version of every rule in ``validation.deterministic``.  The worker
#: records these on each deterministic validation row (``rule_versions``, the
#: rules that failed in ``failed_rules``).
#:
#: BUMP A RULE'S VERSION WHENEVER A CHANGE CAN TURN ONE OF ITS ``fail``
#: RESULTS INTO ``flag`` OR ``pass`` (a relaxation).  A key whose rulings all
#: failed is skipped, not written; the raw-key reconciler re-ingests a key
#: whose recorded failure was judged by an older version of a rule that
#: failed it, so the relaxed rule judges it again.  Without the bump the key
#: stays hidden for good.  A tightening (a new fail, a stricter threshold)
#: needs no bump.  A new rule starts at 1.
#:
#: The fingerprint test
#: (``packages/scraper-framework/tests/test_rule_versions_fingerprint.py``)
#: pins each rule's version with a hash of its check and fails when the
#: code moves without a bump or a repin with a reason (#5134).
RULE_VERSIONS: dict[str, int] = {
    "no_html_in_ruling_text": 1,
    # 2: a structured-source date out of the 180-day window flags (#4793).
    "hearing_date_in_range": 2,
    "no_multiple_adversarial_patterns": 1,
    "ruling_text_not_empty": 1,
    "case_number_not_unknown": 1,
    "unknown_and_null_title_fail": 1,
    "no_duplicate_ruling_text": 1,
    "no_cross_case_ruling_text": 1,
    "ruling_text_case_number_marker": 1,
    "case_number_matches_county_format": 1,
    "ruling_text_reasonable_length": 1,
}

#: When each rule version after 1 went live on dev, for rows written before
#: the versions were recorded (migration 73).  A later version needs no entry:
#: every unversioned row predates it.  The time is after the deploy, with
#: margin, so a row an old worker task wrote during the rollout counts as old
#: (an extra reingest is cheap; a key hidden for good is not).
RULE_VERSION_LIVE_AT: dict[tuple[str, int], datetime] = {
    # #4793 (PR #4806, merged 2026-09-27T02:12Z; the first row the new rule
    # wrote, a structured-date flag, is from 02:23Z).
    ("hearing_date_in_range", 2): datetime(2026, 9, 27, 3, 0, tzinfo=UTC),
}

#: A fixed fragment of each rule's ``fail`` reason, to read the failing rules
#: of an unversioned row.  The row's reason joins the reasons of every
#: non-pass rule, flags included; only rules that can fail are listed.
RULE_REASON_MARKERS: dict[str, str] = {
    "no_html_in_ruling_text": "raw HTML detected",
    "hearing_date_in_range": "hearing_date (",
    "ruling_text_not_empty": "ruling_text is null or empty",
    "unknown_and_null_title_fail": "bad-split fragment",
    "case_number_matches_county_format": "case-number format",
}


@dataclass(frozen=True)
class FailRow:
    """One failed ``telemetry.validation_results`` row of a key."""

    created_at: datetime
    reason: str | None = None
    #: ``failed_rules``; ``None`` on a row written before migration 73.
    failed_rules: tuple[str, ...] | None = None
    #: ``rule_versions``; ``None`` on a row written before migration 73.
    rule_versions: Mapping[str, int] | None = None


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def version_live_at(rule: str, at: datetime) -> int:
    """The version of *rule* that judged an unversioned row written at *at*."""
    version = 1
    for (name, v), live_at in RULE_VERSION_LIVE_AT.items():
        if name == rule and _utc(at) >= live_at:
            version = max(version, v)
    return version


def judged_versions(row: FailRow) -> dict[str, int]:
    """The rule versions that judged *row*: recorded, or inferred."""
    if row.rule_versions:
        return {str(k): int(v) for k, v in row.rule_versions.items()}
    return {rule: version_live_at(rule, row.created_at) for rule in RULE_VERSIONS}


def failed_rules(row: FailRow) -> set[str]:
    """The rules that failed *row*: recorded, or read from its reason."""
    if row.failed_rules is not None:
        return set(row.failed_rules)
    text = row.reason or ""
    return {rule for rule, marker in RULE_REASON_MARKERS.items() if marker in text}


def stale_rules(rows: Iterable[FailRow], current: Mapping[str, int] = RULE_VERSIONS) -> set[str]:
    """Rules relaxed since they failed one of a key's *rows*, and not
    re-judged since: no row of the key was judged by their current version.

    Empty when the key's failures stand under the current rules.  A rule
    unknown to *current* (renamed or removed) is never stale.
    """
    rows = list(rows)
    judged = [judged_versions(row) for row in rows]
    stale: set[str] = set()
    for row, versions in zip(rows, judged, strict=True):
        for rule in failed_rules(row):
            now = current.get(rule)
            if now is not None and versions.get(rule, 1) < now:
                stale.add(rule)
    return {
        rule
        for rule in stale
        if not any(versions.get(rule, 1) >= current[rule] for versions in judged)
    }


def fail_row_from_db(
    created_at: datetime,
    reason: str | None,
    failed: Sequence[str] | None,
    versions: Any,
) -> FailRow:
    """A :class:`FailRow` from the columns as psycopg returns them
    (``failed_rules`` a list or ``None``, ``rule_versions`` a dict or
    ``None``)."""
    return FailRow(
        created_at=created_at,
        reason=reason,
        failed_rules=tuple(failed) if failed is not None else None,
        rule_versions=dict(versions) if isinstance(versions, Mapping) else None,
    )
