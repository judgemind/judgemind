"""Tests for RedisEventBus — the REDIS_URL-aware event bus wrapper."""

from __future__ import annotations

import json
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import redis

from framework import CapturedDocument, ContentFormat, ScraperHealthEvent
from framework.event_bus import RedisEventBus
from framework.events import EventBus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_doc(**overrides: object) -> CapturedDocument:
    defaults: dict = {
        "scraper_id": "ca-la-tentatives",
        "state": "CA",
        "county": "Los Angeles",
        "court": "Superior Court",
        "source_url": "https://example.com/ruling/1",
        "capture_timestamp": datetime(2026, 3, 3, 12, 0, 0),
        "content_format": ContentFormat.HTML,
        "raw_content": b"<html>tentative ruling</html>",
        "content_hash": "abc123",
        "s3_key": "ca/los_angeles/superior_court/raw/2026/03/03/doc-1.html",
    }
    defaults.update(overrides)
    return CapturedDocument(**defaults)


def _make_health() -> ScraperHealthEvent:
    return ScraperHealthEvent(
        producer_id="ca-la-tentatives",
        scraper_id="ca-la-tentatives",
        success=True,
        records_captured=5,
        response_time_seconds=2.5,
    )


# ---------------------------------------------------------------------------
# Tests: no REDIS_URL (disabled mode)
# ---------------------------------------------------------------------------


class TestRedisEventBusDisabled:
    def test_no_url_skips_emit_document(self) -> None:
        bus = RedisEventBus(redis_url=None)
        result = bus.emit_document_captured(_make_doc(), producer_id="test")

        assert result is None

    def test_no_url_skips_emit_health(self) -> None:
        bus = RedisEventBus(redis_url=None)
        result = bus.emit_health(_make_health())

        assert result is None

    def test_empty_string_url_skips_emit(self) -> None:
        bus = RedisEventBus(redis_url="")
        result = bus.emit_document_captured(_make_doc(), producer_id="test")

        assert result is None

    def test_from_env_without_redis_url(self) -> None:
        env = os.environ.copy()
        env.pop("REDIS_URL", None)

        with patch.dict(os.environ, env, clear=True):
            bus = RedisEventBus.from_env()

        assert bus._inner is None


# ---------------------------------------------------------------------------
# Tests: with REDIS_URL (connected mode)
# ---------------------------------------------------------------------------


class TestRedisEventBusConnected:
    def test_emit_document_captured_delegates_to_inner(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True
        mock_client.xadd.return_value = b"1709467200000-0"

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://localhost:6379")

        doc = _make_doc()
        result = bus.emit_document_captured(doc, producer_id="ca-la-tentatives")

        assert result is not None
        mock_client.xadd.assert_called_once()
        call_args = mock_client.xadd.call_args
        assert call_args[0][0] == "document.captured"

    def test_emit_health_delegates_to_inner(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True
        mock_client.xadd.return_value = b"1709467200000-0"

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://localhost:6379")

        health = _make_health()
        result = bus.emit_health(health)

        assert result is not None
        mock_client.xadd.assert_called_once()
        call_args = mock_client.xadd.call_args
        assert call_args[0][0] == "scraper.health"

    def test_emit_document_captured_includes_required_fields(self) -> None:
        """Verify the emitted payload contains the fields required by the issue."""
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True
        mock_client.xadd.return_value = b"1709467200000-0"

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://localhost:6379")

        doc = _make_doc()
        bus.emit_document_captured(doc, producer_id="ca-la-tentatives")

        # Extract the JSON payload sent to xadd
        import json

        call_args = mock_client.xadd.call_args
        payload = json.loads(call_args[0][1]["data"])

        assert payload["s3_key"] == doc.s3_key
        assert payload["scraper_id"] == doc.scraper_id
        assert payload["content_hash"] == doc.content_hash
        assert payload["state"] == doc.state
        assert payload["county"] == doc.county
        assert payload["court"] == doc.court
        assert "capture_timestamp" in payload

    def test_from_env_with_redis_url(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True

        with (
            patch.dict(os.environ, {"REDIS_URL": "redis://redis:6379"}),
            patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client),
        ):
            bus = RedisEventBus.from_env()

        assert bus._inner is not None


# ---------------------------------------------------------------------------
# Tests: error handling / graceful degradation
# ---------------------------------------------------------------------------


class TestRedisEventBusErrorHandling:
    def test_connection_failure_degrades_gracefully(self) -> None:
        with patch(
            "framework.event_bus.redis.Redis.from_url",
            side_effect=redis.ConnectionError("Connection refused"),
        ):
            bus = RedisEventBus(redis_url="redis://bad-host:6379")

        assert bus._inner is None
        # Should still work (no-op)
        result = bus.emit_document_captured(_make_doc(), producer_id="test")
        assert result is None

    def test_ping_failure_degrades_gracefully(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.side_effect = redis.ConnectionError("Connection refused")

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://bad-host:6379")

        assert bus._inner is None

    def test_emit_failure_returns_none(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True
        mock_client.xadd.side_effect = redis.ConnectionError("Lost connection")

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://localhost:6379")

        result = bus.emit_document_captured(_make_doc(), producer_id="test")
        assert result is None

    def test_emit_health_failure_returns_none(self) -> None:
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.ping.return_value = True
        mock_client.xadd.side_effect = redis.ConnectionError("Lost connection")

        with patch("framework.event_bus.redis.Redis.from_url", return_value=mock_client):
            bus = RedisEventBus(redis_url="redis://localhost:6379")

        result = bus.emit_health(_make_health())
        assert result is None


# ---------------------------------------------------------------------------
# Regression: hearing_date survives serialization round-trip (#191)
# ---------------------------------------------------------------------------


class TestHearingDateSerialization:
    """Regression tests for #191: hearing_date must survive the full
    EventBus serialization path (scraper -> Redis -> ingestion worker).
    """

    def test_hearing_date_present_in_emitted_payload(self) -> None:
        """hearing_date set on CapturedDocument must appear in the Redis payload."""
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)

        doc = _make_doc(hearing_date=datetime(2026, 3, 5))
        bus.emit_document_captured(doc, producer_id="test")

        call_args = mock_redis.xadd.call_args
        payload = json.loads(call_args[0][1]["data"])

        assert payload["hearing_date"] is not None
        assert payload["hearing_date"] == "2026-03-05T00:00:00"

    def test_hearing_date_none_serializes_as_null(self) -> None:
        """hearing_date=None should serialize as JSON null (not be dropped)."""
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)

        doc = _make_doc(hearing_date=None)
        bus.emit_document_captured(doc, producer_id="test")

        call_args = mock_redis.xadd.call_args
        payload = json.loads(call_args[0][1]["data"])

        assert "hearing_date" in payload
        assert payload["hearing_date"] is None

    def test_capture_timestamp_present_in_emitted_payload(self) -> None:
        """capture_timestamp must also survive serialization."""
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)

        doc = _make_doc(capture_timestamp=datetime(2026, 3, 5, 10, 0, 0))
        bus.emit_document_captured(doc, producer_id="test")

        call_args = mock_redis.xadd.call_args
        payload = json.loads(call_args[0][1]["data"])

        assert payload["capture_timestamp"] == "2026-03-05T10:00:00"

    def test_full_round_trip_hearing_date_to_ingestion_worker(self) -> None:
        """End-to-end: hearing_date set by scraper must be parseable by
        the ingestion worker after a Redis round-trip.
        """
        from ingestion.worker import _parse_date

        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)

        doc = _make_doc(hearing_date=datetime(2026, 3, 5))
        bus.emit_document_captured(doc, producer_id="test")

        # Extract the payload as the ingestion worker would see it
        call_args = mock_redis.xadd.call_args
        raw_data = call_args[0][1]["data"]
        event_data = json.loads(raw_data)

        # Ingestion worker parses hearing_date via _parse_date
        hearing_dt = _parse_date(event_data.get("hearing_date"))
        assert hearing_dt is not None
        assert hearing_dt.year == 2026
        assert hearing_dt.month == 3
        assert hearing_dt.day == 5

    def test_all_datetime_fields_survive_serialization(self) -> None:
        """All datetime fields on the event must be non-null ISO strings
        after serialization when they are set on the source document.
        """
        mock_redis = MagicMock()
        mock_redis.xadd.return_value = b"1234-0"
        bus = EventBus(mock_redis)

        doc = _make_doc(
            hearing_date=datetime(2026, 3, 5),
            capture_timestamp=datetime(2026, 3, 4, 23, 0, 0),
        )
        bus.emit_document_captured(doc, producer_id="test")

        call_args = mock_redis.xadd.call_args
        payload = json.loads(call_args[0][1]["data"])

        # All datetime fields must be ISO 8601 strings
        datetime_fields = ["hearing_date", "capture_timestamp", "timestamp"]
        for field in datetime_fields:
            assert payload[field] is not None, f"{field} is null in serialized payload"
            assert isinstance(payload[field], str), f"{field} is not a string"
            # Verify it's parseable as ISO datetime
            parsed = datetime.fromisoformat(payload[field])
            assert parsed is not None, f"{field} is not a valid ISO datetime"
