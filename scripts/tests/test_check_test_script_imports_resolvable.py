# venv: none
"""Unit tests for check_test_script_imports_resolvable.py (#4464).

Covers all four AST shapes (`import X`, `from X import Y`,
`importlib.import_module("X")`, `importlib.util.spec_from_file_location("X", ...)`),
the explicit-archive-intent suppression path (sys.path.insert pointing at
scripts/archive/ or scripts/oneoff/), and the production-tree
defense-in-depth pass.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load the script as a module via spec_from_file_location for parity with
# the sibling test (and to avoid PYTHONPATH gymnastics).
SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "check_test_script_imports_resolvable.py"
)
spec = importlib.util.spec_from_file_location(
    "check_test_script_imports_resolvable", SCRIPT_PATH
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules["check_test_script_imports_resolvable"] = mod
spec.loader.exec_module(mod)

build_resolution_map = mod.build_resolution_map
collect_candidates = mod.collect_candidates
detect_explicit_archive_intent = mod.detect_explicit_archive_intent
check = mod.check
Violation = mod.Violation


# ---------------------------------------------------------------------------
# build_resolution_map
# ---------------------------------------------------------------------------


class TestBuildResolutionMap:
    def test_live_archive_oneoff_categories(self, tmp_path: Path) -> None:
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "live_one.py").write_text("# live script\n")
        archive = scripts / "archive"
        archive.mkdir()
        (archive / "old_one.py").write_text("# archived\n")
        oneoff = scripts / "oneoff"
        oneoff.mkdir()
        (oneoff / "one_off_one.py").write_text("# one-off\n")

        result = build_resolution_map(scripts)
        assert result["live_one"] == ("live", "scripts/live_one.py")
        assert result["old_one"] == ("archive", "scripts/archive/old_one.py")
        assert result["one_off_one"] == ("oneoff", "scripts/oneoff/one_off_one.py")

    def test_live_wins_over_archive(self, tmp_path: Path) -> None:
        """If the same name lives in both subtrees (transient duplicate),
        the live entry wins so we don't spuriously flag working tests."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "X.py").write_text("# live\n")
        archive = scripts / "archive"
        archive.mkdir()
        (archive / "X.py").write_text("# also archived\n")

        result = build_resolution_map(scripts)
        assert result["X"] == ("live", "scripts/X.py")


# ---------------------------------------------------------------------------
# collect_candidates — AST detection of all four import shapes
# ---------------------------------------------------------------------------


class TestCollectCandidates:
    def test_bare_import(self, tmp_path: Path) -> None:
        src = tmp_path / "test_a.py"
        src.write_text("import dedup_rulings\n")
        result = collect_candidates(src)
        assert any(c.name == "dedup_rulings" and c.pattern == "import" for c in result)

    def test_from_import(self, tmp_path: Path) -> None:
        """The headline gap — #4464's primary motivating shape."""
        src = tmp_path / "test_b.py"
        src.write_text("from dedup_rulings import _CURSOR_MIN_UUID\n")
        result = collect_candidates(src)
        assert any(c.name == "dedup_rulings" and c.pattern == "from" for c in result)

    def test_importlib_import_module(self, tmp_path: Path) -> None:
        src = tmp_path / "test_c.py"
        src.write_text(
            'import importlib\nm = importlib.import_module("backfill_thing")\n'
        )
        result = collect_candidates(src)
        assert any(
            c.name == "backfill_thing" and c.pattern == "import_module" for c in result
        )

    def test_spec_from_file_location_literal_path(self, tmp_path: Path) -> None:
        """The second headline gap — #4464's spec_from_file_location shape."""
        src = tmp_path / "test_d.py"
        src.write_text(
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parents[3]\n"
            "spec = importlib.util.spec_from_file_location(\n"
            '    "dedup_split_rulings",\n'
            '    _REPO_ROOT / "scripts" / "dedup_split_rulings.py",\n'
            ")\n"
        )
        result = collect_candidates(src)
        names = {c.name for c in result if c.pattern == "spec_from_file_location"}
        # Both the first-arg name AND the path-leaf basename are detected.
        assert names == {"dedup_split_rulings"}

    def test_spec_from_file_location_with_intermediate_var(
        self, tmp_path: Path
    ) -> None:
        """When the path is bound to a variable, the leaf is not parseable
        from the call site — but the first-arg name is still captured."""
        src = tmp_path / "test_e.py"
        src.write_text(
            "import importlib.util\n"
            "from pathlib import Path\n"
            '_DEDUP_PATH = Path("/x") / "scripts" / "dedup_split_rulings.py"\n'
            "spec = importlib.util.spec_from_file_location(\n"
            '    "dedup_split_rulings", _DEDUP_PATH\n'
            ")\n"
        )
        result = collect_candidates(src)
        names = {c.name for c in result if c.pattern == "spec_from_file_location"}
        assert names == {"dedup_split_rulings"}

    def test_scripts_dot_prefix_in_from_import(self, tmp_path: Path) -> None:
        src = tmp_path / "test_f.py"
        src.write_text("from scripts.dedup_rulings import X\n")
        result = collect_candidates(src)
        assert any(c.name == "dedup_rulings" and c.pattern == "from" for c in result)

    def test_scripts_dot_prefix_in_import(self, tmp_path: Path) -> None:
        src = tmp_path / "test_g.py"
        src.write_text("import scripts.dedup_rulings\n")
        result = collect_candidates(src)
        assert any(c.name == "dedup_rulings" and c.pattern == "import" for c in result)

    def test_syntax_error_silent(self, tmp_path: Path) -> None:
        src = tmp_path / "test_h.py"
        src.write_text("class S(:\n  pass\n")
        result = collect_candidates(src)
        assert result == []

    def test_unrelated_imports_collected_as_candidates(self, tmp_path: Path) -> None:
        """``collect_candidates`` returns every name; the
        resolution-map filter in ``check()`` is what discards unrelated
        names like ``os`` / ``datetime`` / ``pytest``."""
        src = tmp_path / "test_i.py"
        src.write_text("import os\nfrom datetime import date\nimport pytest\n")
        result = collect_candidates(src)
        names = {c.name for c in result}
        # All three are captured at this stage.
        assert names == {"os", "datetime", "pytest"}

    def test_path_constant_leaf_requires_scripts_in_chain(self, tmp_path: Path) -> None:
        """Path / 'data' / 'x.py' is NOT a scripts-tree candidate."""
        src = tmp_path / "test_j.py"
        src.write_text(
            "import importlib.util\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location(\n"
            '    "x", Path("/x") / "data" / "x.py",\n'
            ")\n"
        )
        result = collect_candidates(src)
        # First-arg name is captured, but the path-leaf is not (no "scripts"
        # in chain), so we get exactly one Candidate.
        names = [c.name for c in result if c.pattern == "spec_from_file_location"]
        assert names == ["x"]


# ---------------------------------------------------------------------------
# detect_explicit_archive_intent
# ---------------------------------------------------------------------------


class TestDetectExplicitArchiveIntent:
    def test_string_literal_archive(self, tmp_path: Path) -> None:
        src = tmp_path / "test_a.py"
        src.write_text('import sys\nsys.path.insert(0, "/repo/scripts/archive")\n')
        assert detect_explicit_archive_intent(src) == {"archive"}

    def test_string_literal_oneoff_trailing_slash(self, tmp_path: Path) -> None:
        src = tmp_path / "test_b.py"
        src.write_text('import sys\nsys.path.insert(0, "/repo/scripts/oneoff/")\n')
        assert detect_explicit_archive_intent(src) == {"oneoff"}

    def test_pathlib_chain(self, tmp_path: Path) -> None:
        src = tmp_path / "test_c.py"
        src.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            'sys.path.insert(0, str(Path(__file__).parents[3] / "scripts" / "archive"))\n'
        )
        assert detect_explicit_archive_intent(src) == {"archive"}

    def test_ospath_join_alias_propagation(self, tmp_path: Path) -> None:
        """The production case: a top-level Assign binds a name to
        ``os.path.join(..., "scripts", "oneoff")`` and the call site
        passes the alias.  Must be detected as intent-bearing."""
        src = tmp_path / "test_d.py"
        src.write_text(
            "import os\n"
            "import sys\n"
            "_DIR = os.path.join(\n"
            '    os.path.dirname(__file__), "..", "..", "..", "scripts", "oneoff",\n'
            ")\n"
            "sys.path.insert(0, _DIR)\n"
        )
        assert detect_explicit_archive_intent(src) == {"oneoff"}

    def test_no_intent_when_path_is_opaque(self, tmp_path: Path) -> None:
        """An opaque variable / call returns empty set — fail safe."""
        src = tmp_path / "test_e.py"
        src.write_text(
            "import sys\n"
            "import some_thirdparty\n"
            "sys.path.insert(0, some_thirdparty.guess_dir())\n"
        )
        assert detect_explicit_archive_intent(src) == set()

    def test_plain_scripts_path_is_not_archive_intent(self, tmp_path: Path) -> None:
        """``sys.path.insert(0, ".../scripts/")`` is plain top-level — does
        NOT count as archive/oneoff intent."""
        src = tmp_path / "test_f.py"
        src.write_text('import sys\nsys.path.insert(0, "/repo/scripts")\n')
        assert detect_explicit_archive_intent(src) == set()


# ---------------------------------------------------------------------------
# End-to-end check()
# ---------------------------------------------------------------------------


def _fake_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a minimal fake repo and return (repo_root, scripts_dir, tests_dir)."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    archive = scripts / "archive"
    oneoff = scripts / "oneoff"
    pkg_tests = repo / "packages" / "scraper-framework" / "tests"

    scripts.mkdir(parents=True)
    archive.mkdir()
    oneoff.mkdir()
    pkg_tests.mkdir(parents=True)
    return repo, scripts, pkg_tests


class TestEndToEnd:
    def test_clean_repo_no_violations(self, tmp_path: Path) -> None:
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "live_one.py").write_text("# live\n")
        (tests / "test_live_one.py").write_text(
            'import importlib\nx = importlib.import_module("live_one")\n'
        )
        violations = check(tests, scripts)
        assert violations == []

    def test_from_import_archive_violation(self, tmp_path: Path) -> None:
        """Pattern 1 from #4464 — ``from dedup_rulings import X``."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "archive" / "dedup_rulings.py").write_text("# archived\n")
        (tests / "test_dedup_rulings.py").write_text(
            "import sys\n"
            'sys.path.insert(0, "scripts")\n'
            "from dedup_rulings import _CURSOR_MIN_UUID\n"
        )
        violations = check(tests, scripts)
        assert len(violations) == 1
        v = violations[0]
        assert v.module_name == "dedup_rulings"
        assert v.resolved_path == "scripts/archive/dedup_rulings.py"
        assert v.category == "archive"
        assert v.pattern == "from"

    def test_spec_from_file_location_violation(self, tmp_path: Path) -> None:
        """Pattern 2 from #4464 — ``spec_from_file_location`` literal path."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "archive" / "dedup_split_rulings.py").write_text("# archived\n")
        (tests / "test_dedup_split.py").write_text(
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parents[3]\n"
            "spec = importlib.util.spec_from_file_location(\n"
            '    "dedup_split_rulings",\n'
            '    _REPO_ROOT / "scripts" / "dedup_split_rulings.py",\n'
            ")\n"
        )
        violations = check(tests, scripts)
        # First-arg name + path-leaf basename are de-duped — one violation.
        assert len(violations) == 1
        v = violations[0]
        assert v.module_name == "dedup_split_rulings"
        assert v.resolved_path == "scripts/archive/dedup_split_rulings.py"
        assert v.pattern == "spec_from_file_location"

    def test_bare_import_archive_violation(self, tmp_path: Path) -> None:
        """The ``import X`` shape #4459 originally surfaced — already
        caught by the mapped guard's heuristic, but the resolvable
        guard catches it too for completeness."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "archive" / "backfill_thing.py").write_text("# archived\n")
        (tests / "test_backfill_thing.py").write_text(
            'import sys\nsys.path.insert(0, "scripts")\nimport backfill_thing\n'
        )
        violations = check(tests, scripts)
        assert len(violations) == 1
        assert violations[0].module_name == "backfill_thing"
        assert violations[0].pattern == "import"

    def test_oneoff_violation(self, tmp_path: Path) -> None:
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "oneoff" / "cleanup_thing.py").write_text("# one-off\n")
        (tests / "test_cleanup_thing.py").write_text(
            "import sys\n"
            'sys.path.insert(0, "scripts")\n'
            "import importlib\n"
            'cleanup = importlib.import_module("cleanup_thing")\n'
        )
        violations = check(tests, scripts)
        assert len(violations) == 1
        assert violations[0].category == "oneoff"
        assert violations[0].resolved_path == "scripts/oneoff/cleanup_thing.py"

    def test_explicit_archive_intent_suppresses_violation(self, tmp_path: Path) -> None:
        """A test that points sys.path at scripts/archive/ is intentional —
        no violation."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "archive" / "old_one.py").write_text("# archived\n")
        (tests / "test_old_one.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(\n"
            '    0, str(Path(__file__).parents[3] / "scripts" / "archive")\n'
            ")\n"
            "import old_one\n"
        )
        violations = check(tests, scripts)
        assert violations == []

    def test_explicit_oneoff_intent_via_ospath_join(self, tmp_path: Path) -> None:
        """The production-shape suppression case (mirrors
        test_cleanup_test_telemetry_pollution.py)."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "oneoff" / "cleanup_thing.py").write_text("# one-off\n")
        (tests / "test_cleanup_thing.py").write_text(
            "import os\n"
            "import sys\n"
            "import importlib\n"
            "_DIR = os.path.join(\n"
            '    os.path.dirname(__file__), "..", "..", "..", "scripts", "oneoff",\n'
            ")\n"
            "sys.path.insert(0, _DIR)\n"
            'cleanup = importlib.import_module("cleanup_thing")\n'
        )
        violations = check(tests, scripts)
        assert violations == []

    def test_archive_subtree_tests_exempt(self, tmp_path: Path) -> None:
        """Tests under tests/archive/ are exempted entirely — that's the
        canonical place to keep tests for archived scripts."""
        repo, scripts, tests = _fake_repo(tmp_path)
        archive_subtree = tests / "archive"
        archive_subtree.mkdir()
        (scripts / "archive" / "old_one.py").write_text("# archived\n")
        (archive_subtree / "test_old_one.py").write_text("from old_one import X\n")
        violations = check(tests, scripts)
        assert violations == []

    def test_third_party_imports_ignored(self, tmp_path: Path) -> None:
        """Stdlib / third-party imports are NEVER flagged."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (tests / "test_x.py").write_text(
            "import os\n"
            "from datetime import date\n"
            "import pytest\n"
            "from sqlalchemy import select\n"
        )
        violations = check(tests, scripts)
        assert violations == []

    def test_de_dup_per_test_per_name(self, tmp_path: Path) -> None:
        """Multiple imports of the same archived script in one test
        produce ONE violation, not several."""
        repo, scripts, tests = _fake_repo(tmp_path)
        (scripts / "archive" / "old_one.py").write_text("# archived\n")
        (tests / "test_old_one.py").write_text(
            "import sys\n"
            'sys.path.insert(0, "scripts")\n'
            "import old_one\n"
            "from old_one import x\n"
            "import importlib\n"
            'old_one_again = importlib.import_module("old_one")\n'
        )
        violations = check(tests, scripts)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Production-tree pass (defense-in-depth)
# ---------------------------------------------------------------------------


def test_production_tree_passes() -> None:
    """The real worktree must be green — i.e. no scraper-framework test
    today imports an archived/oneoff script without explicit intent.
    After this guard's PR lands, this test ensures the invariant doesn't
    drift."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    rc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "check_test_script_imports_resolvable.py"),
        ],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, (
        f"Production tree failed the resolvable-imports check.\n"
        f"stdout:\n{rc.stdout}\nstderr:\n{rc.stderr}"
    )


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestCLI:
    def test_help_exits_zero(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        rc = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "check_test_script_imports_resolvable.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 0
        assert "scripts/<name>.py" in rc.stdout
        assert "#4464" in rc.stdout

    def test_missing_tests_dir_exits_two(self, tmp_path: Path) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        rc = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "check_test_script_imports_resolvable.py"),
                "--repo-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert rc.returncode == 2
        assert "tests dir not found" in rc.stderr


# ---------------------------------------------------------------------------
# Smoke: --tests-dir / --scripts-dir override
# ---------------------------------------------------------------------------


def test_overridden_tests_and_scripts_dir(tmp_path: Path) -> None:
    repo, scripts, tests = _fake_repo(tmp_path)
    # Live script — no violation.
    (scripts / "live.py").write_text("# live\n")
    (tests / "test_live.py").write_text("import live\n")

    repo_root = Path(__file__).resolve().parent.parent.parent
    rc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "check_test_script_imports_resolvable.py"),
            "--tests-dir",
            str(tests),
            "--scripts-dir",
            str(scripts),
        ],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0


def test_violation_via_cli(tmp_path: Path) -> None:
    repo, scripts, tests = _fake_repo(tmp_path)
    (scripts / "archive" / "old.py").write_text("# archived\n")
    (tests / "test_old.py").write_text(
        'import sys\nsys.path.insert(0, "scripts")\nfrom old import X\n'
    )
    repo_root = Path(__file__).resolve().parent.parent.parent
    rc = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "check_test_script_imports_resolvable.py"),
            "--tests-dir",
            str(tests),
            "--scripts-dir",
            str(scripts),
        ],
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 1
    assert "tests/test_old.py:3" in rc.stderr
    assert "scripts/archive/old.py" in rc.stderr
    assert "Fix:" in rc.stderr
    assert "git rm" in rc.stderr
    assert "#4464" in rc.stderr


# Module-level constant export sanity (catches a typo in the module
# constants without requiring a separate test class).
def test_category_constants_exposed() -> None:
    assert mod.ARCHIVE_SUBDIR == "archive"
    assert mod.ONEOFF_SUBDIR == "oneoff"
    cats = {c[0] for c in mod.CATEGORIES}
    assert cats == {"live", "archive", "oneoff"}


@pytest.mark.parametrize(
    "verb", ["import", "from", "import_module", "spec_from_file_location"]
)
def test_each_pattern_produces_violation(verb: str, tmp_path: Path) -> None:
    """All four AST patterns trip the guard for an archived target.

    This is the most important regression test — it locks in the
    issue-#4464 acceptance criterion that all three NEW patterns
    (from-import, spec_from_file_location) plus the original bare
    `import`/`import_module` are caught.
    """
    repo, scripts, tests = _fake_repo(tmp_path)
    (scripts / "archive" / "frob.py").write_text("# archived\n")

    sources = {
        "import": ('import sys\nsys.path.insert(0, "scripts")\nimport frob\n'),
        "from": ('import sys\nsys.path.insert(0, "scripts")\nfrom frob import X\n'),
        "import_module": (
            "import sys\n"
            'sys.path.insert(0, "scripts")\n'
            "import importlib\n"
            'frob = importlib.import_module("frob")\n'
        ),
        "spec_from_file_location": (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "spec = importlib.util.spec_from_file_location(\n"
            '    "frob", Path("/x") / "scripts" / "frob.py",\n'
            ")\n"
        ),
    }
    (tests / f"test_frob_{verb}.py").write_text(sources[verb])
    violations = check(tests, scripts)
    assert len(violations) == 1
    assert violations[0].module_name == "frob"
    assert violations[0].pattern == verb
