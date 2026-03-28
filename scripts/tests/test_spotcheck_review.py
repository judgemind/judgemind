"""Tests for scripts/spotcheck/review.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Add scripts directory to path so we can import the module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spotcheck"))

from review import (
    CHECKS,
    BoilerplateCheck,
    CaseNumberInTitleCheck,
    Check,
    DerivedRulingsCountCheck,
    NullFieldCheck,
    register_check,
    review_all,
    review_item,
)


class TestCheckRegistry:
    """Tests for the check registry."""

    def test_rulings_checks_registered(self) -> None:
        assert "rulings" in CHECKS
        assert len(CHECKS["rulings"]) >= 3  # null, boilerplate, case_number_in_title

    def test_originals_checks_registered(self) -> None:
        assert "originals" in CHECKS
        assert len(CHECKS["originals"]) >= 1  # derived_rulings_count

    def test_register_new_check(self) -> None:
        """Verify the register_check decorator works for new entity types."""
        original_checks = CHECKS.get("test_entity", []).copy()

        @register_check("test_entity")
        class TestCheck(Check):
            @property
            def name(self) -> str:
                return "test"

            @property
            def description(self) -> str:
                return "Test check"

            def run(
                self, item_dir: Path, item_data: dict[str, Any]
            ) -> list[dict[str, Any]]:
                return []

        assert "test_entity" in CHECKS
        assert TestCheck in CHECKS["test_entity"]

        # Cleanup
        CHECKS["test_entity"] = original_checks


class TestNullFieldCheck:
    """Tests for the NullFieldCheck."""

    def test_detects_null_fields(self, tmp_path: Path) -> None:
        check = NullFieldCheck()
        item_data = {
            "db_records": [
                {
                    "outcome": None,
                    "motion_type": "",
                    "ruling_text": "Some ruling",
                    "hearing_date": "2026-03-28",
                    "department": "Dept. 1",
                    "case_number": "BC123456",
                    "case_title": "Smith v Jones",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert findings[0]["check"] == "null_fields"
        assert findings[0]["severity"] == "warning"
        assert "outcome" in findings[0]["detail"]["null_fields"]
        assert "motion_type" in findings[0]["detail"]["null_fields"]

    def test_no_findings_when_all_fields_present(self, tmp_path: Path) -> None:
        check = NullFieldCheck()
        item_data = {
            "db_records": [
                {
                    "outcome": "granted",
                    "motion_type": "msj",
                    "ruling_text": "The motion is GRANTED.",
                    "hearing_date": "2026-03-28",
                    "department": "Dept. 1",
                    "case_number": "BC123456",
                    "case_title": "Smith v Jones",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0

    def test_no_findings_when_no_records(self, tmp_path: Path) -> None:
        check = NullFieldCheck()
        findings = check.run(tmp_path, {"db_records": []})
        assert len(findings) == 0

    def test_whitespace_only_treated_as_null(self, tmp_path: Path) -> None:
        check = NullFieldCheck()
        item_data = {
            "db_records": [
                {
                    "outcome": "granted",
                    "motion_type": "   ",
                    "ruling_text": "Text",
                    "hearing_date": "2026-03-28",
                    "department": "Dept. 1",
                    "case_number": "BC123",
                    "case_title": "Smith v Jones",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert "motion_type" in findings[0]["detail"]["null_fields"]


class TestBoilerplateCheck:
    """Tests for the BoilerplateCheck."""

    def test_detects_boilerplate(self, tmp_path: Path) -> None:
        check = BoilerplateCheck()
        item_data = {
            "db_records": [{"ruling_text": "No tentative ruling."}],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert findings[0]["check"] == "boilerplate"

    def test_detects_see_attached(self, tmp_path: Path) -> None:
        check = BoilerplateCheck()
        item_data = {
            "db_records": [{"ruling_text": "See attached."}],
        }

        findings = check.run(tmp_path, item_data)
        assert any(f["check"] == "boilerplate" for f in findings)

    def test_detects_short_text(self, tmp_path: Path) -> None:
        check = BoilerplateCheck()
        item_data = {
            "db_records": [{"ruling_text": "Denied."}],
        }

        findings = check.run(tmp_path, item_data)
        assert any("short" in f["message"].lower() for f in findings)

    def test_no_finding_for_normal_text(self, tmp_path: Path) -> None:
        check = BoilerplateCheck()
        item_data = {
            "db_records": [
                {
                    "ruling_text": "The motion for summary judgment is GRANTED. "
                    "There are no triable issues of material fact remaining.",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0

    def test_no_finding_for_empty_text(self, tmp_path: Path) -> None:
        """Empty text is caught by NullFieldCheck, not BoilerplateCheck."""
        check = BoilerplateCheck()
        item_data = {
            "db_records": [{"ruling_text": ""}],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0


class TestCaseNumberInTitleCheck:
    """Tests for the CaseNumberInTitleCheck."""

    def test_detects_case_number_in_title(self, tmp_path: Path) -> None:
        check = CaseNumberInTitleCheck()
        item_data = {
            "db_records": [
                {
                    "case_title": "BC123456 Smith v Jones",
                    "case_number": "BC123456",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert findings[0]["check"] == "case_number_in_title"
        assert findings[0]["severity"] == "warning"

    def test_no_finding_when_title_is_clean(self, tmp_path: Path) -> None:
        check = CaseNumberInTitleCheck()
        item_data = {
            "db_records": [
                {
                    "case_title": "Smith v Jones",
                    "case_number": "BC123456",
                },
            ],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0

    def test_handles_null_fields(self, tmp_path: Path) -> None:
        check = CaseNumberInTitleCheck()
        item_data = {
            "db_records": [{"case_title": None, "case_number": None}],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0


class TestDerivedRulingsCountCheck:
    """Tests for the DerivedRulingsCountCheck."""

    def test_flags_zero_derived_rulings(self, tmp_path: Path) -> None:
        check = DerivedRulingsCountCheck()
        item_data = {"derived_rulings": []}

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert findings[0]["detail"]["count"] == 0

    def test_info_for_single_ruling(self, tmp_path: Path) -> None:
        check = DerivedRulingsCountCheck()
        item_data = {
            "derived_rulings": [{"ruling_id": "r1"}],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 1
        assert findings[0]["severity"] == "info"

    def test_no_finding_for_multiple_rulings(self, tmp_path: Path) -> None:
        check = DerivedRulingsCountCheck()
        item_data = {
            "derived_rulings": [{"ruling_id": "r1"}, {"ruling_id": "r2"}],
        }

        findings = check.run(tmp_path, item_data)
        assert len(findings) == 0


class TestReviewItem:
    """Tests for the review_item function."""

    def test_reviews_ruling_item(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "items" / "test-ruling"
        item_dir.mkdir(parents=True)

        db_record = [
            {
                "outcome": None,
                "motion_type": "msj",
                "ruling_text": "Granted.",
                "hearing_date": "2026-03-28",
                "department": "Dept. 1",
                "case_number": "BC123",
                "case_title": "Smith v Jones",
            },
        ]
        (item_dir / "db_record.json").write_text(
            json.dumps(db_record), encoding="utf-8"
        )

        result = review_item("rulings", item_dir)
        assert result["id"] == "test-ruling"
        assert result["flagged"] is True
        assert len(result["findings"]) > 0

    def test_reviews_original_item(self, tmp_path: Path) -> None:
        item_dir = tmp_path / "items" / "test-doc"
        item_dir.mkdir(parents=True)

        derived = [{"ruling_id": "r1"}, {"ruling_id": "r2"}]
        (item_dir / "derived_rulings.json").write_text(
            json.dumps(derived), encoding="utf-8"
        )

        result = review_item("originals", item_dir)
        assert result["id"] == "test-doc"


class TestReviewAll:
    """Tests for the review_all function."""

    def test_reviews_all_items(self, tmp_path: Path) -> None:
        # Create a minimal spotcheck directory
        manifest = {
            "entity": "rulings",
            "county": "Los Angeles",
            "sampled_at": "2026-03-28T00:00:00+00:00",
            "ids": ["uuid-1", "uuid-2"],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        items_dir = tmp_path / "items"
        for uid in ["uuid-1", "uuid-2"]:
            item_dir = items_dir / uid
            item_dir.mkdir(parents=True)
            db_record = [
                {
                    "outcome": "granted",
                    "motion_type": "msj",
                    "ruling_text": "The motion for summary judgment is GRANTED in full.",
                    "hearing_date": "2026-03-28",
                    "department": "Dept. 1",
                    "case_number": "BC123",
                    "case_title": "Smith v Jones",
                },
            ]
            (item_dir / "db_record.json").write_text(
                json.dumps(db_record), encoding="utf-8"
            )

        result = review_all(tmp_path)
        assert result["entity"] == "rulings"
        assert result["total_items"] == 2

    def test_exits_without_manifest(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            review_all(tmp_path)

    def test_exits_without_items_dir(self, tmp_path: Path) -> None:
        manifest = {"entity": "rulings", "county": "LA", "ids": []}
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(SystemExit):
            review_all(tmp_path)
