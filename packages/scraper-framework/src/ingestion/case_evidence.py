"""The case-number gate: does the source document refute a case number?

Every case number the worker stores passes through ``settle_case_number``
(#4927), whatever produced it: the LLM split, the per-field LLM, the regex
fallback, the enrichment engine's fuzzy correction, or a split child
inheriting its parent's number.  The gate compares a number against
*source evidence* only: text the court published (the document's extracted
text layer, its link text, a deterministic splitter's slice of it).  LLM
output (a transcription, markdown, per-field values) is never evidence: it
repeats the LLM's own misreads (#4915, #4921).

``case_number_refuted`` is the one predicate.  The relink decisions in
``db.write_split_rulings`` and ``worker._should_move_off_unsupported_case``
call it with the same evidence.

What counts as evidence:

- A document's extracted text counts only at ``MIN_DOCUMENT_TEXT_CHARS``
  non-blank characters or more (``document_evidence``).  Below that the
  document is a scanned PDF whose rulings the multimodal LLM read from page
  images, so the text cannot confirm or refute anything.  There is no
  evidence then: the gate keeps whatever number the extractors produced,
  no fuzzy correction is refused, and no ruling is moved off its case.
- A split child's own text, cut from the source by a deterministic
  splitter or a scraper, counts at any length (``own_text_evidence``): it
  is that ruling's whole source text, so a number the per-field LLM
  returns for ``See below.`` was invented (#4907).  The same holds for a
  number the per-field LLM read from a whole document's text: it saw
  nothing else.  A child a scraper split with an LLM carries the LLM's
  transcription, so it has no evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .extract import case_number_supported_by_text
from .llm_extract import is_pdf_binary

#: Below this many non-blank characters a document's extracted text cannot
#: confirm or refute a case number (a scanned PDF with no text layer, read
#: by the multimodal LLM from page images).
MIN_DOCUMENT_TEXT_CHARS = 200

#: Event key the LLM split sets on each child it dispatches: the source
#: evidence for the child's case number (``document_evidence`` of the
#: parent document).  Its presence also marks the child's ``case_number``
#: as LLM output, which the gate checks; an event without it carries a
#: scraper's or deterministic splitter's number, which the gate keeps.
CASE_NUMBER_EVIDENCE_KEY = "_case_number_evidence"

#: Event key the LLM split sets on the child of a split into one ruling
#: (the document itself) whose LLM number differs from the parent event's:
#: the parent's number, the gate's next candidate when it refutes the
#: LLM's.
INHERITED_CASE_NUMBER_KEY = "_inherited_case_number"


def _usable(text: str | None) -> bool:
    return bool(text and text.strip()) and not is_pdf_binary(text or "")


def document_evidence(document_text: str | None, *supporting: str | None) -> tuple[str, ...]:
    """Evidence from a document's extracted text, plus *supporting* source
    text (the link text).  Empty when the document text cannot settle a
    case number: no text, raw PDF bytes, or fewer than
    ``MIN_DOCUMENT_TEXT_CHARS`` non-blank characters."""
    if not _usable(document_text) or len((document_text or "").strip()) < MIN_DOCUMENT_TEXT_CHARS:
        return ()
    return (str(document_text), *(s for s in supporting if s))


def own_text_evidence(text: str | None) -> tuple[str, ...]:
    """Evidence from a split child's own source text, at any length.
    Empty when there is no text or it is raw PDF bytes."""
    return (str(text),) if _usable(text) else ()


def case_number_refuted(case_number: str | None, evidence: Iterable[str | None]) -> bool:
    """Return True when *evidence* can settle *case_number* and does not
    carry it.

    False when there is no evidence, no number, or a synthetic
    ``UNKNOWN-`` number.  Matching is ``case_number_supported_by_text``
    (punctuation-insensitive, digit runs, doubled glyphs, wrapped pieces).
    """
    texts = tuple(t for t in evidence if t)
    if not texts or not case_number or case_number.startswith("UNKNOWN-"):
        return False
    return not case_number_supported_by_text(case_number, *texts)


@dataclass(frozen=True)
class CaseNumberCandidate:
    """A case number an extractor produced, in priority order.

    ``method`` is the ``extraction_methods["case_number"]`` tag.  ``trusted``
    numbers came from the source outside the document text (a scraper's
    listing row, a deterministic splitter's CASE # cell) and are kept even
    when the text does not print them.  ``evidence``, when set, replaces the
    write's evidence for this candidate: the per-field LLM read only the
    event's own text, so that text refutes its number at any length.
    """

    case_number: str | None
    method: str | None
    trusted: bool = False
    evidence: tuple[str, ...] | None = None


def settle_case_number(
    candidates: Iterable[CaseNumberCandidate], evidence: Iterable[str | None]
) -> tuple[CaseNumberCandidate | None, list[CaseNumberCandidate]]:
    """Return the first candidate the evidence does not refute, and the
    candidates it refuted on the way.

    *candidates* may be a generator: later candidates (the regex fallback)
    are only produced when every earlier one was empty or refuted.
    ``None`` when no candidate survives; the ruling is then filed under an
    ``UNKNOWN-`` placeholder case.
    """
    texts = tuple(t for t in evidence if t)
    rejected: list[CaseNumberCandidate] = []
    for candidate in candidates:
        if not candidate.case_number:
            continue
        own = texts if candidate.evidence is None else candidate.evidence
        if candidate.trusted or not case_number_refuted(candidate.case_number, own):
            return candidate, rejected
        rejected.append(candidate)
    return None, rejected
