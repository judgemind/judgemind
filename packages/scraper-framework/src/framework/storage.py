"""S3 archival utility — immutable raw document storage per Architecture Spec §4.4."""

from __future__ import annotations

import logging
from datetime import datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .models import CapturedDocument, ContentFormat

logger = logging.getLogger(__name__)

# S3 path convention: /{state}/{county}/{court}/{document_type}/{date}/{document_id}.{ext}
# Per Architecture Spec §4.4
_EXT_MAP = {
    ContentFormat.HTML: "html",
    ContentFormat.PDF: "pdf",
    ContentFormat.DOCX: "docx",
    ContentFormat.TEXT: "txt",
}


def build_s3_key(doc: CapturedDocument, capture_date: datetime | None = None) -> str:
    date = capture_date or doc.capture_timestamp
    date_str = date.strftime("%Y/%m/%d")
    ext = _EXT_MAP.get(doc.content_format, "bin")
    return (
        f"{doc.state.lower()}/{doc.county.lower().replace(' ', '_')}/"
        f"{doc.court.lower().replace(' ', '_')}/raw/"
        f"{date_str}/{doc.document_id}.{ext}"
    )


class S3Archiver:
    """Archives raw captured content to S3. Documents are written once and never modified."""

    def __init__(self, bucket: str, s3_client: object | None = None) -> None:
        self.bucket = bucket
        self._client = s3_client or boto3.client("s3")

    def archive(self, doc: CapturedDocument) -> str:
        """Write raw_content to S3. Returns the S3 key. Raises on failure."""
        key = build_s3_key(doc)
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
        ContentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ContentFormat.TEXT: "text/plain",
    }.get(fmt, "application/octet-stream")
