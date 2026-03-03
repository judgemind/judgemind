"""Tests for framework.search.indexer — IndexingConsumer."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from framework.search.indexer import IndexingConsumer


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
        "s3_key": "ca/los_angeles/la_superior/raw/2026/03/01/doc-001.html",
        "case_number": "BC123456",
        "court": "LA Superior Court",
        "county": "Los Angeles",
        "state": "CA",
        "judge_name": "Hon. Smith",
        "hearing_date": "2026-03-15",
        "content_hash": "abc123def456",
        "content_format": "html",
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
    def test_indexes_multiple_documents(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")

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

    def test_continues_on_failure(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock
    ) -> None:
        # First call fails, second succeeds
        mock_opensearch.get.side_effect = Exception("not found")
        mock_opensearch.index.side_effect = [Exception("write error"), None]

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


class TestLambdaHandler:
    def test_handles_single_event(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")

        result = consumer.lambda_handler(sample_event)

        assert result["total"] == 1
        assert result["indexed"] == 1

    def test_handles_sqs_records(
        self, consumer: IndexingConsumer, mock_opensearch: MagicMock, sample_event: dict
    ) -> None:
        mock_opensearch.get.side_effect = Exception("not found")

        sqs_event = {
            "Records": [
                {"body": json.dumps(sample_event)},
            ]
        }

        result = consumer.lambda_handler(sqs_event)

        assert result["total"] == 1
        assert result["indexed"] == 1
