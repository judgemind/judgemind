"""Anthropic call sites are valid for the installed SDK (#4960).

anthropic 1.x dropped ``temperature`` / ``top_p`` / ``top_k`` from the
``Messages.create`` signature, so passing ``temperature=0`` raises
``TypeError`` before any request is sent.  The older unit tests replaced
``client.messages`` with a bare ``MagicMock``, which accepts any keyword,
so nothing caught it.  Here ``client.messages`` is autospecced against the
real ``anthropic.resources.messages.Messages``: an unsupported keyword
fails in the test exactly as it would in production.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, create_autospec, patch

import anthropic
import pytest
from anthropic.resources.messages import Messages
from judgemind_config import DEFAULT_HAIKU_MODEL

from framework.llm_extractor import LlmExtractor
from ingestion.llm_providers import (
    _call_anthropic,
    _call_anthropic_with_images,
    anthropic_messages_create,
    anthropic_sampling_params,
    new_anthropic_client,
)

_RULING_JSON = json.dumps(
    {
        "extracted_judge_name": "Gassia Apkarian",
        "hearing_date": "2026-02-24",
        "department": "C25",
        "rulings": [
            {
                "extracted_case_number": "2024-01393434",
                "extracted_case_title": "Martinez v. ABC Manufacturing Inc.",
                "case_type": "civil",
                "outcome": "denied",
                "motion_type": "msj_partial",
                "ruling_text": "The motion is DENIED.",
                "hearing_date": "2026-02-24",
                "extracted_parties": [],
                "confidence": {
                    "case_number": "high",
                    "case_title": "high",
                    "parties": "high",
                    "judge": "high",
                    "ruling_text": "high",
                    "outcome": "high",
                },
            }
        ],
    }
)


def _response(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage.input_tokens = 10
    response.usage.output_tokens = 5
    response.stop_reason = "end_turn"
    return response


def _autospec_client(text: str = "ok") -> MagicMock:
    """A client whose ``messages`` enforces the real SDK signature."""
    client = MagicMock()
    client.messages = create_autospec(Messages, instance=True)
    client.messages.create.return_value = _response(text)
    return client


class TestAutospecDouble:
    def test_autospec_create_rejects_temperature_kwarg(self) -> None:
        """The double enforces the installed SDK's signature, so a call site
        passing ``temperature`` fails in unit tests (#4960)."""
        client = _autospec_client()
        with pytest.raises(TypeError):
            client.messages.create(
                model=DEFAULT_HAIKU_MODEL,
                max_tokens=16,
                temperature=0,
                messages=[{"role": "user", "content": "hi"}],
            )


class TestCallSitesUnderAutospec:
    def test_llm_extractor_anthropic_extract(self) -> None:
        client = _autospec_client(_RULING_JSON)
        with patch.object(anthropic, "Anthropic", return_value=client):
            ext = LlmExtractor(provider="anthropic", api_key="test-key")
        ext._client = client

        rulings = ext.extract("Case No. 2024-01393434\nMartinez v. ABC Manufacturing")

        assert client.messages.create.called
        assert len(rulings) == 1
        assert rulings[0].extracted_case_number == "2024-01393434"
        kwargs = client.messages.create.call_args.kwargs
        assert "temperature" not in kwargs
        assert kwargs["extra_body"] == {"temperature": 0}

    def test_call_anthropic(self) -> None:
        client = _autospec_client("hello")

        result = _call_anthropic("sys", "user", DEFAULT_HAIKU_MODEL, client=client)

        assert client.messages.create.called
        assert result is not None
        assert result.text == "hello"

    def test_call_anthropic_with_images(self) -> None:
        client = _autospec_client("seen")

        result = _call_anthropic_with_images(
            "sys", "describe", [(b"\x89PNG", "image/png")], DEFAULT_HAIKU_MODEL, client=client
        )

        assert client.messages.create.called
        assert result is not None
        assert result.text == "seen"


class TestAnthropicMessagesCreateMerge:
    def test_caller_extra_body_merged_and_request_not_mutated(self) -> None:
        client = _autospec_client()
        request = {
            "model": DEFAULT_HAIKU_MODEL,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
            "extra_body": {"temperature": 0.5, "foo": 1},
        }
        snapshot = json.loads(json.dumps(request))

        anthropic_messages_create(
            client, request, max_retries=0, timeout=None, deadline=None, event_prefix="t_"
        )

        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["extra_body"] == {"temperature": 0.5, "foo": 1}
        assert request == snapshot

    def test_request_without_model_passed_through(self) -> None:
        """No model id, no sampling guess: the SDK reports the missing model."""
        client = MagicMock()
        anthropic_messages_create(
            client,
            {"max_tokens": 16, "messages": []},
            max_retries=0,
            timeout=None,
            deadline=None,
            event_prefix="t_",
        )
        assert client.messages.create.call_args.kwargs == {"max_tokens": 16, "messages": []}

    def test_no_sampling_for_rejecting_model(self) -> None:
        client = _autospec_client()
        anthropic_messages_create(
            client,
            {
                "model": "claude-opus-4-7",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            max_retries=0,
            timeout=None,
            deadline=None,
            event_prefix="t_",
        )
        assert "extra_body" not in client.messages.create.call_args.kwargs


@pytest.mark.parametrize(
    "model",
    [
        "claude-3-7-sonnet-20250219",
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
        "claude-opus-4-1-20250805",
        "claude-haiku-4-5-20251001",
        "claude-haiku-4-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-6",
        "claude-opus-4-5@20251101",
        "claude-sonnet-4",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        DEFAULT_HAIKU_MODEL,
    ],
)
def test_sampling_params_accepting_models(model: str) -> None:
    assert anthropic_sampling_params(model) == {"extra_body": {"temperature": 0}}


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-fable-5-1",
        "claude-mythos-1",
        "us.anthropic.claude-opus-4-7",
        "test-model",
        "",
    ],
)
def test_sampling_params_rejecting_models(model: str) -> None:
    assert anthropic_sampling_params(model) == {}


_MESSAGE_BODY = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


class TestRealTransportBody:
    """A real ``anthropic.Anthropic`` over a stubbed transport: the JSON
    body carries ``temperature`` only for models that accept it."""

    @staticmethod
    def _send(model: str) -> dict:
        import httpx2

        bodies: list[dict] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, json=_MESSAGE_BODY)

        http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
        client = new_anthropic_client(api_key="test-key", http_client=http_client)
        result = _call_anthropic("sys", "user", model, client=client)
        assert result is not None
        assert len(bodies) == 1
        return bodies[0]

    def test_haiku_body_has_temperature_zero(self) -> None:
        body = self._send("claude-haiku-4-5-20251001")
        assert body["temperature"] == 0

    def test_opus_4_7_body_has_no_temperature(self) -> None:
        body = self._send("claude-opus-4-7")
        assert "temperature" not in body
