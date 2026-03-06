"""CLI entrypoint for the ingestion worker.

Usage:
    python -m ingestion

Required environment variables:
    DATABASE_URL           — PostgreSQL DSN
    REDIS_URL              — Redis URL (e.g. redis://localhost:6379)
    OPENSEARCH_URL         — OpenSearch endpoint
    JUDGEMIND_ARCHIVE_BUCKET — S3 bucket for ruling content

Optional:
    MAX_RETRIES            — Per-message retry limit (default: 3)
    OPENSEARCH_USERNAME    — OpenSearch basic auth username
    OPENSEARCH_PASSWORD    — OpenSearch basic auth password
"""

from __future__ import annotations

import logging
import os
import sys

import boto3
import redis
import structlog
from opensearchpy import OpenSearch

from .worker import IngestionWorker

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)

logger = structlog.get_logger(__name__)


def _require_env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        logger.error("Missing required environment variable", var=name)
        sys.exit(1)
    return val


def main() -> None:
    """Start the ingestion worker. Runs until SIGINT/SIGTERM."""
    pg_dsn = _require_env("DATABASE_URL")
    redis_url = _require_env("REDIS_URL")
    opensearch_url = _require_env("OPENSEARCH_URL")
    archive_bucket = _require_env("JUDGEMIND_ARCHIVE_BUCKET")
    max_retries = int(os.environ.get("MAX_RETRIES", "3"))

    redis_client = redis.Redis.from_url(redis_url, decode_responses=False)
    redis_client.ping()  # Fail fast on bad URL

    os_kwargs: dict = {"hosts": [opensearch_url]}
    os_user = os.environ.get("OPENSEARCH_USERNAME", "")
    os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
    if os_user and os_pass:
        os_kwargs["http_auth"] = (os_user, os_pass)

    opensearch_client = OpenSearch(**os_kwargs)
    s3_client = boto3.client("s3")

    worker = IngestionWorker(
        redis_client=redis_client,
        pg_dsn=pg_dsn,
        opensearch_client=opensearch_client,
        s3_client=s3_client,
        archive_bucket=archive_bucket,
        max_retries=max_retries,
    )

    logger.info("Starting ingestion worker", archive_bucket=archive_bucket)
    worker.run()


if __name__ == "__main__":
    main()
