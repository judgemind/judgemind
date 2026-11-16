"""Which recorded ingest skips came from an extraction, and when to re-judge them (#5094).

A ``telemetry.ingest_skips`` row says a raw key holds no document on
purpose.  Some verdicts are deterministic: a Santa Clara placeholder notice
(``santa_clara_placeholder``), the governor press-release route
(``governor_bio_route``), a PDF nothing could read
(``no_extractable_text``).  Two come from an extraction instead:

- ``no_ruling_text``: every row the multimodal split found had no ruling
  text (#4714, #5015);
- ``zero_rulings_no_text``: the multimodal split found no rulings in a PDF
  with no text layer to fall back on (#4714).

An extraction can be wrong.  An LLM answering with empty cells, a truncated
response, or OCR that silently read nothing (before #5010 / #5046) produces
the same verdict as a source that really holds no rulings.  So:

- the worker never lets these verdicts replace a key's rulings (it records
  ``empty_extraction`` in ``telemetry.ingest_incomplete`` instead, unless
  the run passes ``--allow-ruling-removal``);
- each such skip row carries the rule version that produced it
  (:data:`EXTRACTION_SKIP_RULE_VERSION`) and the extractor identity (the
  LLM cache namespace: provider, model, prompt hash);
- the raw-key reconciler re-ingests a key whose skip is stale
  (:func:`stale_skip_reason`: an older rule version, or a different
  extractor than the current one, #5134), with ``--bust-llm-cache``,
  through the #5008 incomplete path and its attempt cap (reason
  ``stale_skip``).

Standard library only: the reconciler imports this module without pulling
in the worker.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: ``ingestion.ingest_skips.SKIP_NO_RULING_TEXT``.
SKIP_NO_RULING_TEXT = "no_ruling_text"
#: ``ingestion.ingest_skips.SKIP_ZERO_RULINGS_NO_TEXT``.
SKIP_ZERO_RULINGS_NO_TEXT = "zero_rulings_no_text"

#: Skip verdicts that come from an extraction, not a deterministic detector.
EXTRACTION_SKIP_REASONS = frozenset({SKIP_NO_RULING_TEXT, SKIP_ZERO_RULINGS_NO_TEXT})

#: Version of the rules that produce an extraction skip verdict.  A skip row
#: recorded under an older version (rows without one are version 1) is
#: re-judged by the reconciler.
#:
#: BUMP IT WHENEVER A CHANGE CAN TURN ONE OF THESE VERDICTS INTO RULINGS: an
#: OCR fix, a parser fix for empty cells or fused rows, a change to which
#: rows count as textless, a change to the worker branch that records the
#: verdict.  The fingerprint test
#: (``packages/scraper-framework/tests/test_skip_versions_fingerprint.py``)
#: pins the code behind the verdicts and fails until such a change either
#: bumps this version or repins with a reason the verdicts cannot change.
#:
#: A change of the multimodal model or of the per-page prompt
#: (``PDF_PER_PAGE_PROMPT``) needs no bump: it changes the extractor
#: identity (the LLM cache namespace), and the reconciler re-judges a skip
#: whose recorded extractor differs from the current one
#: (:data:`STALE_EXTRACTOR`).  A row with no recorded extractor is judged by
#: version only.
#:
#: 1: recorded before #5094, when the verdict could replace a key's rulings
#:    and OCR could silently read nothing (#5010, #5046).
#: 2: #5094.
#: 3: #5098 (#5115): the OCR tile split no longer reports a tile cut through
#:    a line of text as a blank region, so an empty OCR fallback behind
#:    ``zero_rulings_no_text`` may have been a false verdict.
EXTRACTION_SKIP_RULE_VERSION = 3

#: Detail key of the rule version on a skip row.
RULE_VERSION_KEY = "rule_version"
#: Detail key of the extractor identity on a skip row.
EXTRACTOR_KEY = "extractor"
#: Detail key of the rulings the key held when the skip was recorded.
HELD_RULINGS_KEY = "held_rulings"
#: Detail key set when ``--allow-ruling-removal`` let the skip remove them.
ALLOWED_REMOVAL_KEY = "allowed_removal"

#: Why a skip is stale (:func:`stale_skip_reason`).
STALE_RULE_VERSION = "rule_version"
STALE_REMOVED_RULINGS = "removed_rulings"
STALE_EXTRACTOR = "extractor"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def stale_skip_reason(
    reason: str | None,
    detail: Mapping[str, Any] | None,
    current_extractor: str | None = None,
) -> str | None:
    """Why a recorded skip must be re-judged, or ``None`` when it stands.

    Only an extraction verdict (:data:`EXTRACTION_SKIP_REASONS`) is ever
    stale:

    - ``rule_version``: recorded under an older rule version than
      :data:`EXTRACTION_SKIP_RULE_VERSION` (no version: 1);
    - ``extractor``: recorded by another extractor (model or per-page
      prompt) than *current_extractor*, the current
      ``LlmExtractor.pdf_cache_namespace`` (#5134).  A row with no recorded
      extractor, or a call with no current one, is judged by version only;
    - ``removed_rulings``: the skip replaced rulings the key held, without
      ``--allow-ruling-removal``.  The worker refuses that write now, so
      such a row predates the fix or a bug let it through.
    """
    if reason not in EXTRACTION_SKIP_REASONS:
        return None
    detail = detail if isinstance(detail, Mapping) else {}
    version = _as_int(detail.get(RULE_VERSION_KEY)) or 1
    if version < EXTRACTION_SKIP_RULE_VERSION:
        return STALE_RULE_VERSION
    recorded = detail.get(EXTRACTOR_KEY)
    if (
        isinstance(current_extractor, str)
        and current_extractor
        and isinstance(recorded, str)
        and recorded
        and recorded != current_extractor
    ):
        return STALE_EXTRACTOR
    held = _as_int(detail.get(HELD_RULINGS_KEY)) or 0
    if held > 0 and detail.get(ALLOWED_REMOVAL_KEY) is not True:
        return STALE_REMOVED_RULINGS
    return None
