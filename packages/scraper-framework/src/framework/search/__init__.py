"""Full-text search indexing for OpenSearch — index mappings and consumer."""

from .indexer import IndexingConsumer
from .mapping import TENTATIVE_RULINGS_ALIAS, TENTATIVE_RULINGS_INDEX, create_index

__all__ = [
    "IndexingConsumer",
    "TENTATIVE_RULINGS_ALIAS",
    "TENTATIVE_RULINGS_INDEX",
    "create_index",
]
