"""Tests for ``scripts/spotcheck/inspect.py``.

Covers the filter helpers (pure, testable without boto3), the S3 URI parser,
and the table renderer. Mirrors the test layout used by
``scripts/tests/test_run_spotcheck.py`` — adds the spotcheck dir to
``sys.path`` so ``import inspect`` resolves to the spotcheck module rather
than the stdlib ``inspect``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

# Resolve to scripts/spotcheck/inspect.py, not the stdlib ``inspect``.
_SPOTCHECK_DIR = Path(__file__).resolve().parent.parent / "spotcheck"
sys.path.insert(0, str(_SPOTCHECK_DIR))

# The stdlib ``inspect`` module may already be cached. Pop it so the
# directory we just inserted wins, then re-pop on teardown so other tests
# (and the test runner itself) get the stdlib back.
_HAD_STDLIB_INSPECT = sys.modules.pop("inspect", None)
import inspect as spotcheck_inspect  # noqa: E402

# Make sure subsequent ``import inspect`` calls in the test process get the
# stdlib back — anything that runs after this module is loaded relies on the
# real ``inspect`` for traceback rendering, dataclass introspection, etc.
sys.modules["inspect"] = _HAD_STDLIB_INSPECT or __import__("importlib").import_module(
    "inspect"
)
# But keep our handle to the spotcheck module accessible to the tests below.
sys.modules["spotcheck_inspect"] = spotcheck_inspect


# ---------------------------------------------------------------------------
# parse_s3_uri / is_s3_uri
# ---------------------------------------------------------------------------


class TestParseS3Uri:
    def test_simple_uri(self) -> None:
        bucket, key = spotcheck_inspect.parse_s3_uri(
            "s3://judgemind-document-archive-dev/spotcheck/20260506T161159Z.json"
        )
        assert bucket == "judgemind-document-archive-dev"
        assert key == "spotcheck/20260506T161159Z.json"

    def test_nested_key(self) -> None:
        bucket, key = spotcheck_inspect.parse_s3_uri("s3://b/a/b/c.json")
        assert bucket == "b"
        assert key == "a/b/c.json"

    def test_missing_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Not an S3 URI"):
            spotcheck_inspect.parse_s3_uri("/local/path.json")

    def test_missing_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="missing key"):
            spotcheck_inspect.parse_s3_uri("s3://bucket-only")

    def test_empty_bucket_rejected(self) -> None:
        with pytest.raises(ValueError):
            spotcheck_inspect.parse_s3_uri("s3:///key/only")

    def test_is_s3_uri_truthy(self) -> None:
        assert spotcheck_inspect.is_s3_uri("s3://b/k") is True
        assert spotcheck_inspect.is_s3_uri("/local/path") is False
        assert spotcheck_inspect.is_s3_uri("https://example.com") is False


# ---------------------------------------------------------------------------
# Fixtures — sample run_spotcheck.py result shape
# ---------------------------------------------------------------------------


@pytest.fixture
def result_fixture() -> dict:
    """Tiny result resembling ``run_spotcheck.py``'s JSON output."""
    return {
        "timestamp": "2026-05-06T16:11:59Z",
        "n_per_county": 5,
        "counties_requested": ["Orange"],
        "rulings_by_county": {
            "Orange": [
                {
                    "ruling_id": "r1",
                    "department": "C20",
                    "case_number": "30-2025-12345",
                    "case_title": "Smith v Jones",
                    "judge_name": "Judge Smith",
                    "ruling_text_length": 500,
                    "s3_key": "ca/orange/oc/raw/aaa.pdf",
                },
                {
                    "ruling_id": "r2",
                    "department": "N15",
                    "case_number": "UNKNOWN-592b",
                    "case_title": "Nguyen vs Beach Grove Dentistry",
                    "judge_name": None,
                    "ruling_text_length": 300,
                    "s3_key": "ca/orange/oc/raw/bbb.pdf",
                },
                {
                    "ruling_id": "r3",
                    "department": None,
                    "case_number": "UNKNOWN-0ade",
                    "case_title": "Gomez vs. BGN Acquisitions La Habra",
                    "judge_name": "Judge Gomez",
                    "ruling_text_length": 0,
                    "s3_key": "ca/orange/oc/raw/ccc.pdf",
                },
                {
                    "ruling_id": "r4",
                    "department": "C10",
                    "case_number": "UNKNOWN-35ff",
                    "case_title": "Grossman v. State Farm",
                    "judge_name": "Judge Grossman",
                    "ruling_text_length": 800,
                    "s3_key": "ca/orange/oc/raw/ddd.pdf",
                },
            ],
        },
        "originals_by_county": {
            "Orange": [
                {
                    "s3_key": "ca/orange/oc/raw/eee.pdf",
                    "derived_count": 1,
                    "derived_rulings": [
                        {
                            "ruling_id": "r5",
                            "department": "C23",
                            "case_number": "UNKNOWN-zzzz",
                            "case_title": "Hidden v Default",
                            "judge_name": None,
                            "ruling_text_length": 10,
                        },
                    ],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# iter_rulings — walks both bidirectional sides
# ---------------------------------------------------------------------------


class TestIterRulings:
    def test_walks_rulings_and_originals_derived(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        # 4 rulings_by_county + 1 originals.derived = 5 total
        assert len(rows) == 5
        sources = {r["__source"] for r in rows}
        assert sources == {"rulings", "originals.derived"}

    def test_county_filter_case_insensitive(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture, counties=["orange"])
        assert len(rows) == 5
        rows = spotcheck_inspect.iter_rulings(result_fixture, counties=["Los Angeles"])
        assert rows == []

    def test_originals_derived_inherits_parent_s3_key(
        self, result_fixture: dict
    ) -> None:
        """Originals' derived_rulings don't carry s3_key directly — the parent
        original's s3_key should surface on each derived row so operators can
        locate the source PDF."""
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        derived = [r for r in rows if r["__source"] == "originals.derived"]
        assert len(derived) == 1
        assert derived[0]["s3_key"] == "ca/orange/oc/raw/eee.pdf"

    def test_handles_missing_buckets(self) -> None:
        """Empty / missing rulings_by_county or originals_by_county is fine."""
        assert spotcheck_inspect.iter_rulings({}) == []
        assert spotcheck_inspect.iter_rulings({"rulings_by_county": {}}) == []


# ---------------------------------------------------------------------------
# filter_rows — composable filters
# ---------------------------------------------------------------------------


class TestFilterRows:
    def test_unknown_only(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(rows, unknown_only=True)
        # r2, r3, r4, r5 are UNKNOWN; r1 is not
        assert {r["ruling_id"] for r in out} == {"r2", "r3", "r4", "r5"}

    def test_null_judge_only(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(rows, null_judge_only=True)
        assert {r["ruling_id"] for r in out} == {"r2", "r5"}

    def test_null_department_only(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(rows, null_department_only=True)
        assert {r["ruling_id"] for r in out} == {"r3"}

    def test_empty_text_only(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(rows, empty_text_only=True)
        assert {r["ruling_id"] for r in out} == {"r3"}

    def test_department_in(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(rows, departments=["C20", "N15"])
        assert {r["ruling_id"] for r in out} == {"r1", "r2"}

    def test_filters_compose_unknown_and_department_in(
        self, result_fixture: dict
    ) -> None:
        """The motivating #3629 case — UNKNOWN rows in the regressed dept set."""
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(
            rows,
            unknown_only=True,
            departments=["C20", "C23", "C25", "C27", "C31", "C32", "CM2", "C34", "N15"],
        )
        # Only r2 (N15, UNKNOWN) and r5 (C23, UNKNOWN) match.
        assert {r["ruling_id"] for r in out} == {"r2", "r5"}

    def test_filters_compose_unknown_and_null_judge(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        out = spotcheck_inspect.filter_rows(
            rows, unknown_only=True, null_judge_only=True
        )
        # r2 (UNKNOWN, null judge) and r5 (UNKNOWN, null judge).
        assert {r["ruling_id"] for r in out} == {"r2", "r5"}

    def test_no_filters_returns_all(self, result_fixture: dict) -> None:
        rows = spotcheck_inspect.iter_rulings(result_fixture)
        assert spotcheck_inspect.filter_rows(rows) == rows

    def test_bad_short_title_only(self) -> None:
        rows = [
            {"ruling_id": "good", "case_title": "Smith v. Jones"},
            {"ruling_id": "legit_matter", "case_title": "In the Matter of Smith"},
            {"ruling_id": "role", "case_title": "Plaintiff v. Defendant"},
            {"ruling_id": "bare", "case_title": "Jane Doe"},
        ]
        out = spotcheck_inspect.filter_rows(rows, bad_short_title_only=True)
        assert {r["ruling_id"] for r in out} == {"role", "bare"}


# ---------------------------------------------------------------------------
# Predicate helpers
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_unknown_case_number_handles_non_string(self) -> None:
        assert spotcheck_inspect.is_unknown_case_number({"case_number": None}) is False
        assert spotcheck_inspect.is_unknown_case_number({"case_number": 12345}) is False
        assert (
            spotcheck_inspect.is_unknown_case_number({"case_number": "UNKNOWN-abc"})
            is True
        )
        assert (
            spotcheck_inspect.is_unknown_case_number({"case_number": "30-2025-1"})
            is False
        )

    def test_null_judge_treats_empty_string_as_null(self) -> None:
        assert spotcheck_inspect.is_null_judge({"judge_name": None}) is True
        assert spotcheck_inspect.is_null_judge({"judge_name": ""}) is True
        assert spotcheck_inspect.is_null_judge({"judge_name": "   "}) is True
        assert spotcheck_inspect.is_null_judge({"judge_name": "Judge Smith"}) is False

    def test_in_departments_no_filter(self) -> None:
        # Empty filter list = no filter active.
        assert spotcheck_inspect.in_departments({"department": "C20"}, []) is True

    def test_in_departments_null_dept_with_explicit_null(self) -> None:
        assert (
            spotcheck_inspect.in_departments({"department": None}, ["C20", "null"])
            is True
        )
        assert spotcheck_inspect.in_departments({"department": None}, ["C20"]) is False


# ---------------------------------------------------------------------------
# is_bad_short_title — refined Federal case_title heuristic (#4620)
# ---------------------------------------------------------------------------


class TestBadShortTitle:
    @pytest.mark.parametrize(
        "title",
        [
            "In the Matter of Smith",
            "In Re Taylor Dant",
            "In Re: Michael White",
            "Doe V. Roe",
            "Doe V Roe",
            "Smith v. Jones",
            "Smith vs. Jones",
            "Peo in Interest of A.B.",
            "Marriage of Lee",
            "In Interest of J.D.",
            "Detention Of Gomez",
            "Parental Resp of Ng",
            "People V. Garcia",
            "Estate of Smith",
        ],
    )
    def test_legit_caption_not_flagged(self, title: str) -> None:
        assert spotcheck_inspect.is_bad_short_title({"case_title": title}) is False

    def test_long_title_not_flagged_even_without_caption(self) -> None:
        # >= 40 chars: length gate short-circuits, never a "short" title.
        long_title = "Abcdefghij Klmnopqrst Uvwxyz Anonymous Name"
        assert len(long_title) >= 40
        assert spotcheck_inspect.is_bad_short_title({"case_title": long_title}) is False

    def test_null_empty_whitespace_not_flagged(self) -> None:
        assert spotcheck_inspect.is_bad_short_title({"case_title": None}) is False
        assert spotcheck_inspect.is_bad_short_title({}) is False
        assert spotcheck_inspect.is_bad_short_title({"case_title": ""}) is False
        assert spotcheck_inspect.is_bad_short_title({"case_title": "   "}) is False

    @pytest.mark.parametrize(
        "title",
        [
            "Plaintiff v. Defendant",
            "Defendants v Plaintiffs",
            "Defendant",
            "Dhc",
            "Jane Doe",
            "Don Mccoin",
            "Abdalla",
        ],
    )
    def test_contamination_flagged(self, title: str) -> None:
        assert spotcheck_inspect.is_bad_short_title({"case_title": title}) is True


# ---------------------------------------------------------------------------
# render_table
# ---------------------------------------------------------------------------


class TestRenderTable:
    def test_no_rows(self) -> None:
        assert spotcheck_inspect.render_table([]) == "(no rows)"

    def test_single_row_has_header_and_count(self) -> None:
        rows = [
            {
                "__county": "Orange",
                "__source": "rulings",
                "department": "C20",
                "case_number": "UNKNOWN-x",
                "case_title": "A v B",
                "s3_key": "ca/orange/raw/x.pdf",
            }
        ]
        out = spotcheck_inspect.render_table(rows)
        assert "county" in out
        assert "case_number" in out
        assert "Orange" in out
        assert "UNKNOWN-x" in out
        assert "(1 row)" in out

    def test_truncates_long_fields(self) -> None:
        rows = [
            {
                "__county": "Orange",
                "__source": "rulings",
                "department": "C20",
                "case_number": "UNKNOWN-x",
                "case_title": "A" * 500,
                "s3_key": "k",
            }
        ]
        out = spotcheck_inspect.render_table(rows)
        # Truncation marker present somewhere in the table body.
        assert "…" in out

    def test_null_renders_as_null_string(self) -> None:
        rows = [
            {
                "__county": "Orange",
                "__source": "rulings",
                "department": None,
                "case_number": "UNKNOWN-y",
                "case_title": "X",
                "s3_key": "k",
            }
        ]
        out = spotcheck_inspect.render_table(rows)
        assert "null" in out


# ---------------------------------------------------------------------------
# load_result — local file path branch (S3 branch needs boto3 which we mock
# in the integration test below)
# ---------------------------------------------------------------------------


class TestLoadResult:
    def test_local_path_round_trip(self, tmp_path: Path, result_fixture: dict) -> None:
        path = tmp_path / "spotcheck.json"
        path.write_text(json.dumps(result_fixture), encoding="utf-8")
        loaded = spotcheck_inspect.load_result(str(path))
        assert loaded["n_per_county"] == 5

    def test_missing_local_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            spotcheck_inspect.load_result(str(tmp_path / "nope.json"))

    def test_s3_uri_uses_boto3(
        self, monkeypatch: pytest.MonkeyPatch, result_fixture: dict
    ) -> None:
        """load_result with an s3:// URI must call boto3.client('s3').get_object."""
        body = json.dumps(result_fixture).encode("utf-8")
        calls: dict = {}

        class FakeClient:
            def get_object(self, Bucket: str, Key: str) -> dict:
                calls["bucket"] = Bucket
                calls["key"] = Key
                return {"Body": io.BytesIO(body)}

        class FakeBoto3:
            @staticmethod
            def client(svc: str) -> FakeClient:
                calls["service"] = svc
                return FakeClient()

        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3)
        loaded = spotcheck_inspect.load_result(
            "s3://judgemind-document-archive-dev/spotcheck/X.json"
        )
        assert loaded["n_per_county"] == 5
        assert calls["service"] == "s3"
        assert calls["bucket"] == "judgemind-document-archive-dev"
        assert calls["key"] == "spotcheck/X.json"


# ---------------------------------------------------------------------------
# CLI integration — go through main() with a local fixture file
# ---------------------------------------------------------------------------


class TestMainCli:
    def test_help_lists_every_flag(self, capsys: pytest.CaptureFixture) -> None:
        """AC#1: ``inspect.py --help`` prints all flags."""
        with pytest.raises(SystemExit):
            spotcheck_inspect.main(["--help"])
        out = capsys.readouterr().out
        for flag in [
            "--county",
            "--unknown-only",
            "--null-judge-only",
            "--null-department-only",
            "--empty-text-only",
            "--bad-short-title-only",
            "--department-in",
            "--format",
        ]:
            assert flag in out, f"--help is missing {flag}"

    def test_default_format_is_table(
        self,
        tmp_path: Path,
        result_fixture: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(result_fixture), encoding="utf-8")
        rc = spotcheck_inspect.main([str(path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "county" in out
        assert "case_number" in out

    def test_unknown_only_dept_in_filters_compose(
        self,
        tmp_path: Path,
        result_fixture: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The #3629 verification scenario, end-to-end through main()."""
        path = tmp_path / "r.json"
        path.write_text(json.dumps(result_fixture), encoding="utf-8")
        rc = spotcheck_inspect.main(
            [
                str(path),
                "--unknown-only",
                "--department-in",
                "C20",
                "C23",
                "C25",
                "C27",
                "C31",
                "C32",
                "CM2",
                "C34",
                "N15",
                "--format",
                "json",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        rows = json.loads(out)
        assert {r["ruling_id"] for r in rows} == {"r2", "r5"}
        # Each row should also carry a ``county`` and ``source`` for traceability.
        assert all(r["county"] == "Orange" for r in rows)
        assert {r["source"] for r in rows} == {"rulings", "originals.derived"}

    def test_format_json_strips_double_underscore_keys(
        self,
        tmp_path: Path,
        result_fixture: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        path = tmp_path / "r.json"
        path.write_text(json.dumps(result_fixture), encoding="utf-8")
        rc = spotcheck_inspect.main([str(path), "--format", "json"])
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        for row in rows:
            assert not any(k.startswith("__") for k in row), (
                "JSON output must strip __source / __county augment keys"
            )

    def test_bad_short_title_only_json(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """``--bad-short-title-only --format json`` selects only contamination."""
        result = {
            "rulings_by_county": {
                "Federal": [
                    {"ruling_id": "good", "case_title": "Smith v. Jones"},
                    {"ruling_id": "legit", "case_title": "In Re Taylor Dant"},
                    {"ruling_id": "role", "case_title": "Plaintiff v. Defendant"},
                    {"ruling_id": "bare", "case_title": "Abdalla"},
                ],
            },
        }
        path = tmp_path / "r.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        rc = spotcheck_inspect.main(
            [str(path), "--bad-short-title-only", "--format", "json"]
        )
        assert rc == 0
        rows = json.loads(capsys.readouterr().out)
        assert {r["ruling_id"] for r in rows} == {"role", "bare"}

    def test_missing_file_returns_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        rc = spotcheck_inspect.main([str(tmp_path / "nope.json")])
        assert rc == 2
        err = capsys.readouterr().err
        assert "ERROR" in err

    def test_malformed_s3_uri_returns_exit_2(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        rc = spotcheck_inspect.main(["s3://bucket-only-no-key"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "ERROR" in err
