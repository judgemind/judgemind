"""nlp-pipeline Anthropic calls are valid for the installed SDK (#4960).

``client.messages`` is autospecced against the real
``anthropic.resources.messages.Messages``, so a keyword the installed SDK
rejects (anthropic 1.x dropped ``temperature``) fails here, unlike a bare
``MagicMock``.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, create_autospec, patch

import pytest
from anthropic.resources.messages import Messages

from classification.classifier import RulingClassifier
from entity_extraction.extractor import EntityExtractor
from summarization.summarizer import RulingSummarizer


def _autospec_client(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    client = MagicMock()
    client.messages = create_autospec(Messages, instance=True)
    client.messages.create.return_value = response
    return client


def test_autospec_double_rejects_temperature() -> None:
    client = _autospec_client("x")
    with pytest.raises(TypeError):
        client.messages.create(model="m", max_tokens=1, messages=[], temperature=0)


def test_classifier_call_matches_sdk_signature() -> None:
    body = json.dumps({"outcome": "granted", "motion_type": "demurrer", "confidence": 0.9})
    client = _autospec_client(body)
    with patch("anthropic.Anthropic", return_value=client):
        classifier = RulingClassifier(api_key="test-key")

    result = classifier.classify("The demurrer is SUSTAINED.")

    assert client.messages.create.called
    assert result.outcome == "granted"


def test_summarizer_call_matches_sdk_signature() -> None:
    client = _autospec_client("A short summary.")
    with patch("anthropic.Anthropic", return_value=client):
        summarizer = RulingSummarizer(api_key="test-key")

    assert summarizer.summarize("The motion is GRANTED.") == "A short summary."
    assert client.messages.create.called


def test_entity_extractor_call_matches_sdk_signature() -> None:
    client = _autospec_client(json.dumps({"judge_name": "Jane Doe"}))
    with patch("anthropic.Anthropic", return_value=client):
        extractor = EntityExtractor(api_key="test-key")

    assert extractor.extract("Judge Jane Doe presiding.").judge_name == "Jane Doe"
    assert client.messages.create.called
