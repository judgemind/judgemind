"""Parity test between worker.py and reingest_from_s3.py regex fallback chains.

Verifies that the worker's inline regex fallback logic and reingest's
``_apply_regex_fallbacks()`` produce identical field extraction results for
a representative set of inputs.  This acts as a contract test — if someone
adds a new fallback to one path without updating the other, a test case
here will fail.

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
# Import the same extract helpers used by both code paths
# ---------------------------------------------------------------------------
from ingestion.extract import (  # noqa: E402
    extract_case_number,
    extract_case_title,
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_hearing_date,
    extract_judge_name,
    extract_motion_type,
    extract_outcome,
    extract_parties_from_caption,
)

# ---------------------------------------------------------------------------
# Reference implementation — mirrors worker.py regex fallback chain
# ---------------------------------------------------------------------------


def _simulate_worker_fallbacks(
    text: str,
    *,
    case_number: str | None = None,
    case_title: str | None = None,
    case_type: str | None = None,
    judge_name: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    hearing_date: Any = None,
    parties: list[dict[str, str]] | None = None,
    scraper_id: str = "",
) -> dict[str, Any]:
    """Simulate the worker's regex fallback chain for the non-LLM path.

    This mirrors the regex fallbacks in ``IngestionWorker._process_event()``
    for ``is_llm_extracted=False`` events with no LLM client.  The order and
    conditions must match the worker exactly.

    When the worker code changes its fallback chain, this reference function
    must be updated in lockstep — and the test below will catch any
    divergence between this reference and reingest's ``_apply_regex_fallbacks``.
    """
    extraction_methods: dict[str, str] = {}
    parties_data: list[dict[str, str]] = list(parties) if parties else []

    # Worker line ~682: hearing_date regex fallback
    if hearing_date is None and text:
        hearing_date = extract_hearing_date(text)
        if hearing_date is not None:
            extraction_methods.setdefault("hearing_date", "regex")

    # Worker line ~691-699: outcome and motion_type regex fallback
    if text and (outcome is None or motion_type is None):
        if outcome is None:
            outcome = extract_outcome(text)
            if outcome is not None:
                extraction_methods.setdefault("outcome", "regex")
        if motion_type is None:
            motion_type = extract_motion_type(text)
            if motion_type is not None:
                extraction_methods.setdefault("motion_type", "regex")

    # Worker line ~744-754: case_number fallback
    if not case_number and text:
        extracted_cn = extract_case_number(text)
        if extracted_cn:
            extraction_methods.setdefault("case_number", "regex")
            case_number = extracted_cn

    # Worker line ~757-761: case_title fallback
    if not case_title and text:
        case_title = extract_case_title(text)
        if case_title:
            extraction_methods.setdefault("case_title", "regex")

    # Worker line ~764-771: judge_name fallback
    if not judge_name and text:
        judge_name = extract_judge_name(text)
        if judge_name:
            extraction_methods.setdefault("judge_name", "regex")

    # Worker line ~800-813: parties from caption
    effective_title = case_title
    if not parties_data and effective_title:
        parties_data = extract_parties_from_caption(effective_title)
        if parties_data:
            extraction_methods.setdefault("parties", "regex")

    # Worker line ~816-819: case_type from case_number prefix
    if case_type is None and case_number:
        case_type = extract_case_type_from_number(case_number)
        if case_type:
            extraction_methods.setdefault("case_type", "regex")

    # Worker line ~825-828: case_type from scraper_id
    if case_type is None and scraper_id:
        case_type = extract_case_type_from_scraper_id(scraper_id)
        if case_type:
            extraction_methods.setdefault("case_type", "scraper_id")

    # Worker line ~835-838: case_type from motion_type
    if case_type is None and motion_type:
        case_type = extract_case_type_from_motion_type(motion_type)
        if case_type:
            extraction_methods.setdefault("case_type", "motion_type")

    return {
        "judge_name": judge_name,
        "outcome": outcome,
        "motion_type": motion_type,
        "case_number": case_number,
        "case_title": case_title,
        "case_type": case_type,
        "hearing_date": hearing_date,
        "parties": parties_data,
        "extraction_methods": extraction_methods,
    }


def _run_reingest_fallbacks(
    text: str,
    *,
    case_number: str | None = None,
    case_title: str | None = None,
    case_type: str | None = None,
    judge_name: str | None = None,
    outcome: str | None = None,
    motion_type: str | None = None,
    hearing_date: Any = None,
    parties: list[dict[str, str]] | None = None,
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
        "parties": list(parties) if parties else [],
        "extraction_methods": {},
    }
    reingest._apply_regex_fallbacks(extracted, text, scraper_id=scraper_id)
    return extracted


# ---------------------------------------------------------------------------
# Fields compared for parity (extraction_methods key names may differ
# between worker and reingest, so we compare field values only).
# ---------------------------------------------------------------------------

_COMPARED_FIELDS = (
    "judge_name",
    "outcome",
    "motion_type",
    "case_number",
    "case_title",
    "case_type",
    "hearing_date",
    "parties",
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

# Scenario 2: Probate scraper_id — case_type from scraper_id "probate"
# Uses a case number with no recognizable type prefix so the scraper_id
# fallback is reached (e.g. OC North JC PDFs have no case numbers or
# use all-digit numbers that don't encode a type).
_PROBATE_SCRAPER_TEXT = "In re Estate of Johnson\nPetition for Probate\nThe petition is GRANTED."

# Scenario 3: "v." style party extraction — no pre-populated parties
# The case_title is pre-populated (simulating scraper-provided title) so
# extract_parties_from_caption can reliably extract both plaintiff and
# defendant from the "v." style caption.
_PARTY_EXTRACTION_TEXT = "Case No. CIVSB2501234\nMotion to Compel\nThe motion is DENIED."
_PARTY_EXTRACTION_TITLE = "ACME Corp. v. Widget Inc."

# Scenario 4: Case number prefix — case_type inferred from "CIVSB" prefix
_CASE_NUMBER_PREFIX_TEXT = (
    "Case No. CIVSB2501234\nHearing: January 10, 2026\nDemurrer\nThe demurrer is GRANTED."
)

# Scenario 5: Motion type fallback — case_type from motion_type when no
# case-number prefix and no scraper_id
_MOTION_TYPE_FALLBACK_TEXT = "Case No. 202300574258\nAnti-SLAPP Motion\nThe motion is DENIED."

# Scenario 6: All fields missing — extensive regex extraction
_ALL_FIELDS_MISSING_TEXT = (
    "SUPERIOR COURT OF CALIFORNIA\n"
    "Case No. 24STCV01234\n"
    "Plaintiff Alpha LLC v. Defendant Beta Corp.\n"
    "Judge: Hon. Robert Williams\n"
    "Hearing Date: February 20, 2026\n"
    "Motion for Preliminary Injunction\n"
    "The motion is GRANTED IN PART and DENIED IN PART."
)

# Scenario 7: Minimal text — only motion_type extractable, no case number
_MINIMAL_TEXT = "Motion to Dismiss\nThe motion is DENIED."


class TestWorkerReingestFallbackParity:
    """Contract tests verifying worker and reingest fallback chains stay in sync.

    Each test scenario feeds the same initial state and ruling text through
    both the worker's regex fallback chain (simulated) and reingest's
    ``_apply_regex_fallbacks()``, then asserts all extracted fields match.

    If a new fallback is added to worker.py without updating reingest (or
    vice versa), at least one of these tests will fail due to a field
    value mismatch.
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
        # Verify the expected extraction happened
        assert worker["case_type"] == "civil"

    def test_probate_scraper_id_derives_case_type(self) -> None:
        """Scenario 2: probate scraper_id populates case_type."""
        worker = _simulate_worker_fallbacks(
            _PROBATE_SCRAPER_TEXT,
            scraper_id="ca-oc-north-jc-tentatives-probate",
        )
        reingest_r = _run_reingest_fallbacks(
            _PROBATE_SCRAPER_TEXT,
            scraper_id="ca-oc-north-jc-tentatives-probate",
        )
        _assert_parity(worker, reingest_r, "probate_scraper_id")
        assert worker["case_type"] == "probate"

    def test_v_style_party_extraction(self) -> None:
        """Scenario 3: parties extracted from 'X v. Y' caption."""
        worker = _simulate_worker_fallbacks(
            _PARTY_EXTRACTION_TEXT,
            case_title=_PARTY_EXTRACTION_TITLE,
        )
        reingest_r = _run_reingest_fallbacks(
            _PARTY_EXTRACTION_TEXT,
            case_title=_PARTY_EXTRACTION_TITLE,
        )
        _assert_parity(worker, reingest_r, "v_style_party_extraction")
        # Should have extracted both plaintiff and defendant
        assert len(worker["parties"]) >= 2
        roles = {p["role"] for p in worker["parties"]}
        assert "plaintiff" in roles
        assert "defendant" in roles

    def test_case_number_prefix_derives_case_type(self) -> None:
        """Scenario 4: case_type inferred from case number prefix."""
        worker = _simulate_worker_fallbacks(
            _CASE_NUMBER_PREFIX_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _CASE_NUMBER_PREFIX_TEXT,
        )
        _assert_parity(worker, reingest_r, "case_number_prefix")
        assert worker["case_type"] == "civil"

    def test_motion_type_fallback_derives_case_type(self) -> None:
        """Scenario 5: case_type from motion_type when no prefix or scraper_id."""
        worker = _simulate_worker_fallbacks(
            _MOTION_TYPE_FALLBACK_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _MOTION_TYPE_FALLBACK_TEXT,
        )
        _assert_parity(worker, reingest_r, "motion_type_fallback")
        # anti_slapp -> civil
        assert worker["case_type"] == "civil"
        assert worker["motion_type"] == "anti_slapp"

    def test_all_fields_extracted_from_text(self) -> None:
        """Scenario 6: all fields extracted from rich ruling text."""
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
        )
        _assert_parity(worker, reingest_r, "all_fields_missing")
        # Verify multiple fields were populated
        assert worker["outcome"] is not None
        assert worker["motion_type"] is not None
        assert worker["case_number"] is not None

    def test_minimal_text_motion_type_only(self) -> None:
        """Scenario 7: minimal text — only motion_type and outcome extracted."""
        worker = _simulate_worker_fallbacks(
            _MINIMAL_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _MINIMAL_TEXT,
        )
        _assert_parity(worker, reingest_r, "minimal_text")
        assert worker["motion_type"] == "mtd"
        assert worker["outcome"] == "denied"

    def test_pre_populated_fields_not_overwritten(self) -> None:
        """Pre-populated fields should not be overwritten by either path."""
        initial = {
            "case_number": "EXISTING-123",
            "case_title": "Already Set",
            "case_type": "family",
            "judge_name": "Pre-Set Judge",
            "outcome": "granted",
            "motion_type": "msj",
            "hearing_date": None,
            "parties": [{"name": "Existing Party", "role": "plaintiff"}],
            "scraper_id": "ca-la-tentatives-civil",
        }
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
            case_number=initial["case_number"],
            case_title=initial["case_title"],
            case_type=initial["case_type"],
            judge_name=initial["judge_name"],
            outcome=initial["outcome"],
            motion_type=initial["motion_type"],
            hearing_date=initial["hearing_date"],
            parties=initial["parties"],
            scraper_id=initial["scraper_id"],
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
            case_number=initial["case_number"],
            case_title=initial["case_title"],
            case_type=initial["case_type"],
            judge_name=initial["judge_name"],
            outcome=initial["outcome"],
            motion_type=initial["motion_type"],
            hearing_date=initial["hearing_date"],
            parties=initial["parties"],
            scraper_id=initial["scraper_id"],
        )
        _assert_parity(worker, reingest_r, "pre_populated_fields")
        # Pre-populated values should be preserved
        assert worker["case_number"] == "EXISTING-123"
        assert worker["case_type"] == "family"
        assert worker["judge_name"] == "Pre-Set Judge"

    def test_extraction_method_keys_match(self) -> None:
        """Both paths should populate the same set of extraction method keys.

        While method *values* may differ (worker uses ``"regex"`` vs reingest
        may use the same), the *keys* (which fields were regex-extracted)
        must match — indicating the same fields were filled by fallback logic.
        """
        worker = _simulate_worker_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
        )
        reingest_r = _run_reingest_fallbacks(
            _ALL_FIELDS_MISSING_TEXT,
        )
        worker_keys = set(worker["extraction_methods"].keys())
        reingest_keys = set(reingest_r["extraction_methods"].keys())
        assert worker_keys == reingest_keys, (
            f"Extraction method key mismatch: "
            f"worker={sorted(worker_keys)}, reingest={sorted(reingest_keys)}"
        )


class TestSimulateWorkerFallbacksMatchesExtractHelpers:
    """Verify that _simulate_worker_fallbacks correctly mirrors the extract helpers.

    These tests validate the reference function itself, ensuring it calls
    the right extract_* functions and respects the fallback order.
    """

    def test_hearing_date_extracted(self) -> None:
        """Hearing date should be extracted from text."""
        result = _simulate_worker_fallbacks(
            "Hearing Date: April 5, 2026\nThe motion is granted.",
        )
        assert result["hearing_date"] is not None

    def test_judge_name_extracted(self) -> None:
        """Judge name should be extracted from text."""
        result = _simulate_worker_fallbacks(
            "Judge: Hon. Sarah Martinez\nThe demurrer is sustained.",
        )
        assert result["judge_name"] is not None

    def test_case_type_priority_number_over_scraper_id(self) -> None:
        """Case number prefix should take priority over scraper_id for case_type."""
        result = _simulate_worker_fallbacks(
            "Case No. CIVSB2501234\nMotion granted.",
            scraper_id="ca-oc-tentatives-probate",
        )
        # CIVSB -> civil, even though scraper_id says probate
        assert result["case_type"] == "civil"

    def test_case_type_priority_scraper_id_over_motion_type(self) -> None:
        """Scraper_id should take priority over motion_type for case_type."""
        # No recognizable case number prefix, but scraper_id says probate
        # and motion_type would suggest civil
        result = _simulate_worker_fallbacks(
            "Case No. 202300574258\nMotion for Summary Judgment\nGranted.",
            scraper_id="ca-oc-tentatives-probate",
        )
        assert result["case_type"] == "probate"
