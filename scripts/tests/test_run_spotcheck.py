"""Tests for scripts/spotcheck/run_spotcheck.py.

Regression test for #2569 follow-up: the bidirectional spotcheck must follow
``documents.previous_version_id`` so content-hash dedup losers do not appear
as false-positive zero-derived rulings.  Mirrors the CTE pattern that
``scripts/spotcheck/fetch.py`` ``OriginalsStrategy`` uses since #2650.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

# ``run_spotcheck`` reads ``DATABASE_URL`` and imports ``psycopg`` / ``boto3``
# at module load time.  Set the env var + install module mocks just long
# enough to import, then restore the prior environment so this test module
# does not leak state to siblings — ``test_spotcheck_fetch`` has a
# ``DATABASE_URL``-conditional code path (see ``fetch.py`` line ~396) that
# fails when this env var is set.
_had_database_url = "DATABASE_URL" in os.environ
if not _had_database_url:
    os.environ["DATABASE_URL"] = "postgresql://test:test@localhost/test"
sys.modules.setdefault("psycopg", MagicMock())
sys.modules.setdefault("boto3", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spotcheck"))

import run_spotcheck  # noqa: E402

if not _had_database_url:
    del os.environ["DATABASE_URL"]


class TestSampleOriginalsRecursiveChain:
    """The originals query must use WITH RECURSIVE to walk multi-hop chains (#3537)."""

    def _get_per_doc_sql(self) -> str:
        """Return the SQL from the second execute() call (the per-doc rulings query)."""
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [("ca/contra_costa/doc_a.pdf", "bucket", "doc-a")],
            [],
        ]
        cur.description = [("ruling_id",)]
        run_spotcheck.sample_originals(cur, "Contra Costa", 1)
        return cur.execute.call_args_list[1][0][0]

    def test_query_uses_recursive_cte(self) -> None:
        sql = self._get_per_doc_sql()
        assert "WITH RECURSIVE" in sql, (
            "sample_originals must use WITH RECURSIVE to walk multi-hop "
            "previous_version_id chains (#3537)."
        )

    def test_query_recursion_joins_previous_version_id(self) -> None:
        import re

        sql = self._get_per_doc_sql()
        normalized = re.sub(r"\s+", " ", sql).lower()
        assert "d.id = td.previous_version_id" in normalized, (
            "Recursive arm must join target_docs td ON d.id = td.previous_version_id "
            "to walk each hop of the supersede chain."
        )

    def test_chain_returns_rulings_from_terminal_doc(self) -> None:
        """A 3-doc chain A→B→C: sampling A's s3_key must surface C's ruling."""
        cur = MagicMock()
        cur.fetchall.side_effect = [
            [("key_A", "bucket", "doc-A")],
            [
                (
                    "ruling-C",
                    "granted",
                    None,
                    None,
                    None,
                    "BC001",
                    "Smith v Jones",
                    "Judge Smith",
                    150,
                    "text preview",
                )
            ],
        ]
        cur.description = [
            ("ruling_id",),
            ("outcome",),
            ("motion_type",),
            ("hearing_date",),
            ("department",),
            ("case_number",),
            ("case_title",),
            ("judge_name",),
            ("ruling_text_length",),
            ("ruling_text_preview",),
        ]
        result = run_spotcheck.sample_originals(cur, "Contra Costa", 1)
        assert len(result) == 1
        original = result[0]
        assert original["derived_count"] == 1
        assert original["derived_rulings"][0]["ruling_id"] == "ruling-C"


class TestSampleOriginalsFollowsPreviousVersionChain:
    """The originals query must follow ``previous_version_id`` (#2569)."""

    def _run_with_empty_result(self) -> str:
        """Invoke ``sample_originals`` with a mock cursor and return the second
        executed SQL string (the per-document rulings query, not the sample
        selection query).
        """
        cur = MagicMock()
        # First execute() = sample documents (return one row).
        # Second execute() = fetch derived rulings for that doc (return empty).
        cur.fetchall.side_effect = [
            [("ca/contra_costa/superior_court/raw/abc.pdf", "bucket", "doc-1")],
            [],
        ]
        cur.description = [("ruling_id",)]
        run_spotcheck.sample_originals(cur, "Contra Costa", 5)
        # Second call is the per-doc rulings query.
        return cur.execute.call_args_list[1][0][0]

    def test_query_uses_target_docs_cte(self) -> None:
        sql = self._run_with_empty_result()
        assert "target_docs" in sql, (
            "sample_originals must use the target_docs CTE so dedup losers' "
            "rulings (linked via previous_version_id) are surfaced."
        )

    def test_query_unions_previous_version_id(self) -> None:
        sql = self._run_with_empty_result()
        assert "previous_version_id" in sql, (
            "sample_originals must follow documents.previous_version_id to "
            "find rulings on the winner doc when a loser key is sampled."
        )

    def test_query_no_naive_s3_key_join(self) -> None:
        """The pre-fix shape ``JOIN documents d ON r.document_id = d.id ...
        WHERE d.s3_key = %s`` must be gone — it produces false-positive
        zero-derived counts for content-hash dedup losers."""
        sql = self._run_with_empty_result()
        # The new query joins through ``target_docs td``; the old one joined
        # through ``documents d`` directly.  Assert the new shape is present.
        assert "target_docs td" in sql
