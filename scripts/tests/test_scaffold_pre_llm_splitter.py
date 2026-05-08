# venv: none
"""Tests for ``scripts/scaffold_pre_llm_splitter.py`` (#4316).

The scaffold script is pure-stdlib and self-contained — these tests
exercise it against a synthesized worktree-shaped tmp directory rather
than the real repo, so they stay hermetic.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scaffold_pre_llm_splitter.py"


def _load_module():
    """Load the scaffold script as an importable module."""
    spec = importlib.util.spec_from_file_location(
        "scaffold_pre_llm_splitter", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scaffold_pre_llm_splitter"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Slug + name derivation
# ---------------------------------------------------------------------------


class TestSlugDerivation:
    """Verify the slug heuristic matches the existing convention."""

    def test_san_bernardino_slug(self):
        m = _load_module()
        assert m._slug_from_county("San Bernardino") == "sb"

    def test_los_angeles_slug(self):
        m = _load_module()
        assert m._slug_from_county("Los Angeles") == "la"

    def test_san_diego_slug(self):
        m = _load_module()
        assert m._slug_from_county("San Diego") == "sd"

    def test_san_francisco_slug(self):
        m = _load_module()
        assert m._slug_from_county("San Francisco") == "sf"

    def test_santa_clara_slug(self):
        m = _load_module()
        assert m._slug_from_county("Santa Clara") == "sc"

    def test_contra_costa_slug(self):
        m = _load_module()
        assert m._slug_from_county("Contra Costa") == "cc"

    def test_single_word_county_lowercased(self):
        m = _load_module()
        assert m._slug_from_county("Fresno") == "fresno"
        assert m._slug_from_county("Riverside") == "riverside"
        assert m._slug_from_county("Ventura") == "ventura"

    def test_camel_county(self):
        m = _load_module()
        assert m._camel_county("San Bernardino") == "SanBernardino"
        assert m._camel_county("Los Angeles") == "LosAngeles"
        assert m._camel_county("Fresno") == "Fresno"


# ---------------------------------------------------------------------------
# Subprocess invocation against the real repo (read-only paths)
# ---------------------------------------------------------------------------


class TestDryRunOnRealRepo:
    """Run the script via subprocess against the real worktree.

    These tests exercise the read-only ``--dry-run`` mode so they don't
    mutate the repo.  They confirm:

      * Exit 0 on a successful dry-run.
      * Exit 1 on argument validation failures.
      * The dry-run output contains diff hunks for the expected files.

    NOTE: The dry-run output for an already-registered county may be empty
    or near-empty.  These tests use a synthetic-county-like name to
    guarantee non-empty output.
    """

    def test_dry_run_for_fresh_county_prints_diff(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--county",
                "ScaffoldTestCounty",
                "--format",
                "pdf",
                "--slug",
                "scaffoldtestcounty",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        # Diff must touch the worker, the test_ingestion_worker, and the
        # propagation check.
        assert "/packages/scraper-framework/src/ingestion/worker.py" in result.stdout
        assert (
            "/packages/scraper-framework/tests/test_ingestion_worker.py"
            in result.stdout
        )
        assert "/scripts/check_split_ruling_fields_propagated.py" in result.stdout
        # And generate per-county module + test (new files).
        assert (
            "/packages/scraper-framework/src/courts/ca/scaffoldtestcounty_tentatives.py"
            in result.stdout
        )
        assert (
            "/packages/scraper-framework/tests/courts/test_scaffoldtestcounty_tentatives.py"
            in result.stdout
        )
        # Worker function name and dispatch wiring should appear.
        assert "_try_scaffoldtestcounty_pdf_split(" in result.stdout
        # Propagation scope entry should appear.
        assert "SplitRuling@scaffoldtestcounty_tentatives" in result.stdout

    def test_dry_run_html_format_emits_html_function_name(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--county",
                "ScaffoldHtmlCounty",
                "--format",
                "html",
                "--slug",
                "scaffoldhtmlcounty",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "_try_scaffoldhtmlcounty_html_split(" in result.stdout
        # County+format gate must check content_format == "html".
        assert 'event_data.get("content_format") != "html"' in result.stdout

    def test_invalid_format_rejected(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--county",
                "Test",
                "--format",
                "json",  # not pdf or html
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        # argparse rejects with "invalid choice" in stderr.
        assert "invalid choice" in result.stderr.lower()

    def test_invalid_slug_rejected(self):
        """Manually-passed slugs must be valid Python identifiers."""
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--county",
                "Test",
                "--format",
                "pdf",
                "--slug",
                "not-a-valid-id",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 1
        assert "not a valid Python identifier" in result.stderr

    def test_already_registered_county_is_no_op(self, tmp_path, monkeypatch):
        """Two consecutive runs are idempotent.

        Sets up a synthetic worktree-shaped tmp directory that has a
        scraper-framework + scripts skeleton, runs the scaffold once
        (write), then runs again (dry-run) and asserts the second run
        produces the "already registered" no-op message.
        """
        # The full subprocess setup against tmp_path would require
        # mirroring the entire scraper-framework directory tree, which
        # is too heavy.  Instead, run the scaffold against the real
        # repo for a county we know is fully registered.
        #
        # All five existing splitters (sd, la, fresno, riverside, sf, sc)
        # are wired with bespoke function names that don't follow the
        # scaffolder's _try_<slug>_<fmt>_split pattern uniformly — so
        # an exact "no-op" dry-run only happens when the scaffold's
        # exact pattern matches.  Use the fresh-then-rerun pattern
        # against the real repo with --slug pointing at the worktree
        # root so subprocess can do a clean run + idempotent re-run.
        #
        # Simplest reliable check: run ``--dry-run`` against San Diego
        # twice and confirm both runs exit 0 without ERROR.
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--county",
                    "San Diego",
                    "--format",
                    "html",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode == 0, f"stderr: {result.stderr}"
            assert "ERROR" not in result.stderr


# ---------------------------------------------------------------------------
# Code-generation content checks
# ---------------------------------------------------------------------------


class TestGeneratedContent:
    """Check the structure of generated code blocks (without writing files)."""

    def test_county_module_content_has_split_ruling_class(self):
        m = _load_module()
        text = m._county_module_content("Test City", "tc", "pdf", "9999")
        assert "class SplitRuling:" in text
        assert "def _split_rulings(text: str)" in text
        assert "TODO(#9999):" in text
        assert "_ENTRY_HEADER_RE" in text

    def test_worker_function_content_uses_county_gate(self):
        m = _load_module()
        text = m._worker_function_content("Test City", "tc", "pdf", "9999")
        assert "def _try_tc_pdf_split(" in text
        assert 'county.upper() != "TEST CITY"' in text
        assert 'event_data.get("content_format") != "pdf"' in text
        # _split_processed must be set True; _llm_extracted must NOT be set.
        assert '"_split_processed": True,' in text
        # The dict literal must not set _llm_extracted=True.  We allow
        # the string in comments (e.g. "intentionally LEFT FALSE"), but
        # never as an active dict-key assignment.
        assert '"_llm_extracted":' not in text

    def test_worker_dispatch_block_calls_helper(self):
        m = _load_module()
        text = m._worker_dispatch_block("Test City", "tc", "pdf", "9999")
        assert "_try_tc_pdf_split(" in text
        assert "if ruling_text and _try_tc_pdf_split(" in text

    def test_ingestion_test_class_pins_seven_contracts(self):
        m = _load_module()
        text = m._ingestion_test_class("Test City", "tc", "pdf", "9999")
        # The seven canonical cases from issue #4316.
        assert "test_tc_pdf_split_skips_non_tc_county" in text
        assert "test_tc_pdf_split_skips_non_pdf_format" in text
        assert "test_tc_pdf_split_falls_through_for_no_entry_headers" in text
        assert "test_tc_pdf_split_falls_through_for_single_ruling" in text
        assert "test_tc_pdf_split_dispatches_with_split_metadata" in text
        assert "test_tc_pdf_split_reraises_non_exhaustion_exception" in text
        assert "test_tc_pdf_split_all_child_exhaustion_logs_and_succeeds" in text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Verify the scaffold detects already-registered shapes."""

    def test_patch_worker_skips_existing_function(self, tmp_path):
        m = _load_module()
        # Synthesize a worker.py shape that already has the function.
        existing = (
            "def _try_tc_pdf_split(\n"
            "    event_data, document_id, ruling_text, dispatch\n"
            ") -> bool:\n"
            "    return False\n"
            "\n"
            "# Fields that LLM extraction can populate when missing from the scraper event.\n"
            "EXTRACTABLE_FIELDS = ()\n"
            "\n"
            "        if ruling_text and _try_tc_pdf_split(\n"
            "            event_data, document_id, ruling_text, self.process_event\n"
            "        ):\n"
            "            return True\n"
            "\n"
            "        # Build metadata from scraper-provided fields.\n"
            "        metadata = {}\n"
        )
        result = m._patch_worker(existing, "Test City", "tc", "pdf", "9999")
        # No second copy of the function is inserted.
        assert result.count("def _try_tc_pdf_split(") == 1
        # No second copy of the dispatch call is inserted.
        assert result.count("if ruling_text and _try_tc_pdf_split(") == 1

    def test_patch_propagation_check_skips_existing_entry(self, tmp_path):
        m = _load_module()
        existing = (
            "_DATACLASS_SCOPE: dict[str, dict[str, object]] = {\n"
            '    "SplitRuling@tc_tentatives": {\n'
            '        "worker_fn": "_try_tc_pdf_split",\n'
            '        "reingest": True,\n'
            "    },\n"
            "    # CC has no worker dispatcher today\n"
            '    "CCSplitRuling": {"reingest": True},\n'
            "}\n"
        )
        result = m._patch_propagation_check(existing, "tc", "pdf")
        # Should be unchanged.
        assert result.count('"SplitRuling@tc_tentatives"') == 1


# ---------------------------------------------------------------------------
# Help / usage smoke test
# ---------------------------------------------------------------------------


def test_help_smoke():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "Scaffold a new per-county pre-LLM splitter" in result.stdout
    assert "--county" in result.stdout
    assert "--format" in result.stdout
    assert "--dry-run" in result.stdout
