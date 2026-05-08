"""Shared ``case_type`` fallback resolver for ingestion + reingest paths.

Two code paths produce a final ``case_type`` for ruling documents:

* Live ingestion — ``packages/scraper-framework/src/ingestion/worker.py``
  applies ``extract_case_type_from_*`` helpers as post-LLM fallbacks just
  before the ``Field extraction summary`` log line.
* Reparse — ``scripts/reingest_from_s3.py`` calls the same helpers inside
  ``_apply_regex_fallbacks``.

For four years the two paths each open-coded the same fallback chain in
slightly different order/structure, and any time a new helper landed it
had to be added to both — a footgun that has fired six times (#1731,
#1749, #1763, #1836, #2062 surfacing as #4263, #2406).  The hygiene
guard added in #4290 (``scripts/check-case-type-fallback-parity.py``)
catches the divergence shape, but the duplication is still the root
cause.

This module exposes one function — :func:`resolve_case_type` — that
encodes the canonical fallback chain.  Both paths import it instead of
inlining the four ``extract_case_type_from_*`` helpers, so divergence
becomes impossible by construction.

The fallback order — case_number prefix → scraper_id → motion_type →
case_title — is the order that ships in production.  See worker.py for
the historical issue references on each step.

Returns
-------
``(case_type, extraction_method)`` — ``extraction_method`` is one of
``"regex"``, ``"scraper_id"``, ``"motion_type"``, ``"title"``, or
``None`` (when no fallback matched).  When the caller passed a non-None
``case_type`` in (i.e. a prior LLM/extraction step already populated
it), this function returns ``(case_type, None)`` unchanged — callers
distinguish between "we resolved a value" and "the value was already
set by an upstream step" by inspecting the returned method.

The method strings exactly match the keys ``worker.py`` and
``_apply_regex_fallbacks`` historically wrote into
``extraction_methods["case_type"]``, so swapping in this resolver is a
behaviour-preserving refactor.
"""

from __future__ import annotations

from .extract import (
    extract_case_type_from_motion_type,
    extract_case_type_from_number,
    extract_case_type_from_scraper_id,
    extract_case_type_from_title,
)


def resolve_case_type(
    *,
    case_type: str | None,
    case_number: str | None,
    scraper_id: str | None,
    motion_type: str | None,
    case_title: str | None,
) -> tuple[str | None, str | None]:
    """Apply the regex/heuristic ``case_type`` fallback chain.

    Parameters
    ----------
    case_type : str | None
        The current ``case_type`` value (e.g. as set by LLM extraction).
        When non-None, the fallback chain is skipped and the returned
        value matches the input — the second tuple element is ``None``
        because no fallback ran.
    case_number : str | None
        Case number string.  When set and recognised by
        :func:`extract_case_type_from_number`, the resolver returns
        ``(case_type, "regex")``.
    scraper_id : str | None
        Scraper ID string.  Used when the case_number prefix doesn't
        encode the type (e.g. OC North JC PDFs lack case numbers).
        When set and recognised by
        :func:`extract_case_type_from_scraper_id`, the resolver returns
        ``(case_type, "scraper_id")``.
    motion_type : str | None
        Normalised motion type string (e.g. ``"motion_to_compel"``).
        Used as a final fallback when the case number is generic and
        the scraper_id is too (e.g. Ventura's all-digit case numbers).
        When set and recognised by
        :func:`extract_case_type_from_motion_type`, the resolver returns
        ``(case_type, "motion_type")``.
    case_title : str | None
        Case title string.  Final fallback for unambiguous probate-style
        titles (``"In the Matter of..."``, ``"Conservatorship of..."``,
        etc.).  When set and recognised by
        :func:`extract_case_type_from_title`, the resolver returns
        ``(case_type, "title")``.

    Returns
    -------
    tuple[str | None, str | None]
        ``(case_type, extraction_method)``.  ``extraction_method`` is
        ``None`` when no fallback ran — either because ``case_type`` was
        already set, or because every fallback returned ``None``.

    Notes
    -----
    The fallback order — number, scraper_id, motion_type, title —
    encodes the production behaviour.  Earlier checks have higher
    confidence (a case number prefix is a deterministic signal; a
    case title heuristic is the weakest).  Do not reorder without
    weighing the regression risk on the ``test_fallback_parity.py``
    and ``test_worker_reingest_parity.py`` suites.
    """
    if case_type is not None:
        return case_type, None

    if case_number:
        resolved = extract_case_type_from_number(case_number)
        if resolved:
            return resolved, "regex"

    if scraper_id:
        resolved = extract_case_type_from_scraper_id(scraper_id)
        if resolved:
            return resolved, "scraper_id"

    if motion_type:
        resolved = extract_case_type_from_motion_type(motion_type)
        if resolved:
            return resolved, "motion_type"

    if case_title:
        resolved = extract_case_type_from_title(case_title)
        if resolved:
            return resolved, "title"

    return None, None
