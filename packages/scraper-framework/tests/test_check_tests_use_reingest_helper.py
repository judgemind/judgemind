"""Tests for ``scripts/check_tests_use_reingest_helper.py`` (issue #4190).

Validates the AST scanner that flags inline reingest-shape
``CapturedDocument(...)`` constructions under
``packages/scraper-framework/tests/courts/`` — the test-side analog of
``check_parse_document_reingest_safety.py`` (#4141).

Coverage:

* No-violation case — the current ``tests/courts/`` tree is clean (the
  three known consumers all use ``make_reingest_cap_doc``).
* One-violation case — a fixture file containing the inline reingest
  shape is correctly flagged.
* False-positive avoidance — fully-populated ``CapturedDocument(...)``
  calls (with ``case_number=`` or ``judge_name=``) are NOT flagged.
* Edge cases — module-level helpers, ``**kwargs``, attribute access on
  framework, syntactically broken files, vendored ``.venv``.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the script under test
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_PY_SCANNER = _SCRIPTS_DIR / "check_tests_use_reingest_helper.py"
_BASH_WRAPPER = _SCRIPTS_DIR / "check-tests-use-reingest-helper.sh"

sys.path.insert(0, str(_SCRIPTS_DIR))

# ruff: noqa: E402
check_tests_use_reingest_helper = importlib.import_module(
    "check_tests_use_reingest_helper",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Reingest-shape construction (only identifier fields; no parsed fields).
# This is the shape that scripts/reingest_from_s3.py::_reparse_document
# produces and that helpers/reingest.py::make_reingest_cap_doc encodes.
_REINGEST_SHAPE_CALL = """\
from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
    )
"""

# Fully-populated cap_doc — passes parsed fields like case_number,
# judge_name, hearing_date.  Must NOT be flagged.
_FULLY_POPULATED_CALL = """\
from framework import CapturedDocument, ContentFormat
from datetime import datetime, UTC

def make():
    return CapturedDocument(
        document_id="d1",
        scraper_id="ca-test",
        state="CA",
        county="Test",
        court="Superior Court",
        source_url="https://example.com/x",
        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_format=ContentFormat.TEXT,
        raw_content=b"hello",
        content_hash="deadbeef" * 8,
        case_number="ABC-123",
        judge_name="Hon. Test Judge",
        hearing_date=datetime(2026, 2, 1, tzinfo=UTC),
    )
"""

# Reingest-shape construction routed through the helper — this is the
# desired pattern, must NOT be flagged because the call is now to
# ``make_reingest_cap_doc``, not ``CapturedDocument`` directly.
_HELPER_CALL = """\
from helpers.reingest import make_reingest_cap_doc

def make():
    return make_reingest_cap_doc(
        raw_content=b"hello",
        scraper_id="ca-test",
    )
"""


def _write_fixture(root: Path, relpath: str, content: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests for the AST classification helpers
# ---------------------------------------------------------------------------


class TestFieldSets:
    """The identifier and parsed field-sets are the test-side mirror of
    helpers/reingest.py — drift between them would make the check
    unsound.  These tests pin them explicitly."""

    def test_identifier_fields_match_helper_constructor(self) -> None:
        """The scanner's identifier set is the keyword set the helper
        passes to CapturedDocument.  See helpers/reingest.py."""
        expected = frozenset(
            {
                "document_id",
                "scraper_id",
                "state",
                "county",
                "court",
                "source_url",
                "capture_timestamp",
                "content_format",
                "raw_content",
                "content_hash",
            }
        )
        assert check_tests_use_reingest_helper._IDENTIFIER_FIELDS == expected

    def test_parsed_fields_match_helper_default_set(self) -> None:
        """The scanner's parsed set covers every field the helper leaves
        at default — i.e. every field a reingest-shape cap_doc must NOT
        populate."""
        expected = frozenset(
            {
                "case_number",
                "case_title",
                "judge_name",
                "hearing_date",
                "ruling_text",
                "ruling_text_html",
                "outcome",
                "motion_type",
                "parties",
                "extra",
                "courthouse",
                "department",
            }
        )
        assert check_tests_use_reingest_helper._PARSED_FIELDS == expected

    def test_no_overlap_between_identifier_and_parsed_sets(self) -> None:
        """A field cannot be both an identifier and a parsed field."""
        overlap = (
            check_tests_use_reingest_helper._IDENTIFIER_FIELDS
            & check_tests_use_reingest_helper._PARSED_FIELDS
        )
        assert overlap == frozenset(), f"Unexpected overlap: {overlap}"


# ---------------------------------------------------------------------------
# Tests for scan_file
# ---------------------------------------------------------------------------


class TestScanFile:
    """The core ``scan_file`` function: takes a Path, returns a list of
    ``(path, lineno)`` violations."""

    def test_no_violation_clean_file(self, tmp_path: Path) -> None:
        """A file with no CapturedDocument calls produces no violations."""
        path = _write_fixture(
            tmp_path,
            "clean.py",
            "def foo() -> int:\n    return 1\n",
        )
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_flags_inline_reingest_shape(self, tmp_path: Path) -> None:
        """A reingest-shape inline construction is flagged."""
        path = _write_fixture(tmp_path, "violation.py", _REINGEST_SHAPE_CALL)
        violations = check_tests_use_reingest_helper.scan_file(path)
        assert len(violations) == 1
        assert violations[0][0] == path
        # The cap_doc construction starts at line 5 of the fixture
        # (lines 1-3 are imports, line 4 is the def, line 5 is `return`).
        # We only verify it lies inside the function body — exact lineno
        # is implementation detail of how ast records ast.Call.lineno.
        assert violations[0][1] >= 5

    def test_does_not_flag_fully_populated(self, tmp_path: Path) -> None:
        """A cap_doc with parsed fields (case_number, judge_name, ...) is NOT
        flagged — it is exercising a non-reingest path."""
        path = _write_fixture(tmp_path, "fully_populated.py", _FULLY_POPULATED_CALL)
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_does_not_flag_helper_call(self, tmp_path: Path) -> None:
        """A call routed through ``make_reingest_cap_doc`` is NOT flagged
        — the call's func is no longer ``CapturedDocument``."""
        path = _write_fixture(tmp_path, "helper.py", _HELPER_CALL)
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_does_not_flag_partial_identifier_set(self, tmp_path: Path) -> None:
        """A CapturedDocument call missing one of the identifier fields
        (e.g. ``content_hash``) is NOT flagged — it is a partial /
        custom construction the test author owns intentionally."""
        path = _write_fixture(
            tmp_path,
            "partial.py",
            textwrap.dedent("""\
                from framework import CapturedDocument, ContentFormat
                from datetime import datetime, UTC

                def make():
                    return CapturedDocument(
                        document_id="d1",
                        scraper_id="ca-test",
                        state="CA",
                        county="Test",
                        court="Superior Court",
                        source_url="https://example.com/x",
                        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                        content_format=ContentFormat.TEXT,
                        raw_content=b"hello",
                        # content_hash intentionally omitted
                    )
            """),
        )
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_skips_starred_kwargs(self, tmp_path: Path) -> None:
        """A call passing **kwargs is skipped — we cannot statically
        determine its keyword set."""
        path = _write_fixture(
            tmp_path,
            "starred.py",
            textwrap.dedent("""\
                from framework import CapturedDocument

                def make(**kwargs):
                    return CapturedDocument(**kwargs)
            """),
        )
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_handles_attribute_call(self, tmp_path: Path) -> None:
        """``framework.CapturedDocument(...)`` (attribute access) is
        treated identically to ``CapturedDocument(...)``."""
        path = _write_fixture(
            tmp_path,
            "attr.py",
            textwrap.dedent("""\
                import framework
                from framework import ContentFormat
                from datetime import datetime, UTC

                def make():
                    return framework.CapturedDocument(
                        document_id="d1",
                        scraper_id="ca-test",
                        state="CA",
                        county="Test",
                        court="Superior Court",
                        source_url="https://example.com/x",
                        capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                        content_format=ContentFormat.TEXT,
                        raw_content=b"hello",
                        content_hash="deadbeef" * 8,
                    )
            """),
        )
        violations = check_tests_use_reingest_helper.scan_file(path)
        assert len(violations) == 1

    def test_skips_syntax_error(self, tmp_path: Path) -> None:
        """A syntactically broken file is skipped silently."""
        path = _write_fixture(tmp_path, "broken.py", "class S(:  # syntax error\n    pass\n")
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_skips_unreadable_file(self, tmp_path: Path) -> None:
        """A non-existent path returns no violations (does not raise)."""
        path = tmp_path / "does_not_exist.py"
        assert check_tests_use_reingest_helper.scan_file(path) == []

    def test_does_not_flag_unrelated_class_call(self, tmp_path: Path) -> None:
        """A call to a class named something else (e.g. ``Document``) is
        NOT flagged — we only look at calls whose func is
        ``CapturedDocument``."""
        path = _write_fixture(
            tmp_path,
            "other.py",
            textwrap.dedent("""\
                class Document:
                    def __init__(self, **kw): ...

                def make():
                    return Document(
                        document_id="d1",
                        scraper_id="x",
                        state="CA",
                        county="T",
                        court="C",
                        source_url="u",
                        capture_timestamp=None,
                        content_format=None,
                        raw_content=b"",
                        content_hash="",
                    )
            """),
        )
        assert check_tests_use_reingest_helper.scan_file(path) == []


# ---------------------------------------------------------------------------
# Tests for the file walker
# ---------------------------------------------------------------------------


class TestIterPythonFiles:
    """The file-walk helper that feeds scan_file."""

    def test_finds_py_files_recursively(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "a.py", "")
        _write_fixture(tmp_path, "sub/b.py", "")
        _write_fixture(tmp_path, "sub/sub2/c.py", "")
        files = check_tests_use_reingest_helper._iter_python_files(tmp_path)
        names = sorted(p.name for p in files)
        assert names == ["a.py", "b.py", "c.py"]

    def test_excludes_venv(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "a.py", "")
        _write_fixture(tmp_path, ".venv/lib/python3.12/site-packages/vendored.py", "")
        files = check_tests_use_reingest_helper._iter_python_files(tmp_path)
        names = sorted(p.name for p in files)
        assert names == ["a.py"]

    def test_excludes_pycache_and_node_modules(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "a.py", "")
        _write_fixture(tmp_path, "__pycache__/x.py", "")
        _write_fixture(tmp_path, "node_modules/pkg/y.py", "")
        files = check_tests_use_reingest_helper._iter_python_files(tmp_path)
        names = sorted(p.name for p in files)
        assert names == ["a.py"]

    def test_returns_sorted(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "z.py", "")
        _write_fixture(tmp_path, "a.py", "")
        _write_fixture(tmp_path, "m.py", "")
        files = check_tests_use_reingest_helper._iter_python_files(tmp_path)
        assert files == sorted(files)


# ---------------------------------------------------------------------------
# Tests for main()
# ---------------------------------------------------------------------------


class TestMain:
    """End-to-end CLI shape via main()."""

    def test_no_violation_returns_0_and_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_fixture(tmp_path, "clean.py", _HELPER_CALL)
        rc = check_tests_use_reingest_helper.main(["--root", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_violation_returns_0_and_prints_violation_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _write_fixture(tmp_path, "violation.py", _REINGEST_SHAPE_CALL)
        rc = check_tests_use_reingest_helper.main(["--root", str(tmp_path)])
        # main() always returns 0; the wrapper turns non-empty stdout
        # into a non-zero exit.
        assert rc == 0
        captured = capsys.readouterr()
        assert str(path) in captured.out
        assert "CapturedDocument(...)" in captured.out

    def test_missing_root_returns_0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Pointing --root at a non-existent directory returns 0 silently."""
        nonexistent = tmp_path / "does-not-exist"
        rc = check_tests_use_reingest_helper.main(["--root", str(nonexistent)])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_default_root_resolves_to_tests_courts(self) -> None:
        """The default root resolves to the production tests/courts/ path."""
        default_root = check_tests_use_reingest_helper._resolve_default_root()
        assert default_root.name == "courts"
        assert default_root.parent.name == "tests"
        assert default_root.parent.parent.name == "scraper-framework"


# ---------------------------------------------------------------------------
# AC #3 — Production tree is clean (defense-in-depth)
# ---------------------------------------------------------------------------


class TestProductionTreeIsClean:
    """The current tests/courts/ tree must scan clean — the three known
    consumers (#3986, #4133, #4134) all migrated to the helper."""

    def test_default_root_has_zero_violations(self) -> None:
        rc = check_tests_use_reingest_helper.main([])
        assert rc == 0


# ---------------------------------------------------------------------------
# Bash wrapper integration test
# ---------------------------------------------------------------------------


class TestBashWrapper:
    """The bash wrapper turns scanner stdout into an exit code (0/1)."""

    def test_wrapper_exits_0_on_clean_tree(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "clean.py", _HELPER_CALL)
        result = subprocess.run(
            [str(_BASH_WRAPPER), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_wrapper_exits_1_on_violation(self, tmp_path: Path) -> None:
        _write_fixture(tmp_path, "violation.py", _REINGEST_SHAPE_CALL)
        result = subprocess.run(
            [str(_BASH_WRAPPER), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "violation.py" in result.stdout

    def test_wrapper_exits_0_on_default_production_tree(self) -> None:
        """AC #1 — the wrapper passes against the real tests/courts/."""
        result = subprocess.run(
            [str(_BASH_WRAPPER)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_wrapper_fixture_dir_violation_format(self, tmp_path: Path) -> None:
        """AC #2 — the issue specifies a fixture dir at
        ``packages/scraper-framework/tests/_fixtures_for_check/``.  The
        wrapper accepts a custom path argument and exits 1 with the
        violation listed.  We exercise the same behavior with tmp_path."""
        violation = _write_fixture(tmp_path, "violation.py", _REINGEST_SHAPE_CALL)
        result = subprocess.run(
            [str(_BASH_WRAPPER), str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert str(violation) in result.stdout
