"""Tests for scripts/audit_document_validation_rules.py.

The audit script re-runs document-level deterministic validation rules
(currently only ``no_cross_case_ruling_text``) against existing rows in
``derived.rulings`` so that contamination in pre-rule rulings can be
measured without a full reingest.  These tests exercise the pure-Python
helpers the CLI is built on — sibling grouping, per-group auditing, and
the DB writeback.

See issue #2567.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Load the script as a module — it lives in scripts/ which is not a package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "audit_document_validation_rules.py"
_spec = importlib.util.spec_from_file_location("audit_document_validation_rules", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_document_validation_rules"] = audit
_spec.loader.exec_module(audit)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ruling(
    ruling_id: str = "11111111-0000-0000-0000-000000000001",
    document_id: str = "22222222-0000-0000-0000-000000000001",
    court_id: str = "33333333-0000-0000-0000-000000000001",
    s3_key: str = "ca/santa_clara/superior/raw/abc.pdf",
    case_number: str = "24CV001",
    case_title: str | None = "Smith v. Jones",
    ruling_text: str | None = "The motion is GRANTED.",
    hearing_date: date | None = None,
    captured_at: date | None = None,
    county: str = "Santa Clara",
) -> dict:
    return {
        "ruling_id": ruling_id,
        "document_id": document_id,
        "court_id": court_id,
        "s3_key": s3_key,
        "case_number": case_number,
        "case_title": case_title,
        "ruling_text": ruling_text,
        "hearing_date": hearing_date,
        "captured_at": captured_at,
        "county": county,
    }


# ---------------------------------------------------------------------------
# group_rulings_by_source
# ---------------------------------------------------------------------------


class TestGroupRulingsBySource:
    def test_singleton_groups_excluded(self) -> None:
        """Rulings with no siblings cannot trigger cross-case rules."""
        rulings = [
            _make_ruling(
                ruling_id="11111111-0000-0000-0000-000000000001",
                document_id="22222222-0000-0000-0000-000000000001",
                s3_key="ca/santa_clara/superior/raw/abc.pdf",
            ),
        ]
        groups = audit.group_rulings_by_source(rulings)
        assert groups == []

    def test_multi_ruling_group_included(self) -> None:
        """Two rulings sharing a source document form one group."""
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            document_id="22222222-0000-0000-0000-000000000001",
            s3_key="ca/santa_clara/superior/raw/abc.pdf",
            case_number="24CV001",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            document_id="22222222-0000-0000-0000-000000000002",
            s3_key="ca/santa_clara/superior/raw/abc.pdf",
            case_number="24CV002",
        )
        groups = audit.group_rulings_by_source([r1, r2])
        assert len(groups) == 1
        members = groups[0]["rulings"]
        assert len(members) == 2
        case_numbers = sorted(r["case_number"] for r in members)
        assert case_numbers == ["24CV001", "24CV002"]

    def test_missing_court_id_or_s3_key_skipped(self) -> None:
        """Rows with missing court_id or s3_key are dropped before bucketing."""
        r1 = _make_ruling(court_id=None, s3_key="k.pdf")
        r2 = _make_ruling(court_id="c1", s3_key=None)
        groups = audit.group_rulings_by_source([r1, r2])
        assert groups == []

    def test_different_courts_not_merged(self) -> None:
        """Two rulings with the same s3_key but different court_id stay separate."""
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            court_id="33333333-0000-0000-0000-000000000001",
            s3_key="shared/key.pdf",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            court_id="33333333-0000-0000-0000-000000000002",
            s3_key="shared/key.pdf",
        )
        groups = audit.group_rulings_by_source([r1, r2])
        # Each is a singleton under its own (court_id, s3_key) — both excluded.
        assert groups == []


# ---------------------------------------------------------------------------
# audit_group
# ---------------------------------------------------------------------------


class TestAuditGroup:
    def test_flag_cross_case_reference(self) -> None:
        """A ruling whose text names a sibling's case number is flagged."""
        # Use LA-style format (\d{2}[A-Z]{2,6}\d{4,10}) which matches the
        # deterministic-rule scanner's case-number candidate pattern.
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            document_id="22222222-0000-0000-0000-000000000001",
            case_number="24STCV12345",
            ruling_text="The court rules on 24STCV99999. Motion GRANTED.",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            document_id="22222222-0000-0000-0000-000000000002",
            case_number="24STCV99999",
            ruling_text="Unrelated motion DENIED.",
        )
        flags = audit.audit_group(
            {"court_id": "ct", "s3_key": "k", "rulings": [r1, r2]},
            rule_filter=None,
        )
        rule_names = [f["rule"] for f in flags]
        assert "no_cross_case_ruling_text" in rule_names
        # Only r1 should be flagged for cross-case reference.
        flagged_ids = {f["ruling_id"] for f in flags if f["rule"] == "no_cross_case_ruling_text"}
        assert flagged_ids == {"11111111-0000-0000-0000-000000000001"}

    def test_no_flag_clean_text(self) -> None:
        """Rulings that don't reference siblings produce no cross-case flag."""
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            case_number="24STCV12345",
            ruling_text="Motion GRANTED.",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            case_number="24STCV99999",
            ruling_text="Motion DENIED.",
        )
        flags = audit.audit_group(
            {"court_id": "ct", "s3_key": "k", "rulings": [r1, r2]},
            rule_filter="no_cross_case_ruling_text",
        )
        assert flags == []

    def test_rule_filter_scopes_to_single_rule(self) -> None:
        """--rule no_cross_case_ruling_text only runs that rule."""
        # Concatenated title would also flag no_multiple_adversarial_patterns,
        # but that's a per-ruling rule and shouldn't be returned when the
        # filter is scoped to the document-level cross-case rule.
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            case_number="24STCV12345",
            case_title="Smith v. Jones vs. Acme Corp.",
            ruling_text="Motion GRANTED.",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            case_number="24STCV99999",
            ruling_text="Motion DENIED.",
        )
        flags = audit.audit_group(
            {"court_id": "ct", "s3_key": "k", "rulings": [r1, r2]},
            rule_filter="no_cross_case_ruling_text",
        )
        # No cross-case reference — no flags for the scoped rule.
        assert flags == []

    def test_skip_rulings_without_ruling_text(self) -> None:
        """Rulings with empty ruling_text are skipped — nothing to scan."""
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            case_number="24STCV12345",
            ruling_text=None,
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            case_number="24STCV99999",
            ruling_text="Motion DENIED.",
        )
        flags = audit.audit_group(
            {"court_id": "ct", "s3_key": "k", "rulings": [r1, r2]},
            rule_filter="no_cross_case_ruling_text",
        )
        # No flags because r1 has no text and r2's text doesn't reference r1.
        assert flags == []

    def test_skip_rulings_without_case_number(self) -> None:
        """Rulings with missing case numbers don't crash; they are skipped."""
        r1 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000001",
            case_number=None,
            ruling_text="Text mentions 24STCV99999.",
        )
        r2 = _make_ruling(
            ruling_id="11111111-0000-0000-0000-000000000002",
            case_number="24STCV99999",
            ruling_text="Motion DENIED.",
        )
        flags = audit.audit_group(
            {"court_id": "ct", "s3_key": "k", "rulings": [r1, r2]},
            rule_filter="no_cross_case_ruling_text",
        )
        # r1 is skipped (no own_case_number).  r2 has a case number but its
        # text doesn't reference r1's (None) case number.
        assert flags == []


# ---------------------------------------------------------------------------
# write_flags_to_db
# ---------------------------------------------------------------------------


class TestWriteFlagsToDb:
    def _mock_conn(self) -> tuple[MagicMock, MagicMock]:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cur

    def test_insert_one_row_per_flag(self) -> None:
        """Each flag produces one validation_results insert."""
        conn, cur = self._mock_conn()
        flags = [
            {
                "ruling_id": "11111111-0000-0000-0000-000000000001",
                "document_id": "22222222-0000-0000-0000-000000000001",
                "rule": "no_cross_case_ruling_text",
                "reason": "ruling_text references 1 other case number(s) ...",
                "result": "flag",
            },
        ]
        inserted = audit.write_flags_to_db(conn, flags)
        assert inserted == 1
        assert cur.execute.call_count == 1

    def test_reason_prefixed_with_rule_name(self) -> None:
        """DB reason column embeds the rule name since there's no rule col."""
        conn, cur = self._mock_conn()
        flags = [
            {
                "ruling_id": "11111111-0000-0000-0000-000000000001",
                "document_id": "22222222-0000-0000-0000-000000000001",
                "rule": "no_cross_case_ruling_text",
                "reason": "ruling_text references 1 other case",
                "result": "flag",
            },
        ]
        audit.write_flags_to_db(conn, flags)
        # The inserted reason must contain both the rule name and the reason.
        insert_args = cur.execute.call_args
        params = insert_args.args[1]
        reason_col = params[3]  # matches INSERT column order in gate.py
        assert "no_cross_case_ruling_text" in reason_col
        assert "ruling_text references" in reason_col

    def test_empty_flags_no_inserts(self) -> None:
        conn, cur = self._mock_conn()
        inserted = audit.write_flags_to_db(conn, [])
        assert inserted == 0
        assert cur.execute.call_count == 0


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------


class TestBuildReport:
    def test_per_county_counts(self) -> None:
        """Report groups flag counts by county."""
        flags = [
            {
                "ruling_id": "1",
                "document_id": "d1",
                "rule": "no_cross_case_ruling_text",
                "reason": "x",
                "result": "flag",
                "county": "Santa Clara",
            },
            {
                "ruling_id": "2",
                "document_id": "d2",
                "rule": "no_cross_case_ruling_text",
                "reason": "y",
                "result": "flag",
                "county": "Santa Clara",
            },
            {
                "ruling_id": "3",
                "document_id": "d3",
                "rule": "no_cross_case_ruling_text",
                "reason": "z",
                "result": "flag",
                "county": "San Diego",
            },
        ]
        report = audit.build_report(flags, groups_scanned=10, rulings_scanned=42)
        assert report["total_flags"] == 3
        assert report["groups_scanned"] == 10
        assert report["rulings_scanned"] == 42
        per_county = {c["county"]: c["flag_count"] for c in report["per_county"]}
        assert per_county == {"Santa Clara": 2, "San Diego": 1}

    def test_report_is_json_serializable(self) -> None:
        import json

        flags = [
            {
                "ruling_id": "1",
                "document_id": "d1",
                "rule": "no_cross_case_ruling_text",
                "reason": "x",
                "result": "flag",
                "county": "Santa Clara",
            },
        ]
        report = audit.build_report(flags, groups_scanned=1, rulings_scanned=2)
        # round-trips without raising
        _ = json.dumps(report)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_dry_run_and_write_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            audit.parse_args(["--dry-run", "--write"])

    def test_default_is_dry_run(self) -> None:
        args = audit.parse_args([])
        assert args.write is False
        assert args.dry_run is True

    def test_rule_flag(self) -> None:
        args = audit.parse_args(["--rule", "no_cross_case_ruling_text"])
        assert args.rule == "no_cross_case_ruling_text"

    def test_county_flag(self) -> None:
        args = audit.parse_args(["--county", "Santa Clara"])
        assert args.county == "Santa Clara"

    def test_json_flag(self) -> None:
        args = audit.parse_args(["--json"])
        assert args.json is True

    def test_limit_flag(self) -> None:
        args = audit.parse_args(["--limit", "100"])
        assert args.limit == 100

    def test_unknown_rule_rejected(self) -> None:
        with pytest.raises(SystemExit):
            audit.parse_args(["--rule", "not_a_real_rule"])


# ---------------------------------------------------------------------------
# _build_query
# ---------------------------------------------------------------------------


class TestBuildQuery:
    def test_no_filters(self) -> None:
        """With no county or limit, the clauses are empty strings."""
        query, params = audit._build_query(county=None, limit=None)
        assert params == []
        # Both clause placeholders are blank.
        assert "{county_clause}" not in query
        assert "{limit_clause}" not in query
        assert "AND ct.county" not in query
        assert "LIMIT" not in query

    def test_county_filter(self) -> None:
        """--county adds a parameterized county clause."""
        query, params = audit._build_query(county="Santa Clara", limit=None)
        assert params == ["Santa Clara"]
        assert "AND ct.county = %s" in query

    def test_limit_only(self) -> None:
        """--limit adds a parameterized LIMIT clause."""
        query, params = audit._build_query(county=None, limit=5)
        assert params == [5]
        assert "LIMIT %s" in query

    def test_county_and_limit(self) -> None:
        """Both flags compose correctly; params are in SQL order."""
        query, params = audit._build_query(county="San Diego", limit=100)
        assert params == ["San Diego", 100]
        assert "AND ct.county = %s" in query
        assert "LIMIT %s" in query


# ---------------------------------------------------------------------------
# _resolve_rules
# ---------------------------------------------------------------------------


class TestResolveRules:
    def test_none_returns_all(self) -> None:
        """None returns the full rule registry."""
        rules = audit._resolve_rules(None)
        assert "no_cross_case_ruling_text" in rules

    def test_named_rule_returns_single(self) -> None:
        """A specific rule returns only that rule."""
        rules = audit._resolve_rules("no_cross_case_ruling_text")
        assert list(rules) == ["no_cross_case_ruling_text"]

    def test_unknown_rule_raises(self) -> None:
        """Unknown rule name raises ValueError with a helpful message."""
        with pytest.raises(ValueError, match="Unknown rule"):
            audit._resolve_rules("not_a_rule")


# ---------------------------------------------------------------------------
# _print_human_report
# ---------------------------------------------------------------------------


class TestPrintHumanReport:
    def test_print_with_flags(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Human report prints per-county and per-rule sections with flags."""
        report = {
            "rulings_scanned": 42,
            "groups_scanned": 5,
            "total_flags": 3,
            "per_county": [
                {"county": "Santa Clara", "flag_count": 2},
                {"county": "San Diego", "flag_count": 1},
            ],
            "per_rule": [
                {"rule": "no_cross_case_ruling_text", "flag_count": 3},
            ],
        }
        audit._print_human_report(report)
        captured = capsys.readouterr()
        assert "Rulings scanned: 42" in captured.out
        assert "Sibling groups:  5" in captured.out
        assert "Total flags:     3" in captured.out
        assert "Santa Clara" in captured.out
        assert "San Diego" in captured.out
        assert "no_cross_case_ruling_text" in captured.out

    def test_print_empty_report(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Human report prints (none) when there are no flags."""
        report = {
            "rulings_scanned": 10,
            "groups_scanned": 0,
            "total_flags": 0,
            "per_county": [],
            "per_rule": [],
        }
        audit._print_human_report(report)
        captured = capsys.readouterr()
        assert "(none)" in captured.out


# ---------------------------------------------------------------------------
# run_audit
# ---------------------------------------------------------------------------


class TestRunAudit:
    def _fake_connect(
        self, rows: list[tuple[Any, ...]], cols: list[str]
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Build a patched psycopg.connect that returns the given rows/cols."""
        mock_cur = MagicMock()
        mock_cur.description = [(c,) for c in cols]
        mock_cur.execute = MagicMock()
        mock_cur.__iter__ = lambda self: iter(rows)

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_ctx.__exit__ = MagicMock(return_value=False)

        return mock_ctx, mock_conn, mock_cur

    def test_dry_run_returns_report_without_writing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dry-run detects flags but does not call write_flags_to_db."""
        cols = [
            "ruling_id",
            "document_id",
            "court_id",
            "s3_key",
            "case_number",
            "case_title",
            "ruling_text",
            "hearing_date",
            "captured_at",
            "county",
        ]
        # Two siblings from the same (court_id, s3_key) where r1 references r2.
        rows = [
            (
                "11111111-0000-0000-0000-000000000001",
                "22222222-0000-0000-0000-000000000001",
                "33333333-0000-0000-0000-000000000001",
                "ca/santa_clara/superior/raw/abc.pdf",
                "24STCV12345",
                "Smith v Jones",
                "Court rules 24STCV99999 GRANTED.",
                None,
                None,
                "Santa Clara",
            ),
            (
                "11111111-0000-0000-0000-000000000002",
                "22222222-0000-0000-0000-000000000002",
                "33333333-0000-0000-0000-000000000001",
                "ca/santa_clara/superior/raw/abc.pdf",
                "24STCV99999",
                "Other case",
                "Motion DENIED.",
                None,
                None,
                "Santa Clara",
            ),
        ]
        ctx, conn, _cur = self._fake_connect(rows, cols)
        monkeypatch.setattr(audit.psycopg, "connect", lambda dsn: ctx)

        called_write = {"n": 0}

        def fake_write(_conn: Any, flags: list[dict]) -> int:
            called_write["n"] += len(flags)
            return len(flags)

        monkeypatch.setattr(audit, "write_flags_to_db", fake_write)

        report = audit.run_audit(
            "postgresql://fake",
            county=None,
            rule_filter=None,
            write=False,
            limit=None,
        )
        # Cross-case flag detected but NOT written.
        assert report["rulings_scanned"] == 2
        assert report["groups_scanned"] == 1
        assert report["total_flags"] >= 1
        assert called_write["n"] == 0
        conn.commit.assert_not_called()

    def test_write_mode_commits_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--write triggers write_flags_to_db and conn.commit when flags exist."""
        cols = [
            "ruling_id",
            "document_id",
            "court_id",
            "s3_key",
            "case_number",
            "case_title",
            "ruling_text",
            "hearing_date",
            "captured_at",
            "county",
        ]
        rows = [
            (
                "11111111-0000-0000-0000-000000000001",
                "22222222-0000-0000-0000-000000000001",
                "33333333-0000-0000-0000-000000000001",
                "ca/sc/raw.pdf",
                "24STCV12345",
                "A",
                "Rules 24STCV99999 GRANTED.",
                None,
                None,
                "Santa Clara",
            ),
            (
                "11111111-0000-0000-0000-000000000002",
                "22222222-0000-0000-0000-000000000002",
                "33333333-0000-0000-0000-000000000001",
                "ca/sc/raw.pdf",
                "24STCV99999",
                "B",
                "DENIED.",
                None,
                None,
                "Santa Clara",
            ),
        ]
        ctx, conn, _cur = self._fake_connect(rows, cols)
        monkeypatch.setattr(audit.psycopg, "connect", lambda dsn: ctx)

        calls = {"write": 0}

        def fake_write(_conn: Any, flags: list[dict]) -> int:
            calls["write"] += len(flags)
            return len(flags)

        monkeypatch.setattr(audit, "write_flags_to_db", fake_write)

        report = audit.run_audit(
            "postgresql://fake",
            county="Santa Clara",
            rule_filter="no_cross_case_ruling_text",
            write=True,
            limit=10,
        )
        assert report["total_flags"] >= 1
        assert calls["write"] == report["total_flags"]
        conn.commit.assert_called_once()

    def test_write_mode_no_flags_skips_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When there are zero flags, no commit is issued."""
        cols = [
            "ruling_id",
            "document_id",
            "court_id",
            "s3_key",
            "case_number",
            "case_title",
            "ruling_text",
            "hearing_date",
            "captured_at",
            "county",
        ]
        rows: list[tuple[Any, ...]] = []  # empty
        ctx, conn, _cur = self._fake_connect(rows, cols)
        monkeypatch.setattr(audit.psycopg, "connect", lambda dsn: ctx)
        report = audit.run_audit(
            "postgresql://fake",
            county=None,
            rule_filter=None,
            write=True,
            limit=None,
        )
        assert report["total_flags"] == 0
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain:
    def test_missing_dsn_returns_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Absent DATABASE_URL exits with code 1."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = audit.main([])
        assert rc == 1

    def test_json_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """--json emits JSON-parseable stdout."""
        import json

        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        def fake_run_audit(*args: Any, **kwargs: Any) -> dict:
            return {
                "generated_at": "2026-04-17T00:00:00+00:00",
                "rulings_scanned": 0,
                "groups_scanned": 0,
                "total_flags": 0,
                "per_county": [],
                "per_rule": [],
                "flags": [],
            }

        monkeypatch.setattr(audit, "run_audit", fake_run_audit)
        rc = audit.main(["--json"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["total_flags"] == 0
        assert rc == 0

    def test_human_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Default output is human-readable."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        def fake_run_audit(*args: Any, **kwargs: Any) -> dict:
            return {
                "generated_at": "2026-04-17T00:00:00+00:00",
                "rulings_scanned": 5,
                "groups_scanned": 2,
                "total_flags": 0,
                "per_county": [],
                "per_rule": [],
                "flags": [],
            }

        monkeypatch.setattr(audit, "run_audit", fake_run_audit)
        rc = audit.main([])
        captured = capsys.readouterr()
        assert "Rulings scanned: 5" in captured.out
        assert rc == 0

    def test_nonzero_exit_when_flags_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit code 1 when there's at least one flag."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake")

        def fake_run_audit(*args: Any, **kwargs: Any) -> dict:
            return {
                "generated_at": "2026-04-17T00:00:00+00:00",
                "rulings_scanned": 1,
                "groups_scanned": 1,
                "total_flags": 1,
                "per_county": [{"county": "Santa Clara", "flag_count": 1}],
                "per_rule": [{"rule": "no_cross_case_ruling_text", "flag_count": 1}],
                "flags": [
                    {
                        "ruling_id": "1",
                        "document_id": "d1",
                        "rule": "no_cross_case_ruling_text",
                        "reason": "x",
                        "result": "flag",
                        "county": "Santa Clara",
                    }
                ],
            }

        monkeypatch.setattr(audit, "run_audit", fake_run_audit)
        rc = audit.main([])
        assert rc == 1
