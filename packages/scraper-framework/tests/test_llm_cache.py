"""Tests for LLM extraction result caching."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from framework.llm_extractor import (
    _LlmCache,
    _content_hash_for_cache,
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
    """str and bytes of same content produce different hashes (by design)."""
    h_str = _content_hash_for_cache("hello")
    h_bytes = _content_hash_for_cache(b"hello")
    # str is encoded to bytes via .encode(), so "hello" -> b"hello" -> same hash
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
    def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = _LlmCache(str(tmp_path), "google", "gemini-2.5-flash-lite")
        assert cache.get("prompt", "nonexistent") is None

    def test_put_then_get(self, tmp_path: Path) -> None:
        cache = _LlmCache(str(tmp_path), "google", "gemini-2.5-flash-lite")
        data = [{"extracted_case_number": "BC123", "outcome": "granted"}]
        cache.put("prompt", "content123", data)
        result = cache.get("prompt", "content123")
        assert result == data

    def test_cache_path_structure(self, tmp_path: Path) -> None:
        cache = _LlmCache(str(tmp_path), "google", "gemini-2.5-flash-lite")
        cache.put("my prompt", "abc123", [{"test": True}])
        # Should create: {base}/google-gemini-2.5-flash-lite/prompt-{hash}/abc123.json
        model_dir = tmp_path / "google-gemini-2.5-flash-lite"
        assert model_dir.exists()
        prompt_dirs = list(model_dir.iterdir())
        assert len(prompt_dirs) == 1
        assert prompt_dirs[0].name.startswith("prompt-")

    def test_different_prompts_different_dirs(self, tmp_path: Path) -> None:
        cache = _LlmCache(str(tmp_path), "google", "gemini-2.5-flash-lite")
        cache.put("prompt A", "content1", [{"a": 1}])
        cache.put("prompt B", "content1", [{"b": 2}])
        model_dir = tmp_path / "google-gemini-2.5-flash-lite"
        prompt_dirs = list(model_dir.iterdir())
        assert len(prompt_dirs) == 2

    def test_corrupted_cache_returns_none(self, tmp_path: Path) -> None:
        cache = _LlmCache(str(tmp_path), "google", "gemini-2.5-flash-lite")
        cache.put("prompt", "content1", [{"test": True}])
        # Corrupt the file
        cache_file = list(tmp_path.rglob("content1.json"))[0]
        cache_file.write_text("not valid json{{{")
        assert cache.get("prompt", "content1") is None

    def test_put_atomic_write(self, tmp_path: Path) -> None:
        """put() uses temp file + rename for atomicity."""
        cache = _LlmCache(str(tmp_path), "google", "model")
        cache.put("prompt", "key1", [{"x": 1}])
        # No .tmp files should remain
        tmp_files = list(tmp_path.rglob("*.tmp"))
        assert len(tmp_files) == 0

    def test_put_handles_write_error(self, tmp_path: Path) -> None:
        """put() catches OSError and doesn't raise."""
        cache = _LlmCache("/nonexistent/readonly/path", "google", "model")
        # Should not raise — just logs warning
        cache.put("prompt", "key1", [{"x": 1}])
