"""S3 archival utility — immutable raw document storage per Architecture Spec §4.4."""

from __future__ import annotations

import logging

from botocore.exceptions import BotoCoreError, ClientError

from .models import CapturedDocument, ContentFormat
from .s3_cache import make_s3_client

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
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=doc.raw_content,
                ContentType=content_type,
                Metadata={
                    "scraper-id": doc.scraper_id,
                    "content-hash": doc.content_hash,
                    "source-url": doc.source_url,
                    "capture-timestamp": doc.capture_timestamp.isoformat(),
                },
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("S3 archival failed for %s: %s", key, exc)
            raise
        logger.info("Archived %s bytes to s3://%s/%s", len(doc.raw_content), self.bucket, key)
        return key


def _content_type(fmt: ContentFormat) -> str:
    return {
        ContentFormat.HTML: "text/html",
        ContentFormat.PDF: "application/pdf",
        ContentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # noqa: E501
        ContentFormat.TEXT: "text/plain",
    }.get(fmt, "application/octet-stream")
