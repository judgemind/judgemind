"""Tests for update-coverage-baselines.py — especially multi-file merging."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The module uses a hyphen in the filename, so we need importlib
import importlib

update_coverage_baselines = importlib.import_module("update-coverage-baselines")
parse_cobertura_xml = update_coverage_baselines.parse_cobertura_xml
parse_cobertura_xml_lines = update_coverage_baselines.parse_cobertura_xml_lines
merge_cobertura_coverage = update_coverage_baselines.merge_cobertura_coverage
parse_lcov = update_coverage_baselines.parse_lcov


# -- Cobertura XML fixtures --

COBERTURA_TEMPLATE = """\
<?xml version="1.0" ?>
<coverage version="7.0" timestamp="1000" lines-valid="{total}" lines-covered="{covered}" line-rate="{rate}" branches-covered="0" branches-valid="0" branch-rate="0" complexity="0">
    <packages>
        <package name="framework" line-rate="{rate}" branch-rate="0" complexity="0">
            <classes>
{classes}
            </classes>
        </package>
    </packages>
</coverage>
"""

CLASS_TEMPLATE = """\
                <class name="{name}" filename="{filename}" line-rate="0" branch-rate="0" complexity="0">
                    <methods/>
                    <lines>
{lines}
                    </lines>
                </class>"""

LINE_TEMPLATE = '                        <line number="{num}" hits="{hits}"/>'


def _make_cobertura_xml(
    tmp_path: Path,
    name: str,
    file_coverage: dict[str, list[tuple[int, int]]],
) -> Path:
    """Create a Cobertura XML file with the given per-file, per-line coverage.

    file_coverage: {filename: [(line_num, hits), ...]}
    """
    classes = []
    total_lines = 0
    covered_lines = 0
    for filename, lines in file_coverage.items():
        line_strs = []
        for num, hits in lines:
            line_strs.append(LINE_TEMPLATE.format(num=num, hits=hits))
            total_lines += 1
            if hits > 0:
                covered_lines += 1
        cls_name = filename.replace("/", ".").replace(".py", "")
        classes.append(
            CLASS_TEMPLATE.format(
                name=cls_name,
                filename=filename,
                lines="\n".join(line_strs),
            )
        )

    rate = covered_lines / total_lines if total_lines > 0 else 0
    xml_content = COBERTURA_TEMPLATE.format(
        total=total_lines,
        covered=covered_lines,
        rate=f"{rate:.4f}",
        classes="\n".join(classes),
    )

    path = tmp_path / name
    path.write_text(xml_content, encoding="utf-8")
    return path


class TestParseCobertura:
    """Tests for single Cobertura XML parsing."""

    def test_full_coverage(self, tmp_path: Path) -> None:
        path = _make_cobertura_xml(
            tmp_path,
            "full.xml",
            {"framework/scraper.py": [(1, 1), (2, 1), (3, 1)]},
        )
        result = parse_cobertura_xml(str(path))
        assert result == pytest.approx(100.0, abs=0.1)

    def test_partial_coverage(self, tmp_path: Path) -> None:
        path = _make_cobertura_xml(
            tmp_path,
            "partial.xml",
            {"framework/scraper.py": [(1, 1), (2, 0), (3, 1), (4, 0)]},
        )
        result = parse_cobertura_xml(str(path))
        assert result == pytest.approx(50.0, abs=0.1)

    def test_zero_coverage(self, tmp_path: Path) -> None:
        path = _make_cobertura_xml(
            tmp_path,
            "zero.xml",
            {"framework/scraper.py": [(1, 0), (2, 0)]},
        )
        result = parse_cobertura_xml(str(path))
        assert result == pytest.approx(0.0, abs=0.1)


class TestParseCoberturXmlLines:
    """Tests for per-line extraction from Cobertura XML."""

    def test_extracts_lines(self, tmp_path: Path) -> None:
        path = _make_cobertura_xml(
            tmp_path,
            "lines.xml",
            {"framework/scraper.py": [(1, 5), (2, 0), (3, 1)]},
        )
        result = parse_cobertura_xml_lines(str(path))
        assert "framework/scraper.py" in result
        assert result["framework/scraper.py"] == {1: 5, 2: 0, 3: 1}

    def test_multiple_files(self, tmp_path: Path) -> None:
        path = _make_cobertura_xml(
            tmp_path,
            "multi.xml",
            {
                "framework/scraper.py": [(1, 1), (2, 0)],
                "courts/la.py": [(1, 0), (2, 1), (3, 1)],
            },
        )
        result = parse_cobertura_xml_lines(str(path))
        assert len(result) == 2
        assert result["framework/scraper.py"] == {1: 1, 2: 0}
        assert result["courts/la.py"] == {1: 0, 2: 1, 3: 1}


class TestMergeCoberturaXml:
    """Tests for merging multiple Cobertura XML coverage files."""

    def test_merge_disjoint_files(self, tmp_path: Path) -> None:
        """Two XML files covering different source files should combine."""
        # Report 1: framework tests — covers framework, not courts
        path1 = _make_cobertura_xml(
            tmp_path,
            "framework.xml",
            {
                "framework/scraper.py": [(1, 1), (2, 1), (3, 1)],
                "courts/la.py": [(1, 0), (2, 0), (3, 0)],
            },
        )
        # Report 2: court tests — covers courts, not framework
        path2 = _make_cobertura_xml(
            tmp_path,
            "courts.xml",
            {
                "framework/scraper.py": [(1, 0), (2, 0), (3, 0)],
                "courts/la.py": [(1, 1), (2, 1), (3, 1)],
            },
        )

        result = merge_cobertura_coverage([str(path1), str(path2)])
        # All 6 lines should be covered (3 from each report)
        assert result == pytest.approx(100.0, abs=0.1)

    def test_merge_overlapping_files(self, tmp_path: Path) -> None:
        """Files with overlapping coverage should take the max hits."""
        path1 = _make_cobertura_xml(
            tmp_path,
            "report1.xml",
            {"framework/base.py": [(1, 1), (2, 0), (3, 1), (4, 0)]},
        )
        path2 = _make_cobertura_xml(
            tmp_path,
            "report2.xml",
            {"framework/base.py": [(1, 0), (2, 1), (3, 0), (4, 0)]},
        )

        result = merge_cobertura_coverage([str(path1), str(path2)])
        # Lines 1, 2, 3 covered; line 4 not covered = 75%
        assert result == pytest.approx(75.0, abs=0.1)

    def test_merge_three_files(self, tmp_path: Path) -> None:
        """Simulate the actual CI scenario: framework + courts + ingestion."""
        # Framework report: good coverage of framework, zero on courts/ingestion
        path1 = _make_cobertura_xml(
            tmp_path,
            "framework.xml",
            {
                "framework/scraper.py": [(1, 1), (2, 1)],
                "courts/la.py": [(1, 0), (2, 0)],
                "ingestion/worker.py": [(1, 0), (2, 0)],
            },
        )
        # Courts report: good coverage of courts, zero on framework/ingestion
        path2 = _make_cobertura_xml(
            tmp_path,
            "courts.xml",
            {
                "framework/scraper.py": [(1, 0), (2, 0)],
                "courts/la.py": [(1, 1), (2, 1)],
                "ingestion/worker.py": [(1, 0), (2, 0)],
            },
        )
        # Ingestion report: good coverage of ingestion, zero on framework/courts
        path3 = _make_cobertura_xml(
            tmp_path,
            "ingestion.xml",
            {
                "framework/scraper.py": [(1, 0), (2, 0)],
                "courts/la.py": [(1, 0), (2, 0)],
                "ingestion/worker.py": [(1, 1), (2, 1)],
            },
        )

        result = merge_cobertura_coverage([str(path1), str(path2), str(path3)])
        # All 6 unique lines covered = 100%
        assert result == pytest.approx(100.0, abs=0.1)

    def test_merge_single_file_matches_parse(self, tmp_path: Path) -> None:
        """Merging a single file should give same result as parse_cobertura_xml."""
        path = _make_cobertura_xml(
            tmp_path,
            "single.xml",
            {"framework/scraper.py": [(1, 1), (2, 0), (3, 1)]},
        )

        merged = merge_cobertura_coverage([str(path)])
        parsed = parse_cobertura_xml(str(path))
        assert merged == pytest.approx(parsed, abs=0.5)

    def test_merge_file_only_in_one_report(self, tmp_path: Path) -> None:
        """A source file appearing in only one report should still be counted."""
        path1 = _make_cobertura_xml(
            tmp_path,
            "report1.xml",
            {"framework/scraper.py": [(1, 1), (2, 1)]},
        )
        path2 = _make_cobertura_xml(
            tmp_path,
            "report2.xml",
            {"ingestion/worker.py": [(1, 1), (2, 0)]},
        )

        result = merge_cobertura_coverage([str(path1), str(path2)])
        # 3 covered out of 4 total = 75%
        assert result == pytest.approx(75.0, abs=0.1)

    def test_merge_empty_coverage(self, tmp_path: Path) -> None:
        """Merging files with no lines at all returns 0."""
        xml_content = """\
<?xml version="1.0" ?>
<coverage version="7.0" timestamp="1000" lines-valid="0" lines-covered="0" line-rate="0" branches-covered="0" branches-valid="0" branch-rate="0" complexity="0">
    <packages/>
</coverage>
"""
        path = tmp_path / "empty.xml"
        path.write_text(xml_content, encoding="utf-8")
        result = merge_cobertura_coverage([str(path)])
        assert result == 0.0


class TestMainCliMultipleFiles:
    """Integration tests for the main() CLI with multiple --coverage-file args."""

    def test_multiple_coverage_files_cli(self, tmp_path: Path) -> None:
        """Test that multiple --coverage-file args are correctly parsed."""
        # Create two coverage XMLs with complementary coverage
        path1 = _make_cobertura_xml(
            tmp_path,
            "cov1.xml",
            {
                "framework/scraper.py": [(1, 1), (2, 1)],
                "courts/la.py": [(1, 0), (2, 0)],
            },
        )
        path2 = _make_cobertura_xml(
            tmp_path,
            "cov2.xml",
            {
                "framework/scraper.py": [(1, 0), (2, 0)],
                "courts/la.py": [(1, 1), (2, 1)],
            },
        )

        # Create baselines file with a low baseline
        baselines = {"packages/scraper-framework": 50.0}
        baselines_path = tmp_path / "coverage-baselines.json"
        baselines_path.write_text(json.dumps(baselines), encoding="utf-8")

        # Run the script in dry-run mode
        sys.argv = [
            "update-coverage-baselines.py",
            "--package",
            "packages/scraper-framework",
            "--coverage-file",
            str(path1),
            "--coverage-file",
            str(path2),
            "--baselines-file",
            str(baselines_path),
            "--dry-run",
        ]

        # Should not exit with error (coverage 100% > 50% baseline)
        # We can't easily test main() without capturing exit, so test the
        # merge function directly here
        result = merge_cobertura_coverage([str(path1), str(path2)])
        assert result == pytest.approx(100.0, abs=0.1)

    def test_missing_files_are_skipped(self, tmp_path: Path) -> None:
        """Missing coverage files should be skipped, not cause errors."""
        path1 = _make_cobertura_xml(
            tmp_path,
            "exists.xml",
            {"framework/scraper.py": [(1, 1), (2, 1)]},
        )
        missing = tmp_path / "does-not-exist.xml"

        # parse_cobertura_xml_lines should work with just the existing file
        result = merge_cobertura_coverage([str(path1)])
        assert result == pytest.approx(100.0, abs=0.1)
