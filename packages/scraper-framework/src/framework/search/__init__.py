"""Full-text search indexing for OpenSearch — index mappings and consumer."""

from .indexer import IndexingConsumer, search_doc_metadata
from .mapping import TENTATIVE_RULINGS_ALIAS, TENTATIVE_RULINGS_INDEX, create_index
from .ruling_doc import build_search_event, fetch_ruling_search_rows, load_search_events

__all__ = [
    "IndexingConsumer",
    "TENTATIVE_RULINGS_ALIAS",
    "TENTATIVE_RULINGS_INDEX",
    "build_search_event",
    "create_index",
    "fetch_ruling_search_rows",
    "load_search_events",
    "search_doc_metadata",
]
