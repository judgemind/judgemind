"""Document splitting framework for multi-case calendar pages.

Some court scrapers capture entire department calendars (all tentative rulings for
a judge on a given date) as a single document.  This module detects multi-case
documents and splits them into individual per-case rulings so the ingestion worker
can create separate ruling records.

County-specific splitting logic registers via ``register_splitter``.  The main
entry point is ``split_document(event_data)``, called by the ingestion worker
before field extraction.

Design decisions:
  - Splitting happens at ingestion time, not in the scraper, so the raw archive
    (one PDF per department) is preserved intact.
  - Each split ruling gets a deterministic synthetic document_id derived from the
    original via UUID5, ensuring idempotent re-processing.
  - When no splitter matches or the splitter returns a single result, the document
    passes through unchanged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# UUID namespace for generating deterministic split document IDs.
# Using NAMESPACE_URL is arbitrary but stable — the important thing is
# consistency across re-processing runs.
_SPLIT_UUID_NAMESPACE = uuid.UUID("a3f1b2c4-d5e6-7890-abcd-ef1234567890")


@dataclass
class SplitResult:
    """One ruling extracted from a multi-case calendar page.

    ``ruling_text`` is always present.  Other fields are populated when the
    splitter can extract them from the text; ``None`` means "let the ingestion
    worker extract via LLM / regex as usual".
    """

    ruling_text: str
    case_title: str | None = None
    case_number: str | None = None
    motion_type: str | None = None
    outcome: str | None = None


# Type alias for county-specific splitter functions.
# A splitter receives the full event_data dict and returns a list of SplitResults.
# Returning an empty list or a single-element list means "no splitting needed".
SplitterFunc = Callable[[dict[str, Any]], list[SplitResult]]

# Registry of county-specific splitters, keyed by (state, county) tuple.
_splitter_registry: dict[tuple[str, str], SplitterFunc] = {}


def register_splitter(state: str, county: str, func: SplitterFunc) -> None:
    """Register a county-specific document splitter.

    Called at module load time by county splitter modules.  The splitter function
    receives the full event_data dict and returns a list of ``SplitResult`` objects.
    An empty list or single-element list means no splitting is needed.

    Args:
        state: Two-letter state code (e.g. ``"CA"``).
        county: County name as it appears in event_data (e.g. ``"Orange"``).
        func: Splitter function.
    """
    key = (state, county)
    if key in _splitter_registry:
        logger.warning(
            "Overwriting existing splitter for %s/%s",
            state,
            county,
        )
    _splitter_registry[key] = func
    logger.debug("Registered document splitter for %s/%s", state, county)


def split_document(event_data: dict[str, Any]) -> list[SplitResult]:
    """Attempt to split a multi-case document into individual rulings.

    Looks up the county-specific splitter in the registry.  If no splitter is
    registered or the splitter returns fewer than 2 results, returns a
    single-element list containing the original ruling text (pass-through).

    Args:
        event_data: The full deserialized ``document.captured`` event dict.

    Returns:
        A list of ``SplitResult`` objects.  Length 1 means no splitting occurred.
        Length > 1 means the document was split into multiple rulings.
    """
    state = event_data.get("state", "")
    county = event_data.get("county", "")
    ruling_text = event_data.get("ruling_text")

    # No ruling text means nothing to split
    if not ruling_text:
        return [
            SplitResult(
                ruling_text=ruling_text or "",
                case_title=event_data.get("case_title"),
                case_number=event_data.get("case_number"),
                motion_type=event_data.get("motion_type"),
                outcome=event_data.get("outcome"),
            )
        ]

    key = (state, county)
    splitter = _splitter_registry.get(key)
    if splitter is None:
        # No splitter for this county — pass through
        return [
            SplitResult(
                ruling_text=ruling_text,
                case_title=event_data.get("case_title"),
                case_number=event_data.get("case_number"),
                motion_type=event_data.get("motion_type"),
                outcome=event_data.get("outcome"),
            )
        ]

    try:
        results = splitter(event_data)
    except Exception:
        logger.exception(
            "Splitter for %s/%s raised an exception — passing through unsplit",
            state,
            county,
        )
        return [
            SplitResult(
                ruling_text=ruling_text,
                case_title=event_data.get("case_title"),
                case_number=event_data.get("case_number"),
                motion_type=event_data.get("motion_type"),
                outcome=event_data.get("outcome"),
            )
        ]

    # If the splitter returned 0 or 1 results, treat as no-split
    if len(results) < 2:
        if results:
            return results
        return [
            SplitResult(
                ruling_text=ruling_text,
                case_title=event_data.get("case_title"),
                case_number=event_data.get("case_number"),
                motion_type=event_data.get("motion_type"),
                outcome=event_data.get("outcome"),
            )
        ]

    logger.info(
        "Document split into %d rulings",
        len(results),
        extra={
            "document_id": event_data.get("document_id"),
            "state": state,
            "county": county,
            "split_count": len(results),
        },
    )
    return results


def make_split_document_id(original_document_id: str, split_index: int) -> str:
    """Generate a deterministic synthetic document_id for a split ruling.

    Uses UUID5 with a fixed namespace to produce the same ID every time the
    same document is re-processed.  This ensures idempotent re-ingestion:
    the ``ON CONFLICT (document_id) DO UPDATE`` in ``insert_ruling()`` will
    upsert cleanly.

    Args:
        original_document_id: The original document UUID string from the scraper.
        split_index: Zero-based index of this ruling within the split results.

    Returns:
        A UUID string suitable for use as ``rulings.document_id``.
    """
    return str(uuid.uuid5(_SPLIT_UUID_NAMESPACE, f"{original_document_id}:{split_index}"))


def _load_county_splitters() -> None:
    """Import county-specific splitter modules to trigger registration.

    Each module calls ``register_splitter()`` at import time.  We import
    them lazily to avoid circular imports (splitter modules import from
    this module).
    """
    import ingestion.oc_splitter  # noqa: F401


_load_county_splitters()
