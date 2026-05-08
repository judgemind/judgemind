"""Tests for scripts/check-no-tmp-oneshot-file-path-derivation.py.

Covers the bug class: any ``scripts/*.py`` file that derives a
filesystem path from ``Path(__file__).resolve().parent.parent`` (or
longer .parent chains) and then passes it to a data-load API
(``open()``, ``read_text()``, ``read_bytes()``,
``importlib.util.spec_from_file_location``) without an at-import
existence assertion. Tracking: issue #4381.

The acceptance criterion (#4381 AC #1) requires:
  - The check exits 0 on the current repo (post-#4374 fix).
  - The check would have failed against the pre-#4374 commit.

The first is asserted by ``TestRealRepoTree::test_repo_scans_clean``;
the second is asserted by
``TestPre4374Regression::test_pre_4374_collapsed_pattern_fails``,
which reconstructs the offending pattern verbatim from the pre-fix
``drain_splitter_carry_forward_clusters.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module loader (the check script has dashes in the filename, so we have
# to load it via importlib rather than ``import``).
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check-no-tmp-oneshot-file-path-derivation.py"
_spec = importlib.util.spec_from_file_location(
    "check_no_tmp_oneshot_file_path_derivation", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
check_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_mod)


# ---------------------------------------------------------------------------
# Fixture helper: write a Python source string to a temp file, then run
# scan_file on it and return the violation list.
# ---------------------------------------------------------------------------


def _scan(tmp_path: Path, source: str, name: str = "fixture.py") -> list[str]:
    path = tmp_path / name
    path.write_text(source)
    return check_mod.scan_file(path)


# ---------------------------------------------------------------------------
# Rule 1: the bug shape — Path(__file__).resolve().parent.parent + unsafe
# data-load API + no existence guard.
# ---------------------------------------------------------------------------


class TestPre4374Regression:
    """Verify the check would have failed against the pre-#4374 commit."""

    def test_pre_4374_collapsed_pattern_fails(self, tmp_path: Path) -> None:
        """The pre-#4374 shape: derive _SCRAPER_SRC from
        Path(__file__).resolve().parent.parent and pass it directly to
        importlib.util.spec_from_file_location with no fallback."""
        source = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_SCRAPER_SRC = _REPO_ROOT / 'packages' / 'scraper-framework' / 'src'\n"
            "def load_split_ids():\n"
            "    target = _SCRAPER_SRC / 'ingestion' / 'split_ids.py'\n"
            "    spec = importlib.util.spec_from_file_location('x', str(target))\n"
            "    return spec\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "spec_from_file_location" in violations[0]

    def test_post_4374_candidate_path_fallback_passes(self, tmp_path: Path) -> None:
        """The post-#4374 fix: build a candidate-path fallback list and
        gate each candidate with .is_file() before calling
        spec_from_file_location on the first that resolves."""
        source = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_SCRAPER_SRC = _REPO_ROOT / 'packages' / 'scraper-framework' / 'src'\n"
            "def load_split_ids():\n"
            "    candidates = [\n"
            "        _SCRAPER_SRC / 'ingestion' / 'split_ids.py',\n"
            "        Path('/app/src/ingestion/split_ids.py'),\n"
            "    ]\n"
            "    for candidate in candidates:\n"
            "        if candidate.is_file():\n"
            "            return importlib.util.spec_from_file_location(\n"
            "                'x', str(candidate)\n"
            "            )\n"
            "    raise RuntimeError('not found')\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 2: parent.parent must be at least 2 levels deep — the empty-string
# collapse only fires when chaining two or more .parent accesses.
# ---------------------------------------------------------------------------


class TestParentDepth:
    def test_single_parent_passes(self, tmp_path: Path) -> None:
        """Single .parent (= scripts/) does NOT collapse. /tmp/_oneshot_script
        -> /tmp is still a valid filesystem path; this is not the bug class."""
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent / 'foo.txt'\n"
            "p.read_text()\n"
        )
        assert _scan(tmp_path, source) == []

    def test_two_parents_with_unsafe_load_fails(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            "p.read_text()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "read_text" in violations[0]

    def test_three_parents_with_unsafe_load_fails(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent.parent / 'foo.txt'\n"
            "p.read_text()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Rule 3: unsafe API set — open, read_text, read_bytes, spec_from_file_location.
# ---------------------------------------------------------------------------


class TestUnsafeAPIs:
    @pytest.mark.parametrize("method", ["read_text", "read_bytes", "open"])
    def test_path_method_fails(self, tmp_path: Path, method: str) -> None:
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            f"p.{method}()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert method in violations[0]

    def test_builtin_open_fails(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            "open(p)\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "open" in violations[0]

    def test_builtin_open_with_file_kwarg_fails(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            "open(file=p)\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_spec_from_file_location_positional_fails(self, tmp_path: Path) -> None:
        source = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.py'\n"
            "importlib.util.spec_from_file_location('x', str(p))\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "spec_from_file_location" in violations[0]

    def test_spec_from_file_location_kwarg_fails(self, tmp_path: Path) -> None:
        source = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.py'\n"
            "importlib.util.spec_from_file_location(name='x', location=str(p))\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Rule 4: safe filesystem probes are NOT flagged.
# ---------------------------------------------------------------------------


class TestSafeFilesystemProbes:
    @pytest.mark.parametrize("probe", ["is_file", "is_dir", "exists"])
    def test_probe_alone_passes(self, tmp_path: Path, probe: str) -> None:
        """is_file / is_dir / exists return False on a missing path —
        they are themselves safe to call without any guard."""
        source = (
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            f"p.{probe}()\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 5: existence-probe in the same scope makes the unsafe call safe.
# ---------------------------------------------------------------------------


class TestSafeProbeInScope:
    def test_if_isfile_guard_passes(self, tmp_path: Path) -> None:
        """The canonical guard pattern: if X.is_file(): X.read_text()."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    schema_path = _REPO_ROOT / 'packages' / 'schema.sql'\n"
            "    if schema_path.is_file():\n"
            "        return schema_path.read_text()\n"
            "    return ''\n"
        )
        assert _scan(tmp_path, source) == []

    def test_if_not_isfile_raise_passes(self, tmp_path: Path) -> None:
        """The defensive raise pattern: if not X.is_file(): raise ..."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    schema_path = _REPO_ROOT / 'packages' / 'schema.sql'\n"
            "    if not schema_path.is_file():\n"
            "        raise SystemExit('schema.sql not found')\n"
            "    return schema_path.read_text()\n"
        )
        assert _scan(tmp_path, source) == []

    def test_module_scope_assert_passes(self, tmp_path: Path) -> None:
        """A module-scope assert short-circuits import — every later
        reference to the variable is safe."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "assert (_REPO_ROOT / 'packages').is_dir(), 'packages missing'\n"
            "def load():\n"
            "    return (_REPO_ROOT / 'packages' / 'schema.sql').read_text()\n"
        )
        assert _scan(tmp_path, source) == []

    def test_module_scope_if_not_isdir_raise_passes(self, tmp_path: Path) -> None:
        """Module-scope if-not-isdir-raise is the same shape as assert."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "if not (_REPO_ROOT / 'packages').is_dir():\n"
            "    raise RuntimeError('packages missing')\n"
            "def load():\n"
            "    return (_REPO_ROOT / 'packages' / 'schema.sql').read_text()\n"
        )
        assert _scan(tmp_path, source) == []

    def test_probe_in_different_function_does_not_pass(self, tmp_path: Path) -> None:
        """A probe in function ``a`` does NOT make an unsafe call in
        function ``b`` safe — the probe must be in the same scope (or
        an enclosing module scope)."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def probe():\n"
            "    return (_REPO_ROOT / 'foo').is_file()\n"
            "def load():\n"
            "    return (_REPO_ROOT / 'foo').read_text()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1
        assert "load" not in violations[0]  # path:line, not function name
        assert "read_text" in violations[0]


# ---------------------------------------------------------------------------
# Rule 6: try/except OSError around the unsafe call is safe.
# ---------------------------------------------------------------------------


class TestTryExceptGuard:
    def test_try_except_oserror_passes(self, tmp_path: Path) -> None:
        """A try/except OSError catches the FileNotFoundError raised on
        a collapsed path — the wrong-path failure surfaces as
        caught-and-handled, not silently-wrong."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    p = _REPO_ROOT / 'foo.txt'\n"
            "    try:\n"
            "        return p.read_text()\n"
            "    except OSError:\n"
            "        return ''\n"
        )
        assert _scan(tmp_path, source) == []

    def test_try_except_filenotfound_passes(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    p = _REPO_ROOT / 'foo.txt'\n"
            "    try:\n"
            "        return p.read_text()\n"
            "    except FileNotFoundError:\n"
            "        return ''\n"
        )
        assert _scan(tmp_path, source) == []

    def test_try_except_bare_passes(self, tmp_path: Path) -> None:
        """Bare except: catches everything including OSError."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    p = _REPO_ROOT / 'foo.txt'\n"
            "    try:\n"
            "        return p.read_text()\n"
            "    except:\n"  # noqa: E722  (test fixture intentionally bare)
            "        return ''\n"
        )
        assert _scan(tmp_path, source) == []

    def test_try_except_unrelated_does_not_pass(self, tmp_path: Path) -> None:
        """try/except ValueError doesn't catch OSError; not a guard."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "def load():\n"
            "    p = _REPO_ROOT / 'foo.txt'\n"
            "    try:\n"
            "        return p.read_text()\n"
            "    except ValueError:\n"
            "        return ''\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Rule 7: sys.path.insert / sys.path.append are NOT flagged at all.
# ---------------------------------------------------------------------------


class TestSysPath:
    def test_sys_path_insert_passes(self, tmp_path: Path) -> None:
        source = (
            "import sys\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_PKG_SRC = _REPO_ROOT / 'packages' / 'foo' / 'src'\n"
            "sys.path.insert(0, str(_PKG_SRC))\n"
        )
        assert _scan(tmp_path, source) == []

    def test_sys_path_append_passes(self, tmp_path: Path) -> None:
        source = (
            "import sys\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "sys.path.append(str(_REPO_ROOT))\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 8: transitive derivation tracking.
# ---------------------------------------------------------------------------


class TestTransitiveDerivation:
    def test_two_step_chain_fails(self, tmp_path: Path) -> None:
        """A = parent.parent; B = A / 'x'; B.read_text() should flag."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_PKG = _REPO_ROOT / 'packages'\n"
            "_PKG.read_text()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_three_step_chain_fails(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_PKG = _REPO_ROOT / 'packages'\n"
            "_FOO = _PKG / 'foo'\n"
            "_FOO.read_text()\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Rule 9: allowlist marker.
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_marker_with_reason_exempts(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_REPO_ROOT.read_text()  # oneshot-path-required: test fixture\n"
        )
        assert _scan(tmp_path, source) == []

    def test_bare_marker_does_not_exempt(self, tmp_path: Path) -> None:
        """``# oneshot-path-required`` without a colon-reason must NOT
        exempt — the rule requires a non-empty justification."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_REPO_ROOT.read_text()  # oneshot-path-required\n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_empty_reason_does_not_exempt(self, tmp_path: Path) -> None:
        """``# oneshot-path-required:`` (colon present, reason empty)
        must NOT exempt."""
        source = (
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "_REPO_ROOT.read_text()  # oneshot-path-required: \n"
        )
        violations = _scan(tmp_path, source)
        assert len(violations) == 1

    def test_marker_inside_multiline_call_exempts(self, tmp_path: Path) -> None:
        source = (
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_REPO_ROOT = Path(__file__).resolve().parent.parent\n"
            "p = _REPO_ROOT / 'foo.py'\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'x', str(p),  # oneshot-path-required: legacy\n"
            ")\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# Rule 10: only files that USE the offending pattern are scanned at all.
# ---------------------------------------------------------------------------


class TestNoCollapseAnchor:
    def test_no_path_file_resolve_passes(self, tmp_path: Path) -> None:
        """A script that doesn't use Path(__file__).resolve().parent.parent
        is not in scope — nothing to flag."""
        source = "from pathlib import Path\np = Path('/etc/hosts')\np.read_text()\n"
        assert _scan(tmp_path, source) == []

    def test_unrelated_path_construction_passes(self, tmp_path: Path) -> None:
        source = (
            "from pathlib import Path\n"
            "import os\n"
            "p = Path(os.environ['HOME']) / 'foo.txt'\n"
            "p.read_text()\n"
        )
        assert _scan(tmp_path, source) == []


# ---------------------------------------------------------------------------
# End-to-end: the real scripts/ tree must scan clean (the post-#4374
# state). This is the primary AC #1 regression gate.
# ---------------------------------------------------------------------------


class TestRealRepoTree:
    def test_repo_scans_clean(self) -> None:
        """The post-#4374 scripts/ tree must scan clean. AC #1."""
        rc = check_mod.main([])
        assert rc == 0, (
            "scripts/*.py has a Path(__file__).resolve().parent.parent path "
            "derivation that reaches an unsafe data-load API without an "
            "existence guard"
        )


# ---------------------------------------------------------------------------
# Path exclusion: scripts/dispatcher/ and scripts/tests/ are out of scope.
# ---------------------------------------------------------------------------


class TestPathExclusion:
    def test_dispatcher_subdir_skipped(self, tmp_path: Path) -> None:
        """scripts/dispatcher/ has its own unbounded-IO check; this
        check only walks scripts/ at the top level (no recursion)."""
        bad_file = tmp_path / "dispatcher" / "fixture.py"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text(
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            "p.read_text()\n"
        )
        rc = check_mod.main([str(tmp_path)])
        # The top-level glob doesn't recurse, so dispatcher/ is invisible.
        assert rc == 0

    def test_top_level_py_scanned(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "fixture.py"
        bad_file.write_text(
            "from pathlib import Path\n"
            "p = Path(__file__).resolve().parent.parent / 'foo.txt'\n"
            "p.read_text()\n"
        )
        rc = check_mod.main([str(tmp_path)])
        assert rc == 1
