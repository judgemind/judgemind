"""Tests for LLM extraction result caching."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from framework.llm_extractor import (
    _content_hash_for_cache,
    _LlmCache,
    _prompt_hash,
)


def test_prompt_hash_deterministic() -> None:
    """Same prompt always produces the same hash."""
    h1 = _prompt_hash("Extract rulings from this document.")
    h2 = _prompt_hash("Extract rulings from this document.")
    assert h1 == h2
    assert len(h1) == 64  # full SHA-256


def test_prompt_hash_differs_for_different_prompts() -> None:
    h1 = _prompt_hash("prompt A")
    h2 = _prompt_hash("prompt B")
    assert h1 != h2


def test_content_hash_str_and_bytes_differ() -> None:
    """str and bytes of same content produce same hash (UTF-8 encoding)."""
    h_str = _content_hash_for_cache("hello")
    h_bytes = _content_hash_for_cache(b"hello")
    assert h_str == h_bytes


def test_content_hash_includes_metadata() -> None:
    """Metadata changes the hash."""
    h1 = _content_hash_for_cache("text", None)
    h2 = _content_hash_for_cache("text", {"judge_name": "Smith"})
    assert h1 != h2


def test_content_hash_metadata_order_independent() -> None:
    """Metadata dict order doesn't affect hash (sort_keys=True)."""
    h1 = _content_hash_for_cache("text", {"a": "1", "b": "2"})
    h2 = _content_hash_for_cache("text", {"b": "2", "a": "1"})
    assert h1 == h2


class TestLlmCache:
    def _make_cache(self, s3_client: MagicMock | None = None) -> _LlmCache:
        return _LlmCache(
            s3_client=s3_client or MagicMock(),
            bucket="test-bucket",
            provider="google",
            model="gemini-2.5-flash-lite",
        )

    def test_miss_returns_none(self) -> None:
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject"
        )
        cache = self._make_cache(mock_s3)
        assert cache.get("prompt", "nonexistent") is None

    def test_put_then_get(self) -> None:
        stored: dict[str, bytes] = {}
        mock_s3 = MagicMock()

        def fake_put(**kwargs: object) -> dict:
            stored[kwargs["Key"]] = kwargs["Body"]
            return {}

        def fake_get(**kwargs: object) -> dict:
            if kwargs["Key"] in stored:
                return {"Body": io.BytesIO(stored[kwargs["Key"]])}
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": ""}}, "GetObject")

        mock_s3.put_object.side_effect = fake_put
        mock_s3.get_object.side_effect = fake_get

        cache = self._make_cache(mock_s3)
        data = [{"extracted_case_number": "BC123", "outcome": "granted"}]
        cache.put("prompt", "content123", data)
        result = cache.get("prompt", "content123")
        assert result == data

    def test_s3_key_structure(self) -> None:
        mock_s3 = MagicMock()
        cache = self._make_cache(mock_s3)
        cache.put("my prompt", "abc123", [{"test": True}])
        call_kwargs = mock_s3.put_object.call_args.kwargs
        key = call_kwargs["Key"]
        assert key.startswith("llm-cache/google-gemini-2.5-flash-lite/prompt-")
        assert key.endswith("/abc123.json")
        assert call_kwargs["Bucket"] == "test-bucket"

    def test_different_prompts_different_keys(self) -> None:
        mock_s3 = MagicMock()
        cache = self._make_cache(mock_s3)
        cache.put("prompt A", "content1", [{"a": 1}])
        cache.put("prompt B", "content1", [{"b": 2}])
        keys = [c.kwargs["Key"] for c in mock_s3.put_object.call_args_list]
        assert keys[0] != keys[1]

    def test_corrupted_cache_returns_none(self) -> None:
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": io.BytesIO(b"not valid json{{{")}
        cache = self._make_cache(mock_s3)
        assert cache.get("prompt", "content1") is None

    def test_put_handles_write_error(self) -> None:
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": ""}}, "PutObject"
        )
        cache = self._make_cache(mock_s3)
        # Should not raise — just logs warning
        cache.put("prompt", "key1", [{"x": 1}])
