"""Indexing consumer — writes validated documents to OpenSearch.

Designed to be triggered in two ways:
1. Directly via index_document() — for scripts, Lambda handlers, or batch jobs
2. Via Redis Streams consumer — reading document.validated events (when available)

The consumer fetches text content from S3, builds an OpenSearch document, and
writes it to the tentative_rulings index. On content hash change (new version
detected), the existing document is overwritten via the same document_id.

Usage (direct):
    from opensearchpy import OpenSearch
    from framework.search.indexer import IndexingConsumer

    consumer = IndexingConsumer(
        opensearch_client=OpenSearch(...),
        s3_client=boto3.client("s3"),
        bucket="judgemind-document-archive-dev",
    )
    consumer.index_document(event_data)

Usage (Redis Streams):
    consumer.run_stream_consumer(redis_client, stream="document.validated")

Usage (Lambda handler):
    handler = consumer.lambda_handler  # pass to AWS Lambda
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opensearchpy import helpers
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import ConnectionTimeout

from .mapping import TENTATIVE_RULINGS_ALIAS, create_index

if TYPE_CHECKING:
    from opensearchpy import OpenSearch
    from redis import Redis

logger = logging.getLogger(__name__)

# Redis Streams consumer group name for the indexing pipeline
CONSUMER_GROUP = "indexer"
CONSUMER_NAME = "indexer-1"
STREAM_DOCUMENT_VALIDATED = "document.validated"


class IndexingConsumer:
    """Indexes validated court documents into OpenSearch for full-text search.

    The consumer is designed to be idempotent: re-indexing the same document_id
    with an unchanged content_hash is a no-op. When the content_hash differs,
    the document is re-indexed (overwritten) in OpenSearch.
    """

    def __init__(
        self,
        opensearch_client: OpenSearch,
        s3_client: Any,
        bucket: str,
        index_name: str = TENTATIVE_RULINGS_ALIAS,
        ensure_index: bool = True,
    ) -> None:
        self._os = opensearch_client
        self._s3 = s3_client
        self._bucket = bucket
        self._index = index_name

        if ensure_index:
            create_index(self._os)

    # ------------------------------------------------------------------
    # Direct indexing interface
    # ------------------------------------------------------------------

    def index_document(self, event: dict[str, Any]) -> bool:
        """Index a single document from event data.

        Expected event fields:
            document_id, s3_key, case_number, court, county, state,
            judge_name, hearing_date, content_hash, content_format

        Returns True if the document was indexed (new or updated),
        False if skipped (same content_hash already indexed) or if the
        OpenSearch write failed with a transient connection/timeout error.

        Transient connection errors (``ConnectionTimeout`` /
        ``ConnectionError`` from ``opensearchpy``) are logged as warnings
        and swallowed — indexing is best-effort because the OpenSearch
        index is fully derivable from ``derived.*`` (see
        ``docs/specs/architecture-spec-v1.md``).  Propagating a transient
        search-indexing failure up the call chain would fail the
        document's Postgres write, which is the source-of-truth write
        and must not be gated on a flaky search backend.  See #2481.
        Non-transient errors (invalid index, 4xx data errors, auth
        failures) still propagate so real bugs are surfaced loudly.
        """
        os_doc = self._build_os_doc(event)
        if os_doc is None:
            return False

        document_id = event["document_id"]

        try:
            self._os.index(
                index=self._index,
                id=document_id,
                body=os_doc,
            )
        except (ConnectionTimeout, OSConnectionError) as exc:
            logger.warning(
                "OpenSearch indexing skipped due to transient connection error "
                "(document_id=%s, case=%s, court=%s) — document's Postgres "
                "write is unaffected; search index will self-heal on re-index. %s",
                document_id,
                os_doc.get("case_number"),
                os_doc.get("court"),
                exc,
            )
            return False

        logger.info(
            "Indexed document %s (case=%s, court=%s)",
            document_id,
            os_doc["case_number"],
            os_doc["court"],
        )
        return True

    def index_batch(self, events: list[dict[str, Any]]) -> int:
        """Index a batch of documents using the OpenSearch bulk API.

        Reduces N HTTP round-trips to 1 per batch. Individual document
        errors are logged but do not prevent other documents from being
        indexed.

        Returns the count of documents successfully indexed.
        """
        actions: list[dict[str, Any]] = []
        for event in events:
            try:
                os_doc = self._build_os_doc(event)
                if os_doc is not None:
                    actions.append(
                        {
                            "_op_type": "index",
                            "_index": self._index,
                            "_id": event["document_id"],
                            "_source": os_doc,
                        }
                    )
            except Exception as exc:
                logger.error(
                    "Failed to build document %s: %s",
                    event.get("document_id", "unknown"),
                    exc,
                )

        if not actions:
            return 0

        try:
            success, errors = helpers.bulk(
                self._os,
                actions,
                stats_only=True,
                raise_on_error=False,
            )
        except (ConnectionTimeout, OSConnectionError) as exc:
            # Same best-effort rationale as ``index_document``: the bulk
            # request hit a transient connection/timeout error before any
            # acks came back, so we treat the whole batch as unindexed
            # and move on.  OpenSearch is rebuildable from ``derived.*``
            # via re-index.  See #2481.
            logger.warning(
                "OpenSearch bulk indexing skipped due to transient connection "
                "error — %d action(s) unindexed; search index will self-heal "
                "on re-index. %s",
                len(actions),
                exc,
            )
            return 0

        if errors:
            logger.error("Bulk indexing had %d failures out of %d actions", errors, len(actions))

        skipped = len(events) - len(actions)
        logger.info("Bulk indexed %d documents (%d skipped, %d failed)", success, skipped, errors)
        return success

    # ------------------------------------------------------------------
    # Lambda handler interface
    # ------------------------------------------------------------------

    def lambda_handler(self, event: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """AWS Lambda compatible handler.

        Accepts a single document event or a batch of events under the
        "Records" key (SQS/SNS trigger pattern).
        """
        if "Records" in event:
            events = [
                json.loads(r.get("body", r.get("Sns", {}).get("Message", "{}")))
                for r in event["Records"]
            ]
        else:
            events = [event]

        indexed = self.index_batch(events)
        return {"indexed": indexed, "total": len(events)}

    # ------------------------------------------------------------------
    # Redis Streams consumer interface
    # ------------------------------------------------------------------

    def run_stream_consumer(
        self,
        redis_client: Redis,
        stream: str = STREAM_DOCUMENT_VALIDATED,
        batch_size: int = 10,
        block_ms: int = 5000,
    ) -> None:
        """Run a blocking Redis Streams consumer loop.

        Creates a consumer group if it does not exist, then reads events
        in a loop. This method blocks indefinitely; run it in a dedicated
        process or thread.

        Note: This method is designed for use when Redis Streams (#21) is
        available. Until then, use index_document() or lambda_handler()
        for direct invocation.
        """
        # Create consumer group (idempotent)
        try:
            redis_client.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
            logger.info("Created consumer group %s on stream %s", CONSUMER_GROUP, stream)
        except Exception:
            # Group already exists
            pass

        logger.info(
            "Starting stream consumer on %s (group=%s, consumer=%s)",
            stream,
            CONSUMER_GROUP,
            CONSUMER_NAME,
        )

        while True:
            try:
                messages = redis_client.xreadgroup(
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {stream: ">"},
                    count=batch_size,
                    block=block_ms,
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for msg_id, data in entries:
                        try:
                            event_data = json.loads(data.get(b"data", data.get("data", "{}")))
                            self.index_document(event_data)
                            redis_client.xack(stream, CONSUMER_GROUP, msg_id)
                        except Exception as exc:
                            logger.error("Failed to process message %s: %s", msg_id, exc)

            except KeyboardInterrupt:
                logger.info("Stream consumer stopped by user")
                break
            except Exception as exc:
                logger.error("Stream consumer error: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_os_doc(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Build an OpenSearch document from an event, or return None to skip.

        Returns None if the document is already indexed with the same
        content_hash (idempotency check).
        """
        document_id = event["document_id"]
        content_hash = event.get("content_hash", "")
        s3_key = event.get("s3_key")

        # Idempotency: skip if already indexed with the same hash
        if content_hash and self._already_indexed(document_id, content_hash):
            logger.debug(
                "Document %s already indexed with hash %s, skipping",
                document_id,
                content_hash[:12],
            )
            return None

        # Use ruling_text from the event if available (scraper already parsed it);
        # fall back to fetching raw content from S3 when it's absent.
        ruling_text = event.get("ruling_text") or (self._fetch_text(s3_key) if s3_key else "")

        return {
            "case_number": event.get("case_number"),
            "court": event.get("court"),
            "county": event.get("county"),
            "state": event.get("state"),
            "judge_name": event.get("judge_name"),
            "hearing_date": event.get("hearing_date"),
            "motion_type": event.get("motion_type"),
            "outcome": event.get("outcome"),
            "case_title": event.get("case_title"),
            "case_type": event.get("case_type"),
            "summary": event.get("summary"),
            "ruling_text": ruling_text,
            "document_id": document_id,
            "s3_key": s3_key,
            "content_hash": content_hash,
            "indexed_at": datetime.now(UTC).isoformat(),
        }

    def _already_indexed(self, document_id: str, content_hash: str) -> bool:
        """Check if a document with the same content_hash is already indexed."""
        try:
            result = self._os.get(index=self._index, id=document_id)
            existing_hash = result["_source"].get("content_hash", "")
            return existing_hash == content_hash
        except Exception:
            # Document not found or index doesn't exist — needs indexing
            return False

    def _fetch_text(self, s3_key: str) -> str:
        """Fetch document text content from S3."""
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=s3_key)
            raw = response["Body"].read()
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("Failed to fetch s3://%s/%s: %s", self._bucket, s3_key, exc)
            return ""
