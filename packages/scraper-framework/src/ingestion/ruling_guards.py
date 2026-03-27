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
from typing import TYPE_CHECKING

from .extract import normalize_motion_type
from .split_ids import make_split_document_id

if TYPE_CHECKING:
    from framework.llm_schema import ExtractedRuling


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

        # --- Ruling text guard (#2057, #2078) ---
        # For multi-ruling documents: an empty/missing ruling_text becomes
        # None.  We NEVER substitute the full document text because that
        # would copy every case's text into every ruling (cross-contamination).
        # For single-ruling documents: fall back to the caller-provided
        # ``fallback_text`` (which may be the full text, an empty string,
        # or None depending on the caller).
        if ruling.ruling_text:
            ruling_text_value: str | None = ruling.ruling_text
        elif is_multi:
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
