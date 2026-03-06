"""Ingestion worker — consumes document.captured events from Redis Streams and writes
to Postgres (courts, cases, documents, rulings) and OpenSearch (tentative_rulings index)."""

from .worker import InfrastructureError, IngestionWorker, is_infrastructure_error

__all__ = ["InfrastructureError", "IngestionWorker", "is_infrastructure_error"]
