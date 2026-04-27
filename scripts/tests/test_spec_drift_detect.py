"""Tests for spec_drift_detect module."""

from __future__ import annotations

import sys
from pathlib import Path


# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spec_drift_detect


class TestDriftInSkillActionList:
    """Test: fake diff adding a daemon.py action without updating SKILL.md action list."""

    def test_drift_in_skill_action_list(self, tmp_path: Path) -> None:
        """Adding a daemon.py action 'foobar' without updating diagnose-failure/SKILL.md
        should produce a finding citing both files."""
        # Set up fake worktree
        ralph_dir = tmp_path / "tmp" / "ralph"
        ralph_dir.mkdir(parents=True)

        # The diff adds a new action in daemon.py
        diff_content = """\
diff --git a/scripts/dispatcher/daemon.py b/scripts/dispatcher/daemon.py
index abc123..def456 100644
--- a/scripts/dispatcher/daemon.py
+++ b/scripts/dispatcher/daemon.py
@@ -100,6 +100,9 @@ class DispatcherDaemon:
     def _handle_action_baz(self) -> None:
         pass

+    def _handle_action_foobar(self) -> None:
+        pass
+
     def _handle_action_end(self) -> None:
         pass
"""
        (ralph_dir / "diff.txt").write_text(diff_content, encoding="utf-8")

        # Set up .md files — one that mentions known actions, but NOT foobar
        skills_dir = tmp_path / ".claude" / "skills" / "diagnose-failure"
        skills_dir.mkdir(parents=True)
        skill_md = skills_dir / "SKILL.md"
        skill_md.write_text(
            "# /diagnose-failure skill\n\nActions: baz, end.\n",
            encoding="utf-8",
        )

        # Also create a docs file that won't trigger
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "unrelated.md").write_text(
            "# Unrelated\n\nNothing about daemon actions here.\n",
            encoding="utf-8",
        )

        findings = spec_drift_detect.detect(tmp_path)

        # Should find that foobar is added in diff but not referenced in SKILL.md
        assert len(findings) >= 1
        finding_files = [(f["side_a_file"], f["side_b_file"]) for f in findings]
        # At least one finding should reference daemon.py and the skill file
        has_daemon_skill_pair = any(
            ("daemon.py" in a or "daemon.py" in b)
            and ("SKILL.md" in a or "SKILL.md" in b)
            for a, b in finding_files
        )
        assert has_daemon_skill_pair, (
            f"Expected a finding between daemon.py and SKILL.md, got: {finding_files}"
        )


class TestDriftMdToMdContradiction:
    """Test: pure .md diff adding a CLAUDE.md rule contradicting code-standards.md."""

    def test_drift_md_to_md_contradiction(self, tmp_path: Path) -> None:
        """Adding a CLAUDE.md rule that contradicts code-standards.md should produce
        a finding with both files cited."""
        ralph_dir = tmp_path / "tmp" / "ralph"
        ralph_dir.mkdir(parents=True)

        # The diff adds a new CLAUDE.md rule about venv handling
        diff_content = """\
diff --git a/CLAUDE.md b/CLAUDE.md
index abc123..def456 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -10,6 +10,8 @@ Some content here.
 ## Rules

+- Never use scripts/install-package-venv.sh; always run pip install directly instead.
+
 ## Other rules
"""
        (ralph_dir / "diff.txt").write_text(diff_content, encoding="utf-8")

        # Set up CLAUDE.md in the fake worktree root
        (tmp_path / "CLAUDE.md").write_text(
            "# Judgemind\n\n## Rules\n\n- Never use scripts/install-package-venv.sh; "
            "always run pip install directly instead.\n",
            encoding="utf-8",
        )

        # code-standards.md mentions install-package-venv.sh as the correct approach
        docs_dir = tmp_path / "docs" / "agent"
        docs_dir.mkdir(parents=True)
        (docs_dir / "code-standards.md").write_text(
            "# Code Standards\n\nUse `scripts/install-package-venv.sh` to install "
            "packages in the correct order.\n",
            encoding="utf-8",
        )

        findings = spec_drift_detect.detect(tmp_path)

        assert len(findings) >= 1
        finding_files = [(f["side_a_file"], f["side_b_file"]) for f in findings]
        # At least one finding should reference CLAUDE.md and code-standards.md
        has_md_pair = any(
            ("CLAUDE.md" in a or "CLAUDE.md" in b)
            and ("code-standards.md" in a or "code-standards.md" in b)
            for a, b in finding_files
        )
        assert has_md_pair, (
            f"Expected a finding between CLAUDE.md and code-standards.md, "
            f"got: {finding_files}"
        )


class TestDriftRenamedScriptPath:
    """Test: .md rename of script-path reference while another .md still references old name."""

    def test_drift_renamed_script_path(self, tmp_path: Path) -> None:
        """Updating one .md to reference scripts/new-name.sh while another .md still
        references scripts/old-name.sh should produce a finding."""
        ralph_dir = tmp_path / "tmp" / "ralph"
        ralph_dir.mkdir(parents=True)

        # The diff renames a path reference in infra docs
        diff_content = """\
diff --git a/docs/agent/infrastructure-reference.md b/docs/agent/infrastructure-reference.md
index abc123..def456 100644
--- a/docs/agent/infrastructure-reference.md
+++ b/docs/agent/infrastructure-reference.md
@@ -5,7 +5,7 @@ Infrastructure reference.

 ## Running scripts

-Use `scripts/old-name.sh` to run data tasks.
+Use `scripts/new-name.sh` to run data tasks.
"""
        (ralph_dir / "diff.txt").write_text(diff_content, encoding="utf-8")

        # Set up the infra doc with new name
        docs_dir = tmp_path / "docs" / "agent"
        docs_dir.mkdir(parents=True)
        infra_md = docs_dir / "infrastructure-reference.md"
        infra_md.write_text(
            "# Infrastructure\n\n## Running scripts\n\nUse `scripts/new-name.sh` to run data tasks.\n",
            encoding="utf-8",
        )

        # Another doc still references old-name.sh
        (docs_dir / "local-dev.md").write_text(
            "# Local Dev\n\nFor data tasks see `scripts/old-name.sh`.\n",
            encoding="utf-8",
        )

        findings = spec_drift_detect.detect(tmp_path)

        assert len(findings) >= 1
        finding_files = [(f["side_a_file"], f["side_b_file"]) for f in findings]
        has_rename_pair = any(
            ("infrastructure-reference.md" in a or "infrastructure-reference.md" in b)
            and ("local-dev.md" in a or "local-dev.md" in b)
            for a, b in finding_files
        )
        assert has_rename_pair, (
            f"Expected a finding between infrastructure-reference.md and local-dev.md, "
            f"got: {finding_files}"
        )


class TestNoDriftPureLeafCode:
    """Test: pure leaf-code change touching no documented contract returns no findings."""

    def test_no_drift_pure_leaf_code(self, tmp_path: Path) -> None:
        """A pure internal implementation change that doesn't touch any documented
        interface/contract should return no findings."""
        ralph_dir = tmp_path / "tmp" / "ralph"
        ralph_dir.mkdir(parents=True)

        # Diff only modifies an internal helper function (no public API change)
        diff_content = """\
diff --git a/packages/scraper-framework/src/utils/internal_helper.py b/packages/scraper-framework/src/utils/internal_helper.py
index abc123..def456 100644
--- a/packages/scraper-framework/src/utils/internal_helper.py
+++ b/packages/scraper-framework/src/utils/internal_helper.py
@@ -5,7 +5,7 @@ def _parse_date(s: str) -> str:
     \"\"\"Parse date string.\"\"\"
-    return s.strip()
+    return s.strip().lower()
"""
        (ralph_dir / "diff.txt").write_text(diff_content, encoding="utf-8")

        # Set up some .md files that don't mention internal_helper
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "architecture.md").write_text(
            "# Architecture\n\nThe scraper uses a modular design.\n",
            encoding="utf-8",
        )
        (docs_dir / "contributing.md").write_text(
            "# Contributing\n\nPlease follow code standards.\n",
            encoding="utf-8",
        )

        findings = spec_drift_detect.detect(tmp_path)

        assert findings == [], (
            f"Expected no findings for pure leaf-code change, got: {findings}"
        )
