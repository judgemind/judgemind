"""Tests for framework.search.indexer — IndexingConsumer."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from opensearchpy.exceptions import AuthorizationException, ConnectionTimeout, TransportError
from opensearchpy.exceptions import ConnectionError as OSConnectionError

from framework.search.indexer import (
    CONSUMER_GROUP,
    STREAM_DOCUMENT_VALIDATED,
    IndexingConsumer,
)


@pytest.fixture()
def mock_opensearch() -> MagicMock:
    client = MagicMock()
    client.indices.exists.return_value = True  # skip index creation in tests
    return client


@pytest.fixture()
def mock_s3() -> MagicMock:
    client = MagicMock()
    client.get_object.return_value = {
        "Body": BytesIO(b"This is the ruling text from S3."),
    }
    return client


@pytest.fixture()
def consumer(mock_opensearch: MagicMock, mock_s3: MagicMock) -> IndexingConsumer:
    return IndexingConsumer(
        opensearch_client=mock_opensearch,
        s3_client=mock_s3,
        bucket="test-bucket",
        ensure_index=True,
    )


@pytest.fixture()
def sample_event() -> dict:
    return {
        "document_id": "doc-001",
        "s3_key": "ca/los_angeles/la_superior/raw/abc123def456.html",
        "case_number": "BC123456",
        "court": "LA Superior Court",
        "county": "Los Angeles",
        "state": "CA",
        "judge_name": "Hon. Smith",
        "hearing_date": "2026-03-15",
        "content_hash": "abc123def456",
        "content_format": "html",
        "motion_type": "Demurrer",
        "outcome": "granted",
        "case_title": "Smith v. Jones",
        "case_type": "civil",
        "summary": "Motion for demurrer is granted.",
    }


class TestIndexDocument:
    def test_indexes_new_document(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        # Document not found in OpenSearch — needs indexing
        mock_opensearch.get.side_effect = Exception("not found")

        result = consumer.index_document(sample_event)

        assert result is True
        mock_opensearch.index.assert_called_once()
        call_kwargs = mock_opensearch.index.call_args.kwargs
        assert call_kwargs["id"] == "doc-001"
        assert call_kwargs["body"]["case_number"] == "BC123456"
        assert call_kwargs["body"]["ruling_text"] == "This is the ruling text from S3."

    def test_indexes_motion_type_outcome_case_title_case_type_summary(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        """Fields (motion_type, outcome, case_title, case_type, summary) are included in OS doc."""
        mock_opensearch.get.side_effect = Exception("not found")

        consumer.index_document(sample_event)

        call_kwargs = mock_opensearch.index.call_args.kwargs
        body = call_kwargs["body"]
        assert body["motion_type"] == "Demurrer"
        assert body["outcome"] == "granted"
        assert body["case_title"] == "Smith v. Jones"
        assert body["case_type"] == "civil"
        assert body["summary"] == "Motion for demurrer is granted."

    def test_indexes_with_missing_new_fields(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """When new fields are absent from the event, they are None in the OS doc."""
        mock_opensearch.get.side_effect = Exception("not found")

        event = {
            "document_id": "doc-minimal",
            "case_number": "BC999",
            "court": "Test Court",
            "county": "Test",
            "state": "CA",
            "content_hash": "hash1",
        }

        consumer.index_document(event)

        call_kwargs = mock_opensearch.index.call_args.kwargs
        body = call_kwargs["body"]
        assert body["motion_type"] is None
        assert body["outcome"] is None
        assert body["case_title"] is None
        assert body["case_type"] is None
        assert body["summary"] is None

    def test_skips_when_same_hash(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        # Document already indexed with same hash
        mock_opensearch.get.return_value = {"_source": {"content_hash": "abc123def456"}}

        result = consumer.index_document(sample_event)

        assert result is False
        mock_opensearch.index.assert_not_called()

    def test_reindexes_on_hash_change(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        # Document exists but with different hash
        mock_opensearch.get.return_value = {"_source": {"content_hash": "old_hash_value"}}

        result = consumer.index_document(sample_event)

        assert result is True
        mock_opensearch.index.assert_called_once()

    def test_uses_ruling_text_from_event_when_no_s3_key(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, mock_s3: MagicMock
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")

        event = {
            "document_id": "doc-002",
            "case_number": "BC789",
            "court": "Test Court",
            "county": "Test County",
            "state": "CA",
            "ruling_text": "Inline ruling text",
            "content_hash": "xyz",
        }

        consumer.index_document(event)

        mock_s3.get_object.assert_not_called()
        call_kwargs = mock_opensearch.index.call_args.kwargs
        assert call_kwargs["body"]["ruling_text"] == "Inline ruling text"

    def test_fetches_text_from_s3(
        self,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
        mock_s3: MagicMock,
        sample_event: dict,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")

        consumer.index_document(sample_event)

        mock_s3.get_object.assert_called_once_with(
            Bucket="test-bucket",
            Key=sample_event["s3_key"],
        )


class TestIndexBatch:
    @patch("framework.search.indexer.helpers.bulk")
    def test_indexes_multiple_documents_via_bulk(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.return_value = (3, 0)

        events = [
            {
                "document_id": f"doc-{i}",
                "case_number": f"BC{i}",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": f"hash{i}",
            }
            for i in range(3)
        ]

        count = consumer.index_batch(events)

        assert count == 3
        mock_bulk.assert_called_once()
        # Verify the actions passed to bulk have the right structure
        call_args = mock_bulk.call_args
        actions = call_args[0][1]  # second positional arg
        assert len(actions) == 3
        assert actions[0]["_op_type"] == "index"
        assert actions[0]["_id"] == "doc-0"
        assert actions[0]["_source"]["case_number"] == "BC0"

    @patch("framework.search.indexer.helpers.bulk")
    def test_bulk_reports_partial_failures(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")
        # 1 success, 1 failure
        mock_bulk.return_value = (1, 1)

        events = [
            {
                "document_id": "doc-fail",
                "case_number": "BC1",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "h1",
            },
            {
                "document_id": "doc-ok",
                "case_number": "BC2",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "h2",
            },
        ]

        count = consumer.index_batch(events)
        assert count == 1

    @patch("framework.search.indexer.helpers.bulk")
    def test_skips_already_indexed_documents_in_batch(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        # First doc already indexed with same hash, second is new
        mock_opensearch.get.side_effect = [
            {"_source": {"content_hash": "existing_hash"}},
            Exception("not found"),
        ]
        mock_bulk.return_value = (1, 0)

        events = [
            {
                "document_id": "doc-existing",
                "case_number": "BC1",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "existing_hash",
            },
            {
                "document_id": "doc-new",
                "case_number": "BC2",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "new_hash",
            },
        ]

        count = consumer.index_batch(events)
        assert count == 1
        # Only one action sent to bulk (the new doc)
        actions = mock_bulk.call_args[0][1]
        assert len(actions) == 1
        assert actions[0]["_id"] == "doc-new"

    def test_returns_zero_when_all_skipped(
        self,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        # All docs already indexed
        mock_opensearch.get.return_value = {"_source": {"content_hash": "same_hash"}}

        events = [
            {
                "document_id": "doc-1",
                "case_number": "BC1",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "same_hash",
            },
        ]

        count = consumer.index_batch(events)
        assert count == 0


class TestLambdaHandler:
    @patch("framework.search.indexer.helpers.bulk")
    def test_handles_single_event(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
        sample_event: dict,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.return_value = (1, 0)

        result = consumer.lambda_handler(sample_event)

        assert result["total"] == 1
        assert result["indexed"] == 1

    @patch("framework.search.indexer.helpers.bulk")
    def test_handles_sqs_records(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
        sample_event: dict,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.return_value = (1, 0)

        sqs_event = {
            "Records": [
                {"body": json.dumps(sample_event)},
            ]
        }

        result = consumer.lambda_handler(sqs_event)

        assert result["total"] == 1
        assert result["indexed"] == 1

    @patch("framework.search.indexer.helpers.bulk")
    def test_handles_sns_records(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
        sample_event: dict,
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.return_value = (1, 0)

        sns_event = {
            "Records": [
                {"Sns": {"Message": json.dumps(sample_event)}},
            ]
        }

        result = consumer.lambda_handler(sns_event)

        assert result["total"] == 1
        assert result["indexed"] == 1


class TestIndexBatchErrorHandling:
    """Tests for error handling in index_batch when _build_os_doc raises."""

    @patch("framework.search.indexer.helpers.bulk")
    def test_batch_handles_build_doc_exception(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        """When _build_os_doc raises for one event, other events still get indexed."""
        # First call raises (missing document_id), second succeeds
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.return_value = (1, 0)

        events = [
            {
                # Missing document_id — will cause KeyError in _build_os_doc
            },
            {
                "document_id": "doc-ok",
                "case_number": "BC2",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "h2",
            },
        ]

        count = consumer.index_batch(events)

        assert count == 1
        actions = mock_bulk.call_args[0][1]
        assert len(actions) == 1
        assert actions[0]["_id"] == "doc-ok"

    @patch("framework.search.indexer.helpers.bulk")
    def test_batch_all_build_failures_returns_zero(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
    ) -> None:
        """When all events fail _build_os_doc, bulk is never called and returns 0."""
        events = [
            {},  # Missing document_id
            {},  # Missing document_id
        ]

        count = consumer.index_batch(events)

        assert count == 0
        mock_bulk.assert_not_called()


class TestStreamConsumer:
    """Tests for the Redis Streams consumer loop (run_stream_consumer)."""

    def test_creates_consumer_group(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """run_stream_consumer creates the consumer group on start."""
        mock_redis = MagicMock()
        # Make xreadgroup raise KeyboardInterrupt to break the loop immediately
        mock_redis.xreadgroup.side_effect = KeyboardInterrupt

        consumer.run_stream_consumer(mock_redis)

        mock_redis.xgroup_create.assert_called_once_with(
            STREAM_DOCUMENT_VALIDATED, CONSUMER_GROUP, id="0", mkstream=True
        )

    def test_group_already_exists(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """If the consumer group already exists, the exception is swallowed."""
        mock_redis = MagicMock()
        mock_redis.xgroup_create.side_effect = Exception("BUSYGROUP group already exists")
        mock_redis.xreadgroup.side_effect = KeyboardInterrupt

        # Should not raise
        consumer.run_stream_consumer(mock_redis)

        mock_redis.xgroup_create.assert_called_once()

    def test_processes_messages_and_acks(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """Consumer reads messages, indexes them, and acknowledges."""
        mock_opensearch.get.side_effect = Exception("not found")

        mock_redis = MagicMock()
        event_data = {
            "document_id": "doc-stream-1",
            "case_number": "BC999",
            "court": "Stream Court",
            "county": "Stream County",
            "state": "CA",
            "content_hash": "streamhash",
        }

        # First call returns a message, second raises KeyboardInterrupt
        mock_redis.xreadgroup.side_effect = [
            [
                (
                    STREAM_DOCUMENT_VALIDATED,
                    [("msg-id-1", {b"data": json.dumps(event_data).encode()})],
                )
            ],
            KeyboardInterrupt,
        ]

        consumer.run_stream_consumer(mock_redis)

        # Document should have been indexed
        mock_opensearch.index.assert_called_once()
        call_kwargs = mock_opensearch.index.call_args.kwargs
        assert call_kwargs["id"] == "doc-stream-1"

        # Message should have been acknowledged
        mock_redis.xack.assert_called_once_with(
            STREAM_DOCUMENT_VALIDATED, CONSUMER_GROUP, "msg-id-1"
        )

    def test_continues_on_empty_read(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """When xreadgroup returns empty, the loop continues."""
        mock_redis = MagicMock()
        # First call returns empty (None/[]), second raises KeyboardInterrupt
        mock_redis.xreadgroup.side_effect = [None, KeyboardInterrupt]

        consumer.run_stream_consumer(mock_redis)

        # Should have been called twice before breaking
        assert mock_redis.xreadgroup.call_count == 2
        mock_opensearch.index.assert_not_called()

    def test_handles_message_processing_error(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """If index_document fails for a message, the loop continues without acking."""
        mock_opensearch.get.side_effect = Exception("not found")
        # Make index() raise to simulate indexing failure
        mock_opensearch.index.side_effect = Exception("OpenSearch write error")

        mock_redis = MagicMock()
        event_data = {
            "document_id": "doc-fail",
            "case_number": "BC000",
            "court": "Fail Court",
            "county": "Fail County",
            "state": "CA",
            "content_hash": "failhash",
        }

        mock_redis.xreadgroup.side_effect = [
            [
                (
                    STREAM_DOCUMENT_VALIDATED,
                    [("msg-id-fail", {b"data": json.dumps(event_data).encode()})],
                )
            ],
            KeyboardInterrupt,
        ]

        consumer.run_stream_consumer(mock_redis)

        # Message should NOT have been acknowledged (processing failed)
        mock_redis.xack.assert_not_called()

    def test_handles_stream_error_and_continues(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """A generic exception in the outer loop is caught and the loop continues."""
        mock_redis = MagicMock()
        # First call raises a generic error, second raises KeyboardInterrupt
        mock_redis.xreadgroup.side_effect = [
            ConnectionError("Redis connection lost"),
            KeyboardInterrupt,
        ]

        consumer.run_stream_consumer(mock_redis)

        # Should have tried twice before breaking
        assert mock_redis.xreadgroup.call_count == 2

    def test_custom_stream_name_and_batch_size(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """Custom stream, batch_size, and block_ms are passed through."""
        mock_redis = MagicMock()
        mock_redis.xreadgroup.side_effect = KeyboardInterrupt

        consumer.run_stream_consumer(
            mock_redis, stream="custom.stream", batch_size=50, block_ms=1000
        )

        mock_redis.xgroup_create.assert_called_once_with(
            "custom.stream", CONSUMER_GROUP, id="0", mkstream=True
        )

    def test_processes_message_with_string_data_key(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """Messages can have string 'data' key (not bytes)."""
        mock_opensearch.get.side_effect = Exception("not found")

        mock_redis = MagicMock()
        event_data = {
            "document_id": "doc-str",
            "case_number": "BC111",
            "court": "Str Court",
            "county": "Str County",
            "state": "CA",
            "content_hash": "strhash",
        }

        mock_redis.xreadgroup.side_effect = [
            [
                (
                    STREAM_DOCUMENT_VALIDATED,
                    [("msg-str-1", {"data": json.dumps(event_data)})],
                )
            ],
            KeyboardInterrupt,
        ]

        consumer.run_stream_consumer(mock_redis)

        mock_opensearch.index.assert_called_once()
        call_kwargs = mock_opensearch.index.call_args.kwargs
        assert call_kwargs["id"] == "doc-str"
        mock_redis.xack.assert_called_once()


class TestEnsureIndexStartup:
    """Regression tests for #3917 — IndexingConsumer.__init__ must not raise
    when OpenSearch returns a 403 (AuthorizationException) or other transport
    errors on startup.  OpenSearch is best-effort and fully derivable from
    derived.*; a transient or auth error at startup must never prevent the
    ingestion worker from starting.
    """

    def test_authorization_exception_during_ensure_index_does_not_raise(self) -> None:
        """AuthorizationException(403) from create_index is swallowed at startup.

        Reproduces the exact crash loop seen in dev: the OpenSearch endpoint
        returned 403 on the HEAD /tentative_rulings_v1 call inside create_index,
        propagating up through IngestionWorker.__init__ and causing sys.exit(1).
        """
        mock_os = MagicMock()
        mock_s3 = MagicMock()
        # Simulate the 403 that dev was returning
        mock_os.indices.exists.side_effect = AuthorizationException(403, "")

        # Must NOT raise — worker should start and log a warning
        consumer = IndexingConsumer(
            opensearch_client=mock_os,
            s3_client=mock_s3,
            bucket="test-bucket",
            ensure_index=True,
        )
        assert consumer is not None

    def test_transport_error_during_ensure_index_does_not_raise(self) -> None:
        """Any TransportError from create_index is swallowed at startup."""
        mock_os = MagicMock()
        mock_s3 = MagicMock()
        mock_os.indices.exists.side_effect = TransportError(503, "Service Unavailable", {})

        consumer = IndexingConsumer(
            opensearch_client=mock_os,
            s3_client=mock_s3,
            bucket="test-bucket",
            ensure_index=True,
        )
        assert consumer is not None

    def test_connection_error_during_ensure_index_does_not_raise(self) -> None:
        """OSConnectionError from create_index is swallowed at startup."""
        mock_os = MagicMock()
        mock_s3 = MagicMock()
        mock_os.indices.exists.side_effect = OSConnectionError("N/A", "Connection refused", None)

        consumer = IndexingConsumer(
            opensearch_client=mock_os,
            s3_client=mock_s3,
            bucket="test-bucket",
            ensure_index=True,
        )
        assert consumer is not None

    def test_ensure_index_false_skips_create_index(self) -> None:
        """When ensure_index=False, create_index is never called."""
        mock_os = MagicMock()
        mock_s3 = MagicMock()

        IndexingConsumer(
            opensearch_client=mock_os,
            s3_client=mock_s3,
            bucket="test-bucket",
            ensure_index=False,
        )

        mock_os.indices.exists.assert_not_called()

    def test_successful_ensure_index_still_works(self) -> None:
        """Normal path: index does not exist, create_index runs without error."""
        mock_os = MagicMock()
        mock_s3 = MagicMock()
        mock_os.indices.exists.return_value = False  # index doesn't exist yet

        consumer = IndexingConsumer(
            opensearch_client=mock_os,
            s3_client=mock_s3,
            bucket="test-bucket",
            ensure_index=True,
        )

        assert consumer is not None
        mock_os.indices.create.assert_called_once()


class TestFetchTextError:
    """Tests for S3 fetch error handling in _fetch_text."""

    def test_fetch_text_returns_empty_on_s3_error(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, mock_s3: MagicMock
    ) -> None:
        """When S3 get_object raises, _fetch_text returns empty string."""
        mock_s3.get_object.side_effect = Exception("Access Denied")
        mock_opensearch.get.side_effect = Exception("not found")

        event = {
            "document_id": "doc-s3-fail",
            "s3_key": "some/key.html",
            "case_number": "BC555",
            "court": "S3 Court",
            "county": "S3 County",
            "state": "CA",
            "content_hash": "s3failhash",
        }

        result = consumer.index_document(event)

        assert result is True
        call_kwargs = mock_opensearch.index.call_args.kwargs
        # ruling_text should be empty string since S3 failed
        assert call_kwargs["body"]["ruling_text"] == ""

    def test_fetch_text_handles_decode_errors(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, mock_s3: MagicMock
    ) -> None:
        """Non-UTF8 bytes are decoded with replacement characters."""
        mock_s3.get_object.return_value = {
            "Body": BytesIO(b"\xff\xfe invalid utf8 \x80\x81"),
        }
        mock_opensearch.get.side_effect = Exception("not found")

        event = {
            "document_id": "doc-decode",
            "s3_key": "some/binary.html",
            "case_number": "BC666",
            "court": "Decode Court",
            "county": "Decode County",
            "state": "CA",
            "content_hash": "decodehash",
        }

        result = consumer.index_document(event)

        assert result is True
        call_kwargs = mock_opensearch.index.call_args.kwargs
        # Should have content with replacement characters, not raise
        assert len(call_kwargs["body"]["ruling_text"]) > 0


class TestConstructor:
    """Tests for IndexingConsumer constructor edge cases."""

    def test_skip_ensure_index(self, mock_opensearch: MagicMock, mock_s3: MagicMock) -> None:
        """When ensure_index=False, create_index is not called."""
        with patch("framework.search.indexer.create_index") as mock_create:
            IndexingConsumer(
                opensearch_client=mock_opensearch,
                s3_client=mock_s3,
                bucket="test-bucket",
                ensure_index=False,
            )
            mock_create.assert_not_called()

    def test_ensure_index_called_by_default(
        self, mock_opensearch: MagicMock, mock_s3: MagicMock
    ) -> None:
        """By default, create_index is called during construction."""
        with patch("framework.search.indexer.create_index") as mock_create:
            IndexingConsumer(
                opensearch_client=mock_opensearch,
                s3_client=mock_s3,
                bucket="test-bucket",
            )
            mock_create.assert_called_once_with(mock_opensearch)

    def test_custom_index_name(self, mock_opensearch: MagicMock, mock_s3: MagicMock) -> None:
        """Custom index_name is used for indexing operations."""
        consumer = IndexingConsumer(
            opensearch_client=mock_opensearch,
            s3_client=mock_s3,
            bucket="test-bucket",
            index_name="custom_index",
            ensure_index=False,
        )
        mock_opensearch.get.side_effect = Exception("not found")

        event = {
            "document_id": "doc-custom",
            "case_number": "BC1",
            "court": "Test",
            "county": "Test",
            "state": "CA",
            "content_hash": "hash1",
        }
        consumer.index_document(event)

        call_kwargs = mock_opensearch.index.call_args.kwargs
        assert call_kwargs["index"] == "custom_index"


class TestAlreadyIndexed:
    """Tests for the _already_indexed idempotency check."""

    def test_no_content_hash_skips_check(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        """When content_hash is empty, idempotency check is skipped."""
        event = {
            "document_id": "doc-no-hash",
            "case_number": "BC1",
            "court": "Test",
            "county": "Test",
            "state": "CA",
            "content_hash": "",
        }

        result = consumer.index_document(event)

        assert result is True
        # get() should not be called when hash is empty
        mock_opensearch.get.assert_not_called()


class TestTransientConnectionErrors:
    """Tests for best-effort handling of OpenSearch ConnectionTimeout / ConnectionError.

    See #2481 — transient search-indexing failures must not fail the document's
    Postgres write. OpenSearch is fully derivable from ``derived.*`` and will
    self-heal on re-index, so these errors are logged as warnings and swallowed.
    """

    def test_index_document_returns_false_on_connection_timeout(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        """index_document returns False (does not raise) when OpenSearch times out."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_opensearch.index.side_effect = ConnectionTimeout(
            408, "read_timeout=10", "HTTPSConnectionPool timed out"
        )

        # Must not raise
        result = consumer.index_document(sample_event)

        assert result is False
        mock_opensearch.index.assert_called_once()

    def test_index_document_returns_false_on_connection_error(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        """index_document returns False (does not raise) on ConnectionError."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_opensearch.index.side_effect = OSConnectionError(
            503, "connection_error", "Unable to connect to OpenSearch"
        )

        result = consumer.index_document(sample_event)

        assert result is False
        mock_opensearch.index.assert_called_once()

    def test_index_document_propagates_non_transient_error(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        """Non-transient errors (e.g. auth, 4xx, invalid index) still propagate."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_opensearch.index.side_effect = ValueError("invalid index name")

        with pytest.raises(ValueError, match="invalid index name"):
            consumer.index_document(sample_event)

    @patch("framework.search.indexer.helpers.bulk")
    def test_index_batch_returns_zero_on_connection_timeout(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        """index_batch returns 0 (does not raise) when bulk times out."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.side_effect = ConnectionTimeout(
            408, "read_timeout=10", "HTTPSConnectionPool timed out"
        )

        events = [
            {
                "document_id": f"doc-{i}",
                "case_number": f"BC{i}",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": f"hash{i}",
            }
            for i in range(3)
        ]

        # Must not raise
        count = consumer.index_batch(events)

        assert count == 0
        mock_bulk.assert_called_once()

    @patch("framework.search.indexer.helpers.bulk")
    def test_index_batch_returns_zero_on_connection_error(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        """index_batch returns 0 (does not raise) on ConnectionError from bulk."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.side_effect = OSConnectionError(
            503, "connection_error", "Unable to connect to OpenSearch"
        )

        events = [
            {
                "document_id": "doc-1",
                "case_number": "BC1",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "h1",
            },
        ]

        count = consumer.index_batch(events)

        assert count == 0
        mock_bulk.assert_called_once()

    @patch("framework.search.indexer.helpers.bulk")
    def test_index_batch_propagates_non_transient_error(
        self,
        mock_bulk: MagicMock,
        consumer: IndexingConsumer,
        mock_opensearch: MagicMock,
    ) -> None:
        """Non-transient errors from bulk (e.g. auth, 4xx) still propagate."""
        mock_opensearch.get.side_effect = Exception("not found")
        mock_bulk.side_effect = ValueError("invalid bulk payload")

        events = [
            {
                "document_id": "doc-1",
                "case_number": "BC1",
                "court": "Test",
                "county": "Test",
                "state": "CA",
                "content_hash": "h1",
            },
        ]

        with pytest.raises(ValueError, match="invalid bulk payload"):
            consumer.index_batch(events)
