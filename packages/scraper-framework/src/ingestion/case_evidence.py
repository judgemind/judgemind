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
- A PDF with no text layer is a scanned PDF however long its text is: the
  text ``llm_extract.extract_pdf_text`` returns for it is the LLM's OCR
  transcription (``PdfText.from_ocr``), so it is no evidence at any length
  (``from_ocr``, #4986), and nor is any slice of it.  The worker marks such
  an event (``TEXT_FROM_OCR_KEY``) and its children inherit the mark.
- A text PDF with some scanned pages is OCR'd page by page (#5010).  Its
  text as a whole is no evidence: the text-layer pages cannot refute a
  number printed only on a scanned page.  A split child cut wholly from
  the text-layer pages (``TEXT_LAYER_KEY``) keeps its own text as evidence
  (``text_from_ocr``); one that reaches into OCR text has none.
- A split child's own text, cut from the source by a deterministic
  splitter or a scraper, counts at any length (``own_text_evidence``): it
  is that ruling's whole source text, so a number the per-field LLM
  returns for ``See below.`` was invented (#4907).  The same holds for a
  number the per-field LLM read from a whole document's text: it saw
  nothing else.  A child a scraper split with an LLM carries the LLM's
  transcription, so its own text is no evidence: the worker reads the
  page the scraper archived and gives the child that page's
  ``document_evidence``, as the worker's own LLM split does (#4976).
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

#: Event key the worker sets when the event's document text is the LLM's
#: OCR transcription of a PDF with no text layer (``PdfText.from_ocr``,
#: #4986), or a PDF's text that holds OCR text or lacks scanned pages
#: (#5010).  Split children inherit it with the rest of the event.  The text
#: of such an event, and any slice of it outside ``TEXT_LAYER_KEY``, is no
#: evidence.
TEXT_FROM_OCR_KEY = "_text_from_ocr"

#: Event key the worker sets next to ``TEXT_FROM_OCR_KEY`` when only some
#: of the PDF's pages were scanned (``PdfText.text_layer``, #5010): the
#: runs of text-layer pages around the OCR'd and unread pages.  Split
#: children inherit it.  A child whose text lies inside one of them was
#: cut from the court's own text, so its text is evidence; the document as
#: a whole, and any slice that reaches past a run, is not.
TEXT_LAYER_KEY = "_text_layer"


def _squash(text: str) -> str:
    return " ".join(text.split())


def text_from_ocr(event_data: dict, text: str | None) -> bool:
    """Whether *text*, a split child's slice of the event's document text,
    holds OCR output or reaches past the court's text layer (#4986, #5010).

    False when the event's text is all text layer.  Otherwise True, unless
    *text* lies wholly inside one run of text-layer pages
    (``TEXT_LAYER_KEY``; whitespace is ignored, since splitters normalise
    page joins): only the OCR'd parts are excluded from evidence.  The
    document as a whole is judged by ``TEXT_FROM_OCR_KEY`` alone: when it
    holds OCR text or lacks pages, its text layer cannot refute a number
    printed only on a scanned page.
    """
    if not event_data.get(TEXT_FROM_OCR_KEY):
        return False
    needle = _squash(text or "")
    if not needle:
        return True
    return not any(needle in _squash(run) for run in event_data.get(TEXT_LAYER_KEY) or ())


def _usable(text: str | None) -> bool:
    return bool(text and text.strip()) and not is_pdf_binary(text or "")


def document_evidence(
    document_text: str | None, *supporting: str | None, from_ocr: bool = False
) -> tuple[str, ...]:
    """Evidence from a document's extracted text, plus *supporting* source
    text (the link text).  Empty when the document text cannot settle a
    case number: no text, raw PDF bytes, fewer than
    ``MIN_DOCUMENT_TEXT_CHARS`` non-blank characters, or text OCR
    transcribed from page images (*from_ocr*: a scanned PDF, #4986)."""
    if (
        from_ocr
        or not _usable(document_text)
        or len((document_text or "").strip()) < MIN_DOCUMENT_TEXT_CHARS
    ):
        return ()
    return (str(document_text), *(s for s in supporting if s))


def own_text_evidence(text: str | None, *, from_ocr: bool = False) -> tuple[str, ...]:
    """Evidence from a split child's own source text, at any length.
    Empty when there is no text, it is raw PDF bytes, or it was cut from
    OCR text (*from_ocr*, #4986)."""
    return (str(text),) if _usable(text) and not from_ocr else ()


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
