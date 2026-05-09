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

import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Pre-import mocking — the script imports ``framework.logging`` (which
# transitively imports ``structlog``) at module level; neither is installed
# in the lightweight CI ``scripts-tests (python)`` environment (it only
# installs ``pytest pytest-xdist boto3 -e packages/judgemind-config``).
# Mock both modules in ``sys.modules`` before importing the script under
# test, mirroring ``test_backfill_split_supersede.py``. The mocks are
# scoped: regression tests in ``TestLoggerExtraFieldsSurfaceInOutput`` use
# ``pytest.importorskip("structlog")`` and reach for the real
# ``configure_structlog`` so they only run when structlog is actually
# available (developer laptop with the scraper-framework venv, or any CI
# job that installs scraper-framework). Save/replay envelope is centralised
# in ``scripts/tests/_mock_helpers.py`` (#4430).
# ---------------------------------------------------------------------------

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

    def test_format_b_splitter_receives_pdf_bytes_kwarg(self) -> None:
        """Regression test for #4360.

        ``plan_cluster_drain`` MUST pass ``pdf_bytes`` to the registered
        splitter so SC's bytes-aware format-B path can fire.  Pre-#4360
        the call site was ``split_fn(text)`` and SC dept-6 PDFs reported
        ``skip_no_split`` because format A returns ``[]`` and format B
        couldn't run without bytes.

        We stub a splitter that mimics SC's format-B behaviour: returns
        ``[]`` when invoked with text only, returns >= 2 distinct-title
        rulings when invoked with ``pdf_bytes``.  This proves the
        plan_cluster_drain call site threads bytes through.
        """
        captured: dict[str, Any] = {}

        def fake_split(text: str, pdf_bytes: bytes | None = None) -> list[Any]:
            captured["text"] = text
            captured["pdf_bytes"] = pdf_bytes
            if pdf_bytes is None:
                return []
            sr_a = MagicMock()
            sr_a.case_title = "Huynh vs Redis Labs"
            sr_b = MagicMock()
            sr_b.case_title = "Lee Casper v. Ford"
            return [sr_a, sr_b]

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/raw/dept6.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Plaintiff v. FCA",
            ruling_count=10,
        )
        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=b"%PDF-1.4 ...",
            split_fn=fake_split,
            extract_text_fn=lambda b: "fake dept-6 text",
        )
        # The bytes were threaded — splitter saw pdf_bytes != None and
        # returned the multi-ruling result, so the plan is ready.
        assert captured["pdf_bytes"] == b"%PDF-1.4 ..."
        assert plan.status == "ready"
        assert plan.distinct_titles == 2

    def test_format_b_real_fixture_through_plan_cluster_drain(self) -> None:
        """End-to-end regression test for #4360 using the real SC dept-6
        fixture.  Wires ``plan_cluster_drain`` to the actual SC
        ``_split_rulings`` callable and an extract_text_fn backed by
        ``pdfplumber`` (via the scraper-framework helper) — proves the
        format-B path returns >= 10 distinct titles when bytes flow
        through.

        Skipped when the scraper-framework venv isn't on ``sys.path`` —
        this test runs from the scraper-framework venv during CI's
        per-package pytest stage (it imports SC's ``_split_rulings`` and
        ``extract_pdf_text`` helpers from the package source tree).
        """
        import pathlib

        try:
            from courts.ca.sc_tentatives import (  # type: ignore[import-not-found]
                _split_rulings,
                extract_pdf_text,
            )
        except ImportError:
            import pytest

            pytest.skip(
                "scraper-framework imports not available; "
                "test runs from packages/scraper-framework/.venv"
            )

        fixture_path = (
            pathlib.Path(__file__).parent.parent.parent
            / "packages"
            / "scraper-framework"
            / "tests"
            / "fixtures"
            / "sc_dept6_tues.pdf"
        )
        if not fixture_path.exists():
            import pytest

            pytest.skip(f"SC dept-6 fixture not found at {fixture_path}")

        pdf_bytes = fixture_path.read_bytes()

        cluster = _script.Cluster(
            county="Santa Clara",
            s3_key="ca/santa_clara/superior_court/raw/dept6_tues.pdf",
            s3_bucket="abc-bucket",
            scraper_id="ca-santa-clara-tentatives-civil",
            case_title="Plaintiff v. FCA",
            ruling_count=10,
        )

        plan = _script.plan_cluster_drain(
            cluster,
            pdf_bytes=pdf_bytes,
            split_fn=_split_rulings,
            extract_text_fn=extract_pdf_text,
        )
        # The dept-6 summary table holds 10 distinct case rows.  Without
        # the #4360 fix the plan was ``skip_no_split`` (format A returns
        # 0 entries, format B can't run without bytes).
        assert plan.status == "ready", (
            f"expected status='ready' from real SC dept-6 fixture; got {plan.status}"
        )
        assert plan.distinct_titles >= 10, (
            f"expected >= 10 distinct titles; got {plan.distinct_titles}"
        )


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
# split_ids path resolution (#4374)
# ---------------------------------------------------------------------------


class TestSplitIdsPathResolution:
    """Regression tests for #4374 — when the script runs via
    ``scripts/ecs-run-task.sh``, ``__file__`` is ``/tmp/_oneshot_script``
    so the original ``Path(__file__).resolve().parent.parent`` collapses to
    ``/`` and ``_SCRAPER_SRC`` wrongly becomes
    ``/packages/scraper-framework/src``. The previous implementation passed
    that bogus path straight to ``importlib.util.spec_from_file_location``
    and crashed every cluster's ``Restore transaction failed`` branch with
    ``[Errno 2] No such file or directory:
    '/packages/scraper-framework/src/ingestion/split_ids.py'``.

    The fix is the candidate-path fallback list in
    ``_split_ids_candidate_paths`` — try the dev-laptop path first, then
    the in-image ``/app/src`` path that the scraper-framework Dockerfile
    actually writes the source tree to.
    """

    def test_candidate_paths_include_ecs_layouts(self) -> None:
        # The fallback list must cover the ECS oneshot layouts, otherwise
        # #4374 is back. Specifically, ``/app/src`` is where the
        # scraper-framework Dockerfile copies the package source (line 34:
        # ``COPY packages/scraper-framework/src/ ./src/``).
        candidates = _script._split_ids_candidate_paths()
        candidate_strs = [str(c) for c in candidates]
        # Dev-laptop path (still first — exercised by every other test in
        # this file via the existing ``_load_split_ids_module`` call).
        assert any(
            "packages/scraper-framework/src/ingestion/split_ids.py" in c
            for c in candidate_strs
        )
        # ECS in-image flattened layout — the actual fix for #4374.
        assert "/app/src/ingestion/split_ids.py" in candidate_strs
        # Defense-in-depth path for any future image layout that keeps the
        # per-package directory.
        assert (
            "/app/packages/scraper-framework/src/ingestion/split_ids.py"
            in candidate_strs
        )

    def test_resolve_picks_first_existing_candidate(self, tmp_path: Any) -> None:
        # When multiple candidates exist, ``_resolve_split_ids_path`` picks
        # the FIRST matching one (dev-laptop > ECS app-src > legacy app
        # path). Simulate by monkeypatching the candidate list to a
        # tmp-path that does exist.
        candidate_a = tmp_path / "first" / "split_ids.py"
        candidate_a.parent.mkdir(parents=True)
        candidate_a.write_text("# fake")
        candidate_b = tmp_path / "second" / "split_ids.py"
        candidate_b.parent.mkdir(parents=True)
        candidate_b.write_text("# also fake")
        with patch.object(
            _script,
            "_split_ids_candidate_paths",
            return_value=[candidate_a, candidate_b],
        ):
            assert _script._resolve_split_ids_path() == candidate_a

    def test_resolve_falls_through_to_second_candidate_when_first_missing(
        self, tmp_path: Any
    ) -> None:
        # The bug: when the first candidate (dev-laptop ``_SCRAPER_SRC``)
        # collapses to ``/packages/...`` because ``__file__`` is
        # ``/tmp/_oneshot_script``, that file does NOT exist, and the
        # resolver MUST fall through to the next candidate (the
        # ``/app/src`` in-image path) instead of crashing. This is exactly
        # the path-resolution regression #4374 reports.
        missing = tmp_path / "does-not-exist" / "split_ids.py"
        present = tmp_path / "present" / "split_ids.py"
        present.parent.mkdir(parents=True)
        present.write_text("# real-enough")
        with patch.object(
            _script,
            "_split_ids_candidate_paths",
            return_value=[missing, present],
        ):
            assert _script._resolve_split_ids_path() == present

    def test_resolve_raises_self_diagnosing_error_when_all_missing(
        self, tmp_path: Any
    ) -> None:
        # If every candidate is missing — should never happen in practice
        # but guards against a future Dockerfile change that drops every
        # known layout — the error message lists every candidate that was
        # tried, instead of the original bare ``[Errno 2]`` from inside
        # ``importlib.util.spec_from_file_location``. This makes the
        # failure self-diagnosing per
        # ``feedback_instrument_before_guess_validated.md``.
        missing_a = tmp_path / "a" / "split_ids.py"
        missing_b = tmp_path / "b" / "split_ids.py"
        with patch.object(
            _script,
            "_split_ids_candidate_paths",
            return_value=[missing_a, missing_b],
        ):
            with pytest.raises(RuntimeError) as excinfo:
                _script._resolve_split_ids_path()
        msg = str(excinfo.value)
        # The error must list every tried candidate so an operator can
        # diagnose without re-reading the source.
        assert str(missing_a) in msg
        assert str(missing_b) in msg
        assert "Could not locate" in msg

    def test_load_succeeds_in_simulated_ecs_oneshot_layout(self, tmp_path: Any) -> None:
        # Reproduce the #4374 failure mode end-to-end: when
        # ``_SCRAPER_SRC`` would collapse to ``/packages/scraper-framework/src``
        # (the exact path in the production error message), the loader
        # MUST still find ``split_ids.py`` via the
        # ``/app/src/ingestion/split_ids.py`` candidate. We simulate by:
        #   1. Pointing ``_SCRAPER_SRC`` at a non-existent path
        #      mirroring the ``__file__ = /tmp/_oneshot_script`` collapse.
        #   2. Pointing the ``/app/src`` fallback at a tmp-path that
        #      contains a real (minimal) ``split_ids.py``.
        # Without the fix, this test fails because
        # ``_load_split_ids_module`` would only ever try the bogus
        # ``_SCRAPER_SRC`` path.
        fake_oneshot_root = Path("/packages/scraper-framework/src")
        # ``fake_oneshot_root / "ingestion" / "split_ids.py"`` won't exist
        # on any real filesystem (root is not writable here, by design).
        ecs_app_src = tmp_path / "app-src"
        (ecs_app_src / "ingestion").mkdir(parents=True)
        # Minimal split_ids stand-in — the loader should be able to
        # ``exec_module`` it without errors.
        (ecs_app_src / "ingestion" / "split_ids.py").write_text(
            "MARKER = 'loaded-from-ecs-app-src'\n"
        )

        # Reset the cached module so the next call re-runs path resolution.
        _script._load_split_ids_module._cached = None  # type: ignore[attr-defined]
        try:
            with (
                patch.object(_script, "_SCRAPER_SRC", fake_oneshot_root),
                patch.object(_script, "_ECS_APP_SRC", ecs_app_src),
            ):
                module = _script._load_split_ids_module()
            assert module.MARKER == "loaded-from-ecs-app-src"
        finally:
            # Restore the cache so subsequent tests don't see our stub.
            _script._load_split_ids_module._cached = None  # type: ignore[attr-defined]


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


# ---------------------------------------------------------------------------
# Logger surfacing of ``extra=`` fields (#4368)
# ---------------------------------------------------------------------------


class TestLoggerExtraFieldsSurfaceInOutput:
    """Regression for #4368.

    The script previously called ``logging.basicConfig`` with the format
    string ``"%(asctime)s %(levelname)-8s %(message)s"``. That format string
    silently drops every ``extra=`` field passed to ``logger.info(...)``,
    so CloudWatch Logs Insights output for ``Skipping cluster`` and
    ``Drain summary`` lines was missing the very ``s3_key`` /
    ``plan_status`` / ``clusters_drained`` fields a follow-up agent needs
    to verify each post-deploy run.

    The fix swaps ``logging.basicConfig`` for
    ``configure_structlog(json=True, stdlib_bridge=True)`` (the same
    pattern ``scripts/reingest_from_s3.py`` uses), which routes stdlib
    ``logging.getLogger().info(..., extra={...})`` calls through
    structlog's ``ProcessorFormatter`` + ``ExtraAdder`` so the extras
    are JSON-encoded into the output line.

    These tests assert the fix at the layer the bug lives — they call
    the *real* ``configure_structlog`` (skipping when structlog isn't
    available, e.g. in the lightweight CI ``scripts-tests (python)``
    shard which mocks structlog at module import) and capture stdlib
    logger output to verify the ``extra=`` fields surface as JSON keys.
    """

    @pytest.fixture
    def configured_logger_and_capture(self):
        """Yield ``(logger, capture_buffer)`` backed by a real
        ``configure_structlog(json=True, stdlib_bridge=True)`` setup
        whose bridge ``StreamHandler`` writes into ``capture_buffer``
        instead of ``sys.stderr``.

        Skips when structlog or scraper-framework isn't importable
        (the python CI shard for scripts-tests doesn't install them).
        Restores the root logger's previous handler set after the test.
        """
        # ``importorskip`` fails fast when real structlog isn't present
        # AND unwraps the MagicMock that the module-import-time block
        # above installed in ``sys.modules``.
        pytest.importorskip("structlog")
        scraper_src = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "packages",
                "scraper-framework",
                "src",
            )
        )
        if not os.path.isdir(scraper_src):
            pytest.skip(
                "scraper-framework src not present — only runs on a "
                "developer laptop or a CI job that installs it."
            )
        if scraper_src not in sys.path:
            sys.path.insert(0, scraper_src)
        # Drop the MagicMocks the module-import-time block installed so
        # the real ``framework.logging`` imports cleanly here.
        for _mod in ("framework", "framework.logging"):
            cur = sys.modules.get(_mod)
            if isinstance(cur, MagicMock):
                del sys.modules[_mod]
        try:
            from framework.logging import configure_structlog
        except ImportError:
            pytest.skip("framework.logging not importable in this env.")

        prev_handlers = list(logging.root.handlers)
        prev_level = logging.root.level
        configure_structlog(json=True, stdlib_bridge=True)
        # ``configure_structlog(stdlib_bridge=True)`` installs a
        # ``StreamHandler()`` (default stream=stderr) with a
        # ``ProcessorFormatter`` on the root logger. Redirect that
        # handler's stream into our buffer so we capture exactly the
        # bytes CloudWatch would see.
        bridge_handler = next(
            (
                h
                for h in logging.root.handlers
                if h.formatter is not None
                and h.__class__.__module__ == "logging"
                and isinstance(h, logging.StreamHandler)
            ),
            None,
        )
        assert bridge_handler is not None, (
            "Expected configure_structlog(stdlib_bridge=True) to "
            "install a StreamHandler with a ProcessorFormatter on "
            "the root logger."
        )
        buffer = io.StringIO()
        bridge_handler.setStream(buffer)
        # Suppress propagation to pytest's caplog handler so the buffer
        # contains only the bridge-formatted JSON line.
        logger = logging.getLogger("test_drain_splitter_extras_4368")
        prev_propagate = logger.propagate
        logger.propagate = True  # bridge sits on root, so we DO want to propagate
        try:
            yield logger, buffer
        finally:
            logger.propagate = prev_propagate
            for handler in list(logging.root.handlers):
                logging.root.removeHandler(handler)
            for handler in prev_handlers:
                logging.root.addHandler(handler)
            logging.root.setLevel(prev_level)

    def test_skipping_cluster_log_surfaces_s3_key_and_plan_status(
        self, configured_logger_and_capture
    ) -> None:
        # Mirror the call site at line 659 — the post-deploy verification
        # path needs to be able to grep CloudWatch for the plan_status
        # field to count skip_no_split vs. skip_single_title vs. error.
        logger, buffer = configured_logger_and_capture
        logger.info(
            "Skipping cluster — splitter would not improve it",
            extra={
                "s3_key": "ca/santa_clara/2026/05/09/example.pdf",
                "plan_status": "skip_no_split",
                "distinct_titles": 1,
            },
        )
        output = buffer.getvalue()
        assert "s3_key" in output, (
            "extra={'s3_key': ...} must surface in log output (#4368). "
            f"Captured output: {output!r}"
        )
        assert "plan_status" in output
        assert "distinct_titles" in output
        assert "ca/santa_clara/2026/05/09/example.pdf" in output
        assert "skip_no_split" in output

    def test_drain_summary_log_surfaces_counter_fields(
        self, configured_logger_and_capture
    ) -> None:
        # Mirror the call site at line 803 — the canonical "did the drain
        # actually drain anything" verification field is
        # ``clusters_drained``. Without it surfacing in CloudWatch, the
        # post-deploy verification has no choice but to run a local probe
        # against the in-repo fixture (which is what motivated #4368).
        logger, buffer = configured_logger_and_capture
        logger.info(
            "Drain summary",
            extra={
                "clusters_found": 22,
                "clusters_drained": 15,
                "clusters_skipped": 7,
                "children_deleted_total": 38,
            },
        )
        output = buffer.getvalue()
        assert "clusters_found" in output
        assert "clusters_drained" in output
        assert "clusters_skipped" in output
        assert "children_deleted_total" in output
        # Numeric values must surface — JSON renders them unquoted.
        assert "22" in output
        assert "15" in output
        assert "38" in output

    def test_log_lines_are_parseable_json(self, configured_logger_and_capture) -> None:
        # CloudWatch Logs Insights treats each line as JSON when the
        # message starts with ``{``. The structlog JSONRenderer emits
        # exactly one JSON object per log call.
        logger, buffer = configured_logger_and_capture
        logger.info(
            "Drain summary",
            extra={"clusters_drained": 15},
        )
        output = buffer.getvalue()
        json_lines = [ln for ln in output.strip().split("\n") if ln.startswith("{")]
        assert json_lines, f"Expected at least one JSON-formatted line; got: {output!r}"
        parsed = json.loads(json_lines[-1])
        assert parsed.get("event") == "Drain summary"
        assert parsed.get("clusters_drained") == 15
