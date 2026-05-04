"""Tests for the ingestion validation gate (validation/gate.py).

All LLM calls are mocked so these tests run offline in CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from validation.gate import (
    ValidationResult,
    _parse_validation_response,
    insert_validation_result,
    validate_document,
)

# ---------------------------------------------------------------------------
# Tests for validate_document
# ---------------------------------------------------------------------------


class TestValidateDocument:
    """Tests for the validate_document function."""

    def test_returns_pass_when_ruling_text_is_none(self) -> None:
        """No ruling text means nothing to validate."""
        result = validate_document(
            ruling_text=None,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name="Smith, John",
            motion_type="summary_judgment",
            outcome="granted",
            hearing_date="2026-03-05",
            department="Dept. 1",
            county="Los Angeles",
        )
        assert result.result == "pass"
        assert result.latency_ms == 0

    def test_returns_pass_when_ruling_text_is_too_short(self) -> None:
        """Text shorter than minimum length skips validation."""
        result = validate_document(
            ruling_text="Short text",
            case_number="23STCV12345",
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county=None,
        )
        assert result.result == "pass"
        assert result.latency_ms == 0

    @patch("validation.gate.call_llm")
    def test_returns_pass_on_successful_pass_response(self, mock_call_llm: MagicMock) -> None:
        """LLM returns pass result."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=100,
            output_tokens=20,
        )

        result = validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name="Smith, John",
            motion_type="summary_judgment",
            outcome="granted",
            hearing_date="2026-03-05",
            department="Dept. 1",
            county="Los Angeles",
        )
        assert result.result == "pass"
        assert result.reason is None
        assert result.input_tokens == 100
        assert result.output_tokens == 20
        mock_call_llm.assert_called_once()

    @patch("validation.gate.call_llm")
    def test_returns_flag_on_flag_response(self, mock_call_llm: MagicMock) -> None:
        """LLM returns flag result with reason."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "flag", "reason": "Case title does not match ruling parties"}',
            input_tokens=100,
            output_tokens=30,
        )

        result = validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )
        assert result.result == "flag"
        assert result.reason == "Case title does not match ruling parties"

    @patch("validation.gate.call_llm")
    def test_returns_fail_on_fail_response(self, mock_call_llm: MagicMock) -> None:
        """LLM returns fail result with reason."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "fail", "reason": "Ruling text is about a completely different case"}',
            input_tokens=100,
            output_tokens=30,
        )

        result = validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title="Smith v. Jones",
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )
        assert result.result == "fail"
        assert "completely different case" in (result.reason or "")

    @patch("validation.gate.call_llm")
    def test_returns_error_on_llm_none_response(self, mock_call_llm: MagicMock) -> None:
        """LLM returns None (e.g., timeout) — treated as error/pass-through."""
        mock_call_llm.return_value = None

        result = validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )
        assert result.result == "error"
        assert result.reason == "LLM call returned None"

    @patch("validation.gate.call_llm")
    def test_returns_error_on_llm_exception(self, mock_call_llm: MagicMock) -> None:
        """LLM raises an exception — treated as error/pass-through."""
        mock_call_llm.side_effect = RuntimeError("connection refused")

        result = validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )
        assert result.result == "error"
        assert "connection refused" in (result.reason or "")

    @patch("validation.gate.call_llm")
    def test_only_includes_non_none_fields_in_prompt(self, mock_call_llm: MagicMock) -> None:
        """User message should only include fields that are not None."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=50,
            output_tokens=10,
        )

        validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )

        call_args = mock_call_llm.call_args
        user_message = call_args[0][1]  # Second positional arg
        assert "case_number" in user_message
        assert "county" in user_message
        # None fields should NOT be in the message
        assert "case_title" not in user_message
        assert "judge_name" not in user_message

    @patch("validation.gate.call_llm")
    def test_truncates_ruling_text_excerpt(self, mock_call_llm: MagicMock) -> None:
        """Ruling text is truncated to 500 chars in the prompt."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=50,
            output_tokens=10,
        )

        long_text = "A" * 1000
        validate_document(
            ruling_text=long_text,
            case_number=None,
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county=None,
        )

        call_args = mock_call_llm.call_args
        user_message = call_args[0][1]
        # Should contain truncation marker
        assert "[...]" in user_message


# ---------------------------------------------------------------------------
# Tests for _parse_validation_response
# ---------------------------------------------------------------------------


class TestParseValidationResponse:
    """Tests for _parse_validation_response."""

    def test_parses_valid_pass_json(self) -> None:
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=100,
            output_tokens=20,
        )
        result = _parse_validation_response(response, model="test-model", latency_ms=50)
        assert result.result == "pass"
        assert result.reason is None
        assert result.model == "test-model"
        assert result.latency_ms == 50

    def test_parses_valid_flag_json(self) -> None:
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text='{"result": "flag", "reason": "mismatch detected"}',
            input_tokens=100,
            output_tokens=20,
        )
        result = _parse_validation_response(response, model="test-model", latency_ms=50)
        assert result.result == "flag"
        assert result.reason == "mismatch detected"

    def test_handles_markdown_code_fences(self) -> None:
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text='```json\n{"result": "pass", "reason": null}\n```',
            input_tokens=100,
            output_tokens=20,
        )
        result = _parse_validation_response(response, model="test-model", latency_ms=50)
        assert result.result == "pass"

    def test_unknown_result_treated_as_pass(self) -> None:
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text='{"result": "maybe", "reason": "not sure"}',
            input_tokens=100,
            output_tokens=20,
        )
        result = _parse_validation_response(response, model="test-model", latency_ms=50)
        assert result.result == "pass"

    def test_invalid_json_returns_error(self) -> None:
        from ingestion.llm_providers import LLMResponse

        response = LLMResponse(
            text="This is not JSON at all",
            input_tokens=100,
            output_tokens=20,
        )
        result = _parse_validation_response(response, model="test-model", latency_ms=50)
        assert result.result == "error"
        assert "parse error" in (result.reason or "").lower()


# ---------------------------------------------------------------------------
# Tests for insert_validation_result
# ---------------------------------------------------------------------------


class TestInsertValidationResult:
    """Tests for insert_validation_result."""

    def test_inserts_pass_result(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = ValidationResult(
            result="pass",
            reason=None,
            model="test-model",
            input_tokens=100,
            output_tokens=20,
            latency_ms=50,
        )

        insert_validation_result(
            mock_conn,
            document_id="doc-123",
            ruling_id=None,
            result=result,
        )

        mock_cur.execute.assert_called_once()
        call_args = mock_cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "INSERT INTO validation_results" in sql
        assert params[0] == "doc-123"  # document_id
        assert params[1] is None  # ruling_id
        assert params[2] == "pass"  # result
        assert params[3] is None  # reason
        assert params[4] == "test-model"  # model
        assert params[5] == 100  # input_tokens
        assert params[6] == 20  # output_tokens
        assert params[7] == 50  # latency_ms

    def test_inserts_flag_result_with_reason(self) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = ValidationResult(
            result="flag",
            reason="Case title mismatch",
            model="test-model",
            input_tokens=100,
            output_tokens=30,
            latency_ms=75,
        )

        insert_validation_result(
            mock_conn,
            document_id="doc-456",
            ruling_id="ruling-789",
            result=result,
        )

        call_args = mock_cur.execute.call_args
        params = call_args[0][1]
        assert params[0] == "doc-456"
        assert params[1] == "ruling-789"
        assert params[2] == "flag"
        assert params[3] == "Case title mismatch"


# ---------------------------------------------------------------------------
# Regression test for #4032: provider falls through to LLM_PROVIDER env var.
# ---------------------------------------------------------------------------


class TestProviderFallthrough:
    """Regression tests for #4032 — validation gate must not pin provider."""

    @patch("validation.gate.call_llm")
    def test_does_not_pin_provider_when_caller_omits(self, mock_call_llm: MagicMock) -> None:
        """When the caller omits provider, validate_document does not inject
        a hardcoded ``"anthropic"``. The ``provider`` kwarg passed to
        ``call_llm`` is ``None`` so it falls through to ``LLM_PROVIDER``."""
        from ingestion.llm_providers import LLMResponse

        mock_call_llm.return_value = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=100,
            output_tokens=20,
        )

        validate_document(
            ruling_text="The motion for summary judgment is GRANTED. " * 5,
            case_number="23STCV12345",
            case_title=None,
            judge_name=None,
            motion_type=None,
            outcome=None,
            hearing_date=None,
            department=None,
            county="Los Angeles",
        )

        call_kwargs = mock_call_llm.call_args.kwargs
        assert call_kwargs.get("provider") is None, (
            f"validate_document must not pin provider (#4032), got {call_kwargs.get('provider')!r}"
        )

    def test_routes_through_google_when_env_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """End-to-end: with ``LLM_PROVIDER=google`` set, validate_document
        dispatches through ``_call_google`` (not ``_call_anthropic``)."""
        from ingestion.llm_providers import LLMResponse

        monkeypatch.setenv("LLM_PROVIDER", "google")
        monkeypatch.delenv("LLM_MODEL", raising=False)

        google_response = LLMResponse(
            text='{"result": "pass", "reason": null}',
            input_tokens=100,
            output_tokens=20,
        )

        with (
            patch("ingestion.llm_providers._call_google") as mock_google,
            patch("ingestion.llm_providers._call_anthropic") as mock_anthropic,
        ):
            mock_google.return_value = google_response

            result = validate_document(
                ruling_text="The motion for summary judgment is GRANTED. " * 5,
                case_number="23STCV12345",
                case_title=None,
                judge_name=None,
                motion_type=None,
                outcome=None,
                hearing_date=None,
                department=None,
                county="Los Angeles",
            )

        mock_google.assert_called_once()
        mock_anthropic.assert_not_called()
        # Model resolves to google's default
        google_call_args = mock_google.call_args
        assert google_call_args[0][2] == "gemini-2.5-flash-lite"
        # And the recorded model in ValidationResult matches
        assert result.model == "gemini-2.5-flash-lite"
