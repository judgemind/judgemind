"""OpenSearch index mapping for tentative rulings.

Index alias pattern: tentative_rulings_v1 -> tentative_rulings
This allows zero-downtime re-indexing by creating a new versioned index,
populating it, then swapping the alias atomically.

Usage:
    from opensearchpy import OpenSearch
    from framework.search.mapping import create_index

    client = OpenSearch(...)
    create_index(client)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opensearchpy import OpenSearch

logger = logging.getLogger(__name__)

TENTATIVE_RULINGS_INDEX = "tentative_rulings_v1"
TENTATIVE_RULINGS_ALIAS = "tentative_rulings"

TENTATIVE_RULINGS_MAPPING: dict = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "ruling_text_analyzer": {
                    "type": "custom",
                    "tokenizer": "standard",
                    "filter": ["lowercase", "stop", "snowball"],
                }
            }
        },
    },
    "mappings": {
        "properties": {
            "case_number": {
                "type": "keyword",
            },
            "court": {
                "type": "keyword",
            },
            "county": {
                "type": "keyword",
            },
            "state": {
                "type": "keyword",
            },
            "judge_name": {
                "type": "keyword",
            },
            "hearing_date": {
                "type": "date",
            },
            "ruling_text": {
                "type": "text",
                "analyzer": "ruling_text_analyzer",
            },
            "document_id": {
                "type": "keyword",
            },
            "s3_key": {
                "type": "keyword",
                "index": False,
            },
            "content_hash": {
                "type": "keyword",
                "index": False,
            },
            "indexed_at": {
                "type": "date",
            },
        }
    },
}


def create_index(
    client: OpenSearch,
    index_name: str = TENTATIVE_RULINGS_INDEX,
    alias_name: str = TENTATIVE_RULINGS_ALIAS,
) -> None:
    """Create the tentative_rulings index with mapping and alias if it does not exist.

    This is idempotent — calling it when the index already exists is a no-op.
    """
    if client.indices.exists(index=index_name):
        logger.info("Index %s already exists, skipping creation", index_name)
        return

    body = {**TENTATIVE_RULINGS_MAPPING, "aliases": {alias_name: {}}}
    client.indices.create(index=index_name, body=body)
    logger.info("Created index %s with alias %s", index_name, alias_name)


def swap_alias(
    client: OpenSearch,
    old_index: str,
    new_index: str,
    alias_name: str = TENTATIVE_RULINGS_ALIAS,
) -> None:
    """Atomically swap an alias from old_index to new_index for zero-downtime re-indexing.

    Usage for re-indexing:
        1. Create tentative_rulings_v2 with create_index(client, "tentative_rulings_v2", ...)
        2. Populate tentative_rulings_v2 with data
        3. swap_alias(client, "tentative_rulings_v1", "tentative_rulings_v2")
        4. Delete tentative_rulings_v1 when ready
    """
    client.indices.update_aliases(
        body={
            "actions": [
                {"remove": {"index": old_index, "alias": alias_name}},
                {"add": {"index": new_index, "alias": alias_name}},
            ]
        }
    )
    logger.info("Swapped alias %s: %s -> %s", alias_name, old_index, new_index)
