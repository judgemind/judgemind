"""Tests for ingestion.ruling_summarizer — ruling summarization at ingestion time."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ingestion.llm_providers import LLMResponse, resolve_model
from ingestion.ruling_summarizer import (
    _MIN_TEXT_LENGTH,
    _SYSTEM_PROMPT,
    summarize_ruling,
)


class TestSummarizeRuling:
    """Tests for the summarize_ruling function."""

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_summary_on_success(self, mock_call_llm: MagicMock) -> None:
        """Happy path: returns (summary, model) when LLM succeeds."""
        mock_response = MagicMock()
        mock_response.text = "  The court granted the motion for summary judgment.  "
        mock_response.input_tokens = 100
        mock_response.output_tokens = 20
        mock_call_llm.return_value = mock_response

        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        summary, model = summarize_ruling(ruling_text, client=MagicMock())

        assert summary == "The court granted the motion for summary judgment."
        assert model == resolve_model()
        mock_call_llm.assert_called_once()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_none_on_empty_text(self, mock_call_llm: MagicMock) -> None:
        """Returns (None, model) when ruling_text is empty."""
        summary, model = summarize_ruling("", client=MagicMock())

        assert summary is None
        assert model == resolve_model()
        mock_call_llm.assert_not_called()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_none_on_none_text(self, mock_call_llm: MagicMock) -> None:
        """Returns (None, model) when ruling_text is None."""
        summary, model = summarize_ruling(None, client=MagicMock())

        assert summary is None
        assert model == resolve_model()
        mock_call_llm.assert_not_called()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_none_on_short_text(self, mock_call_llm: MagicMock) -> None:
        """Returns (None, model) when ruling_text is shorter than minimum."""
        short_text = "x" * (_MIN_TEXT_LENGTH - 1)
        summary, model = summarize_ruling(short_text, client=MagicMock())

        assert summary is None
        assert model == resolve_model()
        mock_call_llm.assert_not_called()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_none_on_llm_failure(self, mock_call_llm: MagicMock) -> None:
        """Returns (None, model) when call_llm returns None."""
        mock_call_llm.return_value = None
        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5

        summary, model = summarize_ruling(ruling_text, client=MagicMock())

        assert summary is None
        assert model == resolve_model()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_returns_none_on_llm_exception(self, mock_call_llm: MagicMock) -> None:
        """Returns (None, model) when call_llm raises an exception."""
        mock_call_llm.side_effect = RuntimeError("API error")
        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5

        summary, model = summarize_ruling(ruling_text, client=MagicMock())

        assert summary is None
        assert model == resolve_model()

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_passes_case_context_in_user_message(self, mock_call_llm: MagicMock) -> None:
        """Case context (case_title, motion_type) is included in the user message."""
        mock_response = MagicMock()
        mock_response.text = "Summary text."
        mock_response.input_tokens = 50
        mock_response.output_tokens = 10
        mock_call_llm.return_value = mock_response

        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        case_context = {"case_title": "Smith v. Jones", "motion_type": "Summary Judgment"}

        summarize_ruling(ruling_text, case_context=case_context, client=MagicMock())

        call_kwargs = mock_call_llm.call_args
        user_message = call_kwargs.kwargs["user_message"]
        assert "Smith v. Jones" in user_message
        assert "Summary Judgment" in user_message
        assert "case_title" in user_message

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_does_not_pin_provider_or_model(self, mock_call_llm: MagicMock) -> None:
        """call_llm is invoked WITHOUT explicit provider/model so it falls
        through to ``LLM_PROVIDER`` / ``LLM_MODEL`` env vars (#4032)."""
        mock_response = MagicMock()
        mock_response.text = "Summary."
        mock_response.input_tokens = 50
        mock_response.output_tokens = 10
        mock_call_llm.return_value = mock_response

        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        summarize_ruling(ruling_text, client=MagicMock())

        call_kwargs = mock_call_llm.call_args
        assert "provider" not in call_kwargs.kwargs
        assert "model" not in call_kwargs.kwargs

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_uses_correct_system_prompt(self, mock_call_llm: MagicMock) -> None:
        """System prompt matches the backfill and nlp-pipeline summarizer."""
        mock_response = MagicMock()
        mock_response.text = "Summary."
        mock_response.input_tokens = 50
        mock_response.output_tokens = 10
        mock_call_llm.return_value = mock_response

        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        summarize_ruling(ruling_text, client=MagicMock())

        call_kwargs = mock_call_llm.call_args
        assert call_kwargs.kwargs["system_prompt"] == _SYSTEM_PROMPT

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_no_case_context_omits_context_section(self, mock_call_llm: MagicMock) -> None:
        """When case_context is None, no context section in user message."""
        mock_response = MagicMock()
        mock_response.text = "Summary."
        mock_response.input_tokens = 50
        mock_response.output_tokens = 10
        mock_call_llm.return_value = mock_response

        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        summarize_ruling(ruling_text, case_context=None, client=MagicMock())

        call_kwargs = mock_call_llm.call_args
        user_message = call_kwargs.kwargs["user_message"]
        assert "Case context:" not in user_message

    @patch("ingestion.ruling_summarizer.call_llm")
    def test_passes_client_through(self, mock_call_llm: MagicMock) -> None:
        """The client object is passed through to call_llm."""
        mock_response = MagicMock()
        mock_response.text = "Summary."
        mock_response.input_tokens = 50
        mock_response.output_tokens = 10
        mock_call_llm.return_value = mock_response

        client = MagicMock()
        ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
        summarize_ruling(ruling_text, client=client)

        call_kwargs = mock_call_llm.call_args
        assert call_kwargs.kwargs["client"] is client


# ---------------------------------------------------------------------------
# Regression test for #4032: provider falls through to LLM_PROVIDER env var.
# ---------------------------------------------------------------------------


class TestProviderFallthrough:
    """Regression tests for #4032 — summarizer must not pin provider."""

    def test_routes_through_google_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: with ``LLM_PROVIDER=google`` set, summarize_ruling
        dispatches through ``_call_google`` (not ``_call_anthropic``), and
        the model returned is the Google default — not Anthropic Haiku."""
        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.delenv("LLM_MODEL", raising=False)

        google_response = LLMResponse(
            text="The court granted the motion.",
            input_tokens=100,
            output_tokens=20,
        )

        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch("ingestion.llm_providers._call_anthropic") as mock_anthropic,
        ):
            mock_google.return_value = google_response

            ruling_text = "The motion for summary judgment is hereby GRANTED. " * 5
            summary, model = summarize_ruling(ruling_text, client=MagicMock())

        mock_google.assert_called_once()
        mock_anthropic.assert_not_called()
        # Model resolves to google's default and is returned for DB persistence.
        assert model == "gemini-2.5-flash-lite"
        assert summary == "The court granted the motion."
        # The model arg into the google adapter matches.
        google_call_args = mock_google.call_args
        assert google_call_args[0][2] == "gemini-2.5-flash-lite"
