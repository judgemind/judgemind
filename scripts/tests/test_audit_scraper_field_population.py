"""Tests for audit_scraper_field_population.

Covers:
- REGISTRY contains the 3 highest-volume JSON-envelope scrapers
- lookup_field walks dotted paths and tolerates missing/non-dict segments
- is_populated treats None / "" / [] / {} as not populated and 0 / False as populated
- audit_one_scraper skips below min-captures, returns drift on 0/N, healthy on >0/N
- run_audit scraper_filter limits which scrapers are inspected
- synthetic_drift_result produces a non-healthy AuditResult
- build_issue_body renders a markdown body with the drifted field
- main() --dry-run --write-issue-body writes a non-empty file

The script imports boto3 + psycopg lazily (only inside main's real run);
tests can import the module without those deps. The script also imports
``framework.logging`` (which transitively imports ``structlog``) at module
level since the #4373 migration; we pre-mock both in ``sys.modules`` so the
lightweight CI scripts-tests (python) shard import works (it only installs
pytest, pytest-xdist, boto3, judgemind-config).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pre-import mocking — inject structlog and framework.logging mocks before
# the script loads. The script's top-level
# ``from framework.logging import configure_structlog`` would otherwise raise
# ModuleNotFoundError in the lightweight CI scripts-tests (python) shard.
# See #4373. Save/replay envelope is centralised in
# ``scripts/tests/_mock_helpers.py`` (#4430).
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests._mock_helpers import mock_sys_modules  # noqa: E402

_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()

with mock_sys_modules(
    {
        "structlog": _mock_structlog,
        "framework": MagicMock(),
        "framework.logging": MagicMock(),
    }
):
    import audit_scraper_field_population as _script  # noqa: E402

REGISTRY = _script.REGISTRY
get_spec = _script.get_spec
lookup_field = _script.lookup_field
is_populated = _script.is_populated
FieldResult = _script.FieldResult
AuditResult = _script.AuditResult
audit_one_scraper = _script.audit_one_scraper
run_audit = _script.run_audit
synthetic_drift_result = _script.synthetic_drift_result
build_issue_body = _script.build_issue_body
fetch_capture_count = _script.fetch_capture_count
fetch_sample_keys = _script.fetch_sample_keys
fetch_envelope = _script.fetch_envelope


# ---------------------------------------------------------------------------
# Registry coverage (AC #1 + AC #4)
# ---------------------------------------------------------------------------


class TestRegistry:
    """The registry must cover the 3 highest-volume JSON-envelope scrapers."""

    def test_courtlistener_is_registered(self) -> None:
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None
        # Per #4247: docket.court is the canonical jurisdiction signal.
        assert "docket.court" in spec.field_paths

    def test_sf_civil_is_registered(self) -> None:
        spec = get_spec("ca-sf-civil-tentatives")
        assert spec is not None
        assert "case_number" in spec.field_paths
        assert "department" in spec.field_paths

    def test_cc_portal_is_registered(self) -> None:
        spec = get_spec("ca-cc-tentatives-portal")
        assert spec is not None
        assert "judge_id" in spec.field_paths
        assert "judge_name_dropdown" in spec.field_paths

    def test_at_least_three_scrapers_covered(self) -> None:
        """AC #4: registry covers at least 3 highest-volume scrapers."""
        assert len(REGISTRY) >= 3

    def test_all_specs_are_json_envelope(self) -> None:
        """Initial registry only inspects JSON envelopes -- PDF/HTML are out of scope."""
        for spec in REGISTRY:
            assert spec.envelope_format == "json", (
                f"{spec.scraper_id}: only json envelopes are supported"
            )

    def test_unknown_scraper_returns_none(self) -> None:
        assert get_spec("nonexistent-scraper") is None


# ---------------------------------------------------------------------------
# lookup_field
# ---------------------------------------------------------------------------


class TestLookupField:
    def test_flat_path(self) -> None:
        assert lookup_field({"case_number": "ABC"}, "case_number") == "ABC"

    def test_nested_path(self) -> None:
        assert lookup_field({"docket": {"court": "ca9"}}, "docket.court") == "ca9"

    def test_missing_top_level(self) -> None:
        assert lookup_field({"other": 1}, "docket.court") is None

    def test_missing_intermediate(self) -> None:
        assert lookup_field({"docket": {}}, "docket.court") is None

    def test_non_dict_intermediate(self) -> None:
        # docket is a list -> path cannot resolve through it.
        assert lookup_field({"docket": []}, "docket.court") is None

    def test_explicit_none_returned(self) -> None:
        # Caller distinguishes "missing" (None) from "present but None" via is_populated.
        assert lookup_field({"docket": {"court": None}}, "docket.court") is None

    def test_top_level_not_dict(self) -> None:
        assert lookup_field([1, 2, 3], "foo") is None  # type: ignore[arg-type]

    def test_empty_string_returned_as_is(self) -> None:
        assert lookup_field({"case_number": ""}, "case_number") == ""


# ---------------------------------------------------------------------------
# is_populated
# ---------------------------------------------------------------------------


class TestIsPopulated:
    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "\t\n", [], {}],
    )
    def test_falsy_or_empty(self, value: object) -> None:
        assert not is_populated(value)

    @pytest.mark.parametrize(
        "value",
        ["foo", "ca9", " x ", [1], {"a": 1}, 0, 1, False, True, 0.0],
    )
    def test_populated(self, value: object) -> None:
        # 0/False count as populated -- 0 is a valid judge_id in some courts.
        assert is_populated(value)


# ---------------------------------------------------------------------------
# audit_one_scraper -- min-captures gate + drift detection
# ---------------------------------------------------------------------------


def _make_conn(
    captures_by_scraper: dict[str, int], sample_keys: dict[str, list[str]]
) -> object:
    """Build a mock DB connection that answers fetch_capture_count and fetch_sample_keys.

    The audit script issues two SELECTs per scraper: count, then top-N keys.
    We dispatch on the SQL prefix.
    """

    class _Cursor:
        def __init__(self) -> None:
            self._rows: list = []

        def execute(self, sql: str, params: tuple) -> None:
            sql_lower = sql.lower()
            if "count(*)" in sql_lower:
                scraper_id = params[0]
                self._rows = [(captures_by_scraper.get(scraper_id, 0),)]
            elif "select s3_key" in sql_lower:
                scraper_id = params[0]
                limit = params[1]
                keys = sample_keys.get(scraper_id, [])[:limit]
                self._rows = [(k,) for k in keys]
            else:
                self._rows = []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Conn:
        def cursor(self) -> _Cursor:
            return _Cursor()

    return _Conn()


def _make_s3(envelopes_by_key: dict[str, dict | bytes | None]) -> object:
    """Build a mock S3 client.  Values may be a dict (encoded as JSON), bytes (raw), or None (404)."""

    def get_object(*, Bucket: str, Key: str) -> dict:
        if Key not in envelopes_by_key:
            raise KeyError(Key)
        env = envelopes_by_key[Key]
        if env is None:
            raise KeyError(Key)
        if isinstance(env, dict):
            body_bytes = json.dumps(env).encode()
        else:
            body_bytes = env
        return {"Body": MagicMock(read=lambda: body_bytes)}

    s3 = MagicMock()
    s3.get_object = get_object
    return s3


_NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)


class TestAuditOneScraper:
    def test_skip_when_below_min_captures(self) -> None:
        conn = _make_conn({"federal-courtlistener-opinions": 10}, {})
        s3 = _make_s3({})
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None

        results, skip = audit_one_scraper(
            conn,
            s3,
            "bucket",
            spec,
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert results == []
        assert skip is not None
        assert skip["reason"] == "insufficient_captures"
        assert skip["capture_count"] == 10

    def test_skip_when_no_sample_keys(self) -> None:
        conn = _make_conn({"federal-courtlistener-opinions": 100}, {})
        s3 = _make_s3({})
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None

        results, skip = audit_one_scraper(
            conn,
            s3,
            "bucket",
            spec,
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert results == []
        assert skip is not None
        assert skip["reason"] == "no_sample_keys"

    def test_drift_when_field_empty_in_all_samples(self) -> None:
        """The motivating bug class: docket.court empty in 10/10 samples."""
        keys = [f"federal/dc/dc_district/raw/{'a' * 63}{i:x}.txt" for i in range(10)]
        # Simulates the #4247 scenario: cluster present, docket missing court.
        envelopes = {
            k: {
                "cluster": {"id": i, "case_name": "X v. Y"},
                "opinion": {"id": i, "plain_text": "text"},
                "docket": {"id": i, "court": ""},  # empty -- the bug!
            }
            for i, k in enumerate(keys)
        }
        conn = _make_conn(
            {"federal-courtlistener-opinions": 100},
            {"federal-courtlistener-opinions": keys},
        )
        s3 = _make_s3(envelopes)  # type: ignore[arg-type]
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None

        results, skip = audit_one_scraper(
            conn,
            s3,
            "bucket",
            spec,
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert skip is None
        assert len(results) == 1
        assert results[0].scraper_id == "federal-courtlistener-opinions"
        assert results[0].field_path == "docket.court"
        assert results[0].populated == 0
        assert results[0].sampled == 10
        assert results[0].drifted is True

    def test_healthy_when_field_populated_in_at_least_one(self) -> None:
        """1/N populated is enough to clear -- the audit's floor is 'at least one'."""
        keys = [f"federal/dc/dc_district/raw/{'a' * 63}{i:x}.txt" for i in range(10)]
        # Only one sample has docket.court populated; the audit clears.
        envelopes = {}
        for i, k in enumerate(keys):
            court = "ca9" if i == 0 else ""
            envelopes[k] = {
                "cluster": {},
                "opinion": {},
                "docket": {"court": court},
            }
        conn = _make_conn(
            {"federal-courtlistener-opinions": 100},
            {"federal-courtlistener-opinions": keys},
        )
        s3 = _make_s3(envelopes)  # type: ignore[arg-type]
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None

        results, skip = audit_one_scraper(
            conn,
            s3,
            "bucket",
            spec,
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert skip is None
        assert len(results) == 1
        assert results[0].populated == 1
        assert results[0].sampled == 10
        assert results[0].drifted is False

    def test_unparseable_envelope_skipped(self) -> None:
        """Envelopes that aren't valid JSON or aren't dicts don't count toward sampled."""
        keys = ["k1", "k2", "k3"]
        envelopes = {
            "k1": b"not json at all",  # parse fail
            "k2": {"docket": {"court": "ca9"}},  # OK
            "k3": b"[1, 2, 3]",  # JSON but not a dict
        }
        conn = _make_conn(
            {"federal-courtlistener-opinions": 100},
            {"federal-courtlistener-opinions": keys},
        )
        s3 = _make_s3(envelopes)  # type: ignore[arg-type]
        spec = get_spec("federal-courtlistener-opinions")
        assert spec is not None

        results, skip = audit_one_scraper(
            conn,
            s3,
            "bucket",
            spec,
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert skip is None
        assert len(results) == 1
        # Only k2 was inspected; populated=1, sampled=1.
        assert results[0].sampled == 1
        assert results[0].populated == 1
        assert results[0].drifted is False


# ---------------------------------------------------------------------------
# run_audit -- scraper_filter
# ---------------------------------------------------------------------------


class TestRunAudit:
    def test_scraper_filter_limits_scope(self) -> None:
        # Only courtlistener has captures; the others below threshold.
        keys = [f"federal/dc/dc_district/raw/{'a' * 63}{i:x}.txt" for i in range(10)]
        envelopes = {k: {"docket": {"court": "ca9"}} for k in keys}
        conn = _make_conn(
            {
                "federal-courtlistener-opinions": 100,
                "ca-sf-civil-tentatives": 0,
                "ca-cc-tentatives-portal": 0,
            },
            {"federal-courtlistener-opinions": keys},
        )
        s3 = _make_s3(envelopes)  # type: ignore[arg-type]

        result = run_audit(
            conn,
            s3,
            "bucket",
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            scraper_filter=["federal-courtlistener-opinions"],
            now=_NOW,
        )
        # No skipped entries for the others -- they were filtered out.
        assert len(result.field_results) == 1
        assert result.field_results[0].scraper_id == "federal-courtlistener-opinions"
        assert result.skipped == []

    def test_full_run_records_skipped(self) -> None:
        """Without --scraper, all REGISTRY entries are tried; below-threshold ones are skipped."""
        conn = _make_conn(
            {
                "federal-courtlistener-opinions": 0,
                "ca-sf-civil-tentatives": 0,
                "ca-cc-tentatives-portal": 0,
            },
            {},
        )
        s3 = _make_s3({})
        result = run_audit(
            conn,
            s3,
            "bucket",
            sample_size=10,
            min_captures=50,
            lookback_days=7,
            now=_NOW,
        )
        assert len(result.skipped) == len(REGISTRY)
        assert result.field_results == []
        assert result.healthy is True


# ---------------------------------------------------------------------------
# Synthetic dry-run path (AC #3 verification)
# ---------------------------------------------------------------------------


class TestSyntheticDriftResult:
    def test_synthetic_is_unhealthy(self) -> None:
        """The synthetic scenario simulates the #4247 bug class."""
        result = synthetic_drift_result()
        assert result.healthy is False
        assert result.drift_count == 1
        assert result.field_results[0].field_path == "docket.court"
        assert result.field_results[0].drifted is True


# ---------------------------------------------------------------------------
# Issue body (AC #3 verification)
# ---------------------------------------------------------------------------


class TestBuildIssueBody:
    def test_body_contains_scraper_and_field(self) -> None:
        result = synthetic_drift_result()
        body = build_issue_body(result)
        assert "federal-courtlistener-opinions" in body
        assert "docket.court" in body
        # Markdown table delimiter present.
        assert "| Scraper |" in body

    def test_body_includes_timestamp(self) -> None:
        result = synthetic_drift_result()
        body = build_issue_body(result)
        assert result.timestamp in body


# ---------------------------------------------------------------------------
# Main entry -- dry-run path (AC #3 verification: tmp/ output)
# ---------------------------------------------------------------------------


class TestMainDryRun:
    def test_dry_run_writes_issue_body(self, tmp_path) -> None:
        """AC #3 verification: dry-run produces a generated issue body in tmp/."""
        out = tmp_path / "drift_issue_body.md"
        rc = _script.main(
            [
                "--dry-run",
                "--json",
                "--write-issue-body",
                str(out),
            ]
        )
        assert rc == 1
        assert out.exists()
        text = out.read_text()
        assert "Scraper Field-Mapping Drift Alert" in text
        assert "docket.court" in text

    def test_dry_run_returns_1_when_unhealthy(self) -> None:
        rc = _script.main(["--dry-run"])
        assert rc == 1


# ---------------------------------------------------------------------------
# DB fetch helpers -- exercise the SQL execute path
# ---------------------------------------------------------------------------


class TestDbHelpers:
    def test_fetch_capture_count(self) -> None:
        conn = _make_conn({"federal-courtlistener-opinions": 42}, {})
        n = fetch_capture_count(
            conn, "federal-courtlistener-opinions", _NOW - timedelta(days=7)
        )
        assert n == 42

    def test_fetch_capture_count_unknown_returns_zero(self) -> None:
        conn = _make_conn({}, {})
        n = fetch_capture_count(conn, "unknown", _NOW - timedelta(days=7))
        assert n == 0

    def test_fetch_sample_keys_truncates_to_sample_size(self) -> None:
        conn = _make_conn(
            {},
            {"federal-courtlistener-opinions": [f"k{i}" for i in range(20)]},
        )
        keys = fetch_sample_keys(conn, "federal-courtlistener-opinions", 5)
        assert keys == [f"k{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# fetch_envelope -- error handling
# ---------------------------------------------------------------------------


class TestFetchEnvelope:
    def test_missing_object_returns_none(self) -> None:
        s3 = _make_s3({})
        assert fetch_envelope(s3, "bucket", "missing/key") is None

    def test_invalid_json_returns_none(self) -> None:
        s3 = _make_s3({"k": b"not json"})
        assert fetch_envelope(s3, "bucket", "k") is None

    def test_top_level_not_dict_returns_none(self) -> None:
        s3 = _make_s3({"k": b"[1, 2, 3]"})
        assert fetch_envelope(s3, "bucket", "k") is None

    def test_valid_envelope_returns_dict(self) -> None:
        s3 = _make_s3({"k": {"docket": {"court": "ca9"}}})
        env = fetch_envelope(s3, "bucket", "k")
        assert env == {"docket": {"court": "ca9"}}
