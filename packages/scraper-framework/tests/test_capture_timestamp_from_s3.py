"""Original-capture-time recovery for rebuild / reingest paths (#4661).

Rebuild and reingest re-feed archived S3 objects to the ingestion worker.
They must pass the object's *original* capture time, never ``now()`` — the
deterministic ``hearing_date_in_range`` rule compares the hearing date against
it and rejected every ruling heard >180 days before the rebuild.

The ``rebuild_db.py`` end-to-end regression lives in ``test_rebuild_db.py``
(``TestRebuildPreservesOriginalCaptureTime``).
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import sys
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
sys.path.insert(0, _SCRIPTS_DIR)
_SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC_DIR))

from framework.models import CapturedDocument, ContentFormat  # noqa: E402
from framework.storage import (  # noqa: E402
    CAPTURE_TIMESTAMP_METADATA_KEY,
    S3Archiver,
    capture_provenance_from_s3_object,
    capture_timestamp_from_s3_object,
)
from ingestion.worker import _parse_datetime  # noqa: E402
from validation.deterministic import check_hearing_date_in_range  # noqa: E402

reingest = importlib.import_module("reingest_from_s3")


# ---------------------------------------------------------------------------
# framework.storage.capture_timestamp_from_s3_object
# ---------------------------------------------------------------------------


class TestCaptureTimestampFromS3Object:
    def test_metadata_wins(self) -> None:
        resp = {
            "Metadata": {"capture-timestamp": "2025-03-01T10:00:00+00:00"},
            "LastModified": datetime(2025, 9, 1, tzinfo=UTC),
        }
        assert capture_timestamp_from_s3_object(resp) == datetime(2025, 3, 1, 10, tzinfo=UTC)

    def test_naive_metadata_assumed_utc(self) -> None:
        resp = {"Metadata": {"capture-timestamp": "2025-03-01T10:00:00"}}
        assert capture_timestamp_from_s3_object(resp) == datetime(2025, 3, 1, 10, tzinfo=UTC)

    def test_unparseable_metadata_falls_back_to_last_modified(self) -> None:
        resp = {
            "Metadata": {"capture-timestamp": "not-a-date"},
            "LastModified": datetime(2025, 9, 1, tzinfo=UTC),
        }
        assert capture_timestamp_from_s3_object(resp) == datetime(2025, 9, 1, tzinfo=UTC)

    def test_last_modified_when_no_metadata(self) -> None:
        resp = {"LastModified": datetime(2025, 9, 1, 8, tzinfo=UTC)}
        assert capture_timestamp_from_s3_object(resp) == datetime(2025, 9, 1, 8, tzinfo=UTC)

    def test_naive_last_modified_assumed_utc(self) -> None:
        resp = {"Metadata": None, "LastModified": datetime(2025, 9, 1, 8)}
        assert capture_timestamp_from_s3_object(resp) == datetime(2025, 9, 1, 8, tzinfo=UTC)

    def test_none_when_no_source(self) -> None:
        # Local-cache hits return only {"Body": ...}.
        assert capture_timestamp_from_s3_object({"Body": io.BytesIO(b"x")}) is None

    def test_non_datetime_last_modified_ignored(self) -> None:
        assert capture_timestamp_from_s3_object({"LastModified": "yesterday"}) is None

    def test_round_trips_archiver_metadata(self) -> None:
        """The key the archiver writes is the key the helper reads."""
        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        raw = b"<html>ruling</html>"
        captured = datetime(2025, 4, 2, 12, 0, tzinfo=UTC)
        doc = CapturedDocument(
            scraper_id="s",
            state="CA",
            county="Santa Clara",
            court="Superior Court",
            source_url="https://example.com",
            capture_timestamp=captured,
            content_format=ContentFormat.HTML,
            raw_content=raw,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )
        S3Archiver(bucket="b", s3_client=client).archive(doc)
        metadata = client.put_object.call_args.kwargs["Metadata"]
        assert CAPTURE_TIMESTAMP_METADATA_KEY in metadata
        assert capture_timestamp_from_s3_object({"Metadata": metadata}) == captured


# ---------------------------------------------------------------------------
# reingest_from_s3 prefix mode — same bug shape as rebuild_db (#4661)
# ---------------------------------------------------------------------------


def _run_prefix_document(get_object_response: dict[str, Any]) -> dict[str, Any]:
    content = b"<html>ruling text</html>"
    key = f"ca/santa_clara/superior_court/raw/{hashlib.sha256(content).hexdigest()}.html"
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": io.BytesIO(content), **get_object_response}
    mock_worker = MagicMock()

    if hasattr(reingest._process_prefix_document, "_worker"):
        delattr(reingest._process_prefix_document, "_worker")
    with (
        patch("framework.s3_cache.make_s3_client", return_value=mock_s3),
        patch("ingestion.worker.IngestionWorker", return_value=mock_worker),
        patch("redis.Redis.from_url", return_value=MagicMock()),
    ):
        result = reingest._process_prefix_document(
            key, "test-bucket", "postgresql://test", "redis://localhost:6379", ""
        )
    if hasattr(reingest._process_prefix_document, "_worker"):
        delattr(reingest._process_prefix_document, "_worker")

    assert result["status"] == "ok"
    return mock_worker.process_event.call_args[0][0]


class TestReingestPrefixCaptureTimestamp:
    def test_old_capture_survives_deterministic_validation(self) -> None:
        captured = datetime(2025, 6, 4, 17, 30, tzinfo=UTC)
        event = _run_prefix_document(
            {"Metadata": {"capture-timestamp": captured.isoformat()}, "LastModified": captured}
        )
        capture_ts = _parse_datetime(event["capture_timestamp"])
        assert capture_ts is not None
        result = check_hearing_date_in_range(date(2025, 6, 6), capture_ts.date())
        assert result.result == "pass", result.reason

    def test_no_capture_source_yields_none_not_now(self) -> None:
        event = _run_prefix_document({})
        assert event["capture_timestamp"] is None

    def test_build_prefix_event_default_is_none(self) -> None:
        parsed = {
            "state": "ca",
            "county": "santa_clara",
            "court": "superior_court",
            "content_hash": "abc",
            "ext": "html",
        }
        event = reingest._build_prefix_event(
            "ca/santa_clara/superior_court/raw/abc.html", b"<html/>", parsed, "b"
        )
        assert event["capture_timestamp"] is None


# ---------------------------------------------------------------------------
# Capture provenance: source_url + live scraper id from S3 metadata (#4774)
# ---------------------------------------------------------------------------


class TestCaptureProvenanceFromS3Object:
    def test_reads_source_url_and_scraper_id(self) -> None:
        resp = {
            "Metadata": {
                "source-url": "https://www.fresno.courts.ca.gov/x/03-10-26-dept-403.pdf",
                "scraper-id": "ca-fresno-tentatives-civil",
            }
        }
        assert capture_provenance_from_s3_object(resp) == {
            "source_url": "https://www.fresno.courts.ca.gov/x/03-10-26-dept-403.pdf",
            "capture_scraper_id": "ca-fresno-tentatives-civil",
        }

    def test_missing_or_blank_metadata_omitted(self) -> None:
        assert capture_provenance_from_s3_object({"Body": io.BytesIO(b"x")}) == {}
        assert capture_provenance_from_s3_object({"Metadata": None}) == {}
        assert (
            capture_provenance_from_s3_object({"Metadata": {"source-url": " ", "scraper-id": ""}})
            == {}
        )

    def test_round_trips_archiver_metadata(self) -> None:
        client = MagicMock()
        client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        raw = b"%PDF-1.4 ruling"
        doc = CapturedDocument(
            scraper_id="ca-sb-tentatives-civil",
            state="CA",
            county="San Bernardino",
            court="Superior Court",
            source_url="https://old.sb-court.org/x/CVR17060126.pdf",
            capture_timestamp=datetime(2026, 5, 30, tzinfo=UTC),
            content_format=ContentFormat.PDF,
            raw_content=raw,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )
        S3Archiver(bucket="b", s3_client=client).archive(doc)
        metadata = client.put_object.call_args.kwargs["Metadata"]
        assert capture_provenance_from_s3_object({"Metadata": metadata}) == {
            "source_url": "https://old.sb-court.org/x/CVR17060126.pdf",
            "capture_scraper_id": "ca-sb-tentatives-civil",
        }


class TestReingestPrefixCaptureProvenance:
    def test_prefix_event_carries_metadata_provenance(self) -> None:
        event = _run_prefix_document(
            {
                "Metadata": {
                    "source-url": "https://old.sb-court.org/x/CVR17060126.pdf",
                    "scraper-id": "ca-sb-tentatives-civil",
                }
            }
        )
        assert event["source_url"] == "https://old.sb-court.org/x/CVR17060126.pdf"
        assert event["capture_scraper_id"] == "ca-sb-tentatives-civil"
        assert event["scraper_id"] == "reingest-ca-santa_clara"

    def test_prefix_event_without_metadata_has_empty_source_url(self) -> None:
        event = _run_prefix_document({})
        assert event["source_url"] == ""
        assert "capture_scraper_id" not in event
