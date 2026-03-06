"""Tests for the ruling outcome and motion type classifier."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from classification.classifier import (
    MOTION_TYPE_VALUES,
    OUTCOME_VALUES,
    Classification,
    RulingClassifier,
)

# ---------------------------------------------------------------------------
# Fixture ruling texts
# ---------------------------------------------------------------------------

RULING_GRANTED_MSJ = """\
TENTATIVE RULING

Case No. 23STCV12345
Smith v. Jones

Motion for Summary Judgment

The Court has considered the moving papers, opposition, and reply.
Defendant's Motion for Summary Judgment is GRANTED. There are no triable
issues of material fact and defendant is entitled to judgment as a matter
of law. Judgment shall be entered in favor of defendant.
"""

RULING_DENIED_MTD = """\
TENTATIVE RULING

Case No. 24VECV00789
Doe v. Acme Corp.

Defendant Acme Corp.'s Motion to Dismiss for Failure to State a Claim

The motion to dismiss is DENIED. Plaintiff has sufficiently alleged facts
supporting each element of the cause of action. Defendant shall file an
answer within 30 days.
"""

RULING_GRANTED_IN_PART_DEMURRER = """\
TENTATIVE RULING

Case No. 23SMCV04567
Garcia v. Pacific Holdings LLC

Demurrer to Complaint

The demurrer is SUSTAINED IN PART AND OVERRULED IN PART. The demurrer to
the first cause of action for fraud is sustained with 20 days leave to amend.
The demurrer to the second cause of action for breach of contract is overruled.
"""

RULING_MOOT_MIL = """\
TENTATIVE RULING

Case No. 22STCV33210
Taylor v. Metro Transit Authority

Plaintiff's Motion in Limine No. 3 to Exclude Expert Testimony

The motion is MOOT. The parties have stipulated to exclude the expert
testimony at issue. No further ruling is required.
"""

RULING_CONTINUED = """\
TENTATIVE RULING

Case No. 24NWCV01234
Brown v. State Farm Insurance

Motion to Compel Further Discovery Responses

The hearing on this motion is CONTINUED to April 15, 2025, at 8:30 a.m.
in Department 12. The parties are ordered to meet and confer in good faith
before the continued hearing date.
"""

RULING_OFF_CALENDAR = """\
TENTATIVE RULING

Case No. 23CHCV02468
Wilson v. National Bank

Anti-SLAPP Motion (CCP 425.16)

This matter is taken OFF CALENDAR at the request of the moving party.
"""

RULING_SUBMITTED = """\
TENTATIVE RULING

Case No. 22STCV44556
Johnson v. City of Los Angeles

Motion for Preliminary Injunction

The matter is SUBMITTED on the papers. The Court will issue a ruling
after further consideration.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(outcome: str, motion_type: str, confidence: float) -> MagicMock:
    """Create a mock Anthropic API response with the given classification."""
    payload = json.dumps({"outcome": outcome, "motion_type": motion_type, "confidence": confidence})
    content_block = MagicMock()
    content_block.text = payload
    response = MagicMock()
    response.content = [content_block]
    return response


def _make_classifier() -> RulingClassifier:
    """Create a RulingClassifier with a dummy API key."""
    return RulingClassifier(api_key="test-key-not-real")


# ---------------------------------------------------------------------------
# Tests — classification of each outcome type
# ---------------------------------------------------------------------------


class TestRulingClassifierOutcomes:
    """Test that the classifier returns correct outcomes for fixture rulings."""

    @patch("classification.classifier.anthropic.Anthropic")
    def test_granted_msj(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("granted", "msj", 0.95)

        classifier = _make_classifier()
        result = classifier.classify(RULING_GRANTED_MSJ)

        assert result.outcome == "granted"
        assert result.motion_type == "msj"
        assert result.confidence == 0.95

    @patch("classification.classifier.anthropic.Anthropic")
    def test_denied_mtd(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("denied", "mtd", 0.92)

        classifier = _make_classifier()
        result = classifier.classify(RULING_DENIED_MTD)

        assert result.outcome == "denied"
        assert result.motion_type == "mtd"
        assert result.confidence == 0.92

    @patch("classification.classifier.anthropic.Anthropic")
    def test_granted_in_part_demurrer(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "granted_in_part", "demurrer", 0.88
        )

        classifier = _make_classifier()
        result = classifier.classify(RULING_GRANTED_IN_PART_DEMURRER)

        assert result.outcome == "granted_in_part"
        assert result.motion_type == "demurrer"
        assert result.confidence == 0.88

    @patch("classification.classifier.anthropic.Anthropic")
    def test_moot_mil(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("moot", "mil", 0.91)

        classifier = _make_classifier()
        result = classifier.classify(RULING_MOOT_MIL)

        assert result.outcome == "moot"
        assert result.motion_type == "mil"
        assert result.confidence == 0.91

    @patch("classification.classifier.anthropic.Anthropic")
    def test_continued_motion_to_compel(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "continued", "motion_to_compel", 0.87
        )

        classifier = _make_classifier()
        result = classifier.classify(RULING_CONTINUED)

        assert result.outcome == "continued"
        assert result.motion_type == "motion_to_compel"
        assert result.confidence == 0.87

    @patch("classification.classifier.anthropic.Anthropic")
    def test_off_calendar_anti_slapp(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "off_calendar", "anti_slapp", 0.93
        )

        classifier = _make_classifier()
        result = classifier.classify(RULING_OFF_CALENDAR)

        assert result.outcome == "off_calendar"
        assert result.motion_type == "anti_slapp"
        assert result.confidence == 0.93

    @patch("classification.classifier.anthropic.Anthropic")
    def test_submitted_preliminary_injunction(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "submitted", "preliminary_injunction", 0.85
        )

        classifier = _make_classifier()
        result = classifier.classify(RULING_SUBMITTED)

        assert result.outcome == "submitted"
        assert result.motion_type == "preliminary_injunction"
        assert result.confidence == 0.85


# ---------------------------------------------------------------------------
# Tests — Classification dataclass
# ---------------------------------------------------------------------------


class TestClassification:
    """Test the Classification dataclass."""

    def test_frozen(self) -> None:
        c = Classification(outcome="granted", motion_type="msj", confidence=0.95)
        with pytest.raises(AttributeError):
            c.outcome = "denied"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = Classification(outcome="denied", motion_type="mtd", confidence=0.9)
        b = Classification(outcome="denied", motion_type="mtd", confidence=0.9)
        assert a == b


# ---------------------------------------------------------------------------
# Tests — validation
# ---------------------------------------------------------------------------


class TestRulingClassifierValidation:
    """Test input and output validation."""

    def test_empty_text_raises(self) -> None:
        classifier = _make_classifier()
        with pytest.raises(ValueError, match="must not be empty"):
            classifier.classify("")

    def test_whitespace_only_raises(self) -> None:
        classifier = _make_classifier()
        with pytest.raises(ValueError, match="must not be empty"):
            classifier.classify("   \n\t  ")

    @patch("classification.classifier.anthropic.Anthropic")
    def test_invalid_outcome_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "invalid_outcome", "msj", 0.9
        )

        classifier = _make_classifier()
        with pytest.raises(ValueError, match="Invalid outcome"):
            classifier.classify("Some ruling text")

    @patch("classification.classifier.anthropic.Anthropic")
    def test_invalid_motion_type_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "granted", "invalid_motion", 0.9
        )

        classifier = _make_classifier()
        with pytest.raises(ValueError, match="Invalid motion_type"):
            classifier.classify("Some ruling text")

    @patch("classification.classifier.anthropic.Anthropic")
    def test_confidence_below_zero_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("granted", "msj", -0.1)

        classifier = _make_classifier()
        with pytest.raises(ValueError, match="Confidence must be between"):
            classifier.classify("Some ruling text")

    @patch("classification.classifier.anthropic.Anthropic")
    def test_confidence_above_one_raises(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("granted", "msj", 1.5)

        classifier = _make_classifier()
        with pytest.raises(ValueError, match="Confidence must be between"):
            classifier.classify("Some ruling text")


# ---------------------------------------------------------------------------
# Tests — API interaction
# ---------------------------------------------------------------------------


class TestRulingClassifierApiInteraction:
    """Test that the classifier interacts with the Anthropic API correctly."""

    @patch("classification.classifier.anthropic.Anthropic")
    def test_uses_haiku_model(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("granted", "msj", 0.95)

        classifier = _make_classifier()
        classifier.classify("Test ruling text")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["model"] == "claude-3-5-haiku-latest"

    @patch("classification.classifier.anthropic.Anthropic")
    def test_passes_ruling_text_in_message(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("granted", "msj", 0.95)

        classifier = _make_classifier()
        classifier.classify("My ruling text here")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert len(messages) == 1
        assert "My ruling text here" in messages[0]["content"]

    @patch("classification.classifier.anthropic.Anthropic")
    def test_low_confidence_still_returned(self, mock_anthropic_cls: MagicMock) -> None:
        """Low-confidence results are returned (flagging is handled downstream)."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("other", "other", 0.3)

        classifier = _make_classifier()
        result = classifier.classify("Ambiguous ruling text")

        assert result.confidence == 0.3
        assert result.outcome == "other"
        assert result.motion_type == "other"


# ---------------------------------------------------------------------------
# Tests — allowed value sets
# ---------------------------------------------------------------------------


class TestAllowedValues:
    """Verify the allowed value sets match the spec."""

    def test_outcome_values(self) -> None:
        expected = {
            "granted",
            "denied",
            "granted_in_part",
            "denied_in_part",
            "moot",
            "continued",
            "off_calendar",
            "submitted",
            "other",
        }
        assert OUTCOME_VALUES == expected

    def test_motion_type_values(self) -> None:
        expected = {
            "msj",
            "msj_partial",
            "mtd",
            "mil",
            "demurrer",
            "motion_to_compel",
            "motion_to_strike",
            "anti_slapp",
            "preliminary_injunction",
            "other",
        }
        assert MOTION_TYPE_VALUES == expected
