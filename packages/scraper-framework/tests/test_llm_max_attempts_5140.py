"""``LlmExtractor(max_attempts=N)`` makes exactly N provider calls (#5140).

``max_attempts`` counts attempts (first try included).  Everything below
the extractor (``llm_retry.call_with_retries``, ``call_llm``,
``llm_providers._call_*``) takes ``max_retries``, which counts retries
after the first attempt.  These tests pin the extractor's attempt count
end to end — real ``anthropic`` SDK errors on a mocked client, through
the real ``llm_retry`` policy — on both the Anthropic text path and the
multimodal per-page path, so the two meanings cannot drift apart again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import anthropic
import pytest
from anthropic.resources.messages import Messages
from helpers.anthropic_errors import status_error

from framework.llm_extractor import LlmExtractor
from ingestion import llm_retry

SAMPLE_PDF_PATH = Path(__file__).parent / "fixtures" / "sample_ruling.pdf"


@pytest.fixture
def mock_client() -> MagicMock:
    """A mock Anthropic client whose ``messages`` is autospecced on the SDK."""
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages = create_autospec(Messages, instance=True)
    return client


@pytest.fixture(autouse=True)
def no_sleep() -> Iterator[MagicMock]:
    """The shared retry policy's backoff, without waiting."""
    with patch("ingestion.llm_retry.time.sleep") as mock_sleep:
        yield mock_sleep


def _make_extractor(mock_client: MagicMock, **kwargs: object) -> LlmExtractor:
    with patch.object(anthropic, "Anthropic", return_value=mock_client):
        ext = LlmExtractor(api_key="test-key", **kwargs)  # type: ignore[arg-type]
    ext._client = mock_client
    return ext


def _extract_text(ext: LlmExtractor) -> list:
    # A text this short is a single chunk and is not split for a second try.
    return ext.extract("Some text")


def _extract_pdf(ext: LlmExtractor) -> list:
    with patch(
        "framework.llm_extractor._render_pdf_pages",
        return_value=[(b"\x89PNG_fake", "image/png")],
    ):
        return ext.extract_from_pdf(SAMPLE_PDF_PATH.read_bytes())


_PATHS = pytest.mark.parametrize("extract", [_extract_text, _extract_pdf], ids=["text", "pdf"])


class TestMaxAttemptsCountsAttempts:
    """AC1: ``max_attempts=N`` means N calls on a persistent transient error."""

    @_PATHS
    @pytest.mark.parametrize("attempts", [1, 2, 3, 5])
    def test_persistent_500_makes_exactly_max_attempts_calls(
        self, mock_client: MagicMock, extract: object, attempts: int
    ) -> None:
        mock_client.messages.create.side_effect = status_error(500)
        ext = _make_extractor(mock_client, max_attempts=attempts)

        assert extract(ext) == []  # type: ignore[operator]
        assert mock_client.messages.create.call_count == attempts

    @_PATHS
    def test_max_attempts_1_makes_one_call_and_never_sleeps(
        self, mock_client: MagicMock, no_sleep: MagicMock, extract: object
    ) -> None:
        """One attempt is no retries, even for congestion (no floor at 0)."""
        mock_client.messages.create.side_effect = status_error(529)
        ext = _make_extractor(mock_client, max_attempts=1)

        assert extract(ext) == []  # type: ignore[operator]
        assert mock_client.messages.create.call_count == 1
        no_sleep.assert_not_called()

    @_PATHS
    @pytest.mark.parametrize("status", [429, 503, 529])
    def test_persistent_congestion_gets_the_llm_retry_floor(
        self, mock_client: MagicMock, extract: object, status: int
    ) -> None:
        """Congestion gets ``1 + CONGESTION_MIN_RETRIES`` calls once any retry
        is allowed (#5096); ``max_attempts=3`` is below that floor."""
        mock_client.messages.create.side_effect = status_error(status)
        ext = _make_extractor(mock_client, max_attempts=3)

        assert extract(ext) == []  # type: ignore[operator]
        assert mock_client.messages.create.call_count == 1 + llm_retry.CONGESTION_MIN_RETRIES


class TestDefaultUnchanged:
    """AC2: a default ``LlmExtractor()`` keeps its 3 attempts."""

    @_PATHS
    def test_default_makes_three_attempts_on_persistent_500(
        self, mock_client: MagicMock, extract: object
    ) -> None:
        mock_client.messages.create.side_effect = status_error(500)
        ext = _make_extractor(mock_client)

        assert ext._max_attempts == 3
        assert extract(ext) == []  # type: ignore[operator]
        assert mock_client.messages.create.call_count == 3


class TestAmbiguousNameRejected:
    """The attempt-counting parameter is not called ``max_retries`` (AC1)."""

    def test_max_retries_keyword_raises_type_error(self, mock_client: MagicMock) -> None:
        with pytest.raises(TypeError, match="max_retries"):
            _make_extractor(mock_client, max_retries=3)
