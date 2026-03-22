"""Deterministic document ID generation for split rulings.

When a multi-case document (e.g. an OC calendar PDF containing 5 separate
rulings) is split into individual ruling records, each split ruling needs its
own unique ``document_id``.  This module provides a deterministic UUID5-based
generator so re-processing the same document always produces the same IDs.

This function is used by:
  - ``ingestion.worker._llm_split_document()``
  - ``scripts/reingest_from_s3.py``
"""

from __future__ import annotations

import uuid

# UUID namespace for generating deterministic split document IDs.
# Using NAMESPACE_URL is arbitrary but stable -- the important thing is
# consistency across re-processing runs.
_SPLIT_UUID_NAMESPACE = uuid.UUID("a3f1b2c4-d5e6-7890-abcd-ef1234567890")


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
