"""Ingestion worker — consumes document.captured events from Redis Streams and writes
to Postgres (courts, cases, documents, rulings) and OpenSearch (tentative_rulings index)."""

from .worker import IngestionWorker

__all__ = ["IngestionWorker"]
