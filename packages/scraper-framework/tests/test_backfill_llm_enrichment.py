"""Tests for the backfill_llm_enrichment script.

All database access and LLM calls are mocked — these tests verify the
orchestration, filtering, and update logic without requiring a live
database or API keys.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill = importlib.import_module("backfill_llm_enrichment")

from framework.llm_enrichment import EnrichmentParties, LlmEnrichmentResult  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RULING_ID_1 = str(uuid.uuid4())
_RULING_ID_2 = str(uuid.uuid4())
_CASE_ID_1 = str(uuid.uuid4())
_CASE_ID_2 = str(uuid.uuid4())


def _make_ruling(
    ruling_id: str = _RULING_ID_1,
    case_id: str = _CASE_ID_1,
    ruling_text: str = "Smith v. Jones\nMotion for Summary Judgment\nGranted.",
    motion_type: str | None = None,
    outcome: str | None = None,
    case_title: str | None = None,
) -> dict:
    return {
        "ruling_id": ruling_id,
        "case_id": case_id,
        "ruling_text": ruling_text,
        "motion_type": motion_type,
        "outcome": outcome,
        "case_title": case_title,
        "case_id_check": case_id,
    }


def _make_enrichment_result(
    case_title: str | None = "Smith v. Jones",
    motion_type: str | None = "msj",
    outcome: str | None = "granted",
    plaintiffs: list[str] | None = None,
    defendants: list[str] | None = None,
) -> LlmEnrichmentResult:
    return LlmEnrichmentResult(
        case_title=case_title,
        motion_type=motion_type,
        outcome=outcome,
        parties=EnrichmentParties(
            plaintiffs=plaintiffs or ["Smith"],
            defendants=defendants or ["Jones"],
        ),
    )


# ---------------------------------------------------------------------------
# enrich_one_ruling tests
# ---------------------------------------------------------------------------


class TestEnrichOneRuling:
    """Tests for the enrich_one_ruling function."""

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_enriches_ruling_with_null_fields(self, mock_enrich: MagicMock) -> None:
        """Ruling with all null fields gets enriched."""
        mock_enrich.return_value = _make_enrichment_result()
        ruling = _make_ruling()

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=False
        )

        assert result is not None
        assert result["new_motion_type"] == "msj"
        assert result["new_outcome"] == "granted"
        assert result["new_case_title"] == "Smith v. Jones"
        assert len(result["new_parties"]) == 2
        mock_enrich.assert_called_once()

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_skips_populated_fields_without_force(self, mock_enrich: MagicMock) -> None:
        """Fields already populated are not overwritten without --force."""
        mock_enrich.return_value = _make_enrichment_result()
        ruling = _make_ruling(
            motion_type="demurrer",
            outcome="denied",
            case_title="Existing Title",
        )

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=False
        )

        # All fields populated, nothing to update
        assert result == backfill._SKIPPED_NO_UPDATE
        mock_enrich.assert_not_called()

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_force_re_enriches_populated_fields(self, mock_enrich: MagicMock) -> None:
        """With --force, populated fields are re-enriched."""
        mock_enrich.return_value = _make_enrichment_result(
            motion_type="msj", outcome="granted", case_title="New Title"
        )
        ruling = _make_ruling(
            motion_type="demurrer",
            outcome="denied",
            case_title="Old Title",
        )

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=True
        )

        assert result is not None
        assert result["new_motion_type"] == "msj"
        assert result["new_outcome"] == "granted"
        assert result["new_case_title"] == "New Title"
        mock_enrich.assert_called_once()

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_llm_api_failure_returns_sentinel(self, mock_enrich: MagicMock) -> None:
        """LLM API failure (enrich_ruling returns None) returns llm_api_failure."""
        mock_enrich.return_value = None
        ruling = _make_ruling()

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=False
        )

        assert result == backfill._LLM_API_FAILURE

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_no_extractable_data_returns_sentinel(self, mock_enrich: MagicMock) -> None:
        """LLM success but no extractable fields returns no_extractable_data."""
        mock_enrich.return_value = LlmEnrichmentResult()
        ruling = _make_ruling()

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=False
        )

        assert result == backfill._NO_EXTRACTABLE_DATA

    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_partial_enrichment(self, mock_enrich: MagicMock) -> None:
        """Ruling with some null fields only updates those."""
        mock_enrich.return_value = _make_enrichment_result(
            case_title="Smith v. Jones",
            motion_type="msj",
            outcome=None,  # LLM couldn't determine outcome
        )
        ruling = _make_ruling(
            motion_type=None,
            outcome=None,
            case_title="Smith v. Jones",  # Already has title
        )

        result = backfill.enrich_one_ruling(
            ruling, provider="google", model=None, client=MagicMock(), force=False
        )

        assert result is not None
        assert result["new_motion_type"] == "msj"
        assert "new_outcome" not in result  # LLM returned None
        assert "new_case_title" not in result  # Already populated


# ---------------------------------------------------------------------------
# apply_updates tests
# ---------------------------------------------------------------------------


class TestApplyUpdates:
    """Tests for the apply_updates function."""

    def test_dry_run_does_not_write(self) -> None:
        """In dry-run mode, no DB writes happen."""
        conn = MagicMock()
        stats = backfill.CountyStats(county="Test")
        updates = [
            {
                "ruling_id": _RULING_ID_1,
                "case_id": _CASE_ID_1,
                "new_motion_type": "msj",
                "new_outcome": "granted",
                "new_case_title": "Smith v. Jones",
                "new_parties": [
                    {"name": "Smith", "role": "plaintiff"},
                ],
                "result": _make_enrichment_result(),
            }
        ]

        backfill.apply_updates(conn, updates, stats, dry_run=True)

        # Stats should be updated
        assert stats.updated_motion_type == 1
        assert stats.updated_outcome == 1
        assert stats.updated_case_title == 1
        assert stats.updated_parties == 1

        # But no DB calls
        conn.cursor.assert_not_called()
        conn.commit.assert_not_called()

    def test_applies_ruling_updates(self) -> None:
        """Non-dry-run applies ruling and case updates."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        stats = backfill.CountyStats(county="Test")
        updates = [
            {
                "ruling_id": _RULING_ID_1,
                "case_id": _CASE_ID_1,
                "new_motion_type": "msj",
                "new_outcome": "granted",
                "result": _make_enrichment_result(),
            }
        ]

        backfill.apply_updates(conn, updates, stats, dry_run=False)

        assert stats.updated_motion_type == 1
        assert stats.updated_outcome == 1
        conn.commit.assert_called_once()

    def test_parties_update_uses_batch_upsert(self) -> None:
        """Party updates go through batch_upsert_parties."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        stats = backfill.CountyStats(county="Test")
        parties = [
            {"name": "Smith", "role": "plaintiff"},
            {"name": "Jones", "role": "defendant"},
        ]
        updates = [
            {
                "ruling_id": _RULING_ID_1,
                "case_id": _CASE_ID_1,
                "new_parties": parties,
                "result": _make_enrichment_result(),
            }
        ]

        with patch.object(backfill, "batch_upsert_parties") as mock_batch:
            backfill.apply_updates(conn, updates, stats, dry_run=False)
            mock_batch.assert_called_once_with(
                conn, _CASE_ID_1, parties, alias_source="llm_enrichment"
            )

        assert stats.updated_parties == 1


# ---------------------------------------------------------------------------
# CountyStats / BackfillStats tests
# ---------------------------------------------------------------------------


class TestStats:
    """Tests for statistics tracking."""

    def test_county_stats_defaults(self) -> None:
        cs = backfill.CountyStats(county="Test")
        assert cs.total_rulings == 0
        assert cs.processed == 0
        assert cs.llm_api_failures == 0
        assert cs.no_extractable_data == 0
        assert cs.skipped_no_update == 0

    def test_backfill_stats_totals(self) -> None:
        stats = backfill.BackfillStats()
        c1 = stats.get_county("Riverside")
        c1.processed = 10
        c1.updated_motion_type = 5
        c1.llm_api_failures = 2
        c1.no_extractable_data = 1
        c1.skipped_no_update = 3

        c2 = stats.get_county("Orange")
        c2.processed = 20
        c2.updated_motion_type = 8
        c2.llm_api_failures = 1
        c2.no_extractable_data = 4
        c2.skipped_no_update = 5

        totals = stats.totals()
        assert totals["processed"] == 30
        assert totals["updated_motion_type"] == 13
        assert totals["llm_api_failures"] == 3
        assert totals["no_extractable_data"] == 5
        assert totals["skipped_no_update"] == 8


# ---------------------------------------------------------------------------
# CLI argument parsing tests
# ---------------------------------------------------------------------------


class TestParseArgs:
    """Tests for CLI argument parsing."""

    def test_county_arg(self) -> None:
        with patch("sys.argv", ["prog", "--county", "Riverside"]):
            args = backfill.parse_args()
        assert args.county == "Riverside"
        assert not args.all_counties

    def test_all_arg(self) -> None:
        with patch("sys.argv", ["prog", "--all"]):
            args = backfill.parse_args()
        assert args.all_counties is True
        assert args.county is None

    def test_dry_run_flag(self) -> None:
        with patch("sys.argv", ["prog", "--all", "--dry-run"]):
            args = backfill.parse_args()
        assert args.dry_run is True

    def test_force_flag(self) -> None:
        with patch("sys.argv", ["prog", "--all", "--force"]):
            args = backfill.parse_args()
        assert args.force is True

    def test_defaults(self) -> None:
        with patch("sys.argv", ["prog", "--all"]):
            args = backfill.parse_args()
        assert args.batch_size == 50
        assert args.concurrency == 8
        assert args.limit is None
        assert args.provider == "google"
        assert args.model is None

    def test_requires_county_or_all(self) -> None:
        with patch("sys.argv", ["prog"]):
            with pytest.raises(SystemExit):
                backfill.parse_args()


# ---------------------------------------------------------------------------
# fetch_rulings_batch tests
# ---------------------------------------------------------------------------


class TestFetchRulingsBatch:
    """Tests for the fetch_rulings_batch query builder."""

    def test_default_filters_null_fields(self) -> None:
        """Without force, only fetches rulings with missing fields."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.description = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        backfill.fetch_rulings_batch(conn, "Riverside", force=False)

        # The SQL should include the NULL field filter
        sql_arg = mock_cursor.execute.call_args[0][0]
        assert "motion_type IS NULL OR r.outcome IS NULL" in sql_arg

    def test_force_skips_null_filter(self) -> None:
        """With force=True, fetches all rulings with text."""
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.description = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        backfill.fetch_rulings_batch(conn, "Riverside", force=True)

        sql_arg = mock_cursor.execute.call_args[0][0]
        assert "motion_type IS NULL" not in sql_arg

    def test_limit_caps_batch(self) -> None:
        """When limit is reached, returns empty list."""
        conn = MagicMock()
        result = backfill.fetch_rulings_batch(conn, "Riverside", force=False, limit=10, offset=15)
        assert result == []


# ---------------------------------------------------------------------------
# fetch_counties tests
# ---------------------------------------------------------------------------


class TestFetchCounties:
    """Tests for fetch_counties."""

    def test_returns_county_names(self) -> None:
        conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Los Angeles",),
            ("Orange",),
            ("Riverside",),
        ]
        conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        counties = backfill.fetch_counties(conn)
        assert counties == ["Los Angeles", "Orange", "Riverside"]


# ---------------------------------------------------------------------------
# Integration: process_county
# ---------------------------------------------------------------------------


class TestProcessCounty:
    """Integration test for process_county with mocked DB and LLM."""

    @patch("backfill_llm_enrichment.apply_updates")
    @patch("backfill_llm_enrichment.fetch_rulings_batch")
    @patch("backfill_llm_enrichment.fetch_before_stats")
    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_processes_batch_and_updates(
        self,
        mock_enrich: MagicMock,
        mock_before_stats: MagicMock,
        mock_fetch_batch: MagicMock,
        mock_apply: MagicMock,
    ) -> None:
        """Full county processing: fetch, enrich, update."""
        mock_enrich.return_value = _make_enrichment_result()

        mock_before_stats.return_value = {
            "total": 100,
            "null_motion_type": 30,
            "null_outcome": 25,
            "null_case_title": 10,
        }

        # First batch: 2 rulings; second batch: empty (end)
        rulings = [
            _make_ruling(ruling_id=_RULING_ID_1, case_id=_CASE_ID_1),
            _make_ruling(ruling_id=_RULING_ID_2, case_id=_CASE_ID_2),
        ]
        mock_fetch_batch.side_effect = [rulings, []]

        conn = MagicMock()
        stats = backfill.BackfillStats(start_time=0)

        backfill.process_county(
            conn,
            "Riverside",
            stats,
            provider="google",
            model=None,
            client=MagicMock(),
            force=False,
            dry_run=True,
            batch_size=50,
            concurrency=2,
            limit=None,
        )

        county_stats = stats.get_county("Riverside")
        assert county_stats.total_rulings == 100
        assert county_stats.processed == 2
        # apply_updates was called with the 2 enrichment results
        assert mock_apply.call_count == 1
        updates_arg = mock_apply.call_args[0][1]
        assert len(updates_arg) == 2
        # Verify LLM was called for each ruling
        assert mock_enrich.call_count == 2

    @patch("backfill_llm_enrichment.apply_updates")
    @patch("backfill_llm_enrichment.fetch_rulings_batch")
    @patch("backfill_llm_enrichment.fetch_before_stats")
    @patch("backfill_llm_enrichment.enrich_ruling")
    def test_tracks_no_extractable_data_separately(
        self,
        mock_enrich: MagicMock,
        mock_before_stats: MagicMock,
        mock_fetch_batch: MagicMock,
        mock_apply: MagicMock,
    ) -> None:
        """LLM success with no data vs API failure tracked separately."""
        # First ruling: LLM succeeds but returns nothing extractable
        # Second ruling: LLM API fails (returns None)
        mock_enrich.side_effect = [LlmEnrichmentResult(), None]

        mock_before_stats.return_value = {
            "total": 100,
            "null_motion_type": 30,
            "null_outcome": 25,
            "null_case_title": 10,
        }

        rulings = [
            _make_ruling(ruling_id=_RULING_ID_1, case_id=_CASE_ID_1),
            _make_ruling(ruling_id=_RULING_ID_2, case_id=_CASE_ID_2),
        ]
        mock_fetch_batch.side_effect = [rulings, []]

        conn = MagicMock()
        stats = backfill.BackfillStats(start_time=0)

        backfill.process_county(
            conn,
            "Orange",
            stats,
            provider="google",
            model=None,
            client=MagicMock(),
            force=False,
            dry_run=True,
            batch_size=50,
            concurrency=1,
            limit=None,
        )

        county_stats = stats.get_county("Orange")
        assert county_stats.processed == 2
        assert county_stats.llm_api_failures == 1
        assert county_stats.no_extractable_data == 1
        assert county_stats.skipped_no_update == 0


# ---------------------------------------------------------------------------
# print_report tests
# ---------------------------------------------------------------------------


class TestPrintReport:
    """Tests for the print_report function."""

    def test_report_shows_separate_failure_categories(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Verify that print_report shows API failures and extraction gaps separately."""
        stats = backfill.BackfillStats(start_time=0.0)
        county = stats.get_county("Riverside")
        county.total_rulings = 100
        county.processed = 50
        county.llm_api_failures = 3
        county.no_extractable_data = 10
        county.skipped_no_update = 5
        county.errors = 1
        county.updated_motion_type = 20
        county.updated_outcome = 15
        county.updated_case_title = 10
        county.updated_parties = 8
        county.before_null_motion_type = 30
        county.after_null_motion_type = 10
        county.before_null_outcome = 25
        county.after_null_outcome = 10

        backfill.print_report(stats)
        captured = capsys.readouterr()

        assert "API fails:  3" in captured.out
        assert "No data:    10" in captured.out
        assert "No-update:  5" in captured.out
        assert "Errors:     1" in captured.out
