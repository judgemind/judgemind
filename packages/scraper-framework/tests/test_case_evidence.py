"""The case-number gate (``ingestion.case_evidence``, #4927)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ingestion.case_evidence import (
    MIN_DOCUMENT_TEXT_CHARS,
    CaseNumberCandidate,
    case_number_refuted,
    document_evidence,
    own_text_evidence,
    settle_case_number,
)

_DOC = "Tentative rulings. 1. CVRI2501831 Smith v. Jones. " + "The court has read the papers. " * 6


def test_document_evidence_needs_a_text_layer() -> None:
    assert len(_DOC.strip()) >= MIN_DOCUMENT_TEXT_CHARS
    assert document_evidence(_DOC, "link CVRI1", None) == (_DOC, "link CVRI1")
    # A scanned PDF (short text layer), raw PDF bytes, no text: no evidence,
    # and the link text alone is not enough.
    assert document_evidence("Page 1", "link CVRI1") == ()
    assert document_evidence("%PDF-1.4 " + "x" * 300) == ()
    assert document_evidence(None) == ()
    assert document_evidence("   ") == ()


def test_own_text_evidence_has_no_minimum() -> None:
    assert own_text_evidence("See below.") == ("See below.",)
    assert own_text_evidence("") == ()
    assert own_text_evidence(None) == ()
    assert own_text_evidence("%PDF-1.4 binary") == ()


@pytest.mark.parametrize(
    ("number", "evidence", "refuted"),
    [
        ("CVRI2501831", (_DOC,), False),
        # The Riverside misread (#4911): not printed.
        ("CVRI2501911", (_DOC,), True),
        # Supporting text (the link text) carries it.
        ("CVRI2501911", (_DOC, "CVRI2501911 Tentative"), False),
        # Nothing to settle it with, or nothing to settle.
        ("CVRI2501911", (), False),
        ("CVRI2501911", (None, ""), False),
        (None, (_DOC,), False),
        ("UNKNOWN-doc-1", (_DOC,), False),
        # #4907: an invented placeholder for a child whose text is "See below.".
        ("23STCV01234", ("See below.",), True),
    ],
)
def test_case_number_refuted(number: str | None, evidence: tuple, refuted: bool) -> None:
    assert case_number_refuted(number, evidence) is refuted


def test_settle_takes_first_unrefuted_candidate() -> None:
    settled, rejected = settle_case_number(
        [
            CaseNumberCandidate(None, "event"),
            CaseNumberCandidate("CVRI2501911", "llm"),
            CaseNumberCandidate("CVRI2501831", "regex"),
        ],
        (_DOC,),
    )
    assert settled == CaseNumberCandidate("CVRI2501831", "regex")
    assert [c.case_number for c in rejected] == ["CVRI2501911"]


def test_settle_refutes_every_candidate() -> None:
    settled, rejected = settle_case_number(
        [CaseNumberCandidate("CVRI2501911", "llm"), CaseNumberCandidate("CVRI2501911", "regex")],
        (_DOC,),
    )
    assert settled is None
    assert [c.method for c in rejected] == ["llm", "regex"]


def test_settle_keeps_trusted_candidate_the_text_does_not_print() -> None:
    settled, rejected = settle_case_number(
        [CaseNumberCandidate("CVRI2509999", None, trusted=True)], (_DOC,)
    )
    assert settled is not None and settled.case_number == "CVRI2509999"
    assert rejected == []


def test_settle_is_lazy() -> None:
    """Later candidates (the regex fallback) are produced only when needed."""
    produced: list[str] = []

    def candidates() -> Iterator[CaseNumberCandidate]:
        produced.append("llm")
        yield CaseNumberCandidate("CVRI2501831", "llm")
        produced.append("regex")
        yield CaseNumberCandidate("CVRI2501831", "regex")

    settled, _ = settle_case_number(candidates(), (_DOC,))
    assert settled is not None and settled.method == "llm"
    assert produced == ["llm"]


def test_settle_without_evidence_keeps_first_number() -> None:
    settled, rejected = settle_case_number([CaseNumberCandidate("CVRI2501911", "llm")], ())
    assert settled is not None and settled.case_number == "CVRI2501911"
    assert rejected == []


def test_candidate_evidence_overrides_write_evidence() -> None:
    """The per-field LLM read only the short text: it refutes the number
    even though the write has no (200+ character) evidence."""
    settled, rejected = settle_case_number(
        [CaseNumberCandidate("2025-00912345", "llm", evidence=("Page 1",))], ()
    )
    assert settled is None
    assert [c.method for c in rejected] == ["llm"]
