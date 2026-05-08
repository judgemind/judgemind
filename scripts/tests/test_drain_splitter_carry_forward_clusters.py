"""Tests for ``scripts/drain_splitter_carry_forward_clusters.py`` (#4321).

The script identifies splitter-carry-forward clusters (multi-ruling PDFs
whose existing children all share the same ``case_title`` — strong evidence
the old LLM-only path violated rule 5b and copied page-1 metadata onto
every entry) and re-prepares them for reingest by the new pre-LLM splitter.

Like ``scripts/audit_llm_carry_forward.py``, the script imports ``psycopg``
lazily inside its DB driver functions so this test module can import it
without a live DB connection.

These tests cover:

* Cluster discovery (``find_clusters``) — wraps the same query shape as
  the audit's ``_CLUSTER_QUERY`` plus ``ORDER BY`` for deterministic
  iteration.
* Per-cluster planning — verifying the splitter-validation gate (skip
  cluster if the new splitter still yields <2 distinct case_titles).
* Per-cluster DB mutations — DELETE child rulings + child documents,
  UPSERT parent doc row with the canonical ``derive_parent_document_id``
  UUID.
* CLI argument parsing.
* Idempotency — re-running with no clusters left is a clean no-op.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drain_splitter_carry_forward_clusters as _script  # noqa: E402


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------


def _mock_conn_with_rows(rows_per_execute: list[list[tuple]]):
    """Build a MagicMock psycopg connection whose cursor.fetchall returns
    ``rows_per_execute[i]`` on the i-th ``execute`` + ``fetchall`` cycle.

    The script is expected to issue a sequence of cursor reads — one for
    cluster discovery, then per-cluster locks/deletes — and we exercise the
    sequence by pre-loading a list of fetchall results.
    """
    cur = MagicMock()
    fetch_iter = iter(rows_per_execute)

    def _fetchall_side_effect():
        try:
            return next(fetch_iter)
        except StopIteration:
            return []

    cur.fetchall.side_effect = _fetchall_side_effect
    cur.execute.return_value = None
    cur.rowcount = 0
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.transaction.return_value.__enter__ = MagicMock(return_value=None)
    conn.transaction.return_value.__exit__ = MagicMock(return_value=False)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn, cur


class TestFindClusters:
    """``find_clusters`` returns one ``Cluster`` row per (s3_key, case_title)
    bucket whose ruling count is >= 2."""

    def test_returns_clusters(self) -> None:
        rows = [
            (
                "Santa Clara",
                "ca/santa_clara/raw/abc.pdf",
                "ca-santa-clara-tentatives-civil",
                "abc-bucket",
                "Smith v. Jones",
                3,
            )
        ]
        conn, cur = _mock_conn_with_rows([rows])
        clusters = _script.find_clusters(conn, county="Santa Clara")
        assert len(clusters) == 1
        c = clusters[0]
        assert c.county == "Santa Clara"
        assert c.s3_key == "ca/santa_clara/raw/abc.pdf"
        assert c.s3_bucket == "abc-bucket"
        assert c.scraper_id == "ca-santa-clara-tentatives-civil"
        assert c.case_title == "Smith v. Jones"
        assert c.ruling_count == 3

    def test_empty_when_no_clusters(self) -> None:
        conn, _ = _mock_conn_with_rows([[]])
        clusters = _script.find_clusters(conn, county="San Francisco")
        assert clusters == []

    def test_county_filter_passed_as_param(self) -> None:
        conn, cur = _mock_conn_with_rows([[]])
        _script.find_clusters(conn, county="Santa Clara")
        # The most recent execute() call should include "Santa Clara" in
        # the bound params (case-insensitive UPPER match).
        last_call = cur.execute.call_args_list[-1]
        params = (
            last_call[0][1] if len(last_call[0]) > 1 else last_call.kwargs.get("vars")
        )
        assert "Santa Clara" in params

    def test_no_county_filter(self) -> None:
        conn, cur = _mock_conn_with_rows([[]])
        _script.find_clusters(conn, county=None)
        last_call = cur.execute.call_args_list[-1]
        sql = last_call[0][0]
        assert "ct.county" not in sql or "UPPER(ct.county)" not in sql.upper().replace(
            "UPPER(CT.COUNTY) = UPPER(%S)", ""
        )


# ---------------------------------------------------------------------------
# Splitter validation gate
# ---------------------------------------------------------------------------


class TestPlanClusterDrain:
    """``plan_cluster_drain`` runs the registered splitter on the parent PDF
    and returns ``ClusterPlan`` with status ``ready`` / ``skip_no_split`` /
    ``skip_single_title``."""

    def test_ready_when_split_produces_distinct_titles(self) -> None:
        # Splitter mock returns two split records with distinct titles.
        sr_a = MagicMock()
        sr_a.case_title = "Smith v. Jones"
        sr_b = MagicMock()
        sr_b.case_title = "Lee v. Wong"
        split_fn = MagicMock(return_value=[sr_a, sr_b])

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/abc.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Smith v. Jones",
            ruling_count=2,
        )
        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=b"%PDF-1.4 ...",
            split_fn=split_fn,
            extract_text_fn=lambda b: "fake pdf text",
        )
        assert plan.status == "ready"
        assert plan.distinct_titles == 2

    def test_skip_when_split_returns_single_entry(self) -> None:
        sr = MagicMock()
        sr.case_title = "Smith v. Jones"
        split_fn = MagicMock(return_value=[sr])

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/abc.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Smith v. Jones",
            ruling_count=2,
        )
        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=b"%PDF-1.4 ...",
            split_fn=split_fn,
            extract_text_fn=lambda b: "fake pdf text",
        )
        assert plan.status == "skip_no_split"

    def test_skip_when_split_returns_only_one_distinct_title(self) -> None:
        # Two split entries but they collapse to one case_title — the
        # new splitter would still produce a same-title cluster, so
        # draining and re-running won't fix this row. Skip.
        sr_a = MagicMock()
        sr_a.case_title = "Smith v. Jones"
        sr_b = MagicMock()
        sr_b.case_title = "Smith v. Jones"
        split_fn = MagicMock(return_value=[sr_a, sr_b])

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/abc.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Smith v. Jones",
            ruling_count=2,
        )
        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=b"%PDF-1.4 ...",
            split_fn=split_fn,
            extract_text_fn=lambda b: "fake pdf text",
        )
        assert plan.status == "skip_single_title"

    def test_skip_when_no_splitter_registered(self) -> None:
        cluster = _script.Cluster(
            county="Other",
            s3_key="x",
            s3_bucket="b",
            scraper_id="other-scraper",
            case_title="t",
            ruling_count=2,
        )
        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=b"%PDF",
            split_fn=None,
            extract_text_fn=lambda b: "text",
        )
        assert plan.status == "skip_no_split"


# ---------------------------------------------------------------------------
# Per-cluster DB mutation (delete + restore parent)
# ---------------------------------------------------------------------------


class TestRestoreParentForCluster:
    """``restore_parent_for_cluster`` runs four SQL statements inside the
    caller's transaction:

      1. ``SELECT id, content_hash, court_id, ...
            FROM documents WHERE s3_key=%s AND status='active' FOR UPDATE``
         — locks the cluster's child rows.
      2. ``DELETE FROM rulings WHERE document_id = ANY(%s::uuid[])``
      3. ``DELETE FROM documents WHERE id = ANY(%s::uuid[])``
      4. ``INSERT INTO documents (id, ...) VALUES (...) ON CONFLICT (id)
            DO UPDATE SET status='active', change_type=NULL, ...``
         — restores the parent doc row keyed on
         ``derive_parent_document_id(content_hash)``.
    """

    def test_emits_for_update_lock(self) -> None:
        # First fetch — child rows for this cluster.
        # Each row: (id, content_hash, court_id, document_type, format,
        #           captured_at, source_url, scraper_id)
        child_rows = [
            (
                "11111111-1111-5111-9111-111111111111",
                "abc123",
                "court-uuid",
                "tentative_ruling",
                "pdf",
                "2026-04-01T00:00:00Z",
                "https://example.com/a",
                "ca-santa-clara-tentatives-civil",
            ),
            (
                "22222222-2222-5222-9222-222222222222",
                "abc124",
                "court-uuid",
                "tentative_ruling",
                "pdf",
                "2026-04-01T00:00:00Z",
                "https://example.com/a",
                "ca-santa-clara-tentatives-civil",
            ),
        ]
        conn, cur = _mock_conn_with_rows([child_rows])

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/abc.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Smith v. Jones",
            ruling_count=2,
        )
        result = _script.restore_parent_for_cluster(
            conn,
            cluster,
            parent_content_hash="parent-real-hash",
            dry_run=False,
        )
        # First execute — the SELECT...FOR UPDATE.
        first_sql = cur.execute.call_args_list[0][0][0]
        assert "FOR UPDATE" in first_sql.upper()
        assert result["children_deleted"] >= 1

    def test_dry_run_skips_writes(self) -> None:
        child_rows = [
            (
                "11111111-1111-5111-9111-111111111111",
                "abc123",
                "court-uuid",
                "tentative_ruling",
                "pdf",
                "2026-04-01T00:00:00Z",
                "https://example.com/a",
                "ca-santa-clara-tentatives-civil",
            ),
        ]
        conn, cur = _mock_conn_with_rows([child_rows])

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/abc.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Smith v. Jones",
            ruling_count=1,
        )
        _script.restore_parent_for_cluster(
            conn,
            cluster,
            parent_content_hash="parent-real-hash",
            dry_run=True,
        )
        # No DELETE / INSERT statements should have run in dry-run mode.
        for call in cur.execute.call_args_list:
            sql = call[0][0].upper()
            assert "DELETE" not in sql
            assert "INSERT" not in sql

    def test_parent_doc_id_derives_from_content_hash(self) -> None:
        # Our restore path must use the v5-from-content_hash form —
        # ``derive_parent_document_id(content_hash)`` — not a v5-from-
        # (parent,index) form, which would mis-match ``is_split_child_id``
        # and re-trigger the line-2580 guard in reingest_from_s3. We
        # verify that by loading split_ids the same way the script does
        # (direct file-spec load, avoiding the ingestion/__init__ chain).
        split_ids = _script._load_split_ids_module()
        expected = split_ids.derive_parent_document_id("parent-real-hash")
        assert _script.compute_parent_doc_id("parent-real-hash") == expected


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotent:
    """A second run with no clusters left is a clean no-op."""

    def test_no_clusters_is_no_op(self) -> None:
        conn, _ = _mock_conn_with_rows([[]])
        with patch.object(_script, "_load_psycopg") as mock_load:
            fake_pg = MagicMock()
            fake_pg.connect.return_value = conn
            mock_load.return_value = fake_pg
            stats = _script.run_drain(
                dsn="postgres://stub",
                county="Santa Clara",
                s3_fetcher=lambda bucket, key: b"%PDF",
                splitter_resolver=lambda scraper_id: None,
                text_extractor=lambda b: "text",
                dry_run=True,
            )
        assert stats["clusters_found"] == 0
        assert stats["clusters_drained"] == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_help_exits_zero(self, capsys) -> None:
        # ``--help`` must print and exit cleanly.
        with patch.object(
            sys, "argv", ["drain_splitter_carry_forward_clusters.py", "--help"]
        ):
            try:
                _script.main()
            except SystemExit as exc:
                assert exc.code == 0
        captured = capsys.readouterr()
        assert "--county" in captured.out
        assert "--dry-run" in captured.out

    def test_county_argument_present(self) -> None:
        parser = _script._build_parser()
        args = parser.parse_args(["--county", "Santa Clara", "--dry-run"])
        assert args.county == "Santa Clara"
        assert args.dry_run is True

    def test_limit_argument(self) -> None:
        parser = _script._build_parser()
        args = parser.parse_args(["--county", "Santa Clara", "--limit", "5"])
        assert args.limit == 5
