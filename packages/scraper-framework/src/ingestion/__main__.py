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
    OPENSEARCH_USERNAME    — OpenSearch basic-auth username (local-dev fallback)
    OPENSEARCH_PASSWORD    — OpenSearch basic-auth password (local-dev fallback)

OpenSearch auth: SigV4-signed requests (from the ambient AWS credential chain)
are the preferred, deployed path; ``OPENSEARCH_USERNAME``/``OPENSEARCH_PASSWORD``
are a local-dev basic-auth fallback.  See framework.opensearch_client and #4040.
"""

from __future__ import annotations

import os
import sys

import redis
import structlog

from framework.logging import configure_structlog
from framework.opensearch_client import make_opensearch_client
from framework.s3_cache import make_s3_client

from .worker import InfrastructureError, IngestionWorker

# Early-flush print before structlog is configured so that any pre-logging
# failures (import errors, configure_structlog crash) are visible in CloudWatch
# as plain text rather than silence.  This is intentionally a bare print()
# because structlog is not yet configured.  See #3917.
print("ingestion-worker starting", flush=True)

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

        # SigV4-preferred client with local-dev basic-auth fallback; keeps the
        # 30s timeout + 3 retries that make rebuilds self-healing under load
        # (#2481).  See framework.opensearch_client (#4040).
        opensearch_client = make_opensearch_client(opensearch_url)
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
