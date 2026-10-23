"""Tests for the reingest_from_s3 script.

The script chooses S3 objects (DB-row mode from ``documents`` rows, prefix
mode from an S3 listing) and runs each through
``IngestionWorker.process_event`` (#4845).  These tests cover key selection,
the per-key driver, the judge pre-pass, failure accounting and the CLI.
What the worker writes is tested by the worker's own tests and by the
real-Postgres invariant harness (``tests/test_split_invariants_pg.py``),
which runs DB-row mode end to end.  All database and S3 access here is
mocked.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import uuid
from datetime import date, datetime
from typing import Any
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

# Ensure the scraper-framework src is importable (needed for auto-discovery)
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

from ingestion.split_ids import make_split_document_id  # noqa: E402

reingest = importlib.import_module("reingest_from_s3")


def _cursor_conn(rows: list[tuple]) -> tuple[MagicMock, MagicMock]:
    """A mock connection whose cursor returns *rows*; returns (conn, cursor)."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=cur)
    ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


# ---------------------------------------------------------------------------
# DB-row mode: key selection + delegation to the shared per-key driver
# ---------------------------------------------------------------------------


class TestSelectDbKeys:
    """``select_db_keys`` turns the row filters into a list of S3 keys."""

    def test_returns_each_key_once_in_query_order(self) -> None:
        conn, cur = _cursor_conn([("ca/orange/superior_court/raw/a.pdf",), ("k2",)])
        keys = reingest.select_db_keys(conn, "AND ct.county = %s", ["Orange"])
        assert keys == ["ca/orange/superior_court/raw/a.pdf", "k2"]
        sql, params = cur.execute.call_args.args
        # One row per key: the split children of one object share its key.
        assert "GROUP BY d.s3_key" in sql
        assert "d.status = 'active'" in sql
        assert "AND ct.county = %s" in sql
        assert "LIMIT" not in sql
        assert params == ["Orange"]

    def test_limit_is_a_key_limit(self) -> None:
        conn, cur = _cursor_conn([])
        reingest.select_db_keys(conn, "", [], limit=7)
        sql, params = cur.execute.call_args.args
        assert sql.rstrip().endswith("LIMIT %s")
        assert params == [7]

    def test_filters_come_from_build_filters(self) -> None:
        filters, params = reingest._build_filters(
            "Fresno", date(2026, 1, 1), None, s3_key_list=["k1", "k2"]
        )
        conn, cur = _cursor_conn([])
        reingest.select_db_keys(conn, filters, params)
        sql, sent = cur.execute.call_args.args
        assert "AND d.s3_key = ANY(%s)" in sql
        assert sent[0] == "Fresno"
        assert sent[-1] == ["k1", "k2"]


class TestRunReingestDbMode:
    """``run_reingest`` selects keys, then runs the same per-key driver as
    prefix mode, so the worker is the only writer (#4845)."""

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.select_db_keys")
    @patch("reingest_from_s3.psycopg")
    def test_selected_keys_go_to_the_shared_driver(
        self, mock_psycopg: MagicMock, mock_select: MagicMock, mock_run_keys: MagicMock
    ) -> None:
        mock_select.return_value = ["k1", "k2"]
        mock_run_keys.return_value = {"total_keys": 2, "processed": 2, "errors": 0}
        with patch.dict(os.environ, {"JUDGEMIND_ARCHIVE_BUCKET": "archive"}):
            stats = reingest.run_reingest(
                "postgres://x",
                county="Orange",
                limit=5,
                bust_llm_cache=True,
                concurrency=3,
                write_failed_manifest="s3://b/failed.txt",
            )
        assert stats["processed"] == 2
        filters, params = mock_select.call_args.args[1:]
        assert "AND ct.county = %s" in filters
        assert params == ["Orange"]
        assert mock_select.call_args.kwargs == {"limit": 5}
        args, kwargs = mock_run_keys.call_args
        assert args == ("postgres://x", ["k1", "k2"])
        assert kwargs["bucket"] == "archive"
        assert kwargs["bust_llm_cache"] is True
        assert kwargs["concurrency"] == 3
        assert kwargs["write_failed_manifest"] == "s3://b/failed.txt"

    @patch("reingest_from_s3._reingest_keys")
    @patch("reingest_from_s3.select_db_keys", return_value=[])
    @patch("reingest_from_s3.psycopg")
    def test_no_matching_rows_processes_nothing(
        self, mock_psycopg: MagicMock, mock_select: MagicMock, mock_run_keys: MagicMock
    ) -> None:
        stats = reingest.run_reingest("postgres://x", county="Nowhere")
        assert stats == {"total_keys": 0, "processed": 0, "errors": 0, "skipped": 0}
        mock_run_keys.assert_not_called()

    def test_db_mode_has_no_write_logic_of_its_own(self) -> None:
        """The DB-row write path is gone (#4845); re-adding one would bring
        back the divergence that produced #4838 / #4839."""
        for name in (
            "reingest_batch",
            "_reparse_document",
            "_full_reparse_document",
            "_reparse_document_multimodal",
            "_supersede_document",
            "_drop_search_orphans",
            "FETCH_DOCUMENTS_QUERY",
        ):
            assert not hasattr(reingest, name), name
        source = open(reingest.__file__, encoding="utf-8").read()
        for writer in (
            "insert_document_and_ruling",
            "delete_stale_split_children",
            "insert_ruling(",
            "upsert_case(",
        ):
            assert writer not in source, writer

    @patch("reingest_from_s3.run_reingest")
    def test_cli_db_mode_forwards_filters(self, mock_run: MagicMock) -> None:
        mock_run.return_value = {"total_keys": 0, "processed": 0, "errors": 0, "skipped": 0}
        argv = [
            "reingest_from_s3",
            "--county",
            "Orange",
            "--date-from",
            "2026-01-02",
            "--department-in",
            "C11",
            "C20",
            "--limit",
            "4",
            "--bust-llm-cache",
        ]
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgres://test"}),
            patch("sys.argv", argv),
        ):
            reingest.main()
        kwargs = mock_run.call_args.kwargs
        assert kwargs["county"] == "Orange"
        assert kwargs["date_from"] == date(2026, 1, 2)
        assert kwargs["department_in"] == ["C11", "C20"]
        assert kwargs["limit"] == 4
        assert kwargs["bust_llm_cache"] is True

    @pytest.mark.parametrize("flag", ["--full-reparse", "--multimodal", "--no-llm"])
    def test_removed_flags_are_rejected(self, flag: str) -> None:
        with patch("sys.argv", ["reingest_from_s3", flag]), pytest.raises(SystemExit):
            reingest._build_parser().parse_args()


class TestFetchS3Content:
    """Tests for the _fetch_s3_content helper."""

    def test_returns_body_bytes(self) -> None:
        s3 = MagicMock()
        body_mock = MagicMock()
        body_mock.read.return_value = b"<html>ruling</html>"
        s3.get_object.return_value = {"Body": body_mock}

        result = reingest._fetch_s3_content(s3, "my-bucket", "my-key")

        s3.get_object.assert_called_once_with(Bucket="my-bucket", Key="my-key")
        assert result == b"<html>ruling</html>"


# ---------------------------------------------------------------------------
# _build_filters
# ---------------------------------------------------------------------------


class TestBuildFilters:
    """Unit tests for the _build_filters helper."""

    def test_no_filters_returns_empty(self) -> None:
        clauses, params = reingest._build_filters(None, None, None)
        assert clauses == ""
        assert params == []

    def test_county_only(self) -> None:
        clauses, params = reingest._build_filters("Orange", None, None)
        assert "AND ct.county = %s" in clauses
        assert params == ["Orange"]

    def test_case_title_regex_adds_clause(self) -> None:
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(None, None, None, case_title_regex=regex)
        assert "AND c.case_title ~ %s" in clauses
        assert regex in params

    def test_county_and_case_title_regex_combined(self) -> None:
        regex = r"(?i)(Before the Court|moves the)"
        clauses, params = reingest._build_filters("Orange", None, None, case_title_regex=regex)
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert params == ["Orange", regex]

    def test_null_motion_type_adds_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, null_motion_type=True)
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        # No additional params needed for this filter
        assert params == []

    def test_null_motion_type_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, null_motion_type=False)
        assert clauses == ""
        assert params == []

    def test_null_motion_type_with_county(self) -> None:
        clauses, params = reingest._build_filters("San Diego", None, None, null_motion_type=True)
        assert "AND ct.county = %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == ["San Diego"]

    def test_null_motion_type_with_county_and_case_title_regex(self) -> None:
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(
            "San Diego", None, None, case_title_regex=regex, null_motion_type=True
        )
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == ["San Diego", regex]

    def test_orphaned_only_adds_not_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, orphaned_only=True)
        assert "AND NOT EXISTS" in clauses
        assert "r.document_id = d.id" in clauses
        assert params == []

    def test_orphaned_only_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, orphaned_only=False)
        assert clauses == ""
        assert params == []

    def test_orphaned_only_with_county(self) -> None:
        clauses, params = reingest._build_filters("Riverside", None, None, orphaned_only=True)
        assert "AND ct.county = %s" in clauses
        assert "AND NOT EXISTS" in clauses
        assert "r.document_id = d.id" in clauses
        assert params == ["Riverside"]

    def test_orphaned_only_and_null_motion_type_mutually_exclusive_in_practice(
        self,
    ) -> None:
        """Both flags can be set but produce contradictory logic (no doc can
        have no rulings AND have a ruling with NULL motion_type). Verify both
        clauses are emitted so the query returns an empty set gracefully."""
        clauses, params = reingest._build_filters(
            None, None, None, null_motion_type=True, orphaned_only=True
        )
        assert "AND EXISTS" in clauses
        assert "AND NOT EXISTS" in clauses

    def test_case_number_like_adds_clause(self) -> None:
        pattern = "UNKNOWN-%"
        clauses, params = reingest._build_filters(None, None, None, case_number_like=pattern)
        assert "AND c.case_number LIKE %s" in clauses
        assert pattern in params

    def test_case_number_like_none_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, case_number_like=None)
        assert clauses == ""
        assert params == []

    def test_case_number_like_with_county(self) -> None:
        pattern = "UNKNOWN-%"
        clauses, params = reingest._build_filters("Orange", None, None, case_number_like=pattern)
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_number LIKE %s" in clauses
        assert params == ["Orange", pattern]

    def test_case_number_like_with_case_title_regex(self) -> None:
        """Both case_number_like and case_title_regex can be combined."""
        pattern = "UNKNOWN-%"
        regex = r"(?i)Before the Court"
        clauses, params = reingest._build_filters(
            "Orange",
            None,
            None,
            case_number_like=pattern,
            case_title_regex=regex,
        )
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_number LIKE %s" in clauses
        assert "AND c.case_title ~ %s" in clauses
        assert params == ["Orange", pattern, regex]

    def test_case_number_like_param_order_before_case_title_regex(self) -> None:
        """case_number_like param appears before case_title_regex in the
        param list, matching the clause order in the SQL query."""
        pattern = "UNKNOWN-%"
        regex = r"vs\.?\s*$"
        clauses, params = reingest._build_filters(
            None, None, None, case_number_like=pattern, case_title_regex=regex
        )
        assert params.index(pattern) < params.index(regex)

    def test_filter_null_outcome_adds_exists_subquery(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, filter_null_outcome=True)
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        # No additional params needed for this filter
        assert params == []

    def test_filter_null_outcome_false_no_clause(self) -> None:
        clauses, params = reingest._build_filters(None, None, None, filter_null_outcome=False)
        assert clauses == ""
        assert params == []

    def test_filter_null_outcome_with_county(self) -> None:
        clauses, params = reingest._build_filters(
            "Santa Clara", None, None, filter_null_outcome=True
        )
        assert "AND ct.county = %s" in clauses
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        assert params == ["Santa Clara"]

    def test_filter_null_outcome_and_orphaned_only_contradictory(self) -> None:
        """Both flags can be set but produce contradictory logic (no doc can
        have no rulings AND have a ruling with NULL outcome). Verify both
        clauses are emitted so the query returns an empty set gracefully."""
        clauses, params = reingest._build_filters(
            None, None, None, filter_null_outcome=True, orphaned_only=True
        )
        assert "AND EXISTS" in clauses
        assert "r.outcome IS NULL" in clauses
        assert "AND NOT EXISTS" in clauses

    def test_filter_null_outcome_with_null_motion_type(self) -> None:
        """Both null-outcome and null-motion-type can be combined to target
        documents that have rulings with both NULL outcome and NULL motion_type."""
        clauses, params = reingest._build_filters(
            None, None, None, filter_null_outcome=True, null_motion_type=True
        )
        assert "r.outcome IS NULL" in clauses
        assert "r.motion_type IS NULL" in clauses
        assert params == []

    def test_department_in_adds_clause(self) -> None:
        """Passing a non-empty department_in list emits the EXISTS subquery for
        the any-ruling semantic and appends the list as the corresponding param."""
        depts = ["C32", "C11"]
        clauses, params = reingest._build_filters(None, None, None, department_in=depts)
        assert "AND EXISTS" in clauses
        assert "r.department = ANY(%s)" in clauses
        assert depts in params

    def test_department_in_uses_any_ruling_semantic_not_latest(self) -> None:
        """The --department-in filter uses an EXISTS subquery that matches a
        document if *any* of its rulings has the given department — not just
        the latest ruling.  The SQL must contain EXISTS and r.department =
        ANY(%s), and must NOT use LATERAL or ORDER BY ruled_on DESC (which
        would imply a latest-ruling join)."""
        depts = ["C11"]
        clauses, params = reingest._build_filters(None, None, None, department_in=depts)
        # Confirm the any-ruling shape is present
        assert "EXISTS" in clauses
        assert "r.department = ANY(%s)" in clauses
        # Confirm no latest-ruling join is used
        assert "LATERAL" not in clauses
        assert "ORDER BY ruled_on DESC" not in clauses
        # A second selector value produces the same clause shape
        depts2 = ["C99"]
        clauses2, params2 = reingest._build_filters(None, None, None, department_in=depts2)
        assert "EXISTS" in clauses2
        assert "r.department = ANY(%s)" in clauses2
        assert "LATERAL" not in clauses2
        assert "ORDER BY ruled_on DESC" not in clauses2

    def test_department_in_none_no_clause(self) -> None:
        """Passing department_in=None emits no clause."""
        clauses, params = reingest._build_filters(None, None, None, department_in=None)
        assert "department" not in clauses
        assert params == []

    def test_department_in_empty_list_no_clause(self) -> None:
        """Passing an empty department_in list emits no clause."""
        clauses, params = reingest._build_filters(None, None, None, department_in=[])
        assert "department" not in clauses
        assert params == []

    def test_department_in_with_county_and_case_number_like(self) -> None:
        """Combined county + case_number_like + department_in; params must be
        ordered [county, case_number_like, dept_list] matching insertion order."""
        depts = ["C32", "C11"]
        pattern = "UNKNOWN-%"
        clauses, params = reingest._build_filters(
            "Orange",
            None,
            None,
            case_number_like=pattern,
            department_in=depts,
        )
        assert "AND ct.county = %s" in clauses
        assert "AND c.case_number LIKE %s" in clauses
        assert "r.department = ANY(%s)" in clauses
        assert params == ["Orange", pattern, depts]

    def test_s3_key_list_adds_any_clause(self) -> None:
        """A non-empty s3_key_list emits ``AND d.s3_key = ANY(%s)`` and
        appends the list verbatim as the corresponding param."""
        keys = [
            "ca/santa_clara/superior_court/raw/aaa.pdf",
            "ca/santa_clara/superior_court/raw/bbb.pdf",
        ]
        clauses, params = reingest._build_filters(None, None, None, s3_key_list=keys)
        assert "AND d.s3_key = ANY(%s)" in clauses
        assert params == [keys]

    def test_s3_key_list_none_no_clause(self) -> None:
        """Passing s3_key_list=None emits no clause."""
        clauses, params = reingest._build_filters(None, None, None, s3_key_list=None)
        assert "s3_key" not in clauses
        assert params == []

    def test_s3_key_list_empty_no_clause(self) -> None:
        """Passing an empty s3_key_list emits no clause (degenerate input
        guarded at the CLI layer, but the SQL builder must not emit
        ``= ANY('{}'::text[])`` which matches no rows accidentally)."""
        clauses, params = reingest._build_filters(None, None, None, s3_key_list=[])
        assert "s3_key" not in clauses
        assert params == []

    def test_s3_key_list_with_county(self) -> None:
        """Combined county + s3_key_list; both clauses present and params
        ordered [county, key_list]."""
        keys = ["ca/santa_clara/superior_court/raw/abc.pdf"]
        clauses, params = reingest._build_filters("Santa Clara", None, None, s3_key_list=keys)
        assert "AND ct.county = %s" in clauses
        assert "AND d.s3_key = ANY(%s)" in clauses
        assert params == ["Santa Clara", keys]


# ---------------------------------------------------------------------------
# _read_s3_key_list_file
# ---------------------------------------------------------------------------


class TestReadS3KeyListFile:
    """Unit tests for the ``_read_s3_key_list_file`` helper."""

    def test_reads_keys_one_per_line(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "keys.txt"  # type: ignore[operator]
        path.write_text(
            "ca/santa_clara/superior_court/raw/aaa.pdf\n"
            "ca/santa_clara/superior_court/raw/bbb.pdf\n",
            encoding="utf-8",
        )
        keys = reingest._read_s3_key_list_file(str(path))
        assert keys == [
            "ca/santa_clara/superior_court/raw/aaa.pdf",
            "ca/santa_clara/superior_court/raw/bbb.pdf",
        ]

    def test_strips_whitespace_and_skips_blank_lines(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "keys.txt"  # type: ignore[operator]
        path.write_text(
            "\n  ca/foo.pdf  \n\n  ca/bar.pdf\n\n",
            encoding="utf-8",
        )
        keys = reingest._read_s3_key_list_file(str(path))
        assert keys == ["ca/foo.pdf", "ca/bar.pdf"]

    def test_skips_comment_lines(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "keys.txt"  # type: ignore[operator]
        path.write_text(
            "# this list was extracted from #3659 spotcheck\n"
            "ca/foo.pdf\n"
            "  # indented comments are stripped too\n"
            "ca/bar.pdf\n",
            encoding="utf-8",
        )
        keys = reingest._read_s3_key_list_file(str(path))
        assert keys == ["ca/foo.pdf", "ca/bar.pdf"]

    def test_deduplicates_preserving_first_order(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "keys.txt"  # type: ignore[operator]
        path.write_text(
            "ca/foo.pdf\nca/bar.pdf\nca/foo.pdf\n",
            encoding="utf-8",
        )
        keys = reingest._read_s3_key_list_file(str(path))
        assert keys == ["ca/foo.pdf", "ca/bar.pdf"]

    def test_empty_file_raises_value_error(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "empty.txt"  # type: ignore[operator]
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="empty"):
            reingest._read_s3_key_list_file(str(path))

    def test_only_comments_and_blanks_raises_value_error(self, tmp_path: object) -> None:
        """An all-comments / all-blank file is the same defect class as an
        empty file: silently matching every document is dangerous, so fail
        loud."""
        from pathlib import Path as _Path

        path: _Path = tmp_path / "comments.txt"  # type: ignore[operator]
        path.write_text(
            "# only comments\n\n# and blanks\n   \n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="empty"):
            reingest._read_s3_key_list_file(str(path))

    def test_missing_file_raises_filenotfound(self, tmp_path: object) -> None:
        from pathlib import Path as _Path

        path: _Path = tmp_path / "does_not_exist.txt"  # type: ignore[operator]
        with pytest.raises(FileNotFoundError):
            reingest._read_s3_key_list_file(str(path))

    def test_s3_uri_reads_keys_from_s3(self) -> None:
        """An ``s3://bucket/key`` URI fetches the keylist body from S3 and
        applies the same parsing semantics as a local file."""
        body_mock = MagicMock()
        body_mock.read.return_value = (
            b"# extracted from #3855 spotcheck\n"
            b"ca/orange/superior_court/raw/aaa.pdf\n"
            b"\n"
            b"  ca/orange/superior_court/raw/bbb.pdf  \n"
            b"ca/orange/superior_court/raw/aaa.pdf\n"
        )
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body_mock}
        with patch.object(reingest.boto3, "client", return_value=s3) as mock_client:
            keys = reingest._read_s3_key_list_file(
                "s3://judgemind-assets-dev/keylists/orange-78.txt"
            )
        assert keys == [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
        ]
        mock_client.assert_called_once_with("s3")
        s3.get_object.assert_called_once_with(
            Bucket="judgemind-assets-dev",
            Key="keylists/orange-78.txt",
        )

    def test_s3_uri_empty_object_raises_value_error(self) -> None:
        """An s3:// object whose body is empty (or all comments/blanks) raises
        ValueError, matching the local-file fail-loud behavior."""
        body_mock = MagicMock()
        body_mock.read.return_value = b"# only comments\n\n   \n"
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": body_mock}
        with patch.object(reingest.boto3, "client", return_value=s3):
            with pytest.raises(ValueError, match="empty"):
                reingest._read_s3_key_list_file("s3://bucket/keylists/empty.txt")

    def test_s3_uri_without_key_raises_value_error(self) -> None:
        """A malformed ``s3://bucket-only`` URI with no key component raises a
        clear ValueError before any S3 call is made."""
        with patch.object(reingest.boto3, "client") as mock_client:
            with pytest.raises(ValueError, match="s3://"):
                reingest._read_s3_key_list_file("s3://bucket-only")
        mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_pdf_text_subprocess tests
# ---------------------------------------------------------------------------


class TestExtractPdfTextSubprocess:
    """Tests for the subprocess-based PDF text extraction."""

    def test_extracts_text_from_real_pdf(self) -> None:
        """Subprocess extraction produces readable text from a real PDF."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        result = reingest._extract_pdf_text_subprocess(pdf_bytes, timeout=30.0)
        assert result is not None
        assert len(result) > 100
        assert "%PDF" not in result

    def test_returns_none_on_invalid_pdf(self) -> None:
        """Invalid PDF bytes return None (subprocess exits non-zero)."""
        result = reingest._extract_pdf_text_subprocess(b"not a real pdf", timeout=5.0)
        assert result is None

    def test_returns_none_on_timeout(self) -> None:
        """A very short timeout returns None without hanging."""
        # Use a tiny timeout that the subprocess can't possibly meet
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # 0.001s timeout — subprocess won't even start pdfplumber in time
        result = reingest._extract_pdf_text_subprocess(pdf_bytes, timeout=0.001)
        # Should return None (timeout), not hang
        assert result is None


# ---------------------------------------------------------------------------
# CLI argument tests
# ---------------------------------------------------------------------------


class TestCLIConcurrencyFlag:
    """Tests that --concurrency CLI flag is properly parsed."""

    def test_parser_has_concurrency_arg(self) -> None:
        """The argument parser accepts --concurrency."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--county", type=str, default=None)
        parser.add_argument("--date-from", type=str, default=None)
        parser.add_argument("--date-to", type=str, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--batch-size", type=int, default=200)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--concurrency", type=int, default=10)

        args = parser.parse_args(["--concurrency", "20"])
        assert args.concurrency == 20

    def test_parser_default_concurrency(self) -> None:
        """Default concurrency is 10 when not specified."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--concurrency", type=int, default=10)
        args = parser.parse_args([])
        assert args.concurrency == 10


# ---------------------------------------------------------------------------
# _extract_text_from_content tests
# ---------------------------------------------------------------------------


class TestExtractTextFromContent:
    """Tests for the _extract_text_from_content helper."""

    def test_html_decoded_as_utf8(self) -> None:
        """HTML content is decoded as UTF-8."""
        html = b"<html><body>Hello world</body></html>"
        result = reingest._extract_text_from_content(html, "html")
        assert "Hello world" in result

    def test_pdf_extracted_via_subprocess(self) -> None:
        """PDF content is extracted via pdfplumber subprocess."""
        fixtures_dir = os.path.join(os.path.dirname(__file__), "fixtures")
        pdf_path = os.path.join(fixtures_dir, "oc_apkarian_c25.pdf")
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        result = reingest._extract_text_from_content(pdf_bytes, "pdf")
        # pdfplumber should extract readable text
        assert len(result) > 100
        # Should NOT contain PDF binary garbage
        assert "%PDF" not in result
        # Should contain actual ruling text
        assert "TENTATIVE" in result.upper() or "DEPT" in result.upper()

    def test_pdf_fallback_to_utf8_on_invalid_pdf(self) -> None:
        """Invalid PDF bytes fall back to UTF-8 decode."""
        fake_pdf = b"not a real pdf"
        result = reingest._extract_text_from_content(fake_pdf, "pdf")
        assert result == "not a real pdf"

    def test_pdf_ocr_fallback_for_image_only_pdf(self) -> None:
        """Image-only PDF uses OCR fallback instead of lossy UTF-8 decode."""
        # Simulate a valid PDF where pdfplumber returns no text (image-only).
        # The subprocess returns None, then extract_text_from_pdf (OCR) is called.
        fake_pdf = b"%PDF-1.4 fake image-only pdf content"
        ocr_text = "OCR extracted text from court document"

        with (
            patch.object(
                reingest,
                "_extract_pdf_text_subprocess",
                return_value=None,
            ),
            patch(
                "reingest_from_s3.extract_text_from_pdf",
                return_value=ocr_text,
            ) as mock_ocr,
        ):
            result = reingest._extract_text_from_content(fake_pdf, "pdf")

        # Should use OCR result, not garbled UTF-8 decode
        assert result == ocr_text
        mock_ocr.assert_called_once_with(fake_pdf)

    def test_pdf_falls_back_to_utf8_when_ocr_also_fails(self) -> None:
        """When both pdfplumber and OCR fail, falls back to UTF-8 decode."""
        fake_pdf = b"not a real pdf"

        with (
            patch.object(
                reingest,
                "_extract_pdf_text_subprocess",
                return_value=None,
            ),
            patch(
                "reingest_from_s3.extract_text_from_pdf",
                return_value=None,
            ),
        ):
            result = reingest._extract_text_from_content(fake_pdf, "pdf")

        # Should fall back to UTF-8 decode
        assert result == "not a real pdf"

    def test_unknown_format_decoded_as_utf8(self) -> None:
        """Unknown format is decoded as UTF-8."""
        content = b"some text content"
        result = reingest._extract_text_from_content(content, "text")
        assert result == "some text content"


# ---------------------------------------------------------------------------
# ExtractionMethod.NONE short-circuit (#4056)
#
# scripts/reingest_from_s3.py historically only consulted
# get_county_extraction_config to read ``max_output_tokens`` (#2355) and ran
# the LLM regardless of the configured ``method``.  That mismatch caused
# ``ExtractionMethod.NONE`` counties (Federal CourtListener clusters, #3967)
# to be re-run through the CA-tuned LLM prompt during reingest, producing
# truncation, JSON-parse errors, and field contamination that blocked
# #3978's federal cleanup pass.  These tests pin the new guard.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# normalize_outcome tests — verifies reingest uses the shared function
# from ingestion.extract (#1878)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Schema validation for _QUALITY_QUERIES
# ---------------------------------------------------------------------------

_SCHEMA_SQL_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "api",
    "src",
    "data-access",
    "schema.sql",
)


def _parse_schema_tables(schema_path: str) -> dict[str, set[str]]:
    """Parse schema.sql to extract {table_name: {column_names}}.

    Only parses public-schema ``CREATE TABLE`` blocks (skips staging.*).
    """
    import re

    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()

    tables: dict[str, set[str]] = {}

    # Match CREATE TABLE [schema.]<name> ( ... );  — skip staging schema tables
    create_re = re.compile(
        r"CREATE\s+TABLE\s+(?!staging\.)(?:(?:public|derived|telemetry)\.)?(\w+)\s*\((.*?)\);",
        re.DOTALL | re.IGNORECASE,
    )

    for match in create_re.finditer(sql):
        table_name = match.group(1).lower()
        body = match.group(2)
        columns: set[str] = set()

        for line in body.split("\n"):
            line = line.strip()
            # Skip empty lines, comments, constraints, and PRIMARY KEY lines
            if not line or line.startswith("--"):
                continue
            if re.match(r"(?i)(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY)\b", line):
                continue
            # First word is the column name (if it's a valid identifier)
            col_match = re.match(r"(\w+)\s+", line)
            if col_match:
                col_name = col_match.group(1).lower()
                # Skip SQL keywords that aren't column names
                if col_name in {"constraint", "primary", "unique", "check", "foreign"}:
                    continue
                columns.add(col_name)

        if columns:
            tables[table_name] = columns

    return tables


def _parse_query_references(
    query: str,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Parse a SQL query to extract table aliases and column references.

    Handles both aliased tables (``FROM cases c``) and unaliased tables
    (``FROM cases WHERE ...``).  When no alias is present, the table name
    itself is used as the lookup key so that ``cases.id`` is validated.

    Returns:
        aliases: {alias_or_table_name -> table_name}
        column_refs: [(alias_or_table_name, column_name), ...]
    """
    import re

    # Normalise whitespace (collapse newlines/tabs into spaces)
    query = " ".join(query.split())

    # Strip the {county_filter} placeholder so it doesn't confuse parsing
    query = query.replace("{county_filter}", "")

    aliases: dict[str, str] = {}

    # SQL clause keywords — if one of these follows a table name, it means
    # the table has no explicit alias.
    clause_keywords = {
        "on",
        "where",
        "left",
        "right",
        "inner",
        "outer",
        "cross",
        "join",
        "group",
        "order",
        "having",
        "limit",
        "and",
        "or",
        "not",
        "set",
    }

    # Extract FROM/JOIN table references with an optional alias.
    # Group 1 = table name, Group 2 = next token (alias or keyword, optional).
    table_re = re.compile(
        r"(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?",
        re.IGNORECASE,
    )
    for m in table_re.finditer(query):
        table_name = m.group(1).lower()
        potential_alias = m.group(2)

        if potential_alias and potential_alias.lower() not in clause_keywords:
            # Explicit alias present (e.g. ``FROM cases c``)
            aliases[potential_alias.lower()] = table_name
        else:
            # No alias or next token is a keyword — use table name itself
            # so that ``table.column`` references are still validated.
            aliases[table_name] = table_name

    # Extract column references: alias.column patterns
    col_ref_re = re.compile(r"\b(\w+)\.(\w+)\b")
    column_refs: list[tuple[str, str]] = []
    for m in col_ref_re.finditer(query):
        alias = m.group(1).lower()
        column = m.group(2).lower()
        # Only include references whose prefix matches a known alias/table
        if alias in aliases:
            column_refs.append((alias, column))

    return aliases, column_refs


# ---------------------------------------------------------------------------
# Prefix-mode tests
# ---------------------------------------------------------------------------


class TestParseS3Key:
    """Tests for _parse_s3_key."""

    def test_valid_key(self) -> None:
        key = "federal/federal/courtlistener/raw/abc123def456.html"
        result = reingest._parse_s3_key(key)
        assert result is not None
        assert result["state"] == "federal"
        assert result["county"] == "federal"
        assert result["court"] == "courtlistener"
        assert result["content_hash"] == "abc123def456"
        assert result["ext"] == "html"

    def test_valid_key_with_underscores(self) -> None:
        key = "ca/los_angeles/los_angeles_superior_court/raw/deadbeef.pdf"
        result = reingest._parse_s3_key(key)
        assert result is not None
        assert result["state"] == "ca"
        assert result["county"] == "los_angeles"
        assert result["court"] == "los_angeles_superior_court"
        assert result["ext"] == "pdf"

    def test_invalid_key_returns_none(self) -> None:
        assert reingest._parse_s3_key("not/a/valid/key") is None
        assert reingest._parse_s3_key("") is None
        assert reingest._parse_s3_key("federal/federal/raw/abc.html") is None

    def test_non_hex_hash_rejected(self) -> None:
        """Content hash must be hex digits only."""
        assert reingest._parse_s3_key("ca/la/court/raw/NOTAHEX.html") is None


class TestUnsluggify:
    """Tests for _unsluggify."""

    def test_short_code_uppercased(self) -> None:
        assert reingest._unsluggify("ca") == "CA"
        assert reingest._unsluggify("ny") == "NY"

    def test_slug_to_title_case(self) -> None:
        assert reingest._unsluggify("los_angeles") == "Los Angeles"
        assert reingest._unsluggify("federal") == "Federal"

    def test_single_char(self) -> None:
        assert reingest._unsluggify("a") == "A"


class TestDeriveCourtCode:
    """Tests for _derive_court_code — must match ingestion.db._derive_court_code."""

    def test_basic(self) -> None:
        assert reingest._derive_court_code("CA", "Orange") == "ca-orange"

    def test_multi_word(self) -> None:
        assert reingest._derive_court_code("CA", "Los Angeles") == "ca-los-angeles"

    def test_federal(self) -> None:
        assert reingest._derive_court_code("Federal", "Federal") == "federal-federal"


class TestDiscoverCourts:
    """Tests for _discover_courts."""

    def test_discovers_unique_courts(self) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "ca/los_angeles/superior/raw/ccc333.pdf",
        ]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 2
        codes = {c["court_code"] for c in courts}
        assert "federal-federal" in codes
        assert "ca-los-angeles" in codes

    def test_deduplicates_by_court_code(self) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 1
        assert courts[0]["court_code"] == "federal-federal"
        assert courts[0]["state"] == "Federal"
        assert courts[0]["county"] == "Federal"
        assert courts[0]["court_name"] == "Courtlistener, County of Federal"

    def test_court_code_matches_ingestion_format(self) -> None:
        """court_code must use {state}-{county} format, not {state}_{county}_{court}."""
        keys = ["ca/santa_clara/superior_court/raw/abc123.html"]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 1
        assert courts[0]["court_code"] == "ca-santa-clara"

    def test_court_name_includes_county(self) -> None:
        """court_name should include 'County of' to match ingestion format."""
        keys = ["ca/orange/superior_court/raw/abc123.html"]
        courts = reingest._discover_courts(keys)
        assert courts[0]["court_name"] == "Superior Court, County of Orange"

    def test_skips_invalid_keys(self) -> None:
        keys = [
            "invalid/key",
            "federal/federal/courtlistener/raw/aaa111.html",
        ]
        courts = reingest._discover_courts(keys)
        assert len(courts) == 1

    def test_empty_keys(self) -> None:
        assert reingest._discover_courts([]) == []


class TestListS3Keys:
    """Tests for _list_s3_keys."""

    def test_lists_matching_keys(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "federal/federal/courtlistener/raw/aaa111.html"},
                    {"Key": "federal/federal/courtlistener/raw/bbb222.pdf"},
                    {"Key": "federal/federal/courtlistener/metadata.json"},  # non-matching
                ]
            }
        ]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert len(keys) == 2
        s3.get_paginator.assert_called_once_with("list_objects_v2")
        paginator.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="federal/")

    def test_handles_empty_pages(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [{}]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert keys == []

    def test_handles_multiple_pages(self) -> None:
        s3 = MagicMock()
        paginator = MagicMock()
        s3.get_paginator.return_value = paginator
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "federal/federal/courtlistener/raw/aaa111.html"}]},
            {"Contents": [{"Key": "federal/federal/courtlistener/raw/bbb222.pdf"}]},
        ]
        keys = reingest._list_s3_keys(s3, "test-bucket", "federal/")
        assert len(keys) == 2


class TestBuildPrefixEvent:
    """Tests for _build_prefix_event."""

    def test_builds_event_for_html(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "abc123",
            "ext": "html",
        }
        content = b"<html>ruling text</html>"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/abc123.html",
            content,
            parsed,
            "test-bucket",
        )
        assert event["state"] == "Federal"
        assert event["county"] == "Federal"
        assert event["court"] == "Courtlistener"
        assert event["content_format"] == "html"
        assert event["content_hash"] == "abc123"
        assert event["s3_bucket"] == "test-bucket"
        assert event["scraper_id"] == "reingest-federal-federal"
        assert event["ruling_text"] == "<html>ruling text</html>"
        # Document ID is deterministic (uuid5 from content_hash)
        expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "abc123"))
        assert event["document_id"] == expected_id

    def test_builds_event_for_pdf(self) -> None:
        parsed = {
            "state": "ca",
            "county": "los_angeles",
            "court": "superior",
            "content_hash": "def456",
            "ext": "pdf",
        }
        content = b"%PDF-1.4 some binary"
        event = reingest._build_prefix_event(
            "ca/los_angeles/superior/raw/def456.pdf",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "pdf"
        assert event["state"] == "CA"
        assert event["county"] == "Los Angeles"
        assert event["court"] == "Superior"
        # PDF content is decoded as latin-1
        assert event["ruling_text"] == content.decode("latin-1")

    def test_builds_event_for_txt(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "aaa111",
            "ext": "txt",
        }
        content = b"Plain text ruling content"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/aaa111.txt",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "txt"
        assert event["ruling_text"] == "Plain text ruling content"

    def test_builds_event_for_docx(self) -> None:
        parsed = {
            "state": "federal",
            "county": "federal",
            "court": "courtlistener",
            "content_hash": "bbb222",
            "ext": "docx",
        }
        content = b"PK\x03\x04 docx binary"
        event = reingest._build_prefix_event(
            "federal/federal/courtlistener/raw/bbb222.docx",
            content,
            parsed,
            "test-bucket",
        )
        assert event["content_format"] == "docx"
        assert event["ruling_text"] == content.decode("latin-1")


class TestSeedCourts:
    """Tests for _seed_courts."""

    def test_seeds_courts_and_returns_ids(self) -> None:
        court_id = uuid.uuid4()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (court_id,)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=cur)
        ctx.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = ctx

        courts = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        result = reingest._seed_courts(conn, courts)
        assert result == {"federal-federal": str(court_id)}
        cur.execute.assert_called_once()
        conn.commit.assert_called_once()


class TestRunReingestFromPrefix:
    """Tests for run_reingest_from_prefix."""

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3.psycopg")
    def test_empty_prefix_returns_zeros(
        self,
        mock_psycopg: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = []
        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="nonexistent/",
        )
        assert stats["total_keys"] == 0
        assert stats["processed"] == 0
        assert stats["errors"] == 0
        assert stats["skipped"] == 0

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_dry_run_skips_processing(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = [
            "federal/federal/courtlistener/raw/aaa111.html",
        ]
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="federal/",
            dry_run=True,
        )
        assert stats["total_keys"] == 1
        assert stats["processed"] == 0
        # A dry run writes nothing, courts included (#4845).
        mock_seed.assert_not_called()

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_processes_documents_with_pool(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        # Mock ProcessPoolExecutor and its futures
        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = "ok"
        future2 = MagicMock()
        future2.result.return_value = "ok"
        pool.submit.side_effect = [future1, future2]

        # Mock as_completed to return both futures.
        #
        # ``skip_judge_prepass=True`` bypasses the #4419 judge pre-pass.
        # Without it the test trips ``KeyError: <MagicMock>`` at the prepass's
        # ``idx = futures[future]`` line: the global ``as_completed`` patch
        # below returns the outer-pool futures, but the prepass passes its
        # own local ``futures`` dict (whose keys are
        # ``ThreadPoolExecutor.submit(_fetch_s3_content, ...)`` futures, not
        # the outer-pool ones) to that same patched ``as_completed``.  See
        # #4449 for the slip-through-via-``ingestion-tests``-skip diagnosis.
        # This test exercises the main ``ProcessPoolExecutor`` flow only —
        # the prepass has dedicated coverage in
        # ``TestRunReingestFromPrefixJudgePrepassBoundary``.
        with patch("reingest_from_s3.as_completed", return_value=[future1, future2]):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
            )

        assert stats["total_keys"] == 2
        assert stats["processed"] == 2
        assert stats["errors"] == 0
        assert pool.submit.call_count == 2

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_intersects_listed_keys(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """#3855/#4049: ``s3_key_list`` narrows the prefix scan to the
        intersection of S3-listed keys and the requested list; requested
        keys not found under the prefix are dropped."""
        mock_list.return_value = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
        ]
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        # Request two real keys + one that does not exist under the prefix.
        requested = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
            "ca/orange/superior_court/raw/missing.pdf",
        ]
        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=requested,
            dry_run=True,
        )
        # Only the 2 keys present under the prefix survive the intersection.
        assert stats["total_keys"] == 2
        # _discover_courts must receive the narrowed set, not the full prefix.
        discovered_keys = mock_discover.call_args[0][0]
        assert set(discovered_keys) == {
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
        }

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_none_processes_full_prefix(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """``s3_key_list=None`` leaves prefix behavior unchanged: every key
        under the prefix is processed."""
        keys = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=None,
            dry_run=True,
        )
        assert stats["total_keys"] == 3
        discovered_keys = mock_discover.call_args[0][0]
        assert set(discovered_keys) == set(keys)

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_empty_processes_full_prefix(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """An empty ``s3_key_list`` is treated like ``None`` (full prefix)."""
        keys = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=[],
            dry_run=True,
        )
        assert stats["total_keys"] == 2

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_dry_run_reports_narrowed_total(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """``--dry-run`` with ``--prefix --s3-key-list`` reports the narrowed
        ``total_keys`` (the intersection count), not the full prefix size."""
        mock_list.return_value = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
            "ca/orange/superior_court/raw/ddd.pdf",
        ]
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=["ca/orange/superior_court/raw/bbb.pdf"],
            dry_run=True,
        )
        assert stats["total_keys"] == 1
        assert stats["processed"] == 0

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_applied_before_limit(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """The intersection happens BEFORE ``--limit`` truncation, so limit
        applies to the narrowed set."""
        mock_list.return_value = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
            "ca/orange/superior_court/raw/ccc.pdf",
            "ca/orange/superior_court/raw/ddd.pdf",
        ]
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=[
                "ca/orange/superior_court/raw/bbb.pdf",
                "ca/orange/superior_court/raw/ccc.pdf",
                "ca/orange/superior_court/raw/ddd.pdf",
            ],
            limit=2,
            dry_run=True,
        )
        # Intersection narrows to 3, then limit caps to 2.
        assert stats["total_keys"] == 2

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3.psycopg")
    def test_s3_key_list_no_matches_returns_zeros(
        self,
        mock_psycopg: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """When none of the requested keys are found under the prefix, the
        narrowed set is empty and the function returns zeros early."""
        mock_list.return_value = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="ca/orange/superior_court/raw/",
            s3_key_list=["ca/orange/superior_court/raw/nope.pdf"],
            dry_run=True,
        )
        assert stats["total_keys"] == 0
        assert stats["processed"] == 0
        assert stats["errors"] == 0
        assert stats["skipped"] == 0
        # The no-match early return honors the function's documented return
        # schema (hash_mismatch_warnings + wall_time_seconds) so any
        # programmatic consumer can read these keys without a KeyError.
        assert stats["hash_mismatch_warnings"] == 0
        assert stats["wall_time_seconds"] == 0.0

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    def test_limit_caps_keys(
        self,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        mock_list.return_value = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "federal/federal/courtlistener/raw/ccc333.html",
        ]
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        stats = reingest.run_reingest_from_prefix(
            "postgresql://test",
            prefix="federal/",
            limit=2,
            dry_run=True,
        )
        # Limit should have capped to 2 keys
        assert stats["total_keys"] == 2

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_summary_reports_hash_mismatch_warnings(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """The prefix-reingest summary must aggregate ``hash_mismatch`` flags
        from ``_process_prefix_document`` dict results and surface the count
        as ``hash_mismatch_warnings`` in the stats.  See #2628."""
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "federal/federal/courtlistener/raw/ccc333.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        # Two successful docs flag hash_mismatch, one is clean.
        future1 = MagicMock()
        future1.result.return_value = {"status": "ok", "hash_mismatch": True}
        future2 = MagicMock()
        future2.result.return_value = {"status": "ok", "hash_mismatch": True}
        future3 = MagicMock()
        future3.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.side_effect = [future1, future2, future3]

        mock_logger = MagicMock()
        # ``skip_judge_prepass=True`` — see comment in
        # ``test_processes_documents_with_pool`` and #4449.
        with (
            patch(
                "reingest_from_s3.as_completed",
                return_value=[future1, future2, future3],
            ),
            patch.object(reingest, "logger", mock_logger),
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
            )

        assert stats["total_keys"] == 3
        assert stats["processed"] == 3
        assert stats["errors"] == 0
        assert stats["hash_mismatch_warnings"] == 2

        # Aggregate summary warning is emitted at the end of the run.
        warn_calls = mock_logger.warning.call_args_list
        aggregate_warnings = [
            call for call in warn_calls if call.kwargs.get("hash_mismatch_warnings") is not None
        ]
        assert aggregate_warnings, (
            f"Expected aggregate hash_mismatch_warnings warning. Got warning calls: {warn_calls!r}"
        )
        assert aggregate_warnings[0].kwargs["hash_mismatch_warnings"] == 2

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_summary_no_warning_when_all_hashes_match(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """When no documents have hash mismatches, ``hash_mismatch_warnings``
        is zero and no aggregate warning is emitted.  See #2628."""
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {"status": "ok", "hash_mismatch": False}
        future2 = MagicMock()
        future2.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.side_effect = [future1, future2]

        mock_logger = MagicMock()
        # ``skip_judge_prepass=True`` — see comment in
        # ``test_processes_documents_with_pool`` and #4449.
        with (
            patch(
                "reingest_from_s3.as_completed",
                return_value=[future1, future2],
            ),
            patch.object(reingest, "logger", mock_logger),
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
            )

        assert stats["hash_mismatch_warnings"] == 0
        # No aggregate warning emitted when count is zero.
        warn_calls = mock_logger.warning.call_args_list
        for call in warn_calls:
            assert call.kwargs.get("hash_mismatch_warnings") is None, (
                f"unexpected hash_mismatch_warnings warning: {call!r}"
            )


class TestProcessPrefixDocument:
    """Tests for _process_prefix_document."""

    @patch("reingest_from_s3.hashlib")
    def test_skips_invalid_key(self, mock_hashlib: MagicMock) -> None:
        result = reingest._process_prefix_document(
            "invalid/key",
            "test-bucket",
            "postgresql://test",
            "redis://localhost:6379",
            "",
        )
        assert isinstance(result, dict)
        assert result["status"] == "skip"
        assert result["hash_mismatch"] is False

    def test_hash_mismatch_logs_warning_and_continues(self) -> None:
        """Hash mismatch must be non-fatal — reingest proceeds and the
        result flags ``hash_mismatch=True`` (see #2494, #2628).

        Previously a mismatch short-circuited with ``status="error"``, which
        is the mechanism that produced the ~4% flat-hash orphan rate on
        Santa Clara: raws stored under a wrong content-hash filename were
        permanently unreingestable.  Now we log a warning, let the worker
        process the raw, and the LLM split path re-derives split-children
        from the raw content.  Mirrors the behavior already in
        ``rebuild_db._process_one_document`` (see #2494).  Root cause of
        the mislabeled filenames is the 2026-03-28 one-time migration; see
        #2638 and ``docs/investigations/mislabeled-s3-writes-2026-04.md``.
        """
        # The key claims hash "abc123" but the actual content hashes
        # elsewhere — _build_prefix_event will still use "abc123" as the
        # canonical content_hash.
        key = "federal/federal/courtlistener/raw/abc123.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = b"<html>content with wrong hash</html>"
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        # Clear cached worker to force re-creation.
        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        mock_logger = MagicMock()
        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
            patch.object(reingest, "logger", mock_logger),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )

        # Proceeds to the worker — does NOT return early with status="error".
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["hash_mismatch"] is True
        mock_worker.process_event.assert_called_once()
        # Event passed to the worker must carry the key-hash as canonical
        # content_hash — the worker's LLM split path derives split-child
        # hashes from that value.
        event = mock_worker.process_event.call_args[0][0]
        assert event["content_hash"] == "abc123"
        # Warning logged (not error) so ops see the integrity signal but the
        # reingest proceeds.
        warn_calls = mock_logger.warning.call_args_list
        assert any("S3 content hash mismatch" in str(call) for call in warn_calls), (
            f"expected hash mismatch warning in {warn_calls!r}"
        )
        # No logger.error call for the mismatch itself.
        for call in mock_logger.error.call_args_list:
            assert "hash mismatch" not in str(call).lower(), (
                f"hash mismatch must not produce logger.error: {call!r}"
            )

    def test_matching_hash_returns_ok_without_mismatch_flag(self) -> None:
        """When the S3 content hash matches the key hash, the result flags
        ``hash_mismatch=False`` and no mismatch warning is emitted."""
        content = b"<html>ok</html>"
        key_hash = hashlib.sha256(content).hexdigest()
        key = f"federal/federal/courtlistener/raw/{key_hash}.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        mock_logger = MagicMock()
        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
            patch.object(reingest, "logger", mock_logger),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )

        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["hash_mismatch"] is False
        # No mismatch warning on a matching hash.
        for call in mock_logger.warning.call_args_list:
            assert "S3 content hash mismatch" not in str(call), (
                f"unexpected hash mismatch warning on matching hash: {call!r}"
            )


# ---------------------------------------------------------------------------
# #2405 — apply_post_extraction_guards and force_update wiring.  The reingest
# path historically bypassed ``IngestionWorker.process_event``'s guard
# pipeline, so bad LLM extractions (probate decedents as judges, repeated
# case titles, implausible hearing dates) survived reingest.  These tests
# verify the guards run in the reingest DB-write path and that
# insert_document_and_ruling is called with force_update=True so cleared
# fields actually reach the DB.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #2424: deterministic validation + LLM cache bust
# ---------------------------------------------------------------------------


def _make_det_result(
    overall: str = "pass",
    reasons: list[str] | None = None,
) -> Any:
    """Create a ``DeterministicValidationResult``-shaped mock."""
    from validation.deterministic import (
        DeterministicRuleResult,
        DeterministicValidationResult,
    )

    rules: list[DeterministicRuleResult] = []
    for reason in reasons or []:
        rules.append(
            DeterministicRuleResult(
                rule="test_rule",
                result=overall,
                reason=reason,
            )
        )
    if not rules and overall != "pass":
        # Ensure reasons list isn't empty for non-pass outcomes.
        rules.append(
            DeterministicRuleResult(
                rule="test_rule",
                result=overall,
                reason=f"synthetic {overall} reason",
            )
        )
    return DeterministicValidationResult(overall=overall, rules=rules)


class TestCLIBustLlmCacheFlag:
    """#2424: --bust-llm-cache CLI flag parsing (exercises the real main() argparse)."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_main_passes_bust_llm_cache_true(
        self,
        mock_run: MagicMock,
    ) -> None:
        """`--bust-llm-cache` on the command line forwards bust_llm_cache=True
        to run_reingest."""
        argv = ["reingest_from_s3", "--county", "Fresno", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["bust_llm_cache"] is True

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_main_default_bust_llm_cache_is_false(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Default (no flag) forwards bust_llm_cache=False to run_reingest."""
        argv = ["reingest_from_s3", "--county", "Fresno"]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["bust_llm_cache"] is False


class TestCLIPrefixS3KeyListPlumbing:
    """#3855/#4049: main() forwards --s3-key-list into the --prefix path."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    @patch("reingest_from_s3._read_s3_key_list_file")
    def test_main_passes_s3_key_list_to_prefix(
        self,
        mock_read: MagicMock,
        mock_run_prefix: MagicMock,
    ) -> None:
        """``--prefix`` + ``--s3-key-list`` forwards the parsed key list to
        ``run_reingest_from_prefix`` (mirrors the standard-mode plumbing test
        ``test_s3_key_list_filter_passed_through``)."""
        keys = [
            "ca/orange/superior_court/raw/aaa.pdf",
            "ca/orange/superior_court/raw/bbb.pdf",
        ]
        mock_read.return_value = keys
        mock_run_prefix.return_value = {
            "total_keys": 2,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "ca/orange/superior_court/raw/",
            "--s3-key-list",
            "keys.txt",
        ]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run_prefix.assert_called_once()
        assert mock_run_prefix.call_args.kwargs["s3_key_list"] == keys

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_main_prefix_without_key_list_forwards_none(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """``--prefix`` alone forwards ``s3_key_list=None`` (unchanged behavior)."""
        mock_run_prefix.return_value = {
            "total_keys": 0,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "ca/orange/superior_court/raw/",
        ]
        with patch("sys.argv", argv):
            reingest.main()
        mock_run_prefix.assert_called_once()
        assert mock_run_prefix.call_args.kwargs["s3_key_list"] is None


class TestLlmExtractorBustCachePlumbing:
    """#2424: LlmExtractor(bust_cache=True) skips cache reads, keeps writes."""

    def test_bust_cache_skips_cache_read_keeps_write(self) -> None:
        """With bust_cache=True, cache.get is NOT called; cache.put IS
        (provided the extraction returned at least one ruling)."""
        from framework.llm_extractor import LlmExtractor
        from framework.llm_schema import ExtractedRuling

        cache = MagicMock()
        # If get WERE called, it would short-circuit with this payload.
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = True
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        extracted = ExtractedRuling(extracted_case_number="FRESH-1")
        with (
            patch.object(
                extractor,
                "_extract_chunk_with_retry",
                return_value=[extracted],
            ),
            patch.object(extractor, "_log_usage"),
            patch.object(extractor, "_merge_results", return_value=[extracted]),
            patch.object(extractor, "_propagate_document_fields", return_value=[extracted]),
        ):
            result = extractor.extract("sample text")

        cache.get.assert_not_called()
        cache.put.assert_called_once()
        assert result
        assert result[0].extracted_case_number == "FRESH-1"

    def test_cache_hit_honored_without_bust_cache(self) -> None:
        """With bust_cache=False (default), cache.get SHORT-CIRCUITS and
        the LLM is NOT invoked."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "CACHED-123"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with patch.object(extractor, "_extract_chunk_with_retry") as mock_call:
            result = extractor.extract("sample text")

        cache.get.assert_called_once()
        mock_call.assert_not_called()
        cache.put.assert_not_called()
        assert result
        assert result[0].extracted_case_number == "CACHED-123"


class TestLlmExtractorExtractFromPdfBustCache:
    """#2424: extract_from_pdf honors bust_cache like extract() does."""

    def test_extract_from_pdf_bust_cache_skips_cache_read(self) -> None:
        """extract_from_pdf(bust_cache=True) does NOT call cache.get — even
        if a cache hit would otherwise short-circuit the extraction."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        # If get WERE called, this would short-circuit extraction.
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with (
            patch(
                "framework.llm_extractor._render_pdf_pages",
                return_value=[],
            ),
        ):
            result = extractor.extract_from_pdf(
                b"%PDF-1.4 minimal",
                bust_cache=True,
            )

        cache.get.assert_not_called()
        assert result == []

    def test_extract_from_pdf_instance_bust_cache_skips_read(self) -> None:
        """Instance-level self._bust_cache=True also skips the cache read."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "SHOULD-NOT-USE"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = True
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()
        extractor._max_retries = 1
        extractor._base_delay = 0.0
        extractor._max_delay = 0.0
        extractor._max_output_tokens = 4096
        extractor._max_chars_per_chunk = 1_000_000

        with patch(
            "framework.llm_extractor._render_pdf_pages",
            return_value=[],
        ):
            result = extractor.extract_from_pdf(b"%PDF-1.4 minimal")

        cache.get.assert_not_called()
        assert result == []

    def test_extract_from_pdf_cache_hit_honored_without_bust(self) -> None:
        """extract_from_pdf(bust_cache=False) honors cache hits."""
        from framework.llm_extractor import LlmExtractor

        cache = MagicMock()
        cache.get.return_value = [{"extracted_case_number": "CACHED-PDF"}]

        extractor = LlmExtractor.__new__(LlmExtractor)
        extractor._cache = cache
        extractor._bust_cache = False
        extractor._provider = "google"
        extractor._model = "test"
        extractor._client = MagicMock()

        result = extractor.extract_from_pdf(b"%PDF-1.4 minimal")

        cache.get.assert_called_once()
        assert result
        assert result[0].extracted_case_number == "CACHED-PDF"


# ---------------------------------------------------------------------------
# #4049 — bust_llm_cache propagation through prefix mode.
#
# DB-row reingest (--multimodal --bust-llm-cache) correctly skips split-child
# rows via the is_split_child_id guard at scripts/reingest_from_s3.py:2245-2253
# to prevent the #2416 exponential explosion.  Once a parent PDF has been
# split, the *only* path to re-extract those children with a fresh LLM call
# is to re-process the parent S3 key in prefix mode with bust_llm_cache=True.
# These tests verify the plumbing: --bust-llm-cache reaches the worker,
# which in turn constructs LlmExtractor with bust_cache=True.
# ---------------------------------------------------------------------------


class TestProcessPrefixDocumentBustCachePropagation:
    """``_process_prefix_document(bust_llm_cache=True)`` must construct
    ``IngestionWorker`` with ``bust_llm_cache=True``."""

    def _build_key(self, content: bytes) -> str:
        """Build a content-addressed S3 key whose embedded hash matches
        ``content``.  Avoids triggering the hash-mismatch warning path so
        the test focuses on the bust_llm_cache plumbing only."""
        return f"federal/federal/courtlistener/raw/{hashlib.sha256(content).hexdigest()}.html"

    def test_propagates_true_to_worker_constructor(self) -> None:
        content = b"<html>ok</html>"
        key = self._build_key(content)
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker_cls = MagicMock(return_value=mock_worker)

        # Clear cached worker to force re-creation.
        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", mock_worker_cls),
            patch("redis.Redis.from_url", return_value=MagicMock()),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
                bust_llm_cache=True,
            )

        assert result["status"] == "ok"
        mock_worker_cls.assert_called_once()
        assert mock_worker_cls.call_args.kwargs.get("bust_llm_cache") is True

    def test_default_is_false(self) -> None:
        """Calling ``_process_prefix_document`` without the new kwarg keeps
        the live worker cache semantics — backward-compatible default."""
        content = b"<html>also ok</html>"
        key = self._build_key(content)
        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker_cls = MagicMock(return_value=mock_worker)

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", mock_worker_cls),
            patch("redis.Redis.from_url", return_value=MagicMock()),
        ):
            reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )

        mock_worker_cls.assert_called_once()
        assert mock_worker_cls.call_args.kwargs.get("bust_llm_cache") is False


class TestRunReingestFromPrefixBustCachePropagation:
    """``run_reingest_from_prefix(bust_llm_cache=True)`` must propagate the
    flag into every ``_process_prefix_document`` invocation submitted to
    the process pool."""

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_propagates_true_to_each_submit(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {"status": "ok", "hash_mismatch": False}
        future2 = MagicMock()
        future2.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.side_effect = [future1, future2]

        # ``skip_judge_prepass=True`` — see comment in
        # ``test_processes_documents_with_pool`` and #4449.
        with patch("reingest_from_s3.as_completed", return_value=[future1, future2]):
            reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                bust_llm_cache=True,
                skip_judge_prepass=True,
            )

        assert pool.submit.call_count == 2
        # Every submit must include bust_llm_cache=True as the trailing
        # positional arg passed to _process_prefix_document.
        for call in pool.submit.call_args_list:
            assert call.args[0] is reingest._process_prefix_document
            # Positional args: (fn, key, bucket, database_url, redis_url, os_url, bust_llm_cache)
            assert call.args[6] is True

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_default_is_false(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Backward-compatibility: omitting ``bust_llm_cache`` from
        ``run_reingest_from_prefix`` keeps the worker's default cache
        semantics (cache reads enabled)."""
        keys = ["federal/federal/courtlistener/raw/ccc333.html"]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future = MagicMock()
        future.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.return_value = future

        # ``skip_judge_prepass=True`` — see comment in
        # ``test_processes_documents_with_pool`` and #4449.
        with patch("reingest_from_s3.as_completed", return_value=[future]):
            reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=1,
                skip_judge_prepass=True,
            )

        assert pool.submit.call_count == 1
        call = pool.submit.call_args_list[0]
        assert call.args[6] is False


class TestRunReingestFromPrefixJudgePrepassBoundary:
    """Regression coverage for #4449 — the #4419 judge pre-pass added to
    ``run_reingest_from_prefix`` must not collide with tests that mock the
    main ``ProcessPoolExecutor``'s ``as_completed`` globally.

    Slip-through pattern (documented so the same shape can't recur silently):

    * PR #4421 added a new ``ThreadPoolExecutor`` + ``as_completed`` block
      to seed judges before the main ``ProcessPoolExecutor`` runs (lines
      ~3940-3963 in ``scripts/reingest_from_s3.py``).
    * The pre-pass's local ``futures`` dict has different keys than the
      main pool's local ``futures`` dict.
    * Five tests in ``TestRunReingestFromPrefix*`` patch
      ``reingest_from_s3.as_completed`` globally with ``return_value=[<main
      pool's futures>]``.  When the prepass calls ``as_completed`` on its
      own local ``futures`` dict, it gets the test's main-pool futures back
      and ``idx = futures[future]`` raises ``KeyError`` (line 3952).
    * The regression hid in main CI because the path-filter-conditional
      ``ingestion-tests`` job in ``.github/workflows/ci.yml`` is skipped on
      every PR/main commit that doesn't touch ``packages/scraper-framework/``.
      PR #4421 only modified ``scripts/reingest_from_s3.py``, so its CI run
      skipped these tests entirely.

    Tests in this class exercise the prepass-vs-main-pool boundary directly
    by mocking ``_seed_judges_from_keys`` itself rather than the global
    ``as_completed``.  They would have caught #4449 at PR-time on PR #4421.
    """

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3._seed_judges_from_keys")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_prepass_runs_by_default_without_keyerror(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_seed_judges: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """Default behavior (``skip_judge_prepass=False``): the prepass
        is invoked and the run completes without ``KeyError``.

        This test mocks ``_seed_judges_from_keys`` at the function boundary
        (rather than mocking ``as_completed`` globally) so it does NOT
        interact with the prepass's internal ``ThreadPoolExecutor`` state.
        On the PR #4421 SHA without the test fix in this PR, every
        ``run_reingest_from_prefix`` call that mocks ``as_completed``
        globally (without bypassing this boundary) trips
        ``KeyError: <MagicMock>`` at line 3952.
        """
        keys = ["federal/federal/courtlistener/raw/aaa111.html"]
        mock_list.return_value = keys
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        # Prepass returns its declared shape — no KeyError, no real S3
        # fetch, no real DB write.
        mock_seed_judges.return_value = {
            "docs_scanned": 1,
            "judges_seeded": 0,
            "judges_skipped_invalid": 0,
        }

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future = MagicMock()
        future.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.return_value = future

        with patch("reingest_from_s3.as_completed", return_value=[future]):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=1,
                # skip_judge_prepass intentionally omitted — defaults to
                # False so the prepass code path runs.
            )

        # Prepass was invoked exactly once, with the listed keys.
        mock_seed_judges.assert_called_once()
        call_kwargs = mock_seed_judges.call_args.kwargs
        call_args = mock_seed_judges.call_args.args
        # Positional arg layout: (conn, s3_client, keys, bucket, court_ids)
        assert call_args[2] == keys, "prepass should receive the same keys list"
        assert call_kwargs.get("concurrency") == 1
        # Prepass stats are surfaced into the run summary.
        assert stats["judge_prepass_docs_scanned"] == 1
        assert stats["judge_prepass_judges_seeded"] == 0
        assert stats["judge_prepass_judges_skipped_invalid"] == 0
        # Main pool ran on top of the prepass without raising.
        assert stats["total_keys"] == 1
        assert stats["processed"] == 1

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3._seed_judges_from_keys")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_skip_judge_prepass_flag_bypasses_prepass(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_seed_judges: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """``skip_judge_prepass=True`` must bypass ``_seed_judges_from_keys``
        entirely.

        This is the escape hatch the five sibling tests in
        ``TestRunReingestFromPrefix`` and
        ``TestRunReingestFromPrefixBustCachePropagation`` rely on to mock
        ``as_completed`` globally without tripping the prepass's local
        ``futures`` dict.  Verify the flag is honored so future refactors
        don't silently re-introduce the prepass on the bypass path.
        """
        mock_list.return_value = [
            "federal/federal/courtlistener/raw/aaa111.html",
        ]
        mock_discover.return_value = [
            {
                "state": "Federal",
                "county": "Federal",
                "court_name": "Courtlistener, County of Federal",
                "court_code": "federal-federal",
                "timezone": "America/Los_Angeles",
            }
        ]
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {"federal-federal": "court-id-1"}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future = MagicMock()
        future.result.return_value = {"status": "ok", "hash_mismatch": False}
        pool.submit.return_value = future

        with patch("reingest_from_s3.as_completed", return_value=[future]):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=1,
                skip_judge_prepass=True,
            )

        # Prepass MUST NOT run when skip flag is set.
        mock_seed_judges.assert_not_called()
        # Prepass keys are absent from the summary when the prepass is skipped.
        assert "judge_prepass_docs_scanned" not in stats
        # Main pool still ran.
        assert stats["total_keys"] == 1
        assert stats["processed"] == 1


# ---------------------------------------------------------------------------
# #4619 — reingest --prefix should surface partial failure.  These tests cover
# the per-key error tracking + error-class capture, the loud WARNING summary,
# the --max-error-ratio threshold-gated non-zero exit, and the optional
# failed-keys S3 manifest.
# ---------------------------------------------------------------------------


class TestEmitPartialFailureWarning:
    """Unit tests for the _emit_partial_failure_warning helper (#4619)."""

    def test_returns_ratio_and_warns_when_errors(self) -> None:
        from collections import Counter

        mock_logger = MagicMock()
        with patch.object(reingest, "logger", mock_logger):
            ratio = reingest._emit_partial_failure_warning(
                errors=3,
                total=10,
                error_classes=Counter({"BrokenProcessPool": 2, "ValueError": 1}),
                context="reingest",
            )
        assert ratio == pytest.approx(0.3)
        mock_logger.warning.assert_called_once()
        kwargs = mock_logger.warning.call_args.kwargs
        assert kwargs["errors"] == 3
        assert kwargs["total"] == 10
        assert kwargs["error_ratio"] == pytest.approx(0.3)
        assert kwargs["top_error_class"] == "BrokenProcessPool"

    def test_no_warning_when_zero_errors(self) -> None:
        mock_logger = MagicMock()
        with patch.object(reingest, "logger", mock_logger):
            ratio = reingest._emit_partial_failure_warning(
                errors=0,
                total=10,
                error_classes=None,
                context="reingest",
            )
        assert ratio == 0.0
        mock_logger.warning.assert_not_called()

    def test_zero_total_returns_zero(self) -> None:
        mock_logger = MagicMock()
        with patch.object(reingest, "logger", mock_logger):
            ratio = reingest._emit_partial_failure_warning(
                errors=0,
                total=0,
                error_classes=None,
            )
        assert ratio == 0.0
        mock_logger.warning.assert_not_called()

    def test_top_error_class_none_without_classes(self) -> None:
        mock_logger = MagicMock()
        with patch.object(reingest, "logger", mock_logger):
            ratio = reingest._emit_partial_failure_warning(
                errors=2,
                total=4,
                error_classes=None,
            )
        assert ratio == pytest.approx(0.5)
        assert mock_logger.warning.call_args.kwargs["top_error_class"] is None


class TestParseS3Uri:
    """Unit tests for the _parse_s3_uri helper (#4619)."""

    def test_parses_bucket_and_key(self) -> None:
        bucket, key = reingest._parse_s3_uri("s3://my-bucket/path/to/keys.txt")
        assert bucket == "my-bucket"
        assert key == "path/to/keys.txt"

    def test_rejects_non_s3_uri(self) -> None:
        with pytest.raises(ValueError):
            reingest._parse_s3_uri("/local/path.txt")

    def test_rejects_missing_key(self) -> None:
        with pytest.raises(ValueError):
            reingest._parse_s3_uri("s3://bucket-only")


class TestProcessPrefixDocumentErrorClass:
    """_process_prefix_document populates error_class (#4619)."""

    def test_error_class_none_on_skip_invalid_key(self) -> None:
        result = reingest._process_prefix_document(
            "invalid/key",
            "test-bucket",
            "postgresql://test",
            "redis://localhost:6379",
            "",
        )
        assert result["status"] == "skip"
        assert result["error_class"] is None

    def test_error_class_set_on_s3_fetch_failure(self) -> None:
        key = "federal/federal/courtlistener/raw/abc123.html"
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = KeyError("boom")

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        with patch("framework.s3_cache.make_s3_client", return_value=mock_s3):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )
        assert result["status"] == "error"
        assert result["error_class"] == "KeyError"

    def test_error_class_set_on_worker_raises(self) -> None:
        content = b"<html>ok</html>"
        key_hash = hashlib.sha256(content).hexdigest()
        key = f"federal/federal/courtlistener/raw/{key_hash}.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event.side_effect = ValueError("bad extract")

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )
        assert result["status"] == "error"
        assert result["error_class"] == "ValueError"

    def test_error_class_none_on_success(self) -> None:
        content = b"<html>ok</html>"
        key_hash = hashlib.sha256(content).hexdigest()
        key = f"federal/federal/courtlistener/raw/{key_hash}.html"

        mock_s3 = MagicMock()
        body = MagicMock()
        body.read.return_value = content
        mock_s3.get_object.return_value = {"Body": body}

        mock_worker = MagicMock()
        mock_worker.process_event = MagicMock()

        if hasattr(reingest._process_prefix_document, "_worker"):
            delattr(reingest._process_prefix_document, "_worker")

        with (
            patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
            patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
            patch("redis.Redis.from_url", return_value=MagicMock()),
        ):
            result = reingest._process_prefix_document(
                key,
                "test-bucket",
                "postgresql://test",
                "redis://localhost:6379",
                "",
            )
        assert result["status"] == "ok"
        assert result["error_class"] is None


class TestPrefixPartialFailureStats:
    """run_reingest_from_prefix surfaces error_ratio / top class / failed_keys
    and writes the failed-keys manifest (#4619)."""

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_partial_failure_surfaces_ratio_and_top_class(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
            "federal/federal/courtlistener/raw/ccc333.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {
            "status": "ok",
            "hash_mismatch": False,
            "error_class": None,
        }
        future2 = MagicMock()
        future2.result.return_value = {
            "status": "error",
            "hash_mismatch": False,
            "error_class": "BrokenProcessPool",
        }
        future3 = MagicMock()
        future3.result.return_value = {
            "status": "error",
            "hash_mismatch": False,
            "error_class": "BrokenProcessPool",
        }
        pool.submit.side_effect = [future1, future2, future3]

        # Map each future back to its key so failed_keys is populated in
        # submit order (futures patched into as_completed in the same order).
        futures_map = {future1: keys[0], future2: keys[1], future3: keys[2]}

        mock_logger = MagicMock()
        # ``skip_judge_prepass=True`` — see test_processes_documents_with_pool
        # and #4449.
        with (
            patch(
                "reingest_from_s3.as_completed",
                return_value=[future1, future2, future3],
            ),
            patch.object(reingest, "logger", mock_logger),
        ):
            # Patch the dict comprehension's submit/key mapping by making
            # submit return our futures; run_reingest_from_prefix builds the
            # futures->key dict itself from the keys list, so the ordering is
            # preserved.
            _ = futures_map
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
            )

        assert stats["errors"] == 2
        assert stats["error_ratio"] == pytest.approx(2 / 3)
        assert stats["top_error_class"] == "BrokenProcessPool"
        assert set(stats["failed_keys"]) == {keys[1], keys[2]}

        warn_calls = mock_logger.warning.call_args_list
        partial_warnings = [
            call for call in warn_calls if call.kwargs.get("top_error_class") == "BrokenProcessPool"
        ]
        assert partial_warnings, (
            f"expected partial-failure warning with top_error_class. Got: {warn_calls!r}"
        )
        assert partial_warnings[0].kwargs["error_ratio"] == pytest.approx(2 / 3, abs=1e-3)

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_future_raising_counts_as_error_with_class(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        """A future that raises (e.g. BrokenProcessPool) is tallied as an
        error and its exception class name is captured in error_classes."""
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {
            "status": "ok",
            "hash_mismatch": False,
            "error_class": None,
        }
        future2 = MagicMock()
        future2.result.side_effect = RuntimeError("pool died")
        pool.submit.side_effect = [future1, future2]

        with patch(
            "reingest_from_s3.as_completed",
            return_value=[future1, future2],
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
            )

        assert stats["errors"] == 1
        assert stats["top_error_class"] == "RuntimeError"
        assert len(stats["failed_keys"]) == 1

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_writes_failed_manifest_when_failures(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        s3_client = MagicMock()
        mock_boto3.client.return_value = s3_client

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {
            "status": "ok",
            "hash_mismatch": False,
            "error_class": None,
        }
        future2 = MagicMock()
        future2.result.return_value = {
            "status": "error",
            "hash_mismatch": False,
            "error_class": "ValueError",
        }
        pool.submit.side_effect = [future1, future2]

        with patch(
            "reingest_from_s3.as_completed",
            return_value=[future1, future2],
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
                write_failed_manifest="s3://manifest-bucket/failed/keys.txt",
            )

        assert stats["errors"] == 1
        put_calls = [
            call
            for call in s3_client.put_object.call_args_list
            if call.kwargs.get("Bucket") == "manifest-bucket"
        ]
        assert put_calls, (
            f"expected put_object to manifest-bucket. Got: {s3_client.put_object.call_args_list!r}"
        )
        call = put_calls[0]
        assert call.kwargs["Key"] == "failed/keys.txt"
        body = call.kwargs["Body"]
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        assert body == "\n".join(stats["failed_keys"]) + "\n"

    @patch.dict(
        os.environ,
        {
            "JUDGEMIND_ARCHIVE_BUCKET": "test-bucket",
            "REDIS_URL": "redis://localhost:6379",
            "OPENSEARCH_URL": "",
        },
    )
    @patch("reingest_from_s3.boto3")
    @patch("reingest_from_s3._list_s3_keys")
    @patch("reingest_from_s3._seed_courts")
    @patch("reingest_from_s3._discover_courts")
    @patch("reingest_from_s3.psycopg")
    @patch("reingest_from_s3.ProcessPoolExecutor")
    def test_no_manifest_when_no_failures(
        self,
        mock_pool_cls: MagicMock,
        mock_psycopg: MagicMock,
        mock_discover: MagicMock,
        mock_seed: MagicMock,
        mock_list: MagicMock,
        mock_boto3: MagicMock,
    ) -> None:
        keys = [
            "federal/federal/courtlistener/raw/aaa111.html",
            "federal/federal/courtlistener/raw/bbb222.html",
        ]
        mock_list.return_value = keys
        mock_discover.return_value = []
        conn_mock = MagicMock()
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=conn_mock)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_seed.return_value = {}

        s3_client = MagicMock()
        mock_boto3.client.return_value = s3_client

        pool = MagicMock()
        mock_pool_cls.return_value.__enter__ = MagicMock(return_value=pool)
        mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)

        future1 = MagicMock()
        future1.result.return_value = {
            "status": "ok",
            "hash_mismatch": False,
            "error_class": None,
        }
        future2 = MagicMock()
        future2.result.return_value = {
            "status": "ok",
            "hash_mismatch": False,
            "error_class": None,
        }
        pool.submit.side_effect = [future1, future2]

        with patch(
            "reingest_from_s3.as_completed",
            return_value=[future1, future2],
        ):
            stats = reingest.run_reingest_from_prefix(
                "postgresql://test",
                prefix="federal/",
                concurrency=4,
                skip_judge_prepass=True,
                write_failed_manifest="s3://manifest-bucket/failed/keys.txt",
            )

        assert stats["errors"] == 0
        manifest_calls = [
            call
            for call in s3_client.put_object.call_args_list
            if call.kwargs.get("Bucket") == "manifest-bucket"
        ]
        assert not manifest_calls


class TestPrefixMaxErrorRatioExit:
    """main() exit-code gating via --max-error-ratio (#4619)."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_no_flag_partial_failure_exits_zero(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """No --max-error-ratio: partial failure must NOT raise SystemExit
        (backward-compatible exit 0)."""
        mock_run_prefix.return_value = {
            "total_keys": 10,
            "processed": 6,
            "errors": 4,
            "skipped": 0,
            "error_ratio": 0.4,
        }
        argv = ["reingest_from_s3", "--prefix", "x/"]
        with patch("sys.argv", argv):
            reingest.main()  # must not raise

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_above_threshold_exits_nonzero(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        mock_run_prefix.return_value = {
            "total_keys": 10,
            "processed": 5,
            "errors": 5,
            "skipped": 0,
            "error_ratio": 0.5,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "x/",
            "--max-error-ratio",
            "0.05",
        ]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as exc:
                reingest.main()
        assert exc.value.code == 1

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_below_threshold_no_exit(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        mock_run_prefix.return_value = {
            "total_keys": 10,
            "processed": 7,
            "errors": 3,
            "skipped": 0,
            "error_ratio": 0.3,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "x/",
            "--max-error-ratio",
            "0.5",
        ]
        with patch("sys.argv", argv):
            reingest.main()  # below threshold → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_all_success_with_zero_threshold_no_exit(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """error_ratio 0.0 with --max-error-ratio 0.0 must NOT exit (strictly
        greater-than semantics)."""
        mock_run_prefix.return_value = {
            "total_keys": 10,
            "processed": 10,
            "errors": 0,
            "skipped": 0,
            "error_ratio": 0.0,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "x/",
            "--max-error-ratio",
            "0.0",
        ]
        with patch("sys.argv", argv):
            reingest.main()  # all-success → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_write_failed_manifest_threaded_into_prefix(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        mock_run_prefix.return_value = {
            "total_keys": 0,
            "processed": 0,
            "errors": 0,
            "skipped": 0,
            "error_ratio": 0.0,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "x/",
            "--write-failed-manifest",
            "s3://b/k",
        ]
        with patch("sys.argv", argv):
            reingest.main()
        assert mock_run_prefix.call_args.kwargs["write_failed_manifest"] == "s3://b/k"


class TestStandardModeErrorRatioExit:
    """main() standard (DB-row) mode threshold gating + run_reingest
    error_ratio surfacing (#4619)."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_standard_above_threshold_exits_nonzero(
        self,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = {
            "total_keys": 10,
            "processed": 5,
            "errors": 5,
            "skipped": 0,
            "error_ratio": 0.5,
        }
        argv = [
            "reingest_from_s3",
            "--county",
            "Fresno",
            "--max-error-ratio",
            "0.1",
        ]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as exc:
                reingest.main()
        assert exc.value.code == 1

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_standard_no_flag_partial_failure_exits_zero(
        self,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = {
            "total_keys": 10,
            "processed": 5,
            "errors": 5,
            "skipped": 0,
            "error_ratio": 0.5,
        }
        argv = ["reingest_from_s3", "--county", "Fresno"]
        with patch("sys.argv", argv):
            reingest.main()  # must not raise


class TestResolveEffectiveMaxErrorRatio:
    """Unit tests for the #4624 effective-threshold resolver.

    Pinpoints the decision logic that makes the partial-failure exit gate the
    default for --bust-llm-cache prefix reingests while leaving every other
    path's exit-code contract untouched (#4619).
    """

    def test_cache_bust_prefix_defaults_to_gate(self) -> None:
        """The headline #4624 behavior: a --bust-llm-cache prefix reingest with
        no explicit threshold defaults to the 10% gate."""
        result = reingest._resolve_effective_max_error_ratio(
            explicit_max_error_ratio=None,
            no_fail_on_errors=False,
            prefix="orange/",
            bust_llm_cache=True,
        )
        assert result == reingest._DEFAULT_CACHE_BUST_PREFIX_MAX_ERROR_RATIO
        assert result == 0.10

    def test_prefix_without_bust_is_unchanged(self) -> None:
        """A prefix reingest WITHOUT --bust-llm-cache keeps the opt-in
        contract — no default gate."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=None,
                no_fail_on_errors=False,
                prefix="orange/",
                bust_llm_cache=False,
            )
            is None
        )

    def test_bust_without_prefix_is_unchanged(self) -> None:
        """--bust-llm-cache in standard (no --prefix) mode keeps the opt-in
        contract — the default gate is scoped to the prefix path only."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=None,
                no_fail_on_errors=False,
                prefix=None,
                bust_llm_cache=True,
            )
            is None
        )

    def test_standard_mode_is_unchanged(self) -> None:
        """Plain standard mode (no prefix, no bust) keeps the opt-in
        contract."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=None,
                no_fail_on_errors=False,
                prefix=None,
                bust_llm_cache=False,
            )
            is None
        )

    def test_explicit_threshold_wins_over_default(self) -> None:
        """An explicit --max-error-ratio always wins, even on the cache-bust
        prefix path that would otherwise default to 0.10."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=0.5,
                no_fail_on_errors=False,
                prefix="orange/",
                bust_llm_cache=True,
            )
            == 0.5
        )

    def test_explicit_zero_threshold_wins(self) -> None:
        """An explicit --max-error-ratio 0.0 (fail on any error) is honored,
        not swallowed by the default — 0.0 is not None."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=0.0,
                no_fail_on_errors=False,
                prefix="orange/",
                bust_llm_cache=True,
            )
            == 0.0
        )

    def test_no_fail_on_errors_opts_out_of_default(self) -> None:
        """--no-fail-on-errors disables the cache-bust prefix default."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=None,
                no_fail_on_errors=True,
                prefix="orange/",
                bust_llm_cache=True,
            )
            is None
        )

    def test_no_fail_on_errors_overrides_explicit_threshold(self) -> None:
        """--no-fail-on-errors overrides --max-error-ratio when both are
        passed — the opt-out always wins."""
        assert (
            reingest._resolve_effective_max_error_ratio(
                explicit_max_error_ratio=0.05,
                no_fail_on_errors=True,
                prefix="orange/",
                bust_llm_cache=True,
            )
            is None
        )


class TestCacheBustPrefixDefaultGateExit:
    """main() exit-code gating for the #4624 default-on cache-bust prefix
    gate.  Exercises the full main() dispatch, not just the resolver."""

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_default_gate_fires_on_cache_bust_partial_failure(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """The headline regression: --prefix + --bust-llm-cache with a partial
        failure above 10% exits non-zero WITHOUT any --max-error-ratio flag."""
        mock_run_prefix.return_value = {
            "total_keys": 768,
            "processed": 601,
            "errors": 167,
            "skipped": 0,
            "error_ratio": 167 / 768,  # ~0.217, the #3855 incident shape
        }
        argv = ["reingest_from_s3", "--prefix", "orange/", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            with pytest.raises(SystemExit) as exc:
                reingest.main()
        assert exc.value.code == 1

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_default_gate_all_success_exits_zero(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """A clean --bust-llm-cache prefix run (error_ratio 0.0) must still
        exit 0 — the default gate must not break legitimate all-success
        runs."""
        mock_run_prefix.return_value = {
            "total_keys": 768,
            "processed": 768,
            "errors": 0,
            "skipped": 0,
            "error_ratio": 0.0,
        }
        argv = ["reingest_from_s3", "--prefix", "orange/", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            reingest.main()  # all-success → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_default_gate_below_threshold_exits_zero(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """A --bust-llm-cache prefix run with a single-key error below the 10%
        default does NOT exit non-zero (permissive default)."""
        mock_run_prefix.return_value = {
            "total_keys": 100,
            "processed": 95,
            "errors": 5,
            "skipped": 0,
            "error_ratio": 0.05,  # below the 0.10 default
        }
        argv = ["reingest_from_s3", "--prefix", "orange/", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            reingest.main()  # below default threshold → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_no_fail_on_errors_opts_out_of_default_gate(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """--no-fail-on-errors disables the default gate even on a cache-bust
        prefix partial failure above 10%."""
        mock_run_prefix.return_value = {
            "total_keys": 768,
            "processed": 601,
            "errors": 167,
            "skipped": 0,
            "error_ratio": 167 / 768,
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "orange/",
            "--bust-llm-cache",
            "--no-fail-on-errors",
        ]
        with patch("sys.argv", argv):
            reingest.main()  # opt-out → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_explicit_threshold_overrides_default_gate(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """An explicit --max-error-ratio above the observed ratio keeps a
        cache-bust prefix run green even though the default 0.10 gate would
        have failed it."""
        mock_run_prefix.return_value = {
            "total_keys": 100,
            "processed": 85,
            "errors": 15,
            "skipped": 0,
            "error_ratio": 0.15,  # above 0.10 default, below explicit 0.5
        }
        argv = [
            "reingest_from_s3",
            "--prefix",
            "orange/",
            "--bust-llm-cache",
            "--max-error-ratio",
            "0.5",
        ]
        with patch("sys.argv", argv):
            reingest.main()  # explicit threshold not exceeded → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest_from_prefix")
    def test_non_cache_bust_prefix_partial_failure_exits_zero(
        self,
        mock_run_prefix: MagicMock,
    ) -> None:
        """A prefix reingest WITHOUT --bust-llm-cache keeps the #4619 opt-in
        contract: a partial failure above 10% still exits 0 when no
        --max-error-ratio is passed (the default gate must NOT leak onto the
        non-cache-bust prefix path)."""
        mock_run_prefix.return_value = {
            "total_keys": 100,
            "processed": 70,
            "errors": 30,
            "skipped": 0,
            "error_ratio": 0.30,
        }
        argv = ["reingest_from_s3", "--prefix", "orange/"]
        with patch("sys.argv", argv):
            reingest.main()  # no bust → opt-in contract → no SystemExit

    @patch.dict(os.environ, {"DATABASE_URL": "postgres://test:test@test/test"})
    @patch("reingest_from_s3.run_reingest")
    def test_standard_mode_with_bust_partial_failure_exits_zero(
        self,
        mock_run: MagicMock,
    ) -> None:
        """Standard (DB-row) mode with --bust-llm-cache keeps the #4619 opt-in
        contract: the default gate is scoped to the prefix path, so a partial
        failure here still exits 0 without --max-error-ratio."""
        mock_run.return_value = {
            "total_keys": 10,
            "processed": 5,
            "errors": 5,
            "skipped": 0,
            "error_ratio": 0.5,
        }
        argv = ["reingest_from_s3", "--county", "Fresno", "--bust-llm-cache"]
        with patch("sys.argv", argv):
            reingest.main()  # standard mode → opt-in contract → no SystemExit


# ---------------------------------------------------------------------------
# #4700 — prefix reingest after a split-set change
# ---------------------------------------------------------------------------
#
# Prefix-mode reingest (``run_reingest_from_prefix`` ->
# ``_process_prefix_document``) hands every S3 object to
# ``IngestionWorker.process_event``.  When a splitter change alters a
# document's split set (e.g. LLM split -> the deterministic Santa Clara
# table-layout split, #4681/#4696), the worker must fully replace that
# document's derived rows:
#
#   1. Surplus old children (indices the new split no longer produces) are
#      removed on EVERY split path, not just the framework-LLM path.
#   2. A reused child row (same ``make_split_document_id(parent, idx)``)
#      takes the re-derived case link instead of keeping the old one.


def _split_set_change_worker() -> Any:
    from ingestion.worker import IngestionWorker

    os_mock = MagicMock()
    os_mock.indices.exists.return_value = False
    worker = IngestionWorker(
        redis_client=MagicMock(),
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=MagicMock(),
        archive_bucket="test-bucket",
    )
    worker._enrichment_client = None
    return worker


def _sc_prefix_event(content_hash: str = "b" * 64) -> dict[str, Any]:
    parsed = {
        "state": "ca",
        "county": "santa_clara",
        "court": "superior_court",
        "content_hash": content_hash,
        "ext": "pdf",
    }
    key = f"ca/santa_clara/superior_court/{content_hash}.pdf"
    return reingest._build_prefix_event(key, b"%PDF-1.4 fake", parsed, "test-bucket")


def _split_set_change_conn(case_id: str) -> MagicMock:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.side_effect = [("court-uuid-1",), (case_id,)] + [None] * 20
    return mock_conn


class TestSplitSetChange:
    """#4700: a prefix reingest after a split-set change replaces the
    document's derived rows (surplus children removed, case links re-derived)."""

    def test_split_set_change_deterministic_split_removes_surplus_children(self) -> None:
        """The deterministic Santa Clara split path cleans stale children
        AFTER dispatching the children (#4820: a stale slot's ruling can move
        to its new slot first), with exactly the new child ids."""
        from courts.ca.sc_tentatives import SplitRuling

        worker = _split_set_change_worker()
        event = _sc_prefix_event()
        parent_id = event["document_id"]

        new_split = [
            SplitRuling(ruling_index=1, case_number="24CV000001", ruling_text="A granted."),
            SplitRuling(ruling_index=2, case_number="24CV000002", ruling_text="B denied."),
        ]
        order: list[str] = []
        calls: list[tuple[str, list[str], dict[str, Any]]] = []

        def _fake_delete(conn: Any, s3_key: str, valid_ids: list[str], **kwargs: Any) -> int:
            order.append("cleanup")
            calls.append((s3_key, list(valid_ids), kwargs))
            return 8

        def _fake_dispatch(ev: dict[str, Any]) -> None:
            order.append(f"dispatch:{ev['_split_index']}")

        with (
            patch("courts.ca.sc_tentatives._split_rulings", return_value=new_split),
            patch("ingestion.worker.delete_stale_split_children", side_effect=_fake_delete),
            patch("ingestion.worker.psycopg"),
            patch.object(worker, "process_event", side_effect=_fake_dispatch),
        ):
            handled = worker._llm_split_document(
                event,
                parent_id,
                "Line 1 ...",
                "CA",
                "Santa Clara",
                raw_pdf_bytes=b"%PDF-1.4 fake",
            )

        assert handled is True
        assert order == ["dispatch:0", "dispatch:1", "cleanup"]
        assert len(calls) == 1
        s3_key, valid_ids, kwargs = calls[0]
        assert s3_key == event["s3_key"]
        assert valid_ids == [
            make_split_document_id(parent_id, 0),
            make_split_document_id(parent_id, 1),
        ]
        # Alerts on removed children are re-pointed at the stable parent id.
        assert kwargs.get("parent_document_id") == parent_id

    def test_split_set_change_removed_children_leave_search_index(self) -> None:
        """Removed children are dropped from OpenSearch too."""
        worker = _split_set_change_worker()
        worker._indexer = MagicMock()
        event = _sc_prefix_event()

        def _fake_delete(conn: Any, s3_key: str, valid_ids: list[str], **kwargs: Any) -> int:
            kwargs["deleted_ids"].extend(["stale-1", "stale-2"])
            return 2

        with (
            patch("ingestion.worker.delete_stale_split_children", side_effect=_fake_delete),
            patch("ingestion.worker.psycopg"),
        ):
            worker._cleanup_stale_split_children(event, event["document_id"], ["keep"])

        worker._indexer.delete_documents.assert_called_once_with(["stale-1", "stale-2"])

    def test_split_set_change_cleanup_failure_rolls_back(self) -> None:
        """A failed cleanup rolls the connection back so the child inserts
        that follow do not run in an aborted transaction."""
        worker = _split_set_change_worker()
        worker._indexer = MagicMock()
        event = _sc_prefix_event()

        with (
            patch("ingestion.worker.delete_stale_split_children", side_effect=RuntimeError("x")),
            patch("ingestion.worker.psycopg") as mock_psycopg,
        ):
            conn = MagicMock()
            conn.closed = False
            mock_psycopg.connect.return_value = conn
            worker._cleanup_stale_split_children(event, event["document_id"], ["keep"])

        conn.rollback.assert_called_once()
        worker._indexer.delete_documents.assert_not_called()

    def test_split_set_change_split_child_ids(self) -> None:
        """A single-case split (child id == parent id) keeps only the parent,
        so every old v5 split child of the key is stale; a multi split keeps
        exactly ``make_split_document_id(parent, 0..N-1)``."""
        from ingestion.worker import split_child_ids_for_event

        parent = "11111111-1111-5111-8111-111111111111"
        single = {"document_id": parent, "_split_count": 1}
        assert split_child_ids_for_event(single, parent) == [parent]

        multi = {"document_id": make_split_document_id(parent, 0), "_split_count": 3}
        assert split_child_ids_for_event(multi, parent) == [
            make_split_document_id(parent, i) for i in range(3)
        ]

    def test_split_set_change_reused_child_relinks_case(self) -> None:
        """A split child's DB write lets the re-derived case link win
        (``relink_case=True``): a reused child id must not keep the old
        case_id via the preserve-first COALESCE."""
        worker = _split_set_change_worker()
        event = _sc_prefix_event()
        parent_id = event["document_id"]
        child = {
            **event,
            "document_id": make_split_document_id(parent_id, 0),
            "_original_document_id": parent_id,
            "_split_processed": True,
            "_llm_extracted": True,
            "_split_index": 0,
            "_split_count": 2,
            "ruling_text": "The demurrer is SUSTAINED.",
            "case_number": "24CV000001",
            "case_title": "Alpha v. Beta",
            "hearing_date": "2026-03-05",
            "outcome": "sustained",
            "motion_type": "demurrer",
        }

        with (
            patch("ingestion.worker.psycopg") as mock_psycopg,
            patch("ingestion.worker.resolve_judge", return_value=None),
            patch("ingestion.worker.batch_upsert_parties"),
            patch("ingestion.worker.insert_document_and_ruling", return_value=False) as mock_ins,
        ):
            mock_psycopg.connect.return_value = _split_set_change_conn("case-uuid-new")
            worker.process_event(child)

        mock_ins.assert_called_once()
        assert mock_ins.call_args.kwargs.get("relink_case") is True

    def test_split_set_change_unsplit_document_keeps_preserve_first(self) -> None:
        """A regular (non-split) document keeps the live preserve-first
        identity semantics: ``relink_case`` is only for split children."""
        worker = _split_set_change_worker()
        event = {
            **_sc_prefix_event(),
            "_llm_extracted": True,
            "content_format": "html",
            "ruling_text": "The motion is GRANTED.",
            "case_number": "24CV000009",
            "hearing_date": "2026-03-05",
        }

        with (
            patch("ingestion.worker.psycopg") as mock_psycopg,
            patch("ingestion.worker.resolve_judge", return_value=None),
            patch("ingestion.worker.batch_upsert_parties"),
            patch("ingestion.worker.insert_document_and_ruling", return_value=False) as mock_ins,
        ):
            mock_psycopg.connect.return_value = _split_set_change_conn("case-uuid-1")
            worker.process_event(event)

        mock_ins.assert_called_once()
        assert mock_ins.call_args.kwargs.get("relink_case", False) is False


class TestSplitChildRelinkScope:
    """#4788: the split-child case relink from #4700 is scoped.

    It applies only to events that replace a split set (prefix reingest and
    rebuild set ``_replace_split_set``), and never onto an ``UNKNOWN-``
    placeholder case.  A live write or retry of a split child keeps the
    preserve-first case link, so a re-process that misses the case number
    cannot delete a correct ruling or detach its alerts.
    """

    def _child(self, event: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        parent_id = event["document_id"]
        child = {
            **event,
            "document_id": make_split_document_id(parent_id, 2),
            "_original_document_id": parent_id,
            "_split_processed": True,
            "_llm_extracted": True,
            "_split_index": 2,
            "_split_count": 3,
            "ruling_text": "The demurrer is SUSTAINED.",
            "case_number": "24CV000001",
            "case_title": "Alpha v. Beta",
            "hearing_date": "2026-03-05",
            "outcome": "sustained",
            "motion_type": "demurrer",
        }
        child.update(overrides)
        return child

    def _relink_kwarg(self, child: dict[str, Any]) -> Any:
        worker = _split_set_change_worker()
        with (
            patch("ingestion.worker.psycopg") as mock_psycopg,
            patch("ingestion.worker.resolve_judge", return_value=None),
            patch("ingestion.worker.batch_upsert_parties"),
            patch("ingestion.worker.insert_document_and_ruling", return_value=False) as mock_ins,
        ):
            mock_psycopg.connect.return_value = _split_set_change_conn("case-uuid-new")
            worker.process_event(child)
        mock_ins.assert_called_once()
        return mock_ins.call_args.kwargs.get("relink_case", False)

    def test_prefix_event_marks_split_set_replacement(self) -> None:
        assert _sc_prefix_event()["_replace_split_set"] is True

    def test_live_split_child_keeps_preserve_first(self) -> None:
        """A live / retry split child (no ``_replace_split_set``) never relinks."""
        event = {k: v for k, v in _sc_prefix_event().items() if k != "_replace_split_set"}
        event["scraper_id"] = "ca-santa-clara-tentatives"
        assert self._relink_kwarg(self._child(event)) is False

    def test_reingest_child_without_case_number_never_relinks(self) -> None:
        """A reingest child whose case number comes back missing gets the
        synthetic ``UNKNOWN-`` case; relinking onto it is never allowed."""
        child = self._child(_sc_prefix_event(), case_number=None)
        assert self._relink_kwarg(child) is False

    def test_reingest_child_with_real_case_relinks(self) -> None:
        assert self._relink_kwarg(self._child(_sc_prefix_event())) is True


# ---------------------------------------------------------------------------
# #4796 — live pre-split capture and prefix reingest write the same ids
# ---------------------------------------------------------------------------
#
# A live Fresno capture used to split the PDF in the scraper and give each
# child ``make_split_document_id(P, <court entry number>)``; a prefix
# reingest of the same S3 key splits in the worker and uses
# ``make_split_document_id(P, 0..N-1)``.  The two id sets never matched, so
# a reingest and the next live run churned each other's rows.  The worker
# also treated each live child as a parent: it re-split it (grandchild ids)
# and ran the stale-child cleanup with the child as the parent.

_FRESNO_FIXTURE_PDF = os.path.join(
    os.path.dirname(__file__), "fixtures", "fresno_403_20260310_d019042f.pdf"
)
_FRESNO_SOURCE_URL = (
    "https://www.fresno.courts.ca.gov/system/files/tentative-rulings/03-10-26-dept-403.pdf"
)


def _fresno_s3_key(pdf_bytes: bytes) -> str:
    return f"ca/fresno/superior_court/raw/{hashlib.sha256(pdf_bytes).hexdigest()}.pdf"


def _live_fresno_events(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Run a live Fresno capture of *pdf_bytes* and return the emitted
    ``document.captured`` payloads, exactly as the worker receives them."""
    from courts.ca.fresno_tentatives import FresnoTentativeRulingsScraper
    from courts.ca.fresno_tentatives import default_config as fresno_config
    from courts.ca.pdf_link_scraper import PdfLinkScraper
    from framework import ContentFormat
    from framework.base import BaseScraper
    from framework.events import EventBus

    redis_mock = MagicMock()
    archiver = MagicMock()
    archiver.archive.return_value = _fresno_s3_key(pdf_bytes)
    archiver.bucket = "test-bucket"
    config = fresno_config()
    config.request_delay_seconds = 0
    scraper = FresnoTentativeRulingsScraper(
        config=config, archiver=archiver, event_bus=EventBus(redis_mock)
    )

    raw = scraper._make_base_doc(
        source_url=_FRESNO_SOURCE_URL,
        raw_content=pdf_bytes,
        content_format=ContentFormat.PDF,
    )
    raw.department = "403"
    raw.courthouse = "B.F. Sisk Federal Courthouse"
    raw.extra["link_text"] = "Dept 403"
    raw.extra["filename"] = "03-10-26-dept-403.pdf"

    with patch.object(PdfLinkScraper, "fetch_documents", return_value=[raw]):
        docs = scraper.fetch_documents()
    for doc in docs:
        # Captured the day before the hearing, as the live scraper would.
        doc.capture_timestamp = datetime(2026, 3, 9, 12, 0, 0)
        BaseScraper._process_document(scraper, doc)
    return [json.loads(c.args[1]["data"]) for c in redis_mock.xadd.call_args_list]


def _reingest_fresno_event(pdf_bytes: bytes) -> dict[str, Any]:
    parsed = {
        "state": "ca",
        "county": "fresno",
        "court": "superior_court",
        "content_hash": hashlib.sha256(pdf_bytes).hexdigest(),
        "ext": "pdf",
    }
    return reingest._build_prefix_event(
        _fresno_s3_key(pdf_bytes),
        pdf_bytes,
        parsed,
        "test-bucket",
        provenance={"source_url": _FRESNO_SOURCE_URL},
    )


def _run_worker_capture(
    events: list[dict[str, Any]], worker: Any = None
) -> tuple[list[str], list[dict[str, Any]]]:
    """Process *events*; return (written document ids, stale-cleanup calls)."""
    if worker is None:
        worker = _split_set_change_worker()
    worker._indexer = MagicMock()
    written: list[str] = []
    cleanups: list[dict[str, Any]] = []

    def _fake_insert(conn: Any, **kwargs: Any) -> bool:
        written.append(kwargs["document_id"])
        return False

    def _fake_delete(conn: Any, s3_key: str, valid_ids: list[str], **kwargs: Any) -> int:
        cleanups.append(
            {
                "s3_key": s3_key,
                "valid_ids": list(valid_ids),
                "parent_document_id": kwargs.get("parent_document_id"),
                "owns_key": kwargs.get("owns_key"),
            }
        )
        return 0

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.closed = False
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.return_value = ("11111111-2222-4333-8444-555555555555",)
    mock_cur.fetchall.return_value = []

    with (
        patch("ingestion.worker.psycopg") as mock_psycopg,
        patch("ingestion.worker.resolve_judge", return_value=None),
        patch("ingestion.worker.batch_upsert_parties"),
        patch("ingestion.worker.insert_document_and_ruling", side_effect=_fake_insert),
        patch("ingestion.worker.delete_stale_split_children", side_effect=_fake_delete),
    ):
        mock_psycopg.connect.return_value = mock_conn
        for event in events:
            worker.process_event(event)
    return written, cleanups


def _fresno_fixture_bytes() -> bytes:
    with open(_FRESNO_FIXTURE_PDF, "rb") as fh:
        return fh.read()


def _content_parent(pdf_bytes: bytes) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(pdf_bytes).hexdigest()))


class TestPreSplitIdParity:
    """#4796: live capture and prefix reingest of one S3 key write the same
    document ids, and a live pre-split child is never re-split."""

    def test_pre_split_live_and_reingest_write_same_ids_fresno(self) -> None:
        pdf_bytes = _fresno_fixture_bytes()

        live_ids, _ = _run_worker_capture(_live_fresno_events(pdf_bytes))
        reingest_ids, _ = _run_worker_capture([_reingest_fresno_event(pdf_bytes)])

        assert len(reingest_ids) > 1, "fixture must be a multi-ruling PDF"
        assert sorted(live_ids) == sorted(reingest_ids)
        parent = _content_parent(pdf_bytes)
        assert sorted(reingest_ids) == sorted(
            make_split_document_id(parent, i) for i in range(len(reingest_ids))
        )

    def test_pre_split_live_fresno_cleanup_scoped_to_content_parent(self) -> None:
        """The live run's stale-child cleanup runs once, for the content
        parent, keeping exactly the ids a reingest writes."""
        pdf_bytes = _fresno_fixture_bytes()

        live_ids, cleanups = _run_worker_capture(_live_fresno_events(pdf_bytes))

        assert [c["parent_document_id"] for c in cleanups] == [_content_parent(pdf_bytes)]
        assert sorted(cleanups[0]["valid_ids"]) == sorted(live_ids)
        # The content parent owns the whole key: leftover entry-number and
        # grandchild rows are removed too.
        assert cleanups[0]["owns_key"] is True

    def test_non_content_parent_cleanup_does_not_own_key(self) -> None:
        """A split whose parent is not the key's content parent keeps the
        #4788 own-rows-only scope."""
        worker = _split_set_change_worker()
        event = {**_sc_prefix_event(), "content_hash": "d" * 64}
        with (
            patch("ingestion.worker.delete_stale_split_children", return_value=0) as mock_del,
            patch("ingestion.worker.psycopg"),
        ):
            worker._cleanup_stale_split_children(event, event["document_id"], ["keep"])
        assert mock_del.call_args.kwargs["owns_key"] is False

    def _pre_split_event(self, **extra: Any) -> dict[str, Any]:
        content_hash = "c" * 64
        parent = str(uuid.uuid5(uuid.NAMESPACE_URL, content_hash))
        return {
            "document_id": make_split_document_id(parent, 1),
            "state": "CA",
            "county": "Contra Costa",
            "court": "Superior Court",
            "content_format": "pdf",
            "content_hash": content_hash,
            "s3_key": f"ca/contra_costa/superior_court/raw/{content_hash}.pdf",
            "s3_bucket": "test-bucket",
            "scraper_id": "ca-cc-tentatives",
            "source_url": "https://example.com/x.pdf",
            "ruling_text": (
                "The demurrer is SUSTAINED. The motion to strike is GRANTED. "
                "Case C24-00001 and case C24-00002 are both on calendar today."
            ),
            "case_number": "C24-00001",
            "case_title": "Alpha v. Beta",
            "hearing_date": "2026-03-05",
            "outcome": "sustained",
            "motion_type": "demurrer",
            "extra": {"pre_split": True, **extra},
        }

    def test_pre_split_child_not_resplit(self) -> None:
        """A live pre-split child is written as-is under its own id: no
        multi-ruling split and no stale-child cleanup with it as parent."""
        event = self._pre_split_event(_llm_extracted=True, split_position=1, split_count=3)
        worker = _split_set_change_worker()
        with patch.object(worker, "_llm_split_document") as mock_split:
            written, cleanups = _run_worker_capture([event], worker)

        mock_split.assert_not_called()
        assert cleanups == []
        assert written == [event["document_id"]]

    def test_as_pre_split_child_marks_only_unmarked_pre_split_events(self) -> None:
        from ingestion.worker import as_pre_split_child

        plain = {"document_id": "d", "extra": {}}
        assert as_pre_split_child(plain) is plain
        already = {"document_id": "d", "_split_processed": True, "extra": {"pre_split": True}}
        assert as_pre_split_child(already) is already

        event = self._pre_split_event(_llm_extracted=True, split_position=2, split_count=3)
        marked = as_pre_split_child(event)
        assert marked["_split_processed"] is True
        assert marked["_llm_extracted"] is True
        assert (marked["_split_index"], marked["_split_count"]) == (2, 3)
        assert marked["_original_document_id"] == str(
            uuid.uuid5(uuid.NAMESPACE_URL, event["content_hash"])
        )

        no_hash = as_pre_split_child({"document_id": "d", "extra": {"pre_split": True}})
        assert no_hash["_original_document_id"] == "d"
        assert "_llm_extracted" not in no_hash

    def test_pre_split_child_not_resplit_without_position_keys(self) -> None:
        """An in-flight child captured before the fix (only ``ruling_index``)
        is not re-split either."""
        event = self._pre_split_event(ruling_index=20)
        worker = _split_set_change_worker()
        with patch.object(worker, "_llm_split_document") as mock_split:
            written, cleanups = _run_worker_capture([event], worker)

        mock_split.assert_not_called()
        assert cleanups == []
        assert written == [event["document_id"]]


# ---------------------------------------------------------------------------
# DB-mode full-reparse uses the canonical split id scheme (#4801)
# ---------------------------------------------------------------------------

_KEY_HASH_4801 = "a9a4db792c506cfb1e776f334f2753eda048b1d927961aa146d194bdf439a512"
_KEY_4801 = f"ca/contra_costa/superior_court/raw/{_KEY_HASH_4801}.pdf"


def _content_parent_4801() -> str:
    from ingestion.split_ids import derive_parent_document_id

    return derive_parent_document_id(_KEY_HASH_4801)


def _split_child_hash_4801(parent_hash: str, position: int) -> str:
    return hashlib.sha256(f"{parent_hash}:ruling:{position}".encode()).hexdigest()
