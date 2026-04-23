"""Tests for the ruling summarization module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from judgemind_config import DEFAULT_HAIKU_MODEL

from summarization.summarizer import RulingSummarizer

# ---------------------------------------------------------------------------
# Fixture ruling texts (realistic California tentative rulings)
# ---------------------------------------------------------------------------

RULING_MSJ_FULL = """\
TENTATIVE RULING

Case No. 23STCV12345
Dept. 68

Hearing Date: March 15, 2025

Smith v. Jones Industries, Inc.

Judge Robert M. Johnson

Motion for Summary Judgment

Defendant Jones Industries, Inc.'s Motion for Summary Judgment is GRANTED.

Attorney for Plaintiff: Sarah Chen
Attorney for Defendant: Michael R. Davis

The Court finds no triable issues of material fact. Defendant is entitled to
judgment as a matter of law pursuant to CCP § 437c. The motion complies with
the requirements of CCP § 437c(b)(1).

Sanctions of $2,500 are imposed against plaintiff under CCP § 128.5.

Judgment shall be entered in favor of defendant.
"""

RULING_DEMURRER = """\
TENTATIVE RULING

Case No. 24VECV00789
Department 12

Hearing Date: April 3, 2025

Garcia v. Pacific Holdings LLC et al.

Demurrer to First Amended Complaint

The demurrer filed by defendant Pacific Holdings LLC is SUSTAINED IN PART
AND OVERRULED IN PART.

The demurrer to the first cause of action for fraud (Cal. Civ. Code § 1572)
is sustained with 20 days leave to amend. The demurrer to the second cause
of action for breach of contract is overruled.

Plaintiff Maria Garcia shall have 20 days to file a Second Amended Complaint.
Defendant Pacific Holdings LLC to give notice.

Attorney for Plaintiff: James T. O'Brien
Attorney for Defendant: Lisa Yamamoto
"""

RULING_MINIMAL = """\
TENTATIVE RULING

The motion is denied.
"""

RULING_MOTION_TO_COMPEL = """\
TENTATIVE RULING

Case No. 22AHCV01340

Motion to Compel Further Discovery Responses

Plaintiffs Johnson and Williams move for an order compelling defendant
American National Insurance Company to provide further responses to
Form Interrogatories (Set Two).

The motion to compel further is GRANTED and Defendant is ordered to serve
a further response to Form Interrogatories, Set Two, Nos. 15.1 and 17.1
within 20 days.

Monetary sanctions of $1,250.00 are awarded against defendant and its
counsel of record, jointly and severally, pursuant to CCP § 2030.300(d).
Sanctions to be paid within 30 days.

Judge of the Superior Court
"""

RULING_ANTI_SLAPP = """\
TENTATIVE RULING

Case No. 24NWCV01234
Dept. 4

Brown v. State Farm Insurance

Anti-SLAPP Motion (CCP § 425.16)

Defendant's special motion to strike under CCP § 425.16 is DENIED.

Plaintiff has demonstrated a probability of prevailing on the merits.
The Court considered the standards set forth in Evidence Code § 452,
Cal. Civ. Code § 47(b), and CCP § 425.16(b)(1).

Attorney fees under CCP § 425.16(c)(1) are denied.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_response(summary_text: str) -> MagicMock:
    """Create a mock Anthropic API response with the given summary text."""
    content_block = MagicMock()
    content_block.text = summary_text
    response = MagicMock()
    response.content = [content_block]
    return response


def _make_summarizer() -> RulingSummarizer:
    """Create a RulingSummarizer with a dummy API key."""
    return RulingSummarizer(api_key="test-key-not-real")


# ---------------------------------------------------------------------------
# Tests — input validation
# ---------------------------------------------------------------------------


class TestRulingSummarizerValidation:
    """Test input validation."""

    def test_empty_text_raises(self) -> None:
        summarizer = _make_summarizer()
        with pytest.raises(ValueError, match="must not be empty"):
            summarizer.summarize("")

    def test_whitespace_only_raises(self) -> None:
        summarizer = _make_summarizer()
        with pytest.raises(ValueError, match="must not be empty"):
            summarizer.summarize("   \n\t  ")

    def test_no_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="api_key must be provided"):
                RulingSummarizer()

    def test_env_var_api_key(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}):
            with patch("summarization.summarizer.anthropic.Anthropic") as mock_cls:
                summarizer = RulingSummarizer()
                assert summarizer is not None
                mock_cls.assert_called_once_with(api_key="env-key")


# ---------------------------------------------------------------------------
# Tests — summarization with mocked API responses
# ---------------------------------------------------------------------------


class TestRulingSummarizerSummarize:
    """Test summarization with mocked API responses."""

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_msj_ruling_summary(self, mock_anthropic_cls: MagicMock) -> None:
        """Test summarization of a motion for summary judgment ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        expected_summary = (
            "The defendant's motion for summary judgment was granted. "
            "The court found no triable issues of material fact, meaning "
            "the defendant wins the case without a trial. The plaintiff "
            "was also ordered to pay $2,500 in sanctions."
        )
        mock_client.messages.create.return_value = _make_mock_response(expected_summary)

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_MSJ_FULL)

        assert result == expected_summary

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_demurrer_ruling_summary(self, mock_anthropic_cls: MagicMock) -> None:
        """Test summarization of a demurrer ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        expected_summary = (
            "The defendant challenged the plaintiff's complaint through a demurrer. "
            "The court partially agreed, dismissing the fraud claim but allowing "
            "the breach of contract claim to proceed. The plaintiff has 20 days "
            "to refile the fraud claim with more details."
        )
        mock_client.messages.create.return_value = _make_mock_response(expected_summary)

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_DEMURRER)

        assert result == expected_summary

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_minimal_ruling_summary(self, mock_anthropic_cls: MagicMock) -> None:
        """Test summarization of a minimal ruling with little information."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        expected_summary = "The court denied the motion."
        mock_client.messages.create.return_value = _make_mock_response(expected_summary)

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_MINIMAL)

        assert result == expected_summary

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_motion_to_compel_summary(self, mock_anthropic_cls: MagicMock) -> None:
        """Test summarization of a motion to compel ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        expected_summary = (
            "The plaintiffs asked the court to force the defendant insurance company "
            "to provide fuller answers to discovery questions. The court granted "
            "the motion and ordered the defendant to respond within 20 days. "
            "The defendant must also pay $1,250 in sanctions."
        )
        mock_client.messages.create.return_value = _make_mock_response(expected_summary)

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_MOTION_TO_COMPEL)

        assert result == expected_summary

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_anti_slapp_summary(self, mock_anthropic_cls: MagicMock) -> None:
        """Test summarization of an anti-SLAPP ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        expected_summary = (
            "The defendant filed a special motion to dismiss the case under "
            "California's anti-SLAPP law, which protects free speech. The court "
            "denied the motion, finding the plaintiff showed a likelihood of "
            "winning on the merits."
        )
        mock_client.messages.create.return_value = _make_mock_response(expected_summary)

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_ANTI_SLAPP)

        assert result == expected_summary

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_returns_string(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify the return type is a plain string."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        result = summarizer.summarize("Test ruling text")

        assert isinstance(result, str)

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_strips_whitespace_from_response(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify leading/trailing whitespace is stripped from API response."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            "  A summary with whitespace.  \n"
        )

        summarizer = _make_summarizer()
        result = summarizer.summarize("Test ruling text")

        assert result == "A summary with whitespace."


# ---------------------------------------------------------------------------
# Tests — case context
# ---------------------------------------------------------------------------


class TestRulingSummarizerCaseContext:
    """Test that case context is passed to the API."""

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_case_context_included_in_message(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify case context is included in the user message."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize(
            RULING_MSJ_FULL,
            case_context={"case_title": "Smith v. Jones", "motion_type": "MSJ"},
        )

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        user_content = messages[0]["content"]
        assert "case_title: Smith v. Jones" in user_content
        assert "motion_type: MSJ" in user_content

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_no_context_still_works(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify summarization works without case context."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_MSJ_FULL)

        assert result == "A summary."

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_empty_context_dict_no_crash(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify an empty context dict does not crash."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        result = summarizer.summarize(RULING_MSJ_FULL, case_context={})

        assert result == "A summary."


# ---------------------------------------------------------------------------
# Tests — API interaction
# ---------------------------------------------------------------------------


class TestRulingSummarizerApiInteraction:
    """Test that the summarizer interacts with the Anthropic API correctly."""

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_uses_haiku_model(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize("Test ruling text")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["model"] == DEFAULT_HAIKU_MODEL

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_passes_ruling_text_in_message(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize("My ruling text here")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert len(messages) == 1
        assert "My ruling text here" in messages[0]["content"]

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_max_tokens_sufficient(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify max_tokens is large enough for summary responses."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize("Test ruling")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["max_tokens"] >= 256

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_system_prompt_mentions_plain_language(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify the system prompt instructs for plain language output."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize("Test ruling")

        call_kwargs = mock_client.messages.create.call_args
        system = call_kwargs.kwargs["system"]
        assert "plain language" in system.lower()

    @patch("summarization.summarizer.anthropic.Anthropic")
    def test_system_prompt_mentions_sentences(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify the system prompt requests 2-4 sentences."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response("A summary.")

        summarizer = _make_summarizer()
        summarizer.summarize("Test ruling")

        call_kwargs = mock_client.messages.create.call_args
        system = call_kwargs.kwargs["system"]
        assert "2-4 sentences" in system
