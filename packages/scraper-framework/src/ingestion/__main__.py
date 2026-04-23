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

import os
import sys

import redis
import structlog
from opensearchpy import OpenSearch

from framework.logging import configure_structlog
from framework.s3_cache import make_s3_client

from .worker import InfrastructureError, IngestionWorker

# Configure structlog for its own loggers (structlog.get_logger()) AND route
# standard-library logging (logging.getLogger()) through structlog.
# json=True forces JSON output regardless of terminal — the ingestion worker
# always runs in ECS where CloudWatch needs structured JSON.
# stdlib_bridge=True installs a ProcessorFormatter on the root stdlib logger.
configure_structlog(json=True, stdlib_bridge=True)

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

    try:
        redis_client = redis.Redis.from_url(redis_url, decode_responses=False)
        redis_client.ping()  # Fail fast on bad URL

        # timeout/max_retries/retry_on_timeout: opensearchpy defaults to a
        # 10s read_timeout and no retries, which produces sporadic
        # ``ConnectionTimeout`` failures during rebuilds under load.  Bumping
        # the timeout to 30s and enabling retries makes transient network
        # blips self-healing; ``IndexingConsumer`` also swallows terminal
        # timeouts as a warning so a Postgres write never gates on a flaky
        # search backend.  See #2481.
        os_kwargs: dict = {
            "hosts": [opensearch_url],
            "timeout": 30,
            "max_retries": 3,
            "retry_on_timeout": True,
        }
        os_user = os.environ.get("OPENSEARCH_USERNAME", "")
        os_pass = os.environ.get("OPENSEARCH_PASSWORD", "")
        if os_user and os_pass:
            os_kwargs["http_auth"] = (os_user, os_pass)

        opensearch_client = OpenSearch(**os_kwargs)
        s3_client = make_s3_client()

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
    except InfrastructureError as exc:
        logger.critical(
            "Infrastructure error — exiting for restart",
            error=str(exc),
            cause=str(exc.__cause__),
        )
        sys.exit(1)
    except Exception as exc:
        logger.critical("Unhandled exception — exiting", error=str(exc), exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
