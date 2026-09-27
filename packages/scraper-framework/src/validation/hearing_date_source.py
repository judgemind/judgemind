"""Where a ruling's hearing date came from (#4793, decision on #4755).

The ingestion pipeline carries a ``hearing_date_source`` next to every
hearing date, from the point the date is assigned to the ruling row
(``derived.rulings.hearing_date_source``) and the validation row
(``telemetry.validation_results.hearing_date_source``).

Structured sources read the date deterministically from a labelled place,
never from free text:

- ``structured_scraper`` — the capturing scraper's ``parse_document`` (a
  labelled header, the PDF filename, a listing ``<time datetime>``), or the
  CC portal envelope's listing row.
- ``structured_hook`` — the scraper's ``hearing_date_for_raw`` hook, used
  when prefix reingest / rebuild builds the event straight from S3 (#4774).
- ``structured_header`` — the CC portal calendar PDF's header (#4762).

Everything else is not structured:

- ``splitter`` — a deterministic splitter's per-entry date, parsed from the
  entry body (the #4667 Santa Clara misparse), when it differs from the
  parent's date.
- ``llm`` — the LLM split or per-field LLM extraction.
- ``regex_fallback`` — ``ingestion.extract.extract_hearing_date``.

The deterministic ``hearing_date_in_range`` rule only flags an out-of-range
structured date, within a sanity floor; the other sources keep the 180-day
rule (``validation.deterministic.check_hearing_date_in_range``).
"""

from __future__ import annotations

STRUCTURED_SCRAPER = "structured_scraper"
STRUCTURED_HOOK = "structured_hook"
STRUCTURED_HEADER = "structured_header"
SPLITTER = "splitter"
LLM = "llm"
REGEX_FALLBACK = "regex_fallback"

STRUCTURED_SOURCES = frozenset({STRUCTURED_SCRAPER, STRUCTURED_HOOK, STRUCTURED_HEADER})


def is_structured(source: str | None) -> bool:
    """Return True when *source* names a structured hearing-date source."""
    return source in STRUCTURED_SOURCES
