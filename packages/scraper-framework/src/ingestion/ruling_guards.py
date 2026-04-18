"""Shared multi-ruling conversion logic for worker.py and reingest_from_s3.py.

Extracts the common pattern of converting ``ExtractedRuling`` objects (from
LLM extraction) into a flat, caller-agnostic representation with the
cross-contamination guard applied.  Both the live ingestion worker and the
reingest script import this module instead of maintaining parallel conversion
loops.

The cross-contamination guard (#2057, #2078) ensures that when a multi-ruling
PDF is split, each ruling's ``ruling_text`` is either its own extracted text
or ``None`` -- never the full document text (which would leak the text of
*every* case into *each* ruling record).

See #2084 for the refactoring that introduced this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .extract import normalize_motion_type
from .split_ids import make_split_document_id

if TYPE_CHECKING:
    from framework.llm_schema import ExtractedRuling


# ---------------------------------------------------------------------------
# All-NULL metadata guard (#2676)
# ---------------------------------------------------------------------------
#
# Capture-side filters (#2486) skip non-ruling PDFs (admin notices, cover
# sheets, "no tentative rulings today" pages) before they ever reach the
# ingestion worker.  This pipeline-side guard is the defense-in-depth
# fallback: if a non-ruling PDF slips past the scraper — a new county
# adds an admin-notice URL, an upstream filter regresses, a one-off
# capture backfill runs without filters — the guard here prevents a
# metadata-free row from polluting ``derived.rulings``.
#
# The helper is consulted at the worker's pre-DB-write boundary.  When it
# returns True, the worker skips the upsert and emits a
# ``data_quality.non_ruling_pdf_dropped`` telemetry event (both as a
# structured log field and as a row in ``telemetry.data_quality_metrics``).

#: Structured log ``telemetry_event`` value emitted by the worker when the
#: pipeline-side guard drops a ruling row.  Fully-qualified namespace so
#: downstream dashboards can filter without ambiguity.
NON_RULING_PDF_DROPPED_EVENT = "data_quality.non_ruling_pdf_dropped"

#: Short-form ``metric_name`` written to ``telemetry.data_quality_metrics``.
#: Matches the naming convention of peer metrics (e.g. ``ruling_empty_text``,
#: ``zero_ruling_extraction``).
NON_RULING_PDF_DROPPED_METRIC = "non_ruling_pdf_dropped"

#: The six metadata fields the guard inspects.  All six must be NULL/empty
#: for a ruling to be classified as all-null.
_METADATA_FIELDS: tuple[str, ...] = (
    "case_number",
    "case_title",
    "judge_name",
    "department",
    "motion_type",
    "outcome",
)

#: Prefix for synthetic placeholder case numbers assigned during enrichment
#: when no real case number can be recovered.  A ruling with an
#: ``UNKNOWN-...`` case_number and no other metadata is still non-ruling
#: noise — the placeholder alone must not keep the row alive.
_UNKNOWN_CASE_NUMBER_PREFIX = "UNKNOWN-"


def _get_field(ruling: Any, name: str) -> Any:
    """Return ``ruling[name]`` for dicts, or ``getattr(ruling, name, None)``
    for objects.  Missing keys/attributes are treated as ``None``."""
    if isinstance(ruling, dict):
        return ruling.get(name)
    return getattr(ruling, name, None)


def _is_nullish(value: Any) -> bool:
    """True when *value* represents NULL/empty for guard purposes.

    ``None``, empty strings, and whitespace-only strings are all treated
    as NULL.  Non-string truthy values (numbers, enums) are treated as
    populated.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def is_all_null_metadata(ruling: Any) -> bool:
    """Return True when a ruling has NO meaningful metadata.

    A ruling is "all-null metadata" when every one of the six core
    metadata fields (case_number, case_title, judge_name, department,
    motion_type, outcome) is ``None``, an empty string, or a
    whitespace-only string.

    A synthetic placeholder ``case_number`` that starts with
    ``UNKNOWN-`` is treated as equivalent to NULL for this check: the
    enrichment layer sometimes assigns such a placeholder when nothing
    real can be extracted, and a row with only that placeholder is
    still non-ruling noise.  A real case number (no ``UNKNOWN-``
    prefix) counts as populated even if the rest is NULL.

    The helper works on any object or dict that exposes the metadata
    field names — ``ConvertedRuling``, the worker's per-ruling dicts,
    ``SimpleNamespace`` used in tests, etc.  Missing fields are treated
    as NULL rather than raising.

    Parameters
    ----------
    ruling:
        An object (attribute access) or dict (key access) exposing the
        six metadata field names.

    Returns
    -------
    bool
        ``True`` if the ruling has no meaningful metadata (pipeline
        should drop it); ``False`` if at least one real field is
        populated.
    """
    for name in _METADATA_FIELDS:
        value = _get_field(ruling, name)
        if _is_nullish(value):
            continue
        # case_number has a sentinel placeholder form we treat as NULL.
        if (
            name == "case_number"
            and isinstance(value, str)
            and value.startswith(_UNKNOWN_CASE_NUMBER_PREFIX)
        ):
            continue
        # Any other populated field -> not all-null.
        return False
    return True


# ---------------------------------------------------------------------------
# Raw-HTML safety net (#2447)
# ---------------------------------------------------------------------------
#
# When LLM extraction returns zero or one ruling with empty/null
# ``ruling_text`` for a document whose text is actually raw HTML (e.g. an
# entire SD calendar page of ~60KB with 30+ cases), the single-ruling
# fallback path previously stored the full HTML as ``ruling_text``.  That
# produced 50000-char raw-HTML dumps and polluted every downstream query.
# The guard below refuses to substitute raw HTML as ``ruling_text`` — the
# caller gets ``None`` instead, which the enrichment pipeline and search
# indexer already handle safely.

_RAW_HTML_PREFIXES: tuple[str, ...] = (
    "<!doctype",
    "<!DOCTYPE",
    "<html",
    "<HTML",
    "<?xml",
)


def _looks_like_raw_html(text: str) -> bool:
    """Return True if *text* is (almost certainly) raw HTML rather than ruling text.

    Trigger conditions (cheap prefix/substring checks):

    - The text, after stripping leading whitespace, starts with a common
      HTML/XML document prefix (``<!DOCTYPE``, ``<html>``, ``<?xml``).
    - The first 2 KB contains a ``<body>`` or ``<BODY>`` opening tag —
      a strong signal that this is a full HTML document rather than the
      plain-text or lightly-formatted ruling_text we expect.

    The check is intentionally conservative.  It does not flag strings
    that merely contain ``<`` or HTML entities — those appear in real
    ruling text (e.g. party names like ``<Name> (PL)``, inline
    annotations).  Only top-of-document structural markers flip the
    guard on.
    """
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    for prefix in _RAW_HTML_PREFIXES:
        if stripped.startswith(prefix):
            return True
    # Some rebuild payloads begin with a BOM or stray whitespace we
    # already stripped, but still contain a <body> opening in the first
    # couple of KB.  Treat that as raw HTML too.
    head = stripped[:2048]
    return "<body" in head or "<BODY" in head


@dataclass(frozen=True, slots=True)
class OrphanCheckResult:
    """Result of the post-extraction orphan integrity check (#1337).

    A document is "orphan" when it produces zero ruling records after LLM
    extraction.  When ``is_orphan`` is ``True``, ``reason`` contains a short
    human-readable explanation suitable for structured log fields.
    """

    is_orphan: bool
    reason: str | None


def check_no_orphan_rulings(ruling_count: int) -> OrphanCheckResult:
    """Return a result indicating whether a document yielded zero rulings.

    A document that produces zero ruling records after LLM extraction is an
    "orphan" -- the parent document will either (a) skip insertion entirely
    (multi-case split path) or (b) fall through to single-doc processing with
    an empty/unreliable ``ruling_text``.  Either way, this is a signal that
    downstream data quality will be impacted (#1337).

    This guard is intentionally side-effect-free: it only inspects the count
    and returns a result.  Callers are responsible for logging or taking
    further action based on the returned ``OrphanCheckResult``.
    """
    if ruling_count == 0:
        return OrphanCheckResult(is_orphan=True, reason="No rulings extracted by LLM")
    return OrphanCheckResult(is_orphan=False, reason=None)


@dataclass(frozen=True, slots=True)
class ConvertedRuling:
    """One ruling converted from ``ExtractedRuling`` to flat fields.

    All fields are plain Python types (str, None, list of dicts) -- no Pydantic
    models or enums.  Callers layer on their own path-specific metadata
    (e.g. ``event_data`` spread in worker, ``extraction_methods`` in reingest).
    """

    document_id: str
    original_document_id: str
    split_index: int
    split_count: int
    is_multi: bool
    ruling_text: str | None
    case_number: str | None
    case_title: str | None
    judge_name: str | None
    department: str | None
    motion_type: str | None
    outcome: str | None
    hearing_date: str | None
    parties: list[dict[str, str]] = field(default_factory=list)
    case_type: str | None = None


def convert_extracted_rulings(
    extracted_rulings: list[ExtractedRuling],
    document_id: str,
    *,
    fallback_text: str | None = None,
    normalize_motion: bool = False,
) -> list[ConvertedRuling]:
    """Convert ``ExtractedRuling`` objects to ``ConvertedRuling`` with guards.

    This is the single source of truth for the multi-ruling
    cross-contamination guard and the ruling-to-dict conversion logic.
    Both ``worker.py`` and ``reingest_from_s3.py`` call this function.

    Parameters
    ----------
    extracted_rulings:
        LLM-extracted rulings from a single document.
    document_id:
        The original (parent) document ID.
    fallback_text:
        Text to use as ``ruling_text`` when an individual ruling has no text
        of its own.  **Only used for single-ruling documents.**  For
        multi-ruling documents, an empty/None ``ruling_text`` always becomes
        ``None`` to prevent cross-contamination (#2057, #2078).
    normalize_motion:
        If ``True``, apply ``normalize_motion_type()`` to each ruling's
        ``motion_type``.  The live worker does not normalize (downstream
        enrichment handles it); the reingest script normalizes in-place.

    Returns
    -------
    list[ConvertedRuling]
        One result per extracted ruling, in the same order.
    """
    is_multi = len(extracted_rulings) > 1
    count = len(extracted_rulings)
    results: list[ConvertedRuling] = []

    for idx, ruling in enumerate(extracted_rulings):
        # --- Document ID ---
        split_doc_id = make_split_document_id(document_id, idx) if is_multi else document_id

        # --- Parties ---
        parties_data: list[dict[str, str]] = [
            {"name": party.name, "role": party.role} for party in ruling.extracted_parties
        ]

        # --- Outcome ---
        outcome_str: str | None = ruling.outcome.value if ruling.outcome is not None else None

        # --- Motion type ---
        motion_type_str: str | None = ruling.motion_type
        if normalize_motion and motion_type_str:
            motion_type_str = normalize_motion_type(motion_type_str)

        # --- Ruling text guard (#2057, #2078, #2447) ---
        # For multi-ruling documents: an empty/missing ruling_text becomes
        # None.  We NEVER substitute the full document text because that
        # would copy every case's text into every ruling (cross-contamination).
        # For single-ruling documents: fall back to the caller-provided
        # ``fallback_text`` (which may be the full text, an empty string,
        # or None depending on the caller) — EXCEPT when the fallback is
        # raw HTML.  Raw HTML in ``ruling_text`` produced 50000-char
        # calendar-page dumps for San Diego (#2447); the guard returns
        # ``None`` instead so downstream consumers treat it as missing.
        if ruling.ruling_text:
            ruling_text_value: str | None = ruling.ruling_text
        elif is_multi:
            ruling_text_value = None
        elif fallback_text and _looks_like_raw_html(fallback_text):
            ruling_text_value = None
        else:
            ruling_text_value = fallback_text

        # --- Case type ---
        case_type_str: str | None = ruling.case_type.value if ruling.case_type is not None else None

        results.append(
            ConvertedRuling(
                document_id=split_doc_id,
                original_document_id=document_id,
                split_index=idx,
                split_count=count,
                is_multi=is_multi,
                ruling_text=ruling_text_value,
                case_number=ruling.extracted_case_number,
                case_title=ruling.extracted_case_title,
                judge_name=ruling.extracted_judge_name,
                department=ruling.department,
                motion_type=motion_type_str,
                outcome=outcome_str,
                hearing_date=ruling.hearing_date,
                parties=parties_data,
                case_type=case_type_str,
            )
        )

    return results
