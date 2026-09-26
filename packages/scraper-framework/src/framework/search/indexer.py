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
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from opensearchpy import helpers
from opensearchpy.exceptions import (
    AuthorizationException,
    ConnectionTimeout,
    NotFoundError,
    TransportError,
)
from opensearchpy.exceptions import ConnectionError as OSConnectionError

from .mapping import TENTATIVE_RULINGS_ALIAS, create_index

if TYPE_CHECKING:
    from opensearchpy import OpenSearch
    from redis import Redis

logger = logging.getLogger(__name__)

# Redis Streams consumer group name for the indexing pipeline
CONSUMER_GROUP = "indexer"
CONSUMER_NAME = "indexer-1"
STREAM_DOCUMENT_VALIDATED = "document.validated"

# Search-document fields derived from ``derived.*`` metadata (everything except
# the ruling text and ``indexed_at``).  The idempotency check compares these as
# well as ``content_hash``: a case relink or a title/date correction leaves the
# content hash unchanged but must still overwrite the stale doc (#4712).
INDEXED_METADATA_FIELDS: tuple[str, ...] = (
    "case_number",
    "court",
    "county",
    "state",
    "judge_name",
    "hearing_date",
    "motion_type",
    "outcome",
    "case_title",
    "case_type",
    "summary",
    "s3_key",
    "content_hash",
)

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:$|[T ])")


def normalize_hearing_date(value: Any) -> str | None:
    """Return *value* as a ``YYYY-MM-DD`` string, or None if it carries no date.

    Scraper events carry ``hearing_date`` as a date, a datetime, or an ISO
    string of either shape (``2026-07-28T00:00:00``).  The index stores the
    calendar date only, matching ``derived.rulings.hearing_date`` (a DATE);
    the API and web client treat ``hearingDate`` as a date string (#4712).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        match = _DATE_PREFIX_RE.match(value.strip())
        if match:
            return match.group(1)
    return None


def _is_anonymous_user_403(exc: BaseException) -> bool:
    """Return True if the exception is OpenSearch's "User: anonymous" 403.

    The 403-anonymous case is OpenSearch-FGAC's behavior when the AWS-level
    domain access policy is narrower than ``Principal: AWS=*`` AND the
    request uses HTTP Basic auth (which has no IAM principal at the AWS
    layer).  Detecting this case lets the worker emit an actionable
    diagnostic instead of a generic 403 swallow.  See issue #3771.

    Robust to either 2-arg ``AuthorizationException(403, body)`` or 3-arg
    ``AuthorizationException(403, msg, info)`` construction — opensearchpy
    uses both shapes depending on the call path.
    """
    if not isinstance(exc, AuthorizationException):
        return False
    # opensearchpy's AuthorizationException stores the response under .error
    # for 2-arg construction and .info for 3-arg construction.  Stringify both
    # because the body may be a JSON string, a dict, or already-decoded text.
    haystack_parts: list[str] = []
    for attr in ("error", "info"):
        try:
            value = getattr(exc, attr, None)
        except IndexError:
            # .info raises IndexError when args has fewer than 3 elements.
            value = None
        if value is not None:
            haystack_parts.append(str(value))
    haystack_parts.append(str(exc))
    haystack = " ".join(haystack_parts).lower()
    return "anonymous" in haystack


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
            try:
                create_index(self._os)
            except (TransportError, OSConnectionError) as exc:
                # OpenSearch is best-effort and fully derivable from derived.*.
                # A transient or auth error at startup must never prevent the
                # ingestion worker from starting.  Log a warning and continue —
                # per-document indexing errors are handled separately in
                # index_document().  See #3917.
                logger.warning(
                    "OpenSearch ensure_index failed at startup — worker will start "
                    "without index pre-check; indexing may degrade until resolved. %s",
                    exc,
                )

                # Self-diagnosing escalation for the 403-anonymous-user case:
                # when OpenSearch returns 403 with "User: anonymous" in the
                # body, log an ERROR-level diagnostic naming the two known
                # root causes so the next occurrence is self-diagnosing
                # instead of mysterious silence.  Acceptance criterion #4
                # of issue #3771.
                if _is_anonymous_user_403(exc):
                    logger.error(
                        "OpenSearch denied request as 'User: anonymous' "
                        "(403). The OpenSearch client is sending requests "
                        "that the cluster treats as having NO authenticated "
                        "AWS principal. Most likely root causes (in priority "
                        "order):\n"
                        "  1. The OPENSEARCH_USERNAME / OPENSEARCH_PASSWORD "
                        "env vars are missing or empty in this task — the "
                        "client falls back to anonymous when basic-auth is "
                        "not configured.\n"
                        "  2. The OpenSearch domain access policy does not "
                        "grant 'Principal: AWS=*' for es:* actions. With "
                        "fine-grained access control + internal user DB, "
                        "basic-auth requests are evaluated as anonymous at "
                        "the AWS layer; if the access policy is narrowed to "
                        "specific role ARNs, basic auth is rejected before "
                        "FGAC can validate the username/password. Either "
                        "widen the policy back to '*' or migrate the "
                        "client to SigV4-signed requests.\n"
                        "  3. The OpenSearch master_user password in "
                        "Secrets Manager has drifted from the actual "
                        "domain master user (rotated out of band). "
                        "See issue #3771 for the full root-cause analysis. "
                        "Underlying exception: %s",
                        exc,
                    )

    # ------------------------------------------------------------------
    # Direct indexing interface
    # ------------------------------------------------------------------

    def index_document(self, event: dict[str, Any], *, force: bool = False) -> bool:
        """Index a single document from event data.

        Expected event fields:
            document_id, s3_key, case_number, court, county, state,
            judge_name, hearing_date, content_hash, content_format

        Returns True if the document was indexed (new or updated),
        False if skipped (already indexed with the same content_hash and
        metadata) or if the OpenSearch write failed with a transient
        connection/timeout error.  ``force=True`` skips the idempotency
        check (used by ``scripts/reindex_search_from_db.py``).

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
        os_doc = self._build_os_doc(event, force=force)
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

    def delete_documents(self, document_ids: list[str]) -> int:
        """Remove documents from the index by id, best-effort.

        Used when stale split children are deleted from ``derived.documents``
        (#4700) so search stops returning rulings that no longer exist.  A
        missing id is not an error.  Any OpenSearch error is logged and
        swallowed: the index is derivable from ``derived.*``, and the
        Postgres cleanup must not fail because of it.

        Returns the number of ids OpenSearch reported as deleted.
        """
        removed = 0
        for document_id in document_ids:
            try:
                self._os.delete(index=self._index, id=document_id)
                removed += 1
            except NotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.warning(
                    "OpenSearch delete skipped for stale document %s: %s",
                    document_id,
                    exc,
                )
        return removed

    def index_batch(self, events: list[dict[str, Any]], *, force: bool = False) -> int:
        """Index a batch of documents using the OpenSearch bulk API.

        Reduces N HTTP round-trips to 1 per batch. Individual document
        errors are logged but do not prevent other documents from being
        indexed.  ``force=True`` skips the per-document idempotency check.

        Returns the count of documents successfully indexed.
        """
        actions: list[dict[str, Any]] = []
        for event in events:
            try:
                os_doc = self._build_os_doc(event, force=force)
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

    def _build_os_doc(self, event: dict[str, Any], *, force: bool = False) -> dict[str, Any] | None:
        """Build an OpenSearch document from an event, or return None to skip.

        Returns None if the document is already indexed with the same
        content_hash and the same metadata (idempotency check), unless
        ``force`` is set.
        """
        document_id = event["document_id"]
        content_hash = event.get("content_hash", "")
        s3_key = event.get("s3_key")

        metadata: dict[str, Any] = {field: event.get(field) for field in INDEXED_METADATA_FIELDS}
        metadata["hearing_date"] = normalize_hearing_date(event.get("hearing_date"))
        metadata["s3_key"] = s3_key
        metadata["content_hash"] = content_hash

        # Idempotency: skip only if already indexed with the same hash AND
        # the same metadata.  A case relink or a title/date fix keeps the
        # hash but must overwrite the stale doc (#4712).
        if not force and content_hash and self._already_indexed(document_id, metadata):
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
            **metadata,
            "ruling_text": ruling_text,
            "document_id": document_id,
            "indexed_at": datetime.now(UTC).isoformat(),
        }

    def _already_indexed(self, document_id: str, metadata: dict[str, Any]) -> bool:
        """Check if the indexed doc already carries this content_hash and metadata."""
        try:
            result = self._os.get(index=self._index, id=document_id)
            existing = result["_source"]
        except Exception:
            # Document not found or index doesn't exist — needs indexing
            return False
        return all(existing.get(field) == value for field, value in metadata.items())

    def _fetch_text(self, s3_key: str) -> str:
        """Fetch document text content from S3."""
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=s3_key)
            raw = response["Body"].read()
            return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("Failed to fetch s3://%s/%s: %s", self._bucket, s3_key, exc)
            return ""
