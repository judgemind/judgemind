"""Tests for ``scripts/audit_llm_carry_forward.py`` (#4289).

Covers the four carry-forward check primitives:
- outcome_continue (granted+ outcome, ruling_text starts with "continue")
- motion_type_contradiction (motion_type='demurrer' but text has no demurrer
  stem, etc.)
- case_title_text_mismatch (no case_title party word in ruling_text)

The script imports ``psycopg`` lazily inside ``run_audit``, so tests can
import the module without a live DB connection. The script also imports
``framework.logging`` (which transitively imports ``structlog``) at module
level since the #4373 migration; we pre-mock both in ``sys.modules`` so the
lightweight CI scripts-tests (python) shard import works (it only installs
pytest, pytest-xdist, boto3, judgemind-config).

Cluster check 4 (all_same_case_title_cluster) is SQL-only and tested via
``run_audit`` integration tests using a mock psycopg connection.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Pre-import mocking — inject structlog and framework.logging mocks before
# the script loads. The script's top-level
# ``from framework.logging import configure_structlog`` would otherwise raise
# ModuleNotFoundError in the lightweight CI scripts-tests (python) shard.
# See #4373.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_mock_structlog = MagicMock()
_mock_structlog.get_logger.return_value = MagicMock()
_mock_framework = MagicMock()
_mock_framework_logging = MagicMock()

_modules_to_mock = {
    "structlog": _mock_structlog,
    "framework": _mock_framework,
    "framework.logging": _mock_framework_logging,
}

_saved_modules: dict[str, object] = {}
for _mod_name, _mock_mod in _modules_to_mock.items():
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]
    sys.modules[_mod_name] = _mock_mod

import audit_llm_carry_forward as _script  # noqa: E402

# Restore sys.modules to avoid polluting other test files.
for _mod_name in list(_modules_to_mock.keys()):
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]

_check_outcome_continue = _script._check_outcome_continue
_check_motion_type_contradiction = _script._check_motion_type_contradiction
_check_case_title_text_mismatch = _script._check_case_title_text_mismatch
_extract_parties_from_title = _script._extract_parties_from_title
_significant_words = _script._significant_words
render_text_report = _script.render_text_report
run_audit = _script.run_audit


# ---------------------------------------------------------------------------
# Check 1 — outcome_continue
# ---------------------------------------------------------------------------


class TestOutcomeContinue:
    """Definitive outcome but ruling_text starts with continuance boilerplate."""

    def test_granted_with_continue_lowercase(self) -> None:
        assert _check_outcome_continue(
            "granted", "Continue all matters on calendar to July 23, 2026."
        )

    def test_granted_with_continued_capitalized(self) -> None:
        assert _check_outcome_continue("granted", "Continued to April 6, 2026.")

    def test_denied_with_motion_continued(self) -> None:
        assert _check_outcome_continue(
            "denied", "The motion is continued to next month."
        )

    def test_granted_in_part_with_court_continues(self) -> None:
        assert _check_outcome_continue(
            "granted_in_part", "The court continues this matter."
        )

    def test_granted_with_normal_text(self) -> None:
        assert not _check_outcome_continue(
            "granted", "The motion is granted in its entirety."
        )

    def test_continued_outcome_is_ok(self) -> None:
        # outcome='continued' on continuance text is correct, not carry-forward.
        assert not _check_outcome_continue("continued", "Continue to April 6, 2026.")

    def test_off_calendar_outcome_is_ok(self) -> None:
        assert not _check_outcome_continue("off_calendar", "Continue to April 6, 2026.")

    def test_null_inputs(self) -> None:
        assert not _check_outcome_continue(None, "Continue.")
        assert not _check_outcome_continue("granted", None)
        assert not _check_outcome_continue(None, None)

    def test_leading_whitespace_tolerated(self) -> None:
        assert _check_outcome_continue("granted", "   Continue to next month.\n")


# ---------------------------------------------------------------------------
# Check 2 — motion_type_contradiction
# ---------------------------------------------------------------------------


class TestMotionTypeContradiction:
    """motion_type label contradicts ruling_text vocabulary."""

    def test_demurrer_with_demur_stem_ok(self) -> None:
        assert (
            _check_motion_type_contradiction("demurrer", "The demurrer is overruled.")
            is None
        )

    def test_demurrer_without_demur_stem_flagged(self) -> None:
        # motion_type says demurrer but text has no demur* stem.
        assert (
            _check_motion_type_contradiction(
                "demurrer",
                "The motion to compel further responses is granted.",
            )
            is not None
        )

    def test_msj_with_summary_judgment_ok(self) -> None:
        assert (
            _check_motion_type_contradiction(
                "summary judgment",
                "Defendant's motion for summary judgment is denied.",
            )
            is None
        )

    def test_msj_label_on_unrelated_text_flagged(self) -> None:
        assert (
            _check_motion_type_contradiction(
                "MSJ",
                "Continue all matters on calendar.",
            )
            is not None
        )

    def test_compel_with_compel_stem_ok(self) -> None:
        assert (
            _check_motion_type_contradiction(
                "motion to compel",
                "Plaintiff's motion to compel further responses is granted.",
            )
            is None
        )

    def test_compel_without_stem_flagged(self) -> None:
        assert (
            _check_motion_type_contradiction(
                "motion to compel",
                "The demurrer is sustained without leave to amend.",
            )
            is not None
        )

    def test_strike_present_ok(self) -> None:
        assert (
            _check_motion_type_contradiction(
                "motion to strike", "The motion to strike is denied."
            )
            is None
        )

    def test_anti_slapp_present_ok(self) -> None:
        # 425.16 also accepted as the SLAPP statute reference.
        assert (
            _check_motion_type_contradiction(
                "anti-slapp", "Defendant's CCP § 425.16 motion is granted."
            )
            is None
        )

    def test_null_motion_type(self) -> None:
        assert _check_motion_type_contradiction(None, "Anything") is None

    def test_null_text(self) -> None:
        assert _check_motion_type_contradiction("demurrer", None) is None

    def test_unmapped_motion_type_skipped(self) -> None:
        # We don't have a stem for "ex parte application" — skip rather
        # than false-positive.
        assert (
            _check_motion_type_contradiction("ex parte application", "Continue.")
            is None
        )


# ---------------------------------------------------------------------------
# Check 3 — case_title_text_mismatch
# ---------------------------------------------------------------------------


class TestCaseTitleTextMismatch:
    """Significant case_title party words not present in ruling_text."""

    def test_party_present_in_text_ok(self) -> None:
        assert not _check_case_title_text_mismatch(
            "Smith v. Jones", "Mr. Smith's motion is granted."
        )

    def test_no_party_in_text_flagged(self) -> None:
        assert _check_case_title_text_mismatch(
            "Smith v. Jones", "Continue to April 6, 2026."
        )

    def test_only_one_party_in_text_ok(self) -> None:
        # We only require ANY significant word match — a one-sided ruling
        # mentioning only the defendant is still correctly attributed.
        assert not _check_case_title_text_mismatch(
            "Smith v. Jones", "The court rules in favor of Jones."
        )

    def test_noise_words_ignored(self) -> None:
        # "The People v. The State" has no significant words after noise
        # stripping — should not produce a false positive (returns False
        # because no words to check).
        assert not _check_case_title_text_mismatch(
            "The People v. The State", "Continue."
        )

    def test_long_party_name_partial_match_ok(self) -> None:
        assert not _check_case_title_text_mismatch(
            "Acme Industries Corp v. Widgets LLC",
            "Acme's motion to compel is granted.",
        )

    def test_no_v_separator_falls_back_to_whole_title(self) -> None:
        # case_title "In re Estate of Smith" has no "v." — should still
        # check word presence.
        assert _check_case_title_text_mismatch("Estate of Smith Trust", "Continue.")
        assert not _check_case_title_text_mismatch(
            "Estate of Smith Trust", "The Smith trust is upheld."
        )

    def test_null_inputs(self) -> None:
        assert not _check_case_title_text_mismatch(None, "Anything")
        assert not _check_case_title_text_mismatch("Smith v. Jones", None)
        assert not _check_case_title_text_mismatch(None, None)

    def test_empty_inputs(self) -> None:
        assert not _check_case_title_text_mismatch("", "Continue.")
        assert not _check_case_title_text_mismatch("Smith v. Jones", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestExtractPartiesFromTitle:
    def test_basic_v(self) -> None:
        assert _extract_parties_from_title("Smith v. Jones") == (
            "smith",
            "jones",
        )

    def test_vs_separator(self) -> None:
        assert _extract_parties_from_title("Smith vs. Jones") == (
            "smith",
            "jones",
        )

    def test_vs_no_period(self) -> None:
        assert _extract_parties_from_title("Smith vs Jones") == (
            "smith",
            "jones",
        )

    def test_no_separator(self) -> None:
        assert _extract_parties_from_title("Smith Jones") is None

    def test_null(self) -> None:
        assert _extract_parties_from_title(None) is None

    def test_empty(self) -> None:
        assert _extract_parties_from_title("") is None


class TestSignificantWords:
    def test_filters_noise(self) -> None:
        assert "smith" in _significant_words("the smith people")
        assert "people" not in _significant_words("the smith people")

    def test_min_length(self) -> None:
        # 'a' and 'is' are filtered as too short.
        words = _significant_words("a smith is jones")
        assert "smith" in words
        assert "jones" in words
        assert "a" not in words
        assert "is" not in words


# ---------------------------------------------------------------------------
# run_audit integration — uses mocked psycopg cursor
# ---------------------------------------------------------------------------


def _mock_cursor_with_rows(
    ruling_rows: list[tuple], cluster_rows: list[tuple] | None = None
):
    """Build a MagicMock psycopg cursor that returns the provided rows.

    First execute = ruling query, second execute = cluster query (if
    cluster_rows is not None — otherwise an empty list).
    """
    cur = MagicMock()
    fetch_results = [ruling_rows, cluster_rows or []]
    fetch_iter = iter(fetch_results)

    def _fetchall_side_effect():
        return next(fetch_iter)

    cur.fetchall.side_effect = _fetchall_side_effect
    cur.execute.return_value = None
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cur
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


class TestRunAuditIntegration:
    """``run_audit`` wires the per-row checks plus the cluster check together."""

    def _patch_psycopg(self, conn):
        # ``run_audit`` calls ``_load_psycopg()`` to lazy-import the module,
        # then ``pg.connect(dsn)``. Replace the loader with a fake that
        # returns a stub module exposing ``connect``.
        fake_pg = MagicMock()
        fake_pg.connect.return_value = conn
        return patch.object(_script, "_load_psycopg", return_value=fake_pg)

    def test_all_clean(self) -> None:
        # One Riverside ruling, perfectly consistent — no signals.
        rows = [
            (
                "ruling-1",
                "Smith v. Jones",
                "The Smith demurrer is sustained without leave to amend.",
                "granted",
                "demurrer",
                "ca/riverside/raw/abc123.pdf",
                "ca-riverside-tentatives-civil",
                "Riverside",
                "CA",
            )
        ]
        conn = _mock_cursor_with_rows(rows, [])
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub")
        assert summary["all_clean"] is True
        riv = summary["counties"]["Riverside"]
        assert riv["total_rulings"] == 1
        assert riv["outcome_continue"]["count"] == 0
        assert riv["motion_type_contradiction"]["count"] == 0
        assert riv["case_title_text_mismatch"]["count"] == 0
        assert riv["all_same_case_title_cluster"]["count"] == 0

    def test_outcome_continue_signal(self) -> None:
        # The shape from #3649 — granted but text starts with "Continue".
        rows = [
            (
                "ruling-2",
                "Smith v. Jones",
                "Continue to April 6, 2026.",
                "granted",
                None,
                "ca/riverside/raw/def456.pdf",
                "ca-riverside-tentatives-civil",
                "Riverside",
                "CA",
            )
        ]
        conn = _mock_cursor_with_rows(rows, [])
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub")
        riv = summary["counties"]["Riverside"]
        assert riv["outcome_continue"]["count"] == 1
        assert summary["all_clean"] is False
        ex = riv["outcome_continue"]["examples"][0]
        assert ex["ruling_id"] == "ruling-2"
        assert ex["outcome"] == "granted"

    def test_motion_type_contradiction_signal(self) -> None:
        # motion_type='demurrer' but text says nothing about demurrers.
        rows = [
            (
                "ruling-3",
                "Smith v. Jones",
                "The motion to compel further responses is granted.",
                "granted",
                "demurrer",
                "ca/sb/raw/ghi789.pdf",
                "ca-sb-tentatives-civil",
                "San Bernardino",
                "CA",
            )
        ]
        conn = _mock_cursor_with_rows(rows, [])
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub")
        sb = summary["counties"]["San Bernardino"]
        assert sb["motion_type_contradiction"]["count"] == 1
        assert summary["all_clean"] is False

    def test_case_title_text_mismatch_signal(self) -> None:
        rows = [
            (
                "ruling-4",
                "Smith v. Jones",
                "Continue to next month.",
                "continued",
                None,
                "ca/sf/raw/aaa.pdf",
                "ca-sf-tentatives-civil",
                "San Francisco",
                "CA",
            )
        ]
        conn = _mock_cursor_with_rows(rows, [])
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub")
        sf = summary["counties"]["San Francisco"]
        assert sf["case_title_text_mismatch"]["count"] == 1

    def test_cluster_signal(self) -> None:
        # Cluster query result shape: (county, s3_key, scraper_id, case_title, count)
        cluster_rows = [
            (
                "Ventura",
                "ca/ventura/raw/cluster.pdf",
                "ca-ventura-tentatives",
                "Smith v. Jones",
                7,
            )
        ]
        conn = _mock_cursor_with_rows([], cluster_rows)
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub")
        ven = summary["counties"]["Ventura"]
        assert ven["all_same_case_title_cluster"]["count"] == 1
        ex = ven["all_same_case_title_cluster"]["examples"][0]
        assert ex["ruling_count"] == 7
        assert ex["s3_key"] == "ca/ventura/raw/cluster.pdf"

    def test_examples_capped(self) -> None:
        # 10 rulings all triggering outcome_continue, default limit_examples=5.
        rows = [
            (
                f"ruling-{i}",
                "Smith v. Jones",
                "Continue to next month.",
                "granted",
                None,
                f"ca/riverside/raw/{i}.pdf",
                "ca-riverside-tentatives-civil",
                "Riverside",
                "CA",
            )
            for i in range(10)
        ]
        conn = _mock_cursor_with_rows(rows, [])
        with self._patch_psycopg(conn):
            summary = run_audit("postgres://stub", limit_examples=5)
        riv = summary["counties"]["Riverside"]
        assert riv["outcome_continue"]["count"] == 10
        assert len(riv["outcome_continue"]["examples"]) == 5


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestRenderTextReport:
    def test_all_clean_summary(self) -> None:
        summary = {
            "filter": {"county": None, "since": None},
            "counties": {
                "Riverside": {
                    "total_rulings": 100,
                    "outcome_continue": {"count": 0, "examples": []},
                    "motion_type_contradiction": {"count": 0, "examples": []},
                    "case_title_text_mismatch": {"count": 0, "examples": []},
                    "all_same_case_title_cluster": {"count": 0, "examples": []},
                }
            },
            "totals": {
                "rulings_audited": 100,
                "outcome_continue": 0,
                "motion_type_contradiction": 0,
                "case_title_text_mismatch": 0,
                "all_same_case_title_cluster": 0,
            },
            "all_clean": True,
        }
        out = render_text_report(summary)
        assert "All clean" in out
        assert "Riverside" in out
        assert "100" in out

    def test_signal_summary(self) -> None:
        summary = {
            "filter": {"county": None, "since": "2026-01-01"},
            "counties": {
                "Ventura": {
                    "total_rulings": 50,
                    "outcome_continue": {
                        "count": 3,
                        "examples": [{"ruling_id": "x"}],
                    },
                    "motion_type_contradiction": {
                        "count": 0,
                        "examples": [],
                    },
                    "case_title_text_mismatch": {"count": 0, "examples": []},
                    "all_same_case_title_cluster": {
                        "count": 2,
                        "examples": [],
                    },
                }
            },
            "totals": {
                "rulings_audited": 50,
                "outcome_continue": 3,
                "motion_type_contradiction": 0,
                "case_title_text_mismatch": 0,
                "all_same_case_title_cluster": 2,
            },
            "all_clean": False,
        }
        out = render_text_report(summary)
        assert "Carry-forward signals detected" in out
        assert "Ventura" in out
        assert "Since: 2026-01-01" in out
