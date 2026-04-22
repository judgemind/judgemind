"""Tests for backfill_sb_party_names.py title validation integration (#1974).

Verifies that the script validates extracted titles via
is_plausible_case_title() before writing to the database.
All database access is mocked.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
    "archive",
)
sys.path.insert(0, _SCRIPTS_DIR)

# Ensure the scraper-framework src is importable
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

backfill_sb = importlib.import_module("backfill_sb_party_names")


# ---------------------------------------------------------------------------
# _fix_case title validation tests (#1974)
# ---------------------------------------------------------------------------


class TestFixCaseTitleValidation:
    """Tests for is_plausible_case_title() integration in _fix_case."""

    def test_implausible_title_rejected(self) -> None:
        """Implausible titles like 'To Respond, Without Objections' are rejected."""
        conn = MagicMock()
        ruling_text = "To Respond, Without Objections"
        # Patch extract_case_title to return an implausible title.
        # create=True is required because extract_case_title was removed from
        # the archived script in #2178 — the function no longer exists at
        # module level, but _fix_case still calls it via its name so the
        # patch must inject it.
        with patch.object(
            backfill_sb,
            "extract_case_title",
            return_value="To Respond, Without Objections",
            create=True,
        ):
            result = backfill_sb._fix_case(
                conn,
                "test-case-id",
                "Old Garbled Title",
                ruling_text,
                dry_run=True,
            )
        assert result["new_title"] is None

    def test_plausible_title_accepted(self) -> None:
        """Plausible titles like 'Smith v. Jones' are accepted."""
        conn = MagicMock()
        ruling_text = "Smith v. Jones\nThe motion is GRANTED"
        with patch.object(
            backfill_sb, "extract_case_title", return_value="Smith v. Jones", create=True
        ):
            result = backfill_sb._fix_case(
                conn,
                "test-case-id",
                "Old Garbled Title",
                ruling_text,
                dry_run=True,
            )
        assert result["new_title"] == "Smith v. Jones"

    def test_none_extraction_produces_none(self) -> None:
        """When extract_case_title returns None, new_title is None."""
        conn = MagicMock()
        with patch.object(backfill_sb, "extract_case_title", return_value=None, create=True):
            result = backfill_sb._fix_case(
                conn,
                "test-case-id",
                "Old Title",
                "some ruling text",
                dry_run=True,
            )
        assert result["new_title"] is None

    def test_no_ruling_text_produces_none(self) -> None:
        """When ruling_text is None, new_title is None."""
        conn = MagicMock()
        result = backfill_sb._fix_case(
            conn,
            "test-case-id",
            "Old Title",
            None,
            dry_run=True,
        )
        assert result["new_title"] is None

    def test_motion_text_title_rejected(self) -> None:
        """Titles containing ruling fragments like 'GRANTED' are rejected."""
        conn = MagicMock()
        with patch.object(
            backfill_sb, "extract_case_title", return_value="Motion is GRANTED", create=True
        ):
            result = backfill_sb._fix_case(
                conn,
                "test-case-id",
                "Old Title",
                "Motion is GRANTED on this date",
                dry_run=True,
            )
        assert result["new_title"] is None

    def test_for_prefix_rejected(self) -> None:
        """Titles starting with 'For' are rejected."""
        conn = MagicMock()
        with patch.object(
            backfill_sb, "extract_case_title", return_value="For Summary Judgment", create=True
        ):
            result = backfill_sb._fix_case(
                conn,
                "test-case-id",
                "Old Title",
                "For Summary Judgment",
                dry_run=True,
            )
        assert result["new_title"] is None
