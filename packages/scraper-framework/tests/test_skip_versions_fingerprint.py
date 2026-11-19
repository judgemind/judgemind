"""The code behind the extraction skip verdicts is pinned to its rule version (#5134).

``no_ruling_text`` and ``zero_rulings_no_text`` come from an extraction.  The
raw-key reconciler re-judges such a skip when it was recorded under an older
``EXTRACTION_SKIP_RULE_VERSION``, or by another extractor (model or per-page
prompt; ``PDF_PER_PAGE_PROMPT`` is left out here for that reason).  #5115
changed the OCR tile split without the bump, and nothing noticed.

This test hashes the code that produces the verdicts: the OCR path behind a
scanned PDF's text fallback, the parser that leaves fused-row tails textless,
and the worker's verdict decisions.  A change to any of it fails here until
its author either

1. bumps ``EXTRACTION_SKIP_RULE_VERSION`` (and its version history in
   ``validation/skip_versions.py``), then sets ``PINNED_VERSION`` and repins
   ``PINNED_FINGERPRINTS``; or
2. repins ``PINNED_FINGERPRINTS`` and says in ``UNCHANGED_VERDICT_REASON``
   why the change cannot turn a skip verdict into rulings (a refactor, a
   log line, a change that only makes verdicts stricter).
"""

from __future__ import annotations

import inspect

import pytest
import verdict_fingerprint as vf

import framework.llm_extractor as llm_extractor
import ingestion.llm_extract as llm_extract
import ingestion.worker as worker
from validation.skip_versions import EXTRACTION_SKIP_RULE_VERSION

#: ``EXTRACTION_SKIP_RULE_VERSION`` when :data:`PINNED_FINGERPRINTS` was pinned.
PINNED_VERSION = 3

#: Why the last repin without a version bump cannot change a verdict.
#: Replace it on every such repin.
UNCHANGED_VERDICT_REASON = (
    "#5147: _read_pdf_pages loops through iter_pdf_pages, which closes each "
    "pdfplumber page after it is read (frees its cached layout).  Every page's "
    "text and scanned flag are read exactly as before, so no verdict changes."
)

#: Modules whose functions and constants a covered function pulls in.
_MODULES = ("ingestion.llm_extract", "framework.llm_extractor", "ingestion.worker")

#: The functions behind the verdicts.  ``PDF_PER_PAGE_PROMPT`` is covered by
#: the recorded extractor identity instead.
_ROOTS = (
    # The OCR path: a scanned PDF's text fallback (``zero_rulings_no_text``
    # fires when it is empty).
    llm_extract.extract_pdf_text,
    llm_extract._ocr_single_page,
    llm_extract._ocr_region,
    llm_extract._split_image,
    llm_extract._blank_cut,
    llm_extract._erase_rules,
    llm_extract._text_line_count,
    # The multimodal split's textless rows (``no_ruling_text``).
    llm_extractor._is_fused_tail,
    llm_extractor._split_fused_row_texts,
    worker._is_textless_multimodal_row,
    # The worker's verdicts.
    worker._empty_split_skip_reason,
    worker._is_zero_rulings_no_text,
)


def covered_fingerprints() -> dict[str, str]:
    """Fingerprints of every covered function and constant, now."""
    out = vf.closure_fingerprints(_ROOTS, _MODULES)
    # Every OCR constant, named or not, so a new one is covered.
    for name in dir(llm_extract):
        if name.startswith("_OCR_"):
            out[f"ingestion.llm_extract.{name}"] = vf.value_fingerprint(getattr(llm_extract, name))
    out["ingestion.worker._MULTIMODAL_OCR_FALLBACK_PAGES"] = vf.value_fingerprint(
        worker._MULTIMODAL_OCR_FALLBACK_PAGES
    )
    return out


PINNED_FINGERPRINTS: dict[str, str] = {
    "framework.llm_extractor._BARE_CONTINUANCE_RE": "97b7b2c4d87b1ba0",
    "framework.llm_extractor._BARE_DISPOSITION_RE": "93b4dace38160508",
    "framework.llm_extractor._BRACKETED_PLACEHOLDER_TITLE_RE": "db301c9e5c3e5aac",
    "framework.llm_extractor._CALENDAR_LISTING_MAX_LENGTH": "ad57366865126e55",
    "framework.llm_extractor._MOTION_TYPE_LINE_RE": "27516eb6399a8ec0",
    "framework.llm_extractor._NO_TENTATIVE_RE": "68832bcf796b9aa1",
    "framework.llm_extractor._OC_ABBREV_RE": "242192838a8b3c0b",
    "framework.llm_extractor._OFF_CALENDAR_RE": "7144981df87aa696",
    "framework.llm_extractor._PLACEHOLDER_RE": "9502a5d334b1dd5c",
    "framework.llm_extractor._PROBATE_CALENDAR_LISTING_RE": "c520989e904a3b7f",
    "framework.llm_extractor._RULING_VERB_RE": "658fabd5bb46c4a0",
    "framework.llm_extractor._STUB_DISPOSITION_LINE_RE": "fc140da11779ad05",
    "framework.llm_extractor._TENTATIVE_RULING_HEADING_RE": "cf5cb001425999ea",
    "framework.llm_extractor._TRAILING_OC_COURT_PREFIX_RE": "66219ee93d0c7cf6",
    "framework.llm_extractor._is_calendar_listing_only": "b27279291be5815a",
    "framework.llm_extractor._is_fused_tail": "1577b3d8b377ccbe",
    "framework.llm_extractor._is_real_fused_tail_entry": "f90217bb51ca9005",
    "framework.llm_extractor._is_stub_disposition_line": "de17d28f4f8a4faf",
    "framework.llm_extractor._next_entry_skips_one": "540827ad0a85794a",
    "framework.llm_extractor._plain_line": "8734622fe729220e",
    "framework.llm_extractor._split_fused_row_texts": "53ec59dfbc6b6620",
    "framework.llm_extractor._split_leading_stub_segments": "f405bd71fe26761d",
    "ingestion.llm_extract.FINISH_RECITATION": "73f57592cac40acb",
    "ingestion.llm_extract.PAGE_SEPARATOR": "5d43233dc7d0a706",
    "ingestion.llm_extract._BAR_SEPARATOR_RE": "1a406030c73af336",
    "ingestion.llm_extract._MIN_OCR_GAIN_CHARS": "f5ca38f748a1d6ea",
    "ingestion.llm_extract._OCR_CONCURRENCY": "4b227777d4dd1fc6",
    "ingestion.llm_extract._OCR_CUT_WINDOW": "a30a043314fa8929",
    "ingestion.llm_extract._OCR_EDGE_RULE_FRACTION": "14be4b45f18e0d8c",
    "ingestion.llm_extract._OCR_INK_LEVEL": "a512db2741cd2069",
    "ingestion.llm_extract._OCR_LINE_INK": "ef2d127de37b942b",
    "ingestion.llm_extract._OCR_MAX_PAGES": "ad57366865126e55",
    "ingestion.llm_extract._OCR_MIN_LINE_PX": "4b227777d4dd1fc6",
    "ingestion.llm_extract._OCR_PAGE_TIMEOUT": "26c9a96ce053a14d",
    "ingestion.llm_extract._OCR_RESOLUTION": "9ae2bdd7beedc2e7",
    "ingestion.llm_extract._OCR_RULE_INK": "72440a20f54075ac",
    "ingestion.llm_extract._OCR_RULE_MAX_PX": "2c624232cdd22177",
    "ingestion.llm_extract._OCR_SINGLE_LINE_MAX_PX": "39fa9ec190eee7b6",
    "ingestion.llm_extract._OCR_SYSTEM_PROMPT": "bd0652bf6a51b97d",
    "ingestion.llm_extract._OCR_TALL_LINE_MAX_PX": "7b69759630f869f2",
    "ingestion.llm_extract._OCR_THINKING_BUDGET": "5feceb66ffc86f38",
    "ingestion.llm_extract._OCR_TILE_MAX_DEPTH": "2c624232cdd22177",
    "ingestion.llm_extract._OCR_TILE_MIN_PX": "4a44dc15364204a8",
    "ingestion.llm_extract._OCR_TOTAL_TIMEOUT": "184999fc4112ff83",
    "ingestion.llm_extract._OCR_WORDS_SYSTEM_PROMPT": "5cb9f0b515271897",
    "ingestion.llm_extract._SCANNED_PAGE_IMAGE_SHARE": "d2cbad71ff333de6",
    "ingestion.llm_extract._SCANNED_PAGE_MAX_TEXT_CHARS": "27badc983df1780b",
    "ingestion.llm_extract._blank_cut": "1cdff058d6165783",
    "ingestion.llm_extract._erase_rules": "5b8a9223efa564e0",
    "ingestion.llm_extract._image_share": "475ea222d7e10c48",
    "ingestion.llm_extract._join_bar_separated": "1a898cc2b762ffb1",
    "ingestion.llm_extract._non_blank": "0112d46be9f393eb",
    "ingestion.llm_extract._ocr_call": "e7dd163c3e4c958e",
    "ingestion.llm_extract._ocr_pages": "3cfd0af8e644c5cf",
    "ingestion.llm_extract._ocr_pages_counted": "ebc1d763ac783570",
    "ingestion.llm_extract._ocr_region": "7437f7a117120bd8",
    "ingestion.llm_extract._ocr_single_page": "caf89172a9617fa5",
    "ingestion.llm_extract._ocr_total_timeout": "2af5503b23db66a0",
    "ingestion.llm_extract._page_list": "94e75e89e2cd78ed",
    "ingestion.llm_extract._pdf_bytes": "02657a3d51148646",
    "ingestion.llm_extract._read_pdf_pages": "2d6bf76e77b0e29b",
    "ingestion.llm_extract._render_page": "71e2be05594f3802",
    "ingestion.llm_extract._scanned_page_indexes": "5f6b374e078ff591",
    "ingestion.llm_extract._split_image": "2c95add1885ea70d",
    "ingestion.llm_extract._text_line_count": "00a2d4f23dd2e32c",
    "ingestion.llm_extract.extract_pdf_text": "cb6e3d89eb059bcf",
    "ingestion.llm_extract.ocr_pdf_document": "23a5470a23ef42b2",
    "ingestion.worker.SKIP_NO_RULING_TEXT": "3d588f1602d74914",
    "ingestion.worker._MULTIMODAL_OCR_FALLBACK_PAGES": "4a44dc15364204a8",
    "ingestion.worker._empty_split_skip_reason": "774637b3326ce33a",
    "ingestion.worker._is_textless_multimodal_row": "8440e404ba86c7b4",
    "ingestion.worker._is_zero_rulings_no_text": "c04c134a8f34d051",
}


def test_pinned_version_is_the_current_rule_version() -> None:
    assert PINNED_VERSION == EXTRACTION_SKIP_RULE_VERSION, (
        f"EXTRACTION_SKIP_RULE_VERSION is {EXTRACTION_SKIP_RULE_VERSION} but the "
        f"fingerprints were pinned under {PINNED_VERSION}: set PINNED_VERSION = "
        f"{EXTRACTION_SKIP_RULE_VERSION} and repin PINNED_FINGERPRINTS in {__file__}."
    )


def test_verdict_code_matches_the_pin() -> None:
    computed = covered_fingerprints()
    diff = vf.changed(PINNED_FINGERPRINTS, computed)
    assert not diff, (
        "The code behind the extraction skip verdicts (no_ruling_text, "
        "zero_rulings_no_text) changed:\n  "
        + "\n  ".join(diff)
        + "\n\nIf the change can turn one of these verdicts into rulings (an OCR "
        "fix, a parser fix, a change to which rows count as textless), bump "
        "EXTRACTION_SKIP_RULE_VERSION in validation/skip_versions.py, add a line "
        "to its version history, set PINNED_VERSION to match, and repin. "
        "Otherwise repin and say why in UNCHANGED_VERDICT_REASON.\n"
        f"New PINNED_FINGERPRINTS for {__file__}:\n" + vf.format_pins(computed)
    )


def test_unchanged_verdict_reason_is_given() -> None:
    assert UNCHANGED_VERDICT_REASON.strip()


def test_fingerprint_covers_the_named_ocr_functions_and_constants() -> None:
    computed = covered_fingerprints()
    for name in (
        "_ocr_single_page",
        "_ocr_region",
        "_split_image",
        "_blank_cut",
        "_erase_rules",
        "_text_line_count",
        "_OCR_LINE_INK",
        "_OCR_TILE_MAX_DEPTH",
    ):
        assert f"ingestion.llm_extract.{name}" in computed
    assert "ingestion.worker._empty_split_skip_reason" in computed
    assert "ingestion.worker._is_zero_rulings_no_text" in computed
    assert not any(name.endswith(".PDF_PER_PAGE_PROMPT") for name in computed)


def test_fingerprint_bites_on_an_ocr_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    before = covered_fingerprints()
    monkeypatch.setattr(llm_extract, "_OCR_LINE_INK", llm_extract._OCR_LINE_INK + 1)
    after = covered_fingerprints()
    assert vf.changed(before, after) == ["ingestion.llm_extract._OCR_LINE_INK"]


def test_fingerprint_bites_on_a_new_ocr_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    before = covered_fingerprints()
    monkeypatch.setattr(llm_extract, "_OCR_NEW_KNOB", 1, raising=False)
    assert vf.changed(before, covered_fingerprints()) == ["ingestion.llm_extract._OCR_NEW_KNOB"]


def test_fingerprint_bites_on_a_function_edit() -> None:
    """Functions are hashed from their module's source: a logic edit to the
    tile split changes the hash, a comment or docstring edit does not."""
    source = inspect.getsource(llm_extract._blank_cut)
    computed = covered_fingerprints()
    assert vf.source_fingerprint(source) == computed["ingestion.llm_extract._blank_cut"]
    assert "return None" in source
    edited = source.replace("return None", "return 0", 1)
    assert vf.source_fingerprint(edited) != computed["ingestion.llm_extract._blank_cut"]
    commented = source.replace("\n", "  # a comment\n", 1)
    assert vf.source_fingerprint(commented) == computed["ingestion.llm_extract._blank_cut"]


def test_fingerprint_ignores_a_test_patch_of_a_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixture that wraps a covered function (conftest's PDF page cache)
    does not change the fingerprint: it is read from the source file."""
    before = covered_fingerprints()
    monkeypatch.setattr(llm_extract, "_ocr_call", lambda *a, **k: None)
    monkeypatch.setattr(llm_extract, "_read_pdf_pages", lambda *a, **k: [])
    assert covered_fingerprints() == before
