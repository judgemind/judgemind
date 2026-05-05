"""Tests for IngestionWorker ``bust_llm_cache`` propagation (#4049).

When the worker is constructed with ``bust_llm_cache=True``, every
``LlmExtractor`` it lazy-creates (multimodal + framework) must be created
with ``bust_cache=True`` so cache reads are skipped on every extraction
call.  This is the only path through which already-split parent PDFs (whose
split-child rows live in ``derived.documents``) can be re-extracted with a
fresh LLM call — DB-row mode (``--multimodal --bust-llm-cache``) skips
split-child rows via the ``is_split_child_id`` guard to avoid the #2416
exponential explosion, leaving prefix-mode the sole avenue once a parent
has been split.

Tests use ``unittest.mock.patch`` to replace ``LlmExtractor`` at the
worker module's import site so no Google API key is required and no real
extractor is instantiated.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ingestion.worker import IngestionWorker


def _make_worker(*, bust_llm_cache: bool = False) -> IngestionWorker:
    """Construct an IngestionWorker with mocked external clients.

    Mirrors the helper in ``test_ingestion_worker.py`` but does NOT stub
    out ``_get_framework_extractor`` — these tests need to exercise the
    real lazy-init code path.
    """
    redis_mock = MagicMock()
    os_mock = MagicMock()
    s3_mock = MagicMock()
    os_mock.indices.exists.return_value = False

    return IngestionWorker(
        redis_client=redis_mock,
        pg_dsn="postgresql://localhost/test",
        opensearch_client=os_mock,
        s3_client=s3_mock,
        archive_bucket="test-bucket",
        bust_llm_cache=bust_llm_cache,
    )


class TestBustLlmCacheStorage:
    """The constructor must record ``bust_llm_cache`` for later use."""

    def test_default_is_false(self) -> None:
        worker = _make_worker()
        assert worker._bust_llm_cache is False

    def test_explicit_true_is_stored(self) -> None:
        worker = _make_worker(bust_llm_cache=True)
        assert worker._bust_llm_cache is True

    def test_explicit_false_is_stored(self) -> None:
        worker = _make_worker(bust_llm_cache=False)
        assert worker._bust_llm_cache is False


class TestMultimodalExtractorBustCachePropagation:
    """``_get_multimodal_extractor`` must construct LlmExtractor with
    ``bust_cache=self._bust_llm_cache`` in both legacy and per-token-limit
    code paths."""

    def test_per_token_limit_path_propagates_true(self) -> None:
        worker = _make_worker(bust_llm_cache=True)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_multimodal_extractor(max_output_tokens=32768)
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is True
        assert kwargs.get("provider") == "google"
        assert kwargs.get("model") == "gemini-2.5-flash-lite"
        assert kwargs.get("max_output_tokens") == 32768

    def test_per_token_limit_path_propagates_false(self) -> None:
        worker = _make_worker(bust_llm_cache=False)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_multimodal_extractor(max_output_tokens=32768)
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is False

    def test_legacy_single_slot_path_propagates_true(self) -> None:
        """The ``max_output_tokens=None`` branch (legacy single-slot cache)
        must also propagate ``bust_cache``."""
        worker = _make_worker(bust_llm_cache=True)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_multimodal_extractor()  # max_output_tokens=None
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is True
        assert kwargs.get("provider") == "google"
        assert kwargs.get("model") == "gemini-2.5-flash-lite"

    def test_legacy_single_slot_path_propagates_false(self) -> None:
        worker = _make_worker(bust_llm_cache=False)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_multimodal_extractor()
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is False

    def test_per_token_limit_cache_stable_across_calls(self) -> None:
        """The per-token-limit cache must reuse the same instance on the
        second call — only the first call constructs the LlmExtractor.
        ``bust_cache`` is captured at construction time."""
        worker = _make_worker(bust_llm_cache=True)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            instance = MagicMock()
            mock_extractor_cls.return_value = instance
            first = worker._get_multimodal_extractor(max_output_tokens=32768)
            second = worker._get_multimodal_extractor(max_output_tokens=32768)
        assert first is second
        assert mock_extractor_cls.call_count == 1


class TestFrameworkExtractorBustCachePropagation:
    """``_get_framework_extractor`` must construct LlmExtractor with
    ``bust_cache=self._bust_llm_cache``."""

    def test_propagates_true(self) -> None:
        worker = _make_worker(bust_llm_cache=True)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_framework_extractor()
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is True

    def test_propagates_false(self) -> None:
        worker = _make_worker(bust_llm_cache=False)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            mock_extractor_cls.return_value = MagicMock()
            worker._get_framework_extractor()
        mock_extractor_cls.assert_called_once()
        kwargs = mock_extractor_cls.call_args.kwargs
        assert kwargs.get("bust_cache") is False

    def test_lazy_init_caches_instance(self) -> None:
        """The framework extractor is lazy-init'd once and reused.  A
        second ``_get_framework_extractor`` call must return the same
        instance without reconstructing."""
        worker = _make_worker(bust_llm_cache=True)
        with patch("ingestion.worker.LlmExtractor") as mock_extractor_cls:
            instance = MagicMock()
            mock_extractor_cls.return_value = instance
            first = worker._get_framework_extractor()
            second = worker._get_framework_extractor()
        assert first is second
        assert mock_extractor_cls.call_count == 1
