"""S3 archival utility — immutable raw document storage per Architecture Spec §4.4."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError
from botocore.exceptions import ConnectionError as BotoConnectionError

from .hashing import sha256_hex
from .models import CapturedDocument, ContentFormat
from .s3_cache import make_s3_client
from .s3_integrity import S3MislabelError

logger = logging.getLogger(__name__)

# S3 path convention: /{state}/{county}/{court}/raw/{content_hash}.{ext}
# Content-addressed keys — same content always produces the same key,
# making S3 PutObject idempotent (no orphaned duplicates).
_EXT_MAP = {
    ContentFormat.HTML: "html",
    ContentFormat.PDF: "pdf",
    ContentFormat.DOCX: "docx",
    ContentFormat.TEXT: "txt",
}


def build_s3_key(doc: CapturedDocument) -> str:
    ext = _EXT_MAP.get(doc.content_format, "bin")
    return (
        f"{doc.state.lower()}/{doc.county.lower().replace(' ', '_')}/"
        f"{doc.court.lower().replace(' ', '_')}/raw/"
        f"{doc.content_hash}.{ext}"
    )


# User-metadata key the archiver stamps on every raw object (boto3 exposes it
# lower-cased, without the ``x-amz-meta-`` prefix, under ``Metadata``).
CAPTURE_TIMESTAMP_METADATA_KEY = "capture-timestamp"


def capture_timestamp_from_s3_object(response: Mapping[str, Any]) -> datetime | None:
    """Recover a raw object's original capture time from a GetObject/HeadObject response.

    Rebuild and reingest paths must feed the worker the *original* capture
    time, never ``now()`` — the deterministic ``hearing_date_in_range`` rule
    compares the hearing date against it, so a rebuild-time stamp rejects every
    ruling heard >180 days before the rebuild (#4661).

    Preference order:

    1. ``Metadata["capture-timestamp"]`` — written by :meth:`S3Archiver.archive`
       at capture time.
    2. ``LastModified`` — archive-first capture writes the object once (the
       archiver skips existing keys), so this is the first-capture time.
    3. ``None`` — no source (e.g. a local-cache hit).  Callers must pass
       ``None`` through rather than substituting ``now()``; the validation
       rule treats a missing capture time as "cannot judge" and passes.

    Naive timestamps are assumed to be UTC.
    """
    metadata = response.get("Metadata") or {}
    raw = metadata.get(CAPTURE_TIMESTAMP_METADATA_KEY)
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    last_modified = response.get("LastModified")
    if isinstance(last_modified, datetime):
        return last_modified if last_modified.tzinfo else last_modified.replace(tzinfo=UTC)
    return None


SOURCE_URL_METADATA_KEY = "source-url"
SCRAPER_ID_METADATA_KEY = "scraper-id"


def capture_provenance_from_s3_object(response: Mapping[str, Any]) -> dict[str, str]:
    """Return the event fields a raw object's capture metadata can restore (#4774).

    :meth:`S3Archiver.archive` stamps the live scraper's ``source_url`` and
    ``scraper_id`` on every raw object.  Rebuild and prefix-mode reingest
    events are built from S3, so these are the only record of the capture's
    URL (which carries the filename some scrapers read the hearing date
    from) and of which live scraper captured it (several share a county).

    Returns ``{"source_url": ..., "capture_scraper_id": ...}`` with only the
    keys whose metadata is present and non-empty — ``{}`` for a local-cache
    read, which has no metadata.
    """
    metadata = response.get("Metadata") or {}
    fields: dict[str, str] = {}
    source_url = str(metadata.get(SOURCE_URL_METADATA_KEY) or "").strip()
    if source_url:
        fields["source_url"] = source_url
    scraper_id = str(metadata.get(SCRAPER_ID_METADATA_KEY) or "").strip()
    if scraper_id:
        fields["capture_scraper_id"] = scraper_id
    return fields


def capturing_scraper_id(event_data: Mapping[str, Any]) -> str:
    """The scraper id an ingestion event should be routed by (#5016, #5145).

    Every decision keyed on "which scraper captured this raw" (the governor
    bio route, ``extraction_config`` scraper overrides, the case-type
    fallback, the ``hearing_date_for_raw`` hook) goes through here, so the
    answer does not depend on which entry point built the event:

    - a live event carries the live id as ``scraper_id``;
    - ``rebuild_db`` / prefix-mode ``reingest_from_s3`` events set
      ``scraper_id`` to ``rebuild-<state>-<county>`` /
      ``reingest-<state>-<county>`` and carry the live id as
      ``capture_scraper_id`` (restored from the raw object's metadata by
      :func:`capture_provenance_from_s3_object`), which wins;
    - a rebuild from a local S3 cache has no metadata, so the synthetic
      ``rebuild-…`` id is all there is and is returned as-is.

    Returns ``""`` when the event names no scraper.  Recording fields
    (``documents.scraper_id``, skip and validation telemetry) keep the
    event's own ``scraper_id``: they record the entry point, not routing.
    """
    return str(event_data.get("capture_scraper_id") or event_data.get("scraper_id") or "")


class S3Archiver:
    """Archives raw captured content to S3. Documents are written once and never modified."""

    def __init__(self, bucket: str, s3_client: object | None = None) -> None:
        self.bucket = bucket
        self._client = s3_client or make_s3_client()

    def archive(self, doc: CapturedDocument) -> str:
        """Write raw_content to S3 if not already present. Returns the S3 key.

        With content-addressed keys, the same content always maps to the same
        key. A HeadObject check avoids re-uploading unchanged documents,
        saving bandwidth and S3 PUT costs on repeated scraper runs.
        """
        key = build_s3_key(doc)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            logger.debug("Already archived s3://%s/%s, skipping upload", self.bucket, key)
            return key
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "404":
                logger.error("S3 HeadObject failed for %s: %s", key, exc)
                raise

        content_type = _content_type(doc.content_format)
        computed_hash = sha256_hex(doc.raw_content)
        if computed_hash != doc.content_hash:
            raise S3MislabelError(
                f"content_hash mismatch before S3 PUT: "
                f"doc.content_hash={doc.content_hash!r} "
                f"but sha256_hex(raw_content)={computed_hash!r}"
            )
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=doc.raw_content,
                ContentType=content_type,
                Metadata={
                    SCRAPER_ID_METADATA_KEY: doc.scraper_id,
                    "content-hash": doc.content_hash,
                    SOURCE_URL_METADATA_KEY: doc.source_url,
                    CAPTURE_TIMESTAMP_METADATA_KEY: doc.capture_timestamp.isoformat(),
                },
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("S3 archival failed for %s: %s", key, exc)
            raise
        logger.info("Archived %s bytes to s3://%s/%s", len(doc.raw_content), self.bucket, key)
        return key


#: S3 error codes that mean "try again" rather than "this will never work".
#: A missing bucket, access denied or a malformed request is not here: a
#: retry gives the same answer, so ``BaseScraper`` counts it at once (#5009).
_TRANSIENT_S3_ERROR_CODES = frozenset(
    {
        "500",
        "502",
        "503",
        "504",
        "InternalError",
        "ServiceUnavailable",
        "SlowDown",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "Throttling",
        "ThrottlingException",
    }
)


def is_transient_archive_error(exc: BaseException) -> bool:
    """True when an exception from :meth:`S3Archiver.archive` is worth a retry.

    Connection drops, timeouts, throttling and S3 5xx are transient.  A
    content-hash mismatch (:class:`S3MislabelError`), a missing bucket and
    access denied are deterministic and are not retried.
    """
    if isinstance(exc, S3MislabelError):
        return False
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return code in _TRANSIENT_S3_ERROR_CODES
    if isinstance(exc, BotoCoreError):
        # botocore ConnectionError (EndpointConnectionError,
        # ConnectTimeoutError) and HTTPClientError (ReadTimeoutError,
        # ConnectionClosedError) are network-level.  Other BotoCoreErrors
        # (NoCredentialsError, ParamValidationError) are configuration.
        return isinstance(exc, (BotoConnectionError, HTTPClientError))
    return isinstance(exc, (ConnectionError, TimeoutError))


def _content_type(fmt: ContentFormat) -> str:
    return {
        ContentFormat.HTML: "text/html",
        ContentFormat.PDF: "application/pdf",
        ContentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # noqa: E501
        ContentFormat.TEXT: "text/plain",
    }.get(fmt, "application/octet-stream")
