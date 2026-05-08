"""Parity test between worker.py and reingest_from_s3.py regex fallback chains.

Verifies that the worker's inline regex fallback logic and reingest's
``_apply_regex_fallbacks()`` produce identical field extraction results for
a representative set of inputs.  This acts as a contract test — if someone
adds a new fallback to one path without updating the other, a test case
here will fail.

After #2178, the enrichment fields (outcome, motion_type, case_title,
parties) are handled exclusively by LLM enrichment.  The regex fallback
chains only cover: judge_name, case_number, hearing_date, and case_type.

See #1842 (parent: #1836) for background on the drift problem this prevents.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Import reingest module from scripts/
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "scripts",
)
sys.path.insert(0, _SCRIPTS_DIR)

reingest = importlib.import_module("reingest_from_s3")

# ---------------------------------------------------------------------------
# Import the same extract helpers used by both code paths.
#
# After #4295 the case_type fallback chain is encoded in
# ``ingestion.case_type_resolver.resolve_case_type`` and both worker.py
# and ``_apply_regex_fallbacks`` call it.  The simulator below uses the
# same resolver so its case_type output continues to mirror production.
# ---------------------------------------------------------------------------
from ingestion.case_type_resolver import resolve_case_type  # noqa: E402
from ingestion.extract import (  # noqa: E402
    extract_case_number,
    extract_hearing_date,
    extract_judge_name,
)

# ---------------------------------------------------------------------------
# Reference implementation — mirrors worker.py regex fallback chain
# ---------------------------------------------------------------------------


def _simulate_worker_fallbacks(
    text: str,
    *,
    case_number: str | None = None,
    case_type: str | None = None,
    judge_name: str | None = None,
    hearing_date: Any = None,
    motion_type: str | None = None,
    scraper_id: str = "",
) -> dict[str, Any]:
    """Simulate the worker's regex fallback chain for non-enrichment fields.

    After #2178, the worker only uses regex fallbacks for judge_name,
    case_number, hearing_date, and case_type.  The enrichment fields
    (outcome, motion_type, case_title, parties) are handled exclusively
    by LLM enrichment.

    Note: motion_type is accepted as a parameter because it feeds into
    case_type inference, but it is NOT extracted from text by regex.
    """
    extraction_methods: dict[str, str] = {}

    # Worker: hearing_date regex fallback
    if hearing_date is None and text:
        hearing_date = extract_hearing_date(text)
        if hearing_date is not None:
            extraction_methods.setdefault("hearing_date", "regex")

    # Worker: judge_name regex fallback
    if not judge_name and text:
        judge_name = extract_judge_name(text)
        if judge_name:
            extraction_methods.setdefault("judge_name", "regex")

    # Worker: case_number fallback
    if not case_number and text:
        extracted_cn = extract_case_number(text)
        if extracted_cn:
            extraction_methods.setdefault("case_number", "regex")
            case_number = extracted_cn

    # Worker: full case_type fallback chain — delegated to the shared
    # resolver since #4295 (number -> scraper_id -> motion_type ->
    # case_title).  The simulator mirrors worker.py's call site so the
    # parity assertion stays meaningful when the resolver evolves.
    resolved_ct, resolved_method = resolve_case_type(
        case_type=case_type,
        case_number=case_number,
        scraper_id=scraper_id or None,
        motion_type=motion_type,
        case_title=None,
    )
    if resolved_method is not None:
        case_type = resolved_ct
        extraction_methods.setdefault("case_type", resolved_method)

    return {
        "judge_name": judge_name,
        "case_number": case_number,
        "case_type": case_type,
        "hearing_date": hearing_date,
        "extraction_methods": extraction_methods,
    }


def _run_reingest_fallbacks(
    text: str,
    *,
    case_number: str | None = None,
    case_type: str | None = None,
    judge_name: str | None = None,
    hearing_date: Any = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    case_title: str | None = None,
    scraper_id: str = "",
) -> dict[str, Any]:
    """Run reingest's ``_apply_regex_fallbacks`` with the given initial state."""
    extracted: dict[str, Any] = {
        "judge_name": judge_name,
        "outcome": outcome,
        "motion_type": motion_type,
        "case_number": case_number,
        "case_title": case_title,
        "case_type": case_type,
        "hearing_date": hearing_date,
        "parties": [],
        "extraction_methods": {},
    }
    reingest._apply_regex_fallbacks(extracted, text, scraper_id=scraper_id)
    return extracted


# ---------------------------------------------------------------------------
# Fields compared for parity
# ---------------------------------------------------------------------------

_COMPARED_FIELDS = (
    "judge_name",
    "case_number",
    "case_type",
    "hearing_date",
)


def _assert_parity(
    worker_result: dict[str, Any],
    reingest_result: dict[str, Any],
    scenario: str,
) -> None:
    """Assert that the worker and reingest results match for all compared fields."""
    for field in _COMPARED_FIELDS:
        w_val = worker_result[field]
        r_val = reingest_result[field]
        assert w_val == r_val, (
            f"Parity mismatch in scenario '{scenario}', field '{field}': "
            f"worker={w_val!r}, reingest={r_val!r}"
        )


# ---------------------------------------------------------------------------
# Representative input scenarios
# ---------------------------------------------------------------------------

# Scenario 1: Civil scraper_id — case_type derived from scraper_id suffix
_CIVIL_SCRAPER_TEXT = (
    "Case No. 30-2025-01234567-CU-BC-CJC\n"
    "Smith v. Jones\n"
    "Judge: Hon. Jane Doe\n"
    "Hearing Date: March 15, 2026\n"
    "Motion for Summary Judgment\n"
    "The motion is GRANTED."
)

# Scenario 2: Case number prefix — case_type inferred from "CIVSB" prefix
_CASE_NUMBER_PREFIX_TEXT = (
    "Case No. CIVSB2501234\nHearing: January 10, 2026\nDemurrer\nThe demurrer is GRANTED."
)

# Scenario 3: All fields extracted from text
_ALL_FIELDS_TEXT = (
    "SUPERIOR COURT OF CALIFORNIA\n"
    "Case No. 24STCV01234\n"
    "Judge: Hon. Robert Williams\n"
    "Hearing Date: February 20, 2026\n"
    "Motion for Preliminary Injunction\n"
    "The motion is GRANTED IN PART."
)


class TestWorkerReingestFallbackParity:
    """Contract tests verifying worker and reingest fallback chains stay in sync.

    Each test scenario feeds the same initial state and ruling text through
    both the worker's regex fallback chain (simulated) and reingest's
    ``_apply_regex_fallbacks()``, then asserts all extracted fields match.

    After #2178, only non-enrichment fields (judge_name, case_number,
    hearing_date, case_type) are tested for parity.
    """

    def test_civil_scraper_id_derives_case_type(self) -> None:
        """Scenario 1: civil scraper_id populates case_type."""
        worker = _simulate_worker_fallbacks(
            _CIVIL_SCRAPER_TEXT,
            scraper_id="ca-oc-tentatives-civil",
        )
        reingest_r = _run_reingest_fallbacks(
            _CIVIL_SCRAPER_TEXT,
            scraper_id="ca-oc-tentatives-civil",
        )
        _assert_parity(worker, reingest_r, "civil_scraper_id")
        assert worker["case_type"] == "civil"

    def test_case_number_prefix_derives_case_type(self) -> None:
        """Scenario 2: case_type inferred from case number prefix."""
        worker = _simulate_worker_fallbacks(
            _CASE_NUMBER_PREFIX_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _CASE_NUMBER_PREFIX_TEXT,
        )
        _assert_parity(worker, reingest_r, "case_number_prefix")
        assert worker["case_type"] == "civil"

    def test_judge_and_hearing_date_extracted(self) -> None:
        """Scenario 3: judge_name and hearing_date extracted from text."""
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_TEXT,
        )
        _assert_parity(worker, reingest_r, "all_fields")
        assert worker["judge_name"] is not None
        assert worker["hearing_date"] is not None
        assert worker["case_number"] is not None

    def test_pre_populated_fields_not_overwritten(self) -> None:
        """Pre-populated fields should not be overwritten by either path."""
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_TEXT,
            case_number="EXISTING-123",
            case_type="family",
            judge_name="Pre-Set Judge",
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_TEXT,
            case_number="EXISTING-123",
            case_type="family",
            judge_name="Pre-Set Judge",
        )
        _assert_parity(worker, reingest_r, "pre_populated_fields")
        assert worker["case_number"] == "EXISTING-123"
        assert worker["case_type"] == "family"
        assert worker["judge_name"] == "Pre-Set Judge"

    def test_case_type_priority_number_over_scraper_id(self) -> None:
        """Case number prefix should take priority over scraper_id for case_type."""
        worker = _simulate_worker_fallbacks(
            "Case No. CIVSB2501234\nMotion granted.",
            scraper_id="ca-oc-tentatives-probate",
        )
        reingest_r = _run_reingest_fallbacks(
            "Case No. CIVSB2501234\nMotion granted.",
            scraper_id="ca-oc-tentatives-probate",
        )
        _assert_parity(worker, reingest_r, "priority_number_over_scraper_id")
        # CIVSB -> civil, even though scraper_id says probate
        assert worker["case_type"] == "civil"

    def test_extraction_method_keys_match(self) -> None:
        """Both paths should populate the same set of extraction method keys."""
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_TEXT,
        )
        worker_keys = set(worker["extraction_methods"].keys())
        reingest_keys = set(reingest_r["extraction_methods"].keys())
        assert worker_keys == reingest_keys, (
            f"Extraction method key mismatch: "
            f"worker={sorted(worker_keys)}, reingest={sorted(reingest_keys)}"
        )
