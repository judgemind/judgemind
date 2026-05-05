"""Parity test between ``ingestion.worker`` and ``scripts/reingest_from_s3.py``
extraction-config consumers.

This is a structural-divergence detector for the recurring class of bugs in
which ``worker.py`` adds (or removes) an ``ExtractionMethod`` guard but the
parallel reingest path is not updated, causing reingest to corrupt data the
live worker correctly handled.  The pattern has now repeated five times:

- #2490 — title-fixed-point bug (multimodal branch)
- #2501 — judge_name / department missing from ``FETCH_DOCUMENTS_QUERY``
- #2502 — multimodal ``case_title`` fallback caused fixed-point wrong titles
- #2521 — text-split branch had the same fixed-point pattern
- #4056 — ``ExtractionMethod.NONE`` not honored

After Option B (#4081) landed, both paths consume the shared
``decide_extraction_strategy`` helper from
``framework.extraction_config``.  The test now verifies:

1. **Reingest live behavior** — ``_reparse_document`` is invoked with a
   mocked ``extract_fields_llm``; the number of times it was called matches
   the expectation derived from ``get_county_extraction_config``.  This
   catches regressions where someone deletes the NONE-skip guard from
   ``reingest_from_s3.py`` and the reingest path starts running the LLM on
   ``ExtractionMethod.NONE`` counties.  This live-call check is the
   primary regression guard — it directly observes the divergence symptom.

2. **Strategy-helper consumer sentinels** — both ``ingestion/worker.py``
   and ``scripts/reingest_from_s3.py`` are read as text and asserted to
   import ``decide_extraction_strategy`` and read ``strategy.skip_llm``.
   The Option B refactor (#4081) made ``decide_extraction_strategy`` the
   single source of truth for the gate logic; deleting either side's
   call would re-open the divergence pattern.  The sentinels are kept
   intentionally narrow — they catch deletion of the helper consumer,
   not every possible behavior regression (the live-call check above is
   the contract for those).

The deliberate-revert verification mentioned in #4071's acceptance criterion
is captured by the Verifies-fail-on-revert behavior of (1) — ``patch.object``
on ``reingest.extract_fields_llm`` directly observes whether the live path
called the LLM, so removing the NONE short-circuit from
``reingest_from_s3.py`` flips the call count from ``0`` to ``1`` and the
NONE cases fail.  The (2) consumer sentinels likewise fail when either
side stops calling the helper.

When ``ExtractionMethod`` gains a new value or ``ExtractionStrategy``
gains a new field, ``decide_extraction_strategy`` is the only place that
needs to learn about it — both consumers automatically pick up the new
field.  See #4081 for the structural fix and #4071 for the original
divergence catalog.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

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

# Ensure the scraper-framework src is importable.
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

reingest = importlib.import_module("reingest_from_s3")

from framework.extraction_config import (  # noqa: E402
    ExtractionMethod,
    get_county_extraction_config,
)

# ---------------------------------------------------------------------------
# Source-file paths for the worker-side regex sentinel checks
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKER_SRC = _REPO_ROOT / "packages/scraper-framework/src/ingestion/worker.py"
_REINGEST_SRC = _REPO_ROOT / "scripts/reingest_from_s3.py"


# ---------------------------------------------------------------------------
# (worker_pattern, reingest_pattern) sentinel pairs
# ---------------------------------------------------------------------------
# Each pair is a structural guard that MUST exist on both sides.  When a new
# extraction-config consumer is added (e.g. a new ``ExtractionMethod`` value or
# a new ``CountyExtractionConfig`` field), append a new entry here.  Both
# regexes must match or both must not-match — a partial match indicates a
# divergence and fails the test.
#
# Patterns are matched in default (single-line) mode.  Use ``[^\n]*`` to
# constrain matches to one logical statement so an incidental keyword
# elsewhere in the file (e.g. in a comment) cannot satisfy the pattern.

_GUARD_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "decide_extraction_strategy_imported",
        # Worker: imports the helper from ``framework.extraction_config``.
        # The Option B refactor (#4081) made this the single source of
        # truth for the NONE / MULTIMODAL / max_output_tokens gates.
        r"from\s+framework\.extraction_config\s+import\s+decide_extraction_strategy",
        # Reingest: imports the helper alongside ExtractionMethod and
        # get_county_extraction_config (the latter is still consumed by
        # ``_full_reparse_document``, which #4081 declared out of scope).
        r"decide_extraction_strategy",
    ),
    (
        "strategy_skip_llm_consumed",
        # Worker: reads ``strategy.skip_llm`` to short-circuit the
        # per-event NONE gate (replaces the historic
        # ``is_extraction_none = True`` assignment) and to skip framework
        # extraction inside ``_llm_split_document``.
        r"strategy\.skip_llm",
        # Reingest: reads ``strategy.skip_llm`` in ``_reparse_document``
        # to short-circuit the LLM call.  This is the live regression
        # site catalogued in #4056.
        r"strategy\.skip_llm",
    ),
    (
        "strategy_max_output_tokens_consumed",
        # Worker: reads ``strategy.max_output_tokens`` for the per-field
        # LLM call (#2355) and for the multimodal extractor's per-page
        # token cap (#2369).  No more 4096 fallback constant — the
        # helper resolves the default.
        r"strategy\.max_output_tokens",
        # Reingest: reads ``strategy.max_output_tokens`` in
        # ``_reparse_document`` for the same reason.
        r"strategy\.max_output_tokens",
    ),
)


# ---------------------------------------------------------------------------
# (state, county, scraper_id) parity cases — required by issue #4071
# ---------------------------------------------------------------------------
# Each case is paired with the expected ``ExtractionMethod`` so the test can
# derive the expected ``extract_fields_llm`` call count for the reingest live
# path.  Cases come from #4071's acceptance criterion — extending the list
# is fine, removing one is not (each documents a real divergence-prone
# scraper).

_PARITY_CASES: tuple[tuple[str, str, str, ExtractionMethod | None], ...] = (
    # LA HTML — no scraper-level config, county registry has no LA entry,
    # so the framework default LLM path runs.  ``get_county_extraction_config``
    # returns ``None`` here; the worker treats ``None`` as "default LLM".
    ("CA", "Los Angeles", "ca-la-tentatives-civil", None),
    # SD calendar — scraper-level override sets NONE.  Both paths must skip
    # the LLM (#2331, #4056).
    ("CA", "San Diego", "ca-sd-calendar", ExtractionMethod.NONE),
    # Federal CourtListener — county-level NONE (#3967, #4056).  Live regression
    # site: this is the case that motivated #4056's reingest fix.
    ("Federal", "Federal", "courtlistener", ExtractionMethod.NONE),
    # Orange County — MULTIMODAL.  Worker uses multimodal extractor;
    # reingest's _reparse_document falls through to the text LLM path
    # (the multimodal model is a worker-only feature), so the call count
    # there is ≥1.  This case guards against the worker losing the
    # MULTIMODAL selector.
    ("CA", "Orange", "ca-oc-tentatives-civil", ExtractionMethod.MULTIMODAL),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc_meta(state: str, county: str, scraper_id: str) -> dict:
    """Build a minimal ``doc_meta`` dict accepted by ``_reparse_document``."""
    return {
        "document_id": str(UUID(int=1)),
        "state": state,
        "county": county,
        "court_name": f"{county} Superior Court",
        "source_url": "https://example.test/ruling",
        "captured_at": datetime(2026, 5, 1, 0, 0, 0),
        "content_hash": "deadbeef",
        "format": "html",
        # Empty fields force the missing-fields branch to run, so a non-NONE
        # path actually invokes the LLM.  NONE-config cases short-circuit
        # before the missing-fields check.
        "case_number": None,
        "case_title": None,
        "case_type": None,
        "hearing_date": None,
        "court_id": str(UUID(int=2)),
        "scraper_id": scraper_id,
        "s3_key": "test-key",
        "s3_bucket": "test-bucket",
        "stored_ruling_text": None,
    }


def _expected_llm_calls(method: ExtractionMethod | None) -> int:
    """Return the expected ``extract_fields_llm`` call count for reingest."""
    if method == ExtractionMethod.NONE:
        return 0
    # LLM, MULTIMODAL, or default (None) all hit the text LLM path in
    # reingest — the multimodal model is a worker-only feature, so reingest
    # falls through to ``extract_fields_llm`` for MULTIMODAL counties too.
    return 1


# ---------------------------------------------------------------------------
# Source-text sentinel checks — fail when either side's guard is removed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def worker_source() -> str:
    """Return the worker.py source as a single string."""
    return _WORKER_SRC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def reingest_source() -> str:
    """Return the reingest_from_s3.py source as a single string."""
    return _REINGEST_SRC.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "worker_pattern", "reingest_pattern"),
    _GUARD_PATTERNS,
    ids=[name for name, _, _ in _GUARD_PATTERNS],
)
def test_guard_pattern_present_in_both(
    worker_source: str,
    reingest_source: str,
    name: str,
    worker_pattern: str,
    reingest_pattern: str,
) -> None:
    """For each named guard, both worker.py and reingest_from_s3.py must
    contain the corresponding regex.  When one side's match disappears, the
    test fails — preventing the recurring divergence pattern (#4071)."""
    worker_match = re.search(worker_pattern, worker_source)
    reingest_match = re.search(reingest_pattern, reingest_source)

    # Both-or-neither contract.  Both-missing would still fail because the
    # guard is supposed to exist; both-present is the desired state.
    assert worker_match, (
        f"Guard '{name}' missing from worker.py — pattern {worker_pattern!r}. "
        f"If this guard was intentionally removed, also remove the "
        f"reingest pattern from _GUARD_PATTERNS in this test."
    )
    assert reingest_match, (
        f"Guard '{name}' missing from reingest_from_s3.py — pattern "
        f"{reingest_pattern!r}. If this guard was intentionally removed, "
        f"also remove the worker pattern from _GUARD_PATTERNS in this test."
    )


# ---------------------------------------------------------------------------
# Live behavior parity — counts extract_fields_llm invocations per case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "county", "scraper_id", "expected_method"),
    _PARITY_CASES,
    ids=[
        f"{state}-{county}-{scraper_id}".replace(" ", "_")
        for state, county, scraper_id, _ in _PARITY_CASES
    ],
)
def test_reingest_extract_fields_llm_call_count(
    state: str,
    county: str,
    scraper_id: str,
    expected_method: ExtractionMethod | None,
) -> None:
    """The reingest path's ``extract_fields_llm`` call count must match what
    ``get_county_extraction_config`` predicts for the (state, county,
    scraper_id) tuple.  When the NONE short-circuit is removed, the
    NONE-configured cases (San Diego calendar, Federal CourtListener)
    produce ``call_count >= 1`` instead of ``0`` — the test fails.

    This is the "deliberate revert" check from #4071's acceptance criterion:
    revert the L1042-1051 short-circuit in ``reingest_from_s3._reparse_document``
    and the ``CA-San_Diego-ca-sd-calendar`` and ``Federal-Federal-courtlistener``
    cases will fail with the recorded call count of 1 instead of 0.
    """
    # Sanity: the case's expected method matches what the live registry says.
    actual_config = get_county_extraction_config(state, county, scraper_id=scraper_id)
    actual_method = actual_config.method if actual_config else None
    assert actual_method == expected_method, (
        f"Test fixture drift: ({state}, {county}, {scraper_id}) registry "
        f"returned method={actual_method!r} but the test expected "
        f"{expected_method!r}. Update _PARITY_CASES to match the registry."
    )

    # Build the inputs and a fake LLM client.  Use simple HTML so the
    # scraper registry's auto-discovery does not try to parse a real PDF.
    raw_content = b"<html><body>Test ruling text body.</body></html>"
    doc_meta = _doc_meta(state, county, scraper_id)
    llm_client = MagicMock()

    # Mock both ``extract_fields_llm`` (so the live LLM never fires) and
    # ``_load_scraper_registry`` (to avoid the production scraper registry
    # walking the courts/ tree).  Returning ``None`` from the patched
    # ``extract_fields_llm`` triggers the "LLM returned None" branch in
    # reingest, which is fine — we only care about the call count.
    with (
        patch.object(reingest, "extract_fields_llm", return_value=None) as mock_llm,
        patch.object(reingest, "_load_scraper_registry"),
    ):
        result = reingest._reparse_document(
            raw_content=raw_content,
            scraper_id=scraper_id,
            doc_meta=doc_meta,
            llm_client=llm_client,
            llm_provider="anthropic",
            llm_model="claude-3-5-haiku",
            llm_timeout=60.0,
        )

    expected = _expected_llm_calls(expected_method)
    assert mock_llm.call_count == expected, (
        f"Reingest path called extract_fields_llm {mock_llm.call_count} "
        f"time(s) for ({state}, {county}, {scraper_id}); expected {expected}. "
        f"Configured method: {expected_method!r}. "
        f"llm_outcome={result.get('llm_outcome')!r}, "
        f"llm_skipped={result.get('llm_skipped')!r}."
    )

    # Cross-check: NONE-configured cases must record ``llm_outcome ==
    # 'skipped_none'`` so observability stays consistent.  Without this,
    # an alternative regression — e.g. someone setting llm_skipped=True
    # without flipping the outcome — would slip past the call-count check.
    if expected_method == ExtractionMethod.NONE:
        assert result["llm_skipped"] is True
        assert result["llm_outcome"] == "skipped_none"
