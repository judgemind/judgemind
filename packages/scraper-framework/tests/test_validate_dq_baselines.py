"""Tests for scripts/validate-dq-baselines.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Add scripts/ to sys.path so we can import the module.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

# ruff: noqa: E402
from importlib import import_module

# Import the script as a module (it has hyphens in its name).
vdqb = import_module("validate-dq-baselines")

validate = vdqb.validate
validate_county_consistency = vdqb.validate_county_consistency
validate_reasonable_expectations = vdqb.validate_reasonable_expectations
validate_required_fc_fields = vdqb.validate_required_fc_fields
validate_county_config = vdqb.validate_county_config
load_baselines_json = vdqb.load_baselines_json
main = vdqb.main


def _valid_baselines() -> dict[str, Any]:
    """Return a structurally valid baselines dict."""
    return {
        "counties": {
            "Los Angeles": {
                "expected_daily_rulings": 50,
                "schedule_type": "daily",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
            "Orange": {
                "expected_daily_rulings": 20,
                "schedule_type": "daily",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        },
        "field_completeness": {
            "_updated": "2026-03-12",
            "_note": "Test baselines",
            "Los Angeles": {
                "total_documents": 748,
                "ruling": 100.0,
                "judge": 38.4,
                "motion_type": 99.6,
                "outcome": 99.9,
                "case_title": 96.9,
                "case_number": 99.3,
                "parties": 96.1,
                "hearing_date": 100.0,
            },
            "Orange": {
                "total_documents": 135,
                "ruling": 100.0,
                "judge": 100.0,
                "motion_type": 100.0,
                "outcome": 100.0,
                "case_title": 100.0,
                "case_number": 84.4,
                "parties": 100.0,
                "hearing_date": 100.0,
            },
        },
    }


class TestValidateCountyConsistency:
    """Tests for county name matching between sections."""

    def test_matching_counties_no_errors(self) -> None:
        data = _valid_baselines()
        errors = validate_county_consistency(data["counties"], data["field_completeness"])
        assert errors == []

    def test_county_only_in_counties_section(self) -> None:
        data = _valid_baselines()
        data["counties"]["Phantom County"] = {
            "expected_daily_rulings": 5,
            "schedule_type": "daily",
        }
        errors = validate_county_consistency(data["counties"], data["field_completeness"])
        assert len(errors) == 1
        assert "Phantom County" in errors[0]
        assert "missing from 'field_completeness'" in errors[0]

    def test_county_only_in_field_completeness(self) -> None:
        data = _valid_baselines()
        data["field_completeness"]["Stray County"] = {
            "total_documents": 10,
            "ruling": 100.0,
            "judge": 100.0,
            "motion_type": 100.0,
            "outcome": 100.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 100.0,
            "hearing_date": 100.0,
        }
        errors = validate_county_consistency(data["counties"], data["field_completeness"])
        assert len(errors) == 1
        assert "Stray County" in errors[0]
        assert "missing from 'counties'" in errors[0]

    def test_metadata_keys_skipped(self) -> None:
        """Metadata keys like _updated and _note should not be treated as counties."""
        data = _valid_baselines()
        errors = validate_county_consistency(data["counties"], data["field_completeness"])
        assert errors == []


class TestValidateReasonableExpectations:
    """Tests for low-document county expectations."""

    def test_high_doc_county_any_expectation_ok(self) -> None:
        data = _valid_baselines()
        errors = validate_reasonable_expectations(data["counties"], data["field_completeness"])
        assert errors == []

    def test_low_doc_county_high_expectation_flagged(self) -> None:
        data = _valid_baselines()
        data["counties"]["Small County"] = {
            "expected_daily_rulings": 5,
            "schedule_type": "daily",
        }
        data["field_completeness"]["Small County"] = {
            "total_documents": 3,
            "ruling": 100.0,
            "judge": 100.0,
            "motion_type": 100.0,
            "outcome": 100.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 100.0,
            "hearing_date": 100.0,
        }
        errors = validate_reasonable_expectations(data["counties"], data["field_completeness"])
        assert len(errors) == 1
        assert "Small County" in errors[0]
        assert "expected_daily_rulings=5" in errors[0]

    def test_low_doc_county_low_expectation_ok(self) -> None:
        data = _valid_baselines()
        data["counties"]["Small County"] = {
            "expected_daily_rulings": 1,
            "schedule_type": "daily",
        }
        data["field_completeness"]["Small County"] = {
            "total_documents": 2,
            "ruling": 100.0,
            "judge": 100.0,
            "motion_type": 100.0,
            "outcome": 100.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 100.0,
            "hearing_date": 100.0,
        }
        errors = validate_reasonable_expectations(data["counties"], data["field_completeness"])
        assert errors == []

    def test_low_doc_county_zero_expectation_ok(self) -> None:
        data = _valid_baselines()
        data["counties"]["Tiny County"] = {
            "expected_daily_rulings": 0,
            "schedule_type": "daily",
        }
        data["field_completeness"]["Tiny County"] = {
            "total_documents": 1,
            "ruling": 100.0,
            "judge": 100.0,
            "motion_type": 100.0,
            "outcome": 100.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 100.0,
            "hearing_date": 100.0,
        }
        errors = validate_reasonable_expectations(data["counties"], data["field_completeness"])
        assert errors == []

    def test_exactly_ten_docs_not_flagged(self) -> None:
        """Counties with exactly 10 docs are NOT considered low-document."""
        data = _valid_baselines()
        data["counties"]["Border County"] = {
            "expected_daily_rulings": 5,
            "schedule_type": "daily",
        }
        data["field_completeness"]["Border County"] = {
            "total_documents": 10,
            "ruling": 100.0,
            "judge": 100.0,
            "motion_type": 100.0,
            "outcome": 100.0,
            "case_title": 100.0,
            "case_number": 100.0,
            "parties": 100.0,
            "hearing_date": 100.0,
        }
        errors = validate_reasonable_expectations(data["counties"], data["field_completeness"])
        assert errors == []


class TestValidateRequiredFcFields:
    """Tests for required field_completeness fields."""

    def test_all_required_fields_present(self) -> None:
        data = _valid_baselines()
        errors = validate_required_fc_fields(data["field_completeness"])
        assert errors == []

    def test_missing_field_flagged(self) -> None:
        data = _valid_baselines()
        del data["field_completeness"]["Los Angeles"]["judge"]
        errors = validate_required_fc_fields(data["field_completeness"])
        assert len(errors) == 1
        assert "Los Angeles" in errors[0]
        assert "judge" in errors[0]

    def test_multiple_missing_fields(self) -> None:
        data = _valid_baselines()
        del data["field_completeness"]["Orange"]["ruling"]
        del data["field_completeness"]["Orange"]["outcome"]
        errors = validate_required_fc_fields(data["field_completeness"])
        assert len(errors) == 1
        assert "Orange" in errors[0]
        assert "outcome" in errors[0]
        assert "ruling" in errors[0]

    def test_extra_fields_ignored(self) -> None:
        """Extra fields like case_type or _known_gaps should not cause errors."""
        data = _valid_baselines()
        data["field_completeness"]["Los Angeles"]["case_type"] = 99.9
        data["field_completeness"]["Los Angeles"]["_known_gaps"] = {"judge": "notes"}
        errors = validate_required_fc_fields(data["field_completeness"])
        assert errors == []


class TestValidateCountyConfig:
    """Tests for county config value validation."""

    def test_valid_config_no_errors(self) -> None:
        data = _valid_baselines()
        errors = validate_county_config(data["counties"])
        assert errors == []

    def test_invalid_schedule_type(self) -> None:
        counties = {
            "BadSchedule": {
                "expected_daily_rulings": 5,
                "schedule_type": "weekly",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "BadSchedule" in errors[0]
        assert "weekly" in errors[0]

    def test_negative_expected_daily_rulings(self) -> None:
        counties = {
            "Negative County": {
                "expected_daily_rulings": -1,
                "schedule_type": "daily",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "Negative County" in errors[0]
        assert "negative" in errors[0]

    def test_valid_posting_days(self) -> None:
        counties = {
            "Weekday County": {
                "expected_daily_rulings": 5,
                "schedule_type": "daily",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        }
        errors = validate_county_config(counties)
        assert errors == []

    def test_invalid_posting_days(self) -> None:
        counties = {
            "BadDays County": {
                "expected_daily_rulings": 5,
                "schedule_type": "daily",
                "posting_days": ["Mon", "Tues", "Wednesday"],
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "BadDays County" in errors[0]
        assert "Tues" in errors[0]
        assert "Wednesday" in errors[0]

    def test_posting_days_not_a_list(self) -> None:
        counties = {
            "StringDays": {
                "expected_daily_rulings": 5,
                "schedule_type": "daily",
                "posting_days": "Mon-Fri",
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "must be a list" in errors[0]

    def test_frequent_schedule_type_valid(self) -> None:
        counties = {
            "Frequent County": {
                "expected_daily_rulings": 10,
                "schedule_type": "frequent",
                "posting_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            },
        }
        errors = validate_county_config(counties)
        assert errors == []

    def test_missing_posting_days_flagged(self) -> None:
        """County without posting_days should be flagged."""
        counties = {
            "NoDays County": {
                "expected_daily_rulings": 5,
                "schedule_type": "daily",
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "NoDays County" in errors[0]
        assert "posting_days" in errors[0]

    def test_missing_posting_days_does_not_mask_other_errors(self) -> None:
        """Missing posting_days and invalid schedule_type produce separate errors."""
        counties = {
            "BadCounty": {
                "expected_daily_rulings": 5,
                "schedule_type": "weekly",
            },
        }
        errors = validate_county_config(counties)
        assert len(errors) == 2
        messages = " ".join(errors)
        assert "posting_days" in messages
        assert "weekly" in messages


class TestValidateIntegration:
    """Integration tests for the full validate() function."""

    def test_valid_baselines_pass(self) -> None:
        data = _valid_baselines()
        errors = validate(data)
        assert errors == []

    def test_multiple_error_types_collected(self) -> None:
        data = _valid_baselines()
        # Add a phantom county (consistency error)
        data["counties"]["Ghost"] = {
            "expected_daily_rulings": 5,
            "schedule_type": "invalid",
        }
        # Remove a required field (fc fields error)
        del data["field_completeness"]["Los Angeles"]["judge"]
        errors = validate(data)
        assert len(errors) >= 2  # At least consistency + missing field errors

    def test_empty_sections_pass(self) -> None:
        data: dict[str, Any] = {"counties": {}, "field_completeness": {}}
        errors = validate(data)
        assert errors == []

    def test_missing_sections_treated_as_empty(self) -> None:
        data: dict[str, Any] = {}
        errors = validate(data)
        assert errors == []

    def test_real_baselines_file_valid(self) -> None:
        """The actual data-quality-baselines.json in the repo should pass validation."""
        baselines_path = _REPO_ROOT / "data-quality-baselines.json"
        if not baselines_path.exists():
            return  # Skip if running outside repo context
        data = load_baselines_json(baselines_path)
        errors = validate(data)
        assert errors == [], f"Real baselines file has errors: {errors}"

    def test_real_baselines_all_counties_have_posting_days(self) -> None:
        """Every county in the real baselines file must have posting_days set."""
        baselines_path = _REPO_ROOT / "data-quality-baselines.json"
        if not baselines_path.exists():
            return  # Skip if running outside repo context
        data = load_baselines_json(baselines_path)
        counties = data.get("counties", {})
        missing = [
            name
            for name, cfg in counties.items()
            if isinstance(cfg, dict) and "posting_days" not in cfg
        ]
        assert missing == [], (
            f"Counties missing 'posting_days': {missing}. "
            f"See #1372 for context on why this field is required."
        )


class TestLoadBaselinesJson:
    """Tests for JSON loading."""

    def test_valid_json_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "baselines.json"
        p.write_text(json.dumps(_valid_baselines()))
        data = load_baselines_json(p)
        assert "counties" in data

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        import pytest

        p = tmp_path / "nonexistent.json"
        with pytest.raises(SystemExit):
            load_baselines_json(p)

    def test_invalid_json_exits(self, tmp_path: Path) -> None:
        import pytest

        p = tmp_path / "bad.json"
        p.write_text("not valid json {{{")
        with pytest.raises(SystemExit):
            load_baselines_json(p)


class TestNullAndNonDictRobustness:
    """Tests for handling null/non-dict values in the JSON."""

    def test_null_counties_section(self) -> None:
        data: dict[str, Any] = {"counties": None, "field_completeness": {}}
        errors = validate(data)
        assert errors == []  # null treated as empty

    def test_null_field_completeness_section(self) -> None:
        data: dict[str, Any] = {"counties": {}, "field_completeness": None}
        errors = validate(data)
        assert errors == []  # null treated as empty

    def test_non_dict_county_config(self) -> None:
        counties = {"BadCounty": "not-a-dict"}
        errors = validate_county_config(counties)
        assert len(errors) == 1
        assert "must be a dictionary" in errors[0]

    def test_non_dict_fc_entry_in_required_fields(self) -> None:
        fc_section = {"BadCounty": "not-a-dict"}
        errors = validate_required_fc_fields(fc_section)
        assert len(errors) == 1
        assert "is not a dict" in errors[0]

    def test_null_county_config_in_reasonable_expectations(self) -> None:
        """Non-dict county config should not crash validate_reasonable_expectations."""
        counties: dict[str, Any] = {"NullCounty": None}
        fc_section = {
            "NullCounty": {
                "total_documents": 2,
                "ruling": 100.0,
                "judge": 100.0,
                "motion_type": 100.0,
                "outcome": 100.0,
                "case_title": 100.0,
                "case_number": 100.0,
                "parties": 100.0,
                "hearing_date": 100.0,
            },
        }
        errors = validate_reasonable_expectations(counties, fc_section)
        assert errors == []  # Skipped because config is not a dict

    def test_non_dict_fc_entry_in_reasonable_expectations(self) -> None:
        """Non-dict fc entry should not crash validate_reasonable_expectations."""
        counties = {"BadFC": {"expected_daily_rulings": 5, "schedule_type": "daily"}}
        fc_section: dict[str, Any] = {"BadFC": "not-a-dict"}
        errors = validate_reasonable_expectations(counties, fc_section)
        assert errors == []  # Skipped because fc entry is not a dict


class TestMainFunction:
    """Tests for the main() entrypoint."""

    def test_valid_file_returns_zero(self, tmp_path: Path, monkeypatch: Any) -> None:
        p = tmp_path / "baselines.json"
        p.write_text(json.dumps(_valid_baselines()))
        monkeypatch.setattr("sys.argv", ["validate-dq-baselines.py", "--path", str(p)])
        assert main() == 0

    def test_invalid_file_returns_one(self, tmp_path: Path, monkeypatch: Any) -> None:
        data = _valid_baselines()
        # Add inconsistency
        data["counties"]["Phantom"] = {
            "expected_daily_rulings": 5,
            "schedule_type": "daily",
        }
        p = tmp_path / "baselines.json"
        p.write_text(json.dumps(data))
        monkeypatch.setattr("sys.argv", ["validate-dq-baselines.py", "--path", str(p)])
        assert main() == 1
