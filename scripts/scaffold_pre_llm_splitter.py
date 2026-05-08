#!/usr/bin/env python3
# venv: none
# permanent: true
"""scaffold_pre_llm_splitter.py — Generate the boilerplate for a new
per-county pre-LLM splitter (issue #4316).

The five existing pre-LLM splitters (San Diego HTML #2447, LA HTML
#2450, Fresno PDF #3534, Riverside PDF #3649, San Francisco PDF #4304,
Santa Clara PDF #4303) all follow the same shape: a county+format
gate, a deterministic ``_split_rulings`` helper that emits per-entry
``SplitRuling`` records, a worker function (``_try_<county>_pdf_split``
or ``_try_<county>_html_split``) that dispatches one synthetic
``_split_processed=True`` event per entry, regression tests that pin
the gate negatives + fall-through + dispatch contract + exhaustion
handling, and a registration in ``scripts/check_split_ruling_fields_propagated.py``
``_DATACLASS_SCOPE`` so the propagation guard knows about the new
dataclass.

The next candidates from the cross-county audit (#4289) — San Bernardino,
Ventura, Contra Costa, etc. — all follow the same shape with only the
county name + content_format varying.  This scaffolder takes a
``(county, format)`` pair and emits the structural code so the
contributor only has to fill in:

  1. The county-specific entry-boundary regex.
  2. The county-specific case_number shape (regex).
  3. The actual fixture-driven test bodies (the scaffolded tests pin the
     dispatch contract + gate behavior; per-county PDF/HTML parsing
     tests still require real fixtures).

Running the scaffold for a county that already has a registered splitter
is a no-op (idempotent) — re-running on the same county does not
duplicate inserts.

Usage
-----

    scripts/scaffold_pre_llm_splitter.py \
        --county "San Bernardino" \
        --format pdf \
        [--dry-run] \
        [--state ca]

``--dry-run`` prints a unified diff to stdout without writing.  Without
``--dry-run`` the script writes the files in place and exits 0.

The generated diff touches five files:

  1. ``packages/scraper-framework/src/courts/<state>/<slug>_tentatives.py``
     — adds the ``SplitRuling`` dataclass + entry-boundary regex stub +
     ``_split_rulings`` skeleton.
  2. ``packages/scraper-framework/src/ingestion/worker.py`` — adds
     ``_try_<slug>_<format>_split`` and wires it into ``_llm_split_document``
     after the existing per-county checks.
  3. ``packages/scraper-framework/tests/courts/test_<slug>_tentatives.py``
     — adds a ``Test<CamelCounty><Format>Split`` test class with
     placeholder unit tests for ``_split_rulings``.
  4. ``packages/scraper-framework/tests/test_ingestion_worker.py`` — adds
     ``_make_<slug>_event`` + ``_make_fake_<slug>_rulings`` fixtures and a
     ``Test<CamelCounty><Format>Split`` test class with the seven
     canonical cases (gate negatives, fall-through, dispatch contract,
     exhaustion handling).
  5. ``scripts/check_split_ruling_fields_propagated.py`` — adds the
     ``_DATACLASS_SCOPE`` entry so the propagation guard accepts the
     new dataclass.

Exit codes
----------

  0 — Scaffold succeeded (or was a no-op because the county is already
      registered) on a real run; or the dry-run printed a diff cleanly.
  1 — Argument validation failure (unknown format, county already
      registered with a contradictory format, etc.) or a write failure.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _scraper_framework_src(state: str) -> Path:
    return _repo_root() / "packages" / "scraper-framework" / "src" / "courts" / state


def _scraper_framework_tests_courts(state: str) -> Path:
    # Per-county tentative-ruling tests live directly under
    # ``packages/scraper-framework/tests/courts/`` (NOT in a per-state
    # subdirectory).  The state argument is currently unused but kept
    # in the signature so a future restructure to a per-state layout
    # only needs a one-line change here.
    del state
    return _repo_root() / "packages" / "scraper-framework" / "tests" / "courts"


def _worker_path() -> Path:
    return (
        _repo_root()
        / "packages"
        / "scraper-framework"
        / "src"
        / "ingestion"
        / "worker.py"
    )


def _ingestion_test_path() -> Path:
    return (
        _repo_root()
        / "packages"
        / "scraper-framework"
        / "tests"
        / "test_ingestion_worker.py"
    )


def _propagation_check_path() -> Path:
    return _repo_root() / "scripts" / "check_split_ruling_fields_propagated.py"


# ---------------------------------------------------------------------------
# Slug + name derivation
# ---------------------------------------------------------------------------


def _slug_from_county(county: str) -> str:
    """Return the short ``_<slug>_`` identifier used in worker function names.

    Mirrors the existing convention:
      San Diego       -> ``sd``
      Los Angeles     -> ``la``
      Fresno          -> ``fresno``
      Riverside       -> ``riverside``
      San Francisco   -> ``sf``
      Santa Clara     -> ``sc``
      San Bernardino  -> ``sb``
      Contra Costa    -> ``cc``
      Ventura         -> ``ventura``
      Orange          -> ``oc``

    Two-or-more-word counties take the initials of each word; single-
    word counties use the lowercased name.  The user can override this
    by passing ``--slug`` if the heuristic disagrees.
    """
    parts = county.strip().split()
    if len(parts) >= 2:
        return "".join(p[0].lower() for p in parts)
    return parts[0].lower()


def _camel_county(county: str) -> str:
    """Return ``CamelCase`` form for class names: ``San Bernardino`` -> ``SanBernardino``."""
    return "".join(p.capitalize() for p in county.strip().split())


def _module_stem(slug: str) -> str:
    return f"{slug}_tentatives"


def _split_event_log_prefix(slug: str) -> str:
    """Return the snake_case logger event-name prefix used in ``logger.info``.

    SF uses ``sf_split_fall_through``; SC uses ``santa_clara_split_fall_through``.
    The existing names disagree, so the scaffold picks the shorter
    ``<slug>_split_fall_through`` form for consistency.  Contributors
    can rename to match a per-county logging convention later.
    """
    return f"{slug}_split_fall_through"


# ---------------------------------------------------------------------------
# Code generation — county module (``ca/<slug>_tentatives.py``)
# ---------------------------------------------------------------------------


def _county_module_content(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Generate the source file ``ca/<slug>_tentatives.py``.

    The generated file is a *minimal* skeleton — just enough to make
    ``ruff check`` and ``pytest -k '<slug>'`` pass with the placeholder
    tests, while leaving prominent ``TODO(<issue>):`` markers for the
    county-specific regex and any structural decisions the contributor
    must make.
    """
    fmt_upper = fmt.upper()
    return f'''"""{county} Superior Court — Tentative Rulings Pre-LLM Splitter (#{issue_number}).

Skeleton scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).

This module contains ONLY the multi-ruling splitter — the scraper that
captures {county} {fmt_upper} documents lives in a separate module
that this splitter is wired into via the worker (#{issue_number}).

Multi-case {fmt_upper} splitting:
  Sending whole multi-case {fmt_upper} documents through the framework
  ``LlmExtractor`` lets the LLM violate rule 5b of its own prompt and
  copy the first entry's ``case_title`` (and other LLM-extracted
  fields) onto every subsequent entry — producing the
  ``all_same_case_title_cluster`` pattern flagged by the cross-county
  audit (#4289).  The ``_split_rulings`` helper below splits a multi-
  case document into per-entry ``SplitRuling`` objects using a
  county-specific entry-boundary regex; the ingestion worker hooks the
  splitter into per-document dispatch via ``_try_{slug}_{fmt}_split``
  in ``ingestion.worker`` so each entry gets its own LLM enrichment
  pass.  This mirrors the Riverside (#3649), Fresno (#3534), SF
  (#4304), and Santa Clara (#4303) patterns — same fix family, same
  shape.  Single-ruling documents (the splitter returns ``[]`` or a
  1-element list) fall through to the framework ``LlmExtractor`` path
  so the existing per-field enrichment fills in motion_type and
  outcome.

TODO(#{issue_number}): Replace ``_ENTRY_HEADER_RE`` below with the
    county-specific entry-boundary regex.  Inspect a representative
    multi-case fixture and identify the smallest reliable entry
    boundary (e.g. a ``Line N`` header, a court page header, a
    numbered list marker).

TODO(#{issue_number}): Replace ``_CASE_NUMBER_CAPTION_RE`` below with
    the county-specific case-number shape.  Restrict to the minimal
    pattern that matches in a per-entry caption block (e.g.
    ``Case Number: <pattern>``) so the regex does not match unrelated
    case-number-like substrings inside the body.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Regular expressions
# ---------------------------------------------------------------------------

# TODO(#{issue_number}): Replace with the county-specific entry-boundary
# regex.  Inspect a representative multi-case fixture and identify the
# smallest reliable entry boundary.  The regex must match the start of
# each ruling entry on its own line (``re.MULTILINE``) and tolerate
# minor whitespace variation introduced by the {fmt_upper} text
# extractor.
_ENTRY_HEADER_RE = re.compile(
    r"^\\s*TODO_REPLACE_ENTRY_BOUNDARY_PATTERN\\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# TODO(#{issue_number}): Replace with the county-specific case-number
# shape.  The default pattern matches nothing so the scaffolded module
# fails closed — the contributor must define a real pattern.
_CASE_NUMBER_CAPTION_RE = re.compile(
    r"Case\\s+Number:\\s*(TODO_REPLACE_CASE_NUMBER_PATTERN)",
    re.IGNORECASE,
)

# Minimum body length (in characters) for a split entry to be
# considered real.  The default mirrors the Santa Clara splitter
# (#4303) — short enough to admit cross-reference entries, long enough
# to skip page-footer noise and trailing index labels.
_MIN_ENTRY_BODY_LEN = 80


# ---------------------------------------------------------------------------
# SplitRuling dataclass
# ---------------------------------------------------------------------------


class SplitRuling:
    """A single ruling extracted from a multi-ruling {county} {fmt_upper}.

    Mirrors ``courts.ca.sc_tentatives.SplitRuling`` (#4303).  The splitter
    populates ``case_number`` and ``case_title`` deterministically from the
    per-entry caption headers when present, and leaves ``motion_type`` /
    ``outcome`` ``None`` so per-entry LLM enrichment runs against only the
    entry's own text — eliminating the cross-entry carry-forward window.
    """

    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "case_title",
        "motion_type",
        "outcome",
        "hearing_date",
        "department",
    )

    def __init__(
        self,
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None = None,
        motion_type: str | None = None,
        outcome: str | None = None,
        hearing_date: Any = None,
        department: str | None = None,
    ) -> None:
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.case_title = case_title
        self.motion_type = motion_type
        self.outcome = outcome
        self.hearing_date = hearing_date
        self.department = department


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split a {county} multi-ruling {fmt_upper} into per-entry ``SplitRuling`` objects.

    Returns an empty list when the text contains no entry boundaries,
    which is the expected outcome for compact summary-table documents
    and for single-ruling documents that don't follow the multi-entry
    format.  Single-element returns are also possible — the worker
    treats both cases identically (fall through to the LLM path)
    because there is no cross-entry carry-forward window with 0 or 1
    entries.

    TODO(#{issue_number}): Replace this skeleton with the county-specific
        splitter.  See ``courts.ca.sc_tentatives._split_rulings`` (#4303)
        for a representative reference implementation.
    """
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) < _MIN_ENTRY_BODY_LEN:
            continue

        case_number_match = _CASE_NUMBER_CAPTION_RE.search(body)
        case_number = case_number_match.group(1) if case_number_match else None

        rulings.append(
            SplitRuling(
                ruling_index=i + 1,
                case_number=case_number,
                ruling_text=body,
            )
        )

    return rulings


__all__ = ["SplitRuling", "_split_rulings"]


# Marker for idempotency check — do not remove.
_SCAFFOLD_MARKER = "scaffold_pre_llm_splitter:{slug}:{fmt}"
'''


def _county_module_addendum(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Generate an addendum block to append to an existing scraper module.

    Used when ``ca/<slug>_tentatives.py`` already exists (because there
    is a scraper module for the county) and only the multi-ruling
    splitter needs to be added.  The addendum carries no module-level
    docstring or imports — those are inherited from the existing module
    — but it does have a section banner so the inserted block is
    visually delimited.
    """
    fmt_upper = fmt.upper()
    return f'''

# ---------------------------------------------------------------------------
# Multi-case {fmt_upper} pre-LLM splitter (#{issue_number})
# ---------------------------------------------------------------------------
#
# Skeleton scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
# See ``courts.ca.sc_tentatives`` (#4303) for a reference implementation.
#
# TODO(#{issue_number}): Replace ``_ENTRY_HEADER_RE`` below with the
#     county-specific entry-boundary regex, and replace
#     ``_CASE_NUMBER_CAPTION_RE`` with the county-specific case-number
#     shape.  The default values match nothing so the splitter fails
#     closed until the contributor defines real patterns.

# Module-level imports already exist above; no need to re-import re or typing.

_ENTRY_HEADER_RE = re.compile(
    r"^\\s*TODO_REPLACE_ENTRY_BOUNDARY_PATTERN\\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_CASE_NUMBER_CAPTION_RE = re.compile(
    r"Case\\s+Number:\\s*(TODO_REPLACE_CASE_NUMBER_PATTERN)",
    re.IGNORECASE,
)

_MIN_ENTRY_BODY_LEN = 80


class SplitRuling:
    """A single ruling extracted from a multi-ruling {county} {fmt_upper}.

    Mirrors ``courts.ca.sc_tentatives.SplitRuling`` (#4303).
    """

    __slots__ = (
        "ruling_index",
        "case_number",
        "ruling_text",
        "case_title",
        "motion_type",
        "outcome",
        "hearing_date",
        "department",
    )

    def __init__(
        self,
        ruling_index: int,
        case_number: str | None,
        ruling_text: str,
        case_title: str | None = None,
        motion_type: str | None = None,
        outcome: str | None = None,
        hearing_date: Any = None,
        department: str | None = None,
    ) -> None:
        self.ruling_index = ruling_index
        self.case_number = case_number
        self.ruling_text = ruling_text
        self.case_title = case_title
        self.motion_type = motion_type
        self.outcome = outcome
        self.hearing_date = hearing_date
        self.department = department


def _split_rulings(text: str) -> list[SplitRuling]:
    """Split a {county} multi-ruling {fmt_upper} into per-entry ``SplitRuling`` objects.

    TODO(#{issue_number}): Replace this skeleton with the county-specific
        splitter.  See ``courts.ca.sc_tentatives._split_rulings`` (#4303)
        for a representative reference implementation.
    """
    matches = list(_ENTRY_HEADER_RE.finditer(text))
    if not matches:
        return []

    rulings: list[SplitRuling] = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if len(body) < _MIN_ENTRY_BODY_LEN:
            continue

        case_number_match = _CASE_NUMBER_CAPTION_RE.search(body)
        case_number = case_number_match.group(1) if case_number_match else None

        rulings.append(
            SplitRuling(
                ruling_index=i + 1,
                case_number=case_number,
                ruling_text=body,
            )
        )

    return rulings
'''


def _county_test_addendum(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Generate an addendum block to append to an existing per-county test file."""
    camel = _camel_county(county)
    fmt_camel = fmt.capitalize()
    fmt_upper = fmt.upper()
    return f'''


# ---------------------------------------------------------------------------
# Multi-case {fmt_upper} pre-LLM splitter tests (#{issue_number})
# ---------------------------------------------------------------------------
#
# Scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
# Real fixture tests should live in ``Test{camel}{fmt_camel}Split`` below
# — see ``test_sc_tentatives.py::TestSantaClaraPdfSplit`` for a
# representative reference.
#
# ``SplitRuling`` and ``_split_rulings`` are imported lazily inside each
# test method to avoid colliding with the test file's existing top-of-
# file imports (which may already pull from ``courts.ca.{slug}_tentatives``).
# ``pytest`` is also assumed to be already imported at the top of the
# file — this is the convention for every other per-county test file
# in the repo.


class Test{camel}{fmt_camel}Split:
    """Unit tests for ``_split_rulings`` against {county} {fmt_upper} fixtures."""

    def test_{slug}_{fmt}_split_empty_text_returns_empty(self) -> None:
        """Empty input produces no rulings."""
        from courts.ca.{slug}_tentatives import _split_rulings

        assert _split_rulings("") == []

    def test_{slug}_{fmt}_split_no_boundaries_returns_empty(self) -> None:
        """Text without entry boundaries produces no rulings (fall-through to LLM)."""
        from courts.ca.{slug}_tentatives import _split_rulings

        assert _split_rulings("Some text without entry boundaries") == []

    def test_{slug}_{fmt}_split_returns_split_ruling_instances(self) -> None:
        """All elements are ``SplitRuling`` instances."""
        from courts.ca.{slug}_tentatives import SplitRuling, _split_rulings

        for r in _split_rulings(""):
            assert isinstance(r, SplitRuling)

    @pytest.mark.xfail(
        reason=(
            "TODO(#{issue_number}): replace with a real fixture-driven test "
            "once a representative {county} {fmt_upper} fixture is captured."
        ),
        strict=False,
    )
    def test_{slug}_{fmt}_split_real_fixture_returns_multiple_rulings(self) -> None:
        """Real-fixture test placeholder — fill in once a fixture is captured."""
        raise NotImplementedError("scaffold placeholder — replace with a real fixture test")
'''


# ---------------------------------------------------------------------------
# Code generation — worker function
# ---------------------------------------------------------------------------


def _worker_function_content(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Generate the ``_try_<slug>_<fmt>_split`` function body.

    Mirrors the Santa Clara / SF / Riverside template — county+format
    gate, lazy import of ``_split_rulings``, fall-through on empty or
    single-entry results, dispatch one ``_split_processed=True``
    synthetic event per entry.
    """
    county_upper = county.upper()
    fmt_upper = fmt.upper()
    log_prefix = _split_event_log_prefix(slug)
    extraction_method = f"{slug}_{fmt}_deterministic"
    return f'''def _try_{slug}_{fmt}_split(
    event_data: dict[str, Any],
    document_id: str,
    ruling_text: str,
    dispatch: Any,
) -> bool:
    """If *ruling_text* is a {county} multi-ruling {fmt_upper}, deterministically
    split it into per-case rulings and dispatch one synthetic split event per
    case via *dispatch*.  Returns True if the split ran, False otherwise.

    This path bypasses the framework LLM extractor — see
    ``courts.ca.{slug}_tentatives._split_rulings`` for the regex-based
    splitter.  Sending the whole document through the LLM previously
    produced cross-entry contamination (#{issue_number}): the LLM
    violated rule 5b of its system prompt and copied the first
    entry's ``case_title`` / ``motion_type`` onto subsequent entries.

    Detection gate: only triggers when event is {county} county AND
    content format is ``"{fmt}"`` — avoids false-positive matches on
    other counties.

    When ``_split_rulings`` returns ``[]`` or a 1-element list, this
    function returns ``False`` so the existing LLM path handles
    enrichment.

    Mirrors the SD/LA/Fresno/Riverside/SF/SC pattern.
    """
    county = event_data.get("county") or ""
    if county.upper() != "{county_upper}":
        return False
    if event_data.get("content_format") != "{fmt}":
        return False

    # Lazy import to avoid a circular dependency between the worker and
    # the courts package at module load time.
    from courts.ca.{slug}_tentatives import _split_rulings

    split_rulings = _split_rulings(ruling_text)
    if not split_rulings:
        logger.info(
            "{log_prefix}",
            extra={{
                "document_id": document_id,
                "reason": "no_entry_headers",
                "raw_len": len(split_rulings),
                "scraper_id": event_data.get("scraper_id"),
                "extraction_method": "{extraction_method}",
            }},
        )
        return False
    if len(split_rulings) == 1:
        # Single-entry document — fall through to LLM so framework
        # extraction fills motion_type/outcome.  No carry-forward
        # window with 1 entry.
        logger.info(
            "{log_prefix}",
            extra={{
                "document_id": document_id,
                "reason": "single_ruling_{fmt}",
                "raw_len": len(split_rulings),
                "scraper_id": event_data.get("scraper_id"),
                "extraction_method": "{extraction_method}",
            }},
        )
        return False

    logger.info(
        "{county} {fmt_upper} deterministic split dispatching %d ruling(s)",
        len(split_rulings),
        extra={{
            "document_id": document_id,
            "ruling_count": len(split_rulings),
            "scraper_id": event_data.get("scraper_id"),
            "extraction_method": "{extraction_method}",
        }},
    )

    from .split_ids import make_split_document_id

    # At this point len(split_rulings) > 1 — always generate split doc IDs.
    for idx, sr in enumerate(split_rulings):
        split_doc_id = make_split_document_id(document_id, idx)
        hearing_date_value: str | None = None
        if sr.hearing_date is not None:
            hearing_date_value = (
                sr.hearing_date.date().isoformat()
                if isinstance(sr.hearing_date, datetime)
                else str(sr.hearing_date)
            )

        # ``_split_processed=True`` short-circuits the worker's per-doc
        # split-attempt path; ``_llm_extracted`` is intentionally LEFT
        # FALSE so the synthetic event flows through per-field LLM
        # enrichment and motion_type/outcome get populated for each
        # entry individually.  No cross-entry carry-forward window.
        split_event: dict[str, Any] = {{
            **event_data,
            "document_id": split_doc_id,
            "_original_document_id": document_id,
            "_split_processed": True,
            "_split_index": idx,
            "_split_count": len(split_rulings),
            "ruling_text": sr.ruling_text,
            "ruling_text_html": None,
            "case_number": sr.case_number or event_data.get("case_number"),
            "case_title": sr.case_title or event_data.get("case_title"),
            "department": sr.department or event_data.get("department"),
            "motion_type": sr.motion_type or event_data.get("motion_type"),
            "outcome": sr.outcome or event_data.get("outcome"),
            "hearing_date": hearing_date_value or event_data.get("hearing_date"),
        }}
        try:
            dispatch(split_event)
        except Exception as _exc:
            from framework.llm_enrichment import LlmEnrichmentExhaustedError

            if not isinstance(_exc, LlmEnrichmentExhaustedError):
                raise
            logger.critical(
                "per-child enrichment exhausted on {slug} {fmt_upper} split — ruling permanently lost",
                extra={{
                    "document_id": document_id,
                    "_split_index": idx,
                    "_split_count": len(split_rulings),
                    "case_number": sr.case_number or event_data.get("case_number"),
                    "error": str(_exc),
                }},
            )

    return True
'''


def _worker_dispatch_block(county: str, slug: str, fmt: str, issue_number: str) -> str:
    """Return the dispatch block to insert into ``_llm_split_document``."""
    fmt_upper = fmt.upper()
    return f"""
        # ------------------------------------------------------------------
        # {county} multi-ruling {fmt_upper} deterministic split (#{issue_number})
        # ------------------------------------------------------------------
        # Scaffolded via ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
        # See ``_try_{slug}_{fmt}_split`` above for the gate + dispatch
        # contract.  Single-entry documents fall through to the normal
        # LLM path below.
        if ruling_text and _try_{slug}_{fmt}_split(
            event_data, document_id, ruling_text, self.process_event
        ):
            return True
"""


# ---------------------------------------------------------------------------
# Code generation — per-county tests (``tests/courts/test_<slug>_tentatives.py``)
# ---------------------------------------------------------------------------


def _county_test_content(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Generate the per-county test file.

    The scaffolded tests pin the structural contract (empty input
    returns empty list, etc.) but cannot run real-fixture tests
    until the contributor provides a fixture.  Real fixture tests are
    expected to live in a ``Test<CamelCounty><Format>Split`` class —
    the scaffolder leaves a placeholder docstring and an ``xfail``
    test so the contributor sees where to add them.
    """
    camel = _camel_county(county)
    fmt_camel = fmt.capitalize()
    fmt_upper = fmt.upper()
    return f'''"""Tests for {county} {fmt_upper} pre-LLM splitter (#{issue_number}).

Scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).

Real fixture tests should live in ``Test{camel}{fmt_camel}Split``
below — see ``test_sc_tentatives.py::TestSantaClaraPdfSplit`` for a
representative reference.
"""

from __future__ import annotations

import pytest

from courts.ca.{slug}_tentatives import SplitRuling, _split_rulings


class Test{camel}{fmt_camel}Split:
    """Unit tests for ``_split_rulings`` against {county} {fmt_upper} fixtures."""

    def test_{slug}_{fmt}_split_empty_text_returns_empty(self) -> None:
        """Empty input produces no rulings."""
        assert _split_rulings("") == []

    def test_{slug}_{fmt}_split_no_boundaries_returns_empty(self) -> None:
        """Text without entry boundaries produces no rulings (fall-through to LLM)."""
        assert _split_rulings("Some text without entry boundaries") == []

    def test_{slug}_{fmt}_split_returns_split_ruling_instances(self) -> None:
        """All elements are ``SplitRuling`` instances."""
        # The scaffolded entry-boundary regex matches the literal
        # placeholder string only — once the contributor swaps in the
        # real regex, this test exercises the real splitter.
        for r in _split_rulings(""):
            assert isinstance(r, SplitRuling)

    @pytest.mark.xfail(
        reason=(
            "TODO(#{issue_number}): replace with a real fixture-driven test "
            "once a representative {county} {fmt_upper} fixture is captured."
        ),
        strict=False,
    )
    def test_{slug}_{fmt}_split_real_fixture_returns_multiple_rulings(self) -> None:
        """Real-fixture test placeholder — fill in once a fixture is captured."""
        raise NotImplementedError("scaffold placeholder — replace with a real fixture test")
'''


# ---------------------------------------------------------------------------
# Code generation — ingestion-worker test fixtures + dispatch tests
# ---------------------------------------------------------------------------


def _ingestion_test_fixtures(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Return the ``_make_<slug>_event`` and ``_make_fake_<slug>_rulings`` helpers."""
    state_slug = "ca"
    county_kebab = county.lower().replace(" ", "_")
    return f'''
def _make_{slug}_event(**overrides: object) -> dict:
    """Return a {county} {fmt.upper()}-like event payload (#{issue_number}).

    Scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
    """
    base: dict = {{
        "document_id": "aaaaaaaa-0000-0000-0000-{slug:0>12}",
        "scraper_id": "{state_slug}-{slug}-tentatives-civil",
        "state": "CA",
        "county": "{county}",
        "court": "Superior Court",
        "source_url": "https://example.invalid/{slug}/tentativerulings/0",
        "content_format": "{fmt}",
        "content_hash": "{slug}{fmt}hash",
        "s3_key": "{state_slug}/{county_kebab}/superior_court/raw/{slug}{fmt}hash.{fmt}",
        "s3_bucket": "judgemind-document-archive-dev",
        "ruling_text": "TODO_REPLACE_WITH_REPRESENTATIVE_RULING_TEXT_FOR_{slug.upper()}_{fmt.upper()}",
        "hearing_date": "2026-03-04",
        "capture_timestamp": "2026-03-03T23:00:00",
    }}
    base.update(overrides)
    return base


def _make_fake_{slug}_rulings() -> list:
    """Return 3 fake {county} {fmt.upper()} ``SplitRuling`` objects (#{issue_number}).

    Scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
    """
    from courts.ca.{slug}_tentatives import SplitRuling

    return [
        SplitRuling(
            ruling_index=i + 1,
            case_number=f"FAKE-{{i:06d}}",
            ruling_text=f"{county} ruling {{i}} body text.",
        )
        for i in range(3)
    ]
'''


def _ingestion_test_class(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str = "TBD",
) -> str:
    """Return the ``Test<CamelCounty><Format>Split`` class with the seven canonical cases."""
    camel = _camel_county(county)
    fmt_camel = fmt.capitalize()
    fmt_upper = fmt.upper()
    return f'''
class Test{camel}{fmt_camel}Split:
    """Verify the {county} {fmt_upper} deterministic split path (#{issue_number}).

    Scaffolded by ``scripts/scaffold_pre_llm_splitter.py`` (#4316).
    Mirrors ``TestSfPdfSplit`` (#4304) — pins the same seven contracts:

    1. The county+format gate skips non-{county} events.
    2. The county+format gate skips non-{fmt_upper} {county} events.
    3. Empty splitter results fall through to the LLM path.
    4. Single-entry splitter results fall through to the LLM path.
    5. Multi-entry results dispatch synthetic ``_split_processed=True``
       events without ``_llm_extracted=True`` so per-field LLM
       enrichment runs against each child individually.
    6. Non-exhaustion exceptions in the dispatch callable propagate.
    7. ``LlmEnrichmentExhaustedError`` is swallowed with a CRITICAL log.
    """

    def test_{slug}_{fmt}_split_skips_non_{slug}_county(self) -> None:
        """_try_{slug}_{fmt}_split returns False when county != {county}."""
        from ingestion.worker import _try_{slug}_{fmt}_split

        # Use a distinct county so the gate skips.
        event = _make_{slug}_event(county="Other County")
        result = _try_{slug}_{fmt}_split(
            event,
            event["document_id"],
            event["ruling_text"],
            lambda _e: None,
        )
        assert result is False

    def test_{slug}_{fmt}_split_skips_non_{fmt}_format(self) -> None:
        """_try_{slug}_{fmt}_split returns False when content_format != {fmt}."""
        from ingestion.worker import _try_{slug}_{fmt}_split

        event = _make_{slug}_event(content_format="other-format")
        result = _try_{slug}_{fmt}_split(
            event,
            event["document_id"],
            event["ruling_text"],
            lambda _e: None,
        )
        assert result is False

    def test_{slug}_{fmt}_split_falls_through_for_no_entry_headers(self) -> None:
        """_try_{slug}_{fmt}_split returns False when splitter finds no entry headers."""
        from ingestion.worker import _try_{slug}_{fmt}_split

        event = _make_{slug}_event()
        with patch("courts.ca.{slug}_tentatives._split_rulings", return_value=[]):
            result = _try_{slug}_{fmt}_split(
                event,
                event["document_id"],
                "Some text without entry headers",
                lambda _e: None,
            )
        assert result is False

    def test_{slug}_{fmt}_split_falls_through_for_single_ruling(self) -> None:
        """_try_{slug}_{fmt}_split returns False when splitter finds exactly 1 entry."""
        from courts.ca.{slug}_tentatives import SplitRuling
        from ingestion.worker import _try_{slug}_{fmt}_split

        single_ruling = [
            SplitRuling(
                ruling_index=1,
                case_number="FAKE-000000",
                ruling_text="Lone ruling text",
            )
        ]
        event = _make_{slug}_event()
        with patch("courts.ca.{slug}_tentatives._split_rulings", return_value=single_ruling):
            result = _try_{slug}_{fmt}_split(
                event,
                event["document_id"],
                event["ruling_text"],
                lambda _e: None,
            )
        assert result is False

    def test_{slug}_{fmt}_split_dispatches_with_split_metadata(self) -> None:
        """Multi-entry splitter results dispatch synthetic ``_split_processed=True`` events
        without ``_llm_extracted=True`` (so per-field LLM enrichment runs per child)."""
        from ingestion.worker import _try_{slug}_{fmt}_split

        captured: list[dict] = []

        def capture(event_data: dict) -> None:
            captured.append(event_data)

        fake_rulings = _make_fake_{slug}_rulings()
        with patch(
            "courts.ca.{slug}_tentatives._split_rulings", return_value=fake_rulings
        ):
            event = _make_{slug}_event()
            result = _try_{slug}_{fmt}_split(
                event, event["document_id"], event["ruling_text"], capture
            )

        assert result is True
        assert len(captured) == 3
        for idx, child in enumerate(captured):
            assert child["_split_processed"] is True
            assert not child.get("_llm_extracted", False), (
                f"child {{idx}} has _llm_extracted=True — would skip per-field "
                f"enrichment and lose motion_type/outcome/case_title"
            )
            assert child["_original_document_id"] == event["document_id"]
            assert child["document_id"] != event["document_id"]
            assert child["case_number"] == fake_rulings[idx].case_number
            assert child["ruling_text"] == fake_rulings[idx].ruling_text

    def test_{slug}_{fmt}_split_reraises_non_exhaustion_exception(self) -> None:
        """_try_{slug}_{fmt}_split re-raises ValueError on non-exhaustion exception."""
        fake_rulings = _make_fake_{slug}_rulings()  # >1 to pass the single-ruling guard

        def raises_value_error(event_data: dict) -> None:
            raise ValueError("non-exhaustion error")

        with patch(
            "courts.ca.{slug}_tentatives._split_rulings", return_value=fake_rulings
        ):
            event = _make_{slug}_event()
            from ingestion.worker import _try_{slug}_{fmt}_split

            with pytest.raises(ValueError, match="non-exhaustion error"):
                _try_{slug}_{fmt}_split(
                    event, event["document_id"], event["ruling_text"], raises_value_error
                )

    def test_{slug}_{fmt}_split_all_child_exhaustion_logs_and_succeeds(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All children exhausted → logged as critical, returns True (no re-raise)."""
        import logging

        fake_rulings = _make_fake_{slug}_rulings()

        def always_exhausted(event_data: dict) -> None:
            if event_data.get("_split_processed"):
                raise LlmEnrichmentExhaustedError("all exhausted")

        with (
            patch(
                "courts.ca.{slug}_tentatives._split_rulings", return_value=fake_rulings
            ),
            caplog.at_level(logging.CRITICAL, logger="ingestion.worker"),
        ):
            event = _make_{slug}_event()
            from ingestion.worker import _try_{slug}_{fmt}_split

            result = _try_{slug}_{fmt}_split(
                event, event["document_id"], event["ruling_text"], always_exhausted
            )

        assert result is True
        critical_msgs = [r for r in caplog.records if r.levelname == "CRITICAL"]
        assert len(critical_msgs) == 3
        for record in critical_msgs:
            assert "per-child enrichment exhausted" in record.getMessage()
'''


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


def _is_already_registered(slug: str, fmt: str) -> dict[str, bool]:
    """Return a per-file map of whether the scaffold has already run for ``(slug, fmt)``.

    A re-run is safe: any file already touched is left alone.  The map
    keys are short labels used in the dry-run / final summary.
    """
    worker_text = _read_or_empty(_worker_path())
    ingestion_test_text = _read_or_empty(_ingestion_test_path())
    propagation_text = _read_or_empty(_propagation_check_path())
    county_module = _scraper_framework_src("ca") / f"{slug}_tentatives.py"
    county_test = _scraper_framework_tests_courts("ca") / f"test_{slug}_tentatives.py"

    return {
        "county_module": county_module.exists(),
        "county_test": county_test.exists(),
        "worker_function": f"def _try_{slug}_{fmt}_split(" in worker_text,
        "worker_dispatch": f"_try_{slug}_{fmt}_split(" in worker_text
        and worker_text.count(f"_try_{slug}_{fmt}_split(") >= 2,
        "ingestion_test_fixtures": f"def _make_{slug}_event(" in ingestion_test_text,
        "ingestion_test_class": f"class Test{_camel_county_for_slug(slug)}{fmt.capitalize()}Split:"
        in ingestion_test_text,
        "propagation_scope": f'"SplitRuling@{slug}_tentatives"' in propagation_text,
    }


def _camel_county_for_slug(slug: str) -> str:
    """Best-effort reverse of ``_slug_from_county`` for class-name detection.

    Used only for idempotency probing — when looking at an already-
    scaffolded file we only need to detect whether the class is
    present, and the county name was already captured in the file.
    Falls back to the slug uppercase if it can't be inferred.
    """
    return slug.upper()


def _read_or_empty(p: Path) -> str:
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# File mutation — ``ingestion/worker.py``
# ---------------------------------------------------------------------------


_WORKER_FUNCTION_INSERT_ANCHOR = (
    "# Fields that LLM extraction can populate when missing from the scraper event."
)
_WORKER_DISPATCH_INSERT_ANCHOR_RE = re.compile(
    r"\n        # Build metadata from scraper-provided fields\.\n",
    re.MULTILINE,
)


def _patch_worker(
    text: str, county: str, slug: str, fmt: str, issue_number: str
) -> str:
    """Insert the worker function and dispatch block.

    The function is inserted immediately before the ``EXTRACTABLE_FIELDS``
    block (the conventional anchor used by the existing splitters).
    The dispatch block is inserted immediately before the ``Build
    metadata from scraper-provided fields.`` comment in
    ``_llm_split_document``.
    """
    func_block = _worker_function_content(county, slug, fmt, issue_number) + "\n\n"
    dispatch_block = _worker_dispatch_block(county, slug, fmt, issue_number)

    fn_signature = f"def _try_{slug}_{fmt}_split("
    if fn_signature in text:
        # Function already defined — preserve whatever the contributor /
        # prior PR landed.  This is the idempotency guarantee.
        pass
    else:
        anchor_idx = text.find(_WORKER_FUNCTION_INSERT_ANCHOR)
        if anchor_idx == -1:
            raise RuntimeError(
                f"Could not find insertion anchor in {_worker_path()}: "
                f"{_WORKER_FUNCTION_INSERT_ANCHOR!r}"
            )
        text = text[:anchor_idx] + func_block + text[anchor_idx:]

    # Dispatch block: detect by counting occurrences of the call
    # signature ``_try_<slug>_<fmt>_split(`` across the file.  When the
    # function body and the dispatch call both exist, the count is >= 2.
    # When only the function body exists (e.g. someone added the
    # function but never wired it up), the count is 1 and we still
    # need to insert the dispatch block.
    if text.count(f"_try_{slug}_{fmt}_split(") >= 2:
        return text

    match = _WORKER_DISPATCH_INSERT_ANCHOR_RE.search(text)
    if not match:
        raise RuntimeError(
            "Could not find dispatch insertion anchor "
            "('Build metadata from scraper-provided fields.') in worker.py"
        )
    insert_at = match.start()
    text = text[:insert_at] + dispatch_block + text[insert_at:]
    return text


# ---------------------------------------------------------------------------
# File mutation — ``test_ingestion_worker.py``
# ---------------------------------------------------------------------------


_INGESTION_TEST_FIXTURE_ANCHOR_RE = re.compile(
    r"\n# -+\n# AC1: First-child exhaustion does not abort siblings\n# -+\n",
    re.MULTILINE,
)
_INGESTION_TEST_CLASS_ANCHOR_RE = re.compile(
    r"\n# -+\n# Helpers used by multiple test classes\n# -+\n",
    re.MULTILINE,
)


def _patch_ingestion_tests(
    text: str, county: str, slug: str, fmt: str, issue_number: str
) -> str:
    """Insert the per-county event/fixture helpers and the dispatch test class."""
    fixtures = _ingestion_test_fixtures(county, slug, fmt, issue_number)
    test_class = _ingestion_test_class(county, slug, fmt, issue_number)

    if f"def _make_{slug}_event(" not in text:
        match = _INGESTION_TEST_FIXTURE_ANCHOR_RE.search(text)
        if not match:
            raise RuntimeError(
                "Could not find fixture insertion anchor "
                "('AC1: First-child exhaustion does not abort siblings') in test_ingestion_worker.py"
            )
        insert_at = match.start()
        text = text[:insert_at] + fixtures + text[insert_at:]

    if f"class Test{_camel_county(county)}{fmt.capitalize()}Split:" not in text:
        match = _INGESTION_TEST_CLASS_ANCHOR_RE.search(text)
        if not match:
            raise RuntimeError(
                "Could not find test-class insertion anchor "
                "('Helpers used by multiple test classes') in test_ingestion_worker.py"
            )
        insert_at = match.start()
        text = text[:insert_at] + test_class + text[insert_at:]

    return text


# ---------------------------------------------------------------------------
# File mutation — ``check_split_ruling_fields_propagated.py``
# ---------------------------------------------------------------------------


def _patch_propagation_check(text: str, slug: str, fmt: str) -> str:
    """Insert the ``SplitRuling@<slug>_tentatives`` entry into ``_DATACLASS_SCOPE``."""
    entry_key = f'"SplitRuling@{slug}_tentatives"'
    if entry_key in text:
        return text

    # Insert just before the ``# CC has no worker dispatcher today`` comment
    # (the canonical end-of-list marker in the existing dict).  If that
    # marker is missing, fall back to the closing ``}`` of ``_DATACLASS_SCOPE``.
    cc_marker_idx = text.find("    # CC has no worker dispatcher today")
    insertion = (
        f'    "SplitRuling@{slug}_tentatives": {{\n'
        f'        "worker_fn": "_try_{slug}_{fmt}_split",\n'
        f'        "reingest": True,\n'
        f"    }},\n"
    )
    if cc_marker_idx != -1:
        return text[:cc_marker_idx] + insertion + text[cc_marker_idx:]

    raise RuntimeError(
        "Could not locate insertion point in check_split_ruling_fields_propagated.py: "
        "expected '# CC has no worker dispatcher today' marker"
    )


# ---------------------------------------------------------------------------
# Diff rendering + write
# ---------------------------------------------------------------------------


def _render_diff(path: Path, before: str, after: str) -> str:
    """Return a unified diff suitable for ``--dry-run`` output."""
    rel = str(path)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=rel,
            tofile=rel,
        )
    )


def _generate_changes(
    county: str,
    slug: str,
    fmt: str,
    issue_number: str,
) -> list[tuple[Path, str, str]]:
    """Compute the (path, before, after) triples for every file the scaffold touches.

    Files where the scaffold detects no change needed (idempotent re-run)
    are returned with ``before == after`` so the diff is empty — the
    caller decides whether to write or skip.
    """
    state = "ca"
    county_module_path = _scraper_framework_src(state) / f"{slug}_tentatives.py"
    county_test_path = (
        _scraper_framework_tests_courts(state) / f"test_{slug}_tentatives.py"
    )
    worker_path = _worker_path()
    ingestion_test_path = _ingestion_test_path()
    propagation_check_path = _propagation_check_path()

    # 1. County module — three cases:
    #
    #    * Missing entirely → write the full skeleton (the standalone
    #      SplitRuling-only module).
    #    * Already exists AND has a SplitRuling class → no-op.
    #    * Already exists AND has no SplitRuling class → append the
    #      SplitRuling-only addendum so the existing scraper module
    #      survives intact and the splitter sits below it.
    county_module_before = _read_or_empty(county_module_path)
    if county_module_before == "":
        county_module_after = _county_module_content(county, slug, fmt, issue_number)
    elif "class SplitRuling" in county_module_before or re.search(
        r"\bclass\s+\w*SplitRuling\b", county_module_before
    ):
        county_module_after = county_module_before  # no-op
    else:
        county_module_after = county_module_before + _county_module_addendum(
            county, slug, fmt, issue_number
        )

    # 2. County test — three cases mirroring the source module:
    #
    #    * Missing entirely → write the full skeleton.
    #    * Exists AND has a Test<Camel><Format>Split class → no-op.
    #    * Exists without that class → append the addendum.
    county_test_before = _read_or_empty(county_test_path)
    test_class_marker = f"class Test{_camel_county(county)}{fmt.capitalize()}Split"
    if county_test_before == "":
        county_test_after = _county_test_content(county, slug, fmt, issue_number)
    elif test_class_marker in county_test_before:
        county_test_after = county_test_before  # no-op
    else:
        county_test_after = county_test_before + _county_test_addendum(
            county, slug, fmt, issue_number
        )

    # 3. Worker function + dispatch block.
    worker_before = _read_or_empty(worker_path)
    worker_after = _patch_worker(worker_before, county, slug, fmt, issue_number)

    # 4. Ingestion test fixtures + class.
    ingestion_before = _read_or_empty(ingestion_test_path)
    ingestion_after = _patch_ingestion_tests(
        ingestion_before, county, slug, fmt, issue_number
    )

    # 5. Propagation check scope entry.
    propagation_before = _read_or_empty(propagation_check_path)
    propagation_after = _patch_propagation_check(propagation_before, slug, fmt)

    return [
        (county_module_path, county_module_before, county_module_after),
        (county_test_path, county_test_before, county_test_after),
        (worker_path, worker_before, worker_after),
        (ingestion_test_path, ingestion_before, ingestion_after),
        (propagation_check_path, propagation_before, propagation_after),
    ]


def _write_changes(changes: list[tuple[Path, str, str]]) -> list[Path]:
    """Apply the (path, before, after) changes to disk.  Returns paths actually changed.

    After writing, runs ``ruff format`` against each touched file so the
    generated output matches the repo's formatting baseline.  ``ruff
    format`` is invoked via the scraper-framework venv when available
    (for files under that package); the propagation-check script under
    ``scripts/`` is left alone because the scaffolder's insertion text
    already matches ruff's expected format.

    A failure to invoke ruff (venv missing, ruff missing) is logged to
    stderr but does NOT fail the scaffold — the contributor's pre-PR
    ``ruff format`` step will normalize anything we miss.
    """
    written: list[Path] = []
    for path, before, after in changes:
        if before == after:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after, encoding="utf-8")
        written.append(path)

    if written:
        _ruff_format_touched_files(written)
    return written


def _ruff_format_touched_files(paths: list[Path]) -> None:
    """Run ``ruff format`` against scraper-framework files in *paths*.

    Best-effort — silently skips when the venv ruff is missing.
    """
    import subprocess

    framework_root = _repo_root() / "packages" / "scraper-framework"
    venv_ruff = framework_root / ".venv" / "bin" / "ruff"
    if not venv_ruff.is_file():
        return

    framework_paths = [
        p
        for p in paths
        if framework_root in p.resolve().parents or p.resolve() == framework_root
    ]
    if not framework_paths:
        return

    try:
        subprocess.run(
            [str(venv_ruff), "format", *(str(p) for p in framework_paths)],
            check=False,
            capture_output=True,
            timeout=30,  # local-only ruff format — never network-bound (#3213)
        )
    except (OSError, FileNotFoundError) as exc:
        print(
            f"# ruff format step skipped: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Scaffold a new per-county pre-LLM splitter (#4316).  "
            "Generates the SplitRuling dataclass + worker function + tests + "
            "propagation-check entry for one (county, format) pair."
        ),
    )
    parser.add_argument(
        "--county",
        required=True,
        help='County name (e.g. "San Bernardino", "Ventura", "Contra Costa").',
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=("html", "pdf"),
        help="Content format for the new splitter (html or pdf).",
    )
    parser.add_argument(
        "--state",
        default="ca",
        help="State sub-directory under courts/ (default: ca).",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help=(
            "Override the auto-derived slug.  By default the scaffolder "
            "derives the slug from the county name "
            "(e.g. 'San Bernardino' -> 'sb', 'Ventura' -> 'ventura'). "
            "Pass this flag if the heuristic disagrees with the existing "
            "convention for your county."
        ),
    )
    parser.add_argument(
        "--issue",
        default="TBD",
        help="Tracking issue number for the new splitter (e.g. 4317).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a unified diff without writing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    county = args.county.strip()
    if not county:
        print("ERROR: --county must be a non-empty string.", file=sys.stderr)
        return 1

    slug = args.slug.strip() if args.slug else _slug_from_county(county)
    if not re.match(r"^[a-z][a-z0-9_]*$", slug):
        print(
            f"ERROR: derived slug {slug!r} is not a valid Python identifier — "
            f"pass --slug explicitly.",
            file=sys.stderr,
        )
        return 1

    fmt = args.format
    issue_number = args.issue

    try:
        changes = _generate_changes(county, slug, fmt, issue_number)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        any_diff = False
        for path, before, after in changes:
            if before == after:
                continue
            diff = _render_diff(path, before, after)
            sys.stdout.write(diff)
            any_diff = True
        if not any_diff:
            sys.stderr.write(
                f"# No changes needed — splitter for {county} ({fmt}) is already registered.\n"
            )
        return 0

    written = _write_changes(changes)
    if not written:
        print(
            f"# No changes needed — splitter for {county} ({fmt}) is already registered."
        )
        return 0

    print(f"# Scaffolded {county} ({fmt}) splitter.  Wrote {len(written)} file(s):")
    for p in written:
        print(f"  {p}")
    print()
    print("Next steps:")
    print(
        f"  1. Replace TODO_REPLACE_ENTRY_BOUNDARY_PATTERN in "
        f"packages/scraper-framework/src/courts/{args.state}/{slug}_tentatives.py."
    )
    print("  2. Replace TODO_REPLACE_CASE_NUMBER_PATTERN in the same file.")
    print(
        f"  3. Add a real fixture and replace the xfail in "
        f"packages/scraper-framework/tests/courts/test_{slug}_tentatives.py."
    )
    print(
        "  4. Replace TODO_REPLACE_WITH_REPRESENTATIVE_RULING_TEXT in "
        "packages/scraper-framework/tests/test_ingestion_worker.py "
        "with realistic text once a fixture is captured."
    )
    print(
        "  5. Run from packages/scraper-framework: "
        ".venv/bin/ruff check src tests && .venv/bin/ruff format src tests "
        "&& .venv/bin/pytest tests/ -k '" + slug + "'"
    )
    print("  6. From the repo root: scripts/check-split-ruling-fields-propagated.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
