"""Tests for the backfill_oc_ruling_text script (#1269).

Verifies that the inlined table-aware PDF extraction logic in the backfill
script produces the same output as the original _extract_oc_pdf_text() in
oc_tentatives.py, and that the main() function correctly updates rulings.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Import the backfill script as a module
_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)
backfill_oc = importlib.import_module("backfill_oc_ruling_text")

# Also import the original for comparison
from courts.ca.oc_tentatives import _extract_oc_pdf_text as _original_extract  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ---------------------------------------------------------------------------
# Extraction parity tests — inlined logic must match original
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture",
    [
        "oc_apkarian_c25.pdf",
        "oc_central_c34.pdf",
        "oc_complex_cx.pdf",
        "oc_costa_mesa_cm.pdf",
        "oc_west_w.pdf",
        "oc_north_n.pdf",
    ],
)
def test_inlined_extraction_matches_original(fixture: str) -> None:
    """The backfill script's inlined extraction must match oc_tentatives.py."""
    pdf_path = os.path.join(FIXTURES_DIR, fixture)
    if not os.path.exists(pdf_path):
        pytest.skip(f"Fixture not found: {fixture}")

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    original_text = _original_extract(pdf_bytes)
    backfill_text = backfill_oc._extract_oc_pdf_text(pdf_bytes)

    assert backfill_text == original_text, (
        f"Extraction mismatch for {fixture}:\n"
        f"  Original (first 200): {original_text[:200]!r}\n"
        f"  Backfill (first 200): {backfill_text[:200]!r}"
    )


def test_extraction_separates_case_name_from_ruling() -> None:
    """Table-aware extraction should not interleave case names with ruling text.

    The original bug (#1241) produced text like:
        "Illingworth vs. Before the court is an unopposed motion"
    After the fix, the case name and ruling text are on separate lines.
    """
    pdf_path = os.path.join(FIXTURES_DIR, "oc_apkarian_c25.pdf")
    if not os.path.exists(pdf_path):
        pytest.skip("Fixture not found: oc_apkarian_c25.pdf")

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    text = backfill_oc._extract_oc_pdf_text(pdf_bytes)
    # The text should NOT contain a "vs." followed immediately by ruling prose
    # on the same line (the garbled pattern).
    import re

    garbled_pattern = re.compile(
        r"vs\.?\s+(?:Before the|The court|This matter|This is|Plaintiff)",
        re.IGNORECASE,
    )
    # Check that no line contains the garbled pattern
    for line in text.split("\n"):
        assert not garbled_pattern.search(line), (
            f"Found garbled text pattern in line: {line[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Main function tests — DB and S3 mocked
# ---------------------------------------------------------------------------


def test_main_dry_run_no_db_writes() -> None:
    """Dry-run mode should not write to the database."""
    pdf_path = os.path.join(FIXTURES_DIR, "oc_apkarian_c25.pdf")
    if not os.path.exists(pdf_path):
        pytest.skip("Fixture not found: oc_apkarian_c25.pdf")

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    ruling_id = "test-ruling-id"
    doc_id = "test-doc-id"
    s3_key = "ca/orange/superior_court/raw/2026/03/01/test.pdf"
    s3_bucket = "judgemind-document-archive-dev"

    # Mock S3
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: pdf_bytes)}

    # Mock DB — return one row on first call, empty on second
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall = MagicMock(
        side_effect=[
            [
                (
                    ruling_id,
                    "garbled old text vs. Before the court",
                    doc_id,
                    s3_key,
                    s3_bucket,
                )
            ],
            [],
        ]
    )
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(sys, "argv", ["backfill_oc_ruling_text.py", "--dry-run"]),
        patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}),
        patch("backfill_oc_ruling_text.boto3") as mock_boto3,
        patch("backfill_oc_ruling_text.psycopg") as mock_psycopg,
    ):
        mock_boto3.client.return_value = mock_s3
        mock_psycopg.connect.return_value = mock_conn

        backfill_oc.main()

    # In dry-run mode, the UPDATE query should never be executed.
    # The cursor.execute calls should only be SELECT queries (fetchall).
    for call in mock_cursor.execute.call_args_list:
        query = call[0][0]
        assert "UPDATE" not in query, f"Unexpected UPDATE in dry-run: {query}"


def test_main_skips_unchanged_text() -> None:
    """If the re-extracted text matches the existing text, skip the update."""
    pdf_path = os.path.join(FIXTURES_DIR, "oc_apkarian_c25.pdf")
    if not os.path.exists(pdf_path):
        pytest.skip("Fixture not found: oc_apkarian_c25.pdf")

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    # Pre-compute what the extraction will produce
    expected_text = backfill_oc._normalize_oc_text(
        backfill_oc._extract_oc_pdf_text(pdf_bytes)
    ).replace("\x00", "")

    ruling_id = "test-ruling-id"
    doc_id = "test-doc-id"
    s3_key = "ca/orange/superior_court/raw/2026/03/01/test.pdf"
    s3_bucket = "judgemind-document-archive-dev"

    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: pdf_bytes)}

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    # The existing text already matches what extraction produces
    mock_cursor.fetchall = MagicMock(
        side_effect=[
            [(ruling_id, expected_text, doc_id, s3_key, s3_bucket)],
            [],
        ]
    )
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    with (
        patch.object(sys, "argv", ["backfill_oc_ruling_text.py"]),
        patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}),
        patch("backfill_oc_ruling_text.boto3") as mock_boto3,
        patch("backfill_oc_ruling_text.psycopg") as mock_psycopg,
    ):
        mock_boto3.client.return_value = mock_s3
        mock_psycopg.connect.return_value = mock_conn

        backfill_oc.main()

    # No UPDATE should have been executed
    for call in mock_cursor.execute.call_args_list:
        query = call[0][0]
        assert "UPDATE" not in query, f"Unexpected UPDATE for unchanged text: {query}"
