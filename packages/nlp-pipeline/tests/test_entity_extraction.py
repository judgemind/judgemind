"""Tests for the entity extraction module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from entity_extraction.extractor import EntityExtractor, ExtractedEntities

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

RULING_MINIMAL = """\
TENTATIVE RULING

The motion is denied.
"""

RULING_WITH_MONEY = """\
TENTATIVE RULING

Case No. 23SMCV04567

Petition for Approval of Compromise of Claim

The petition for approval of the compromise of claim of minor
plaintiff is GRANTED. The settlement amount of $150,000 is approved.
Attorney fees in the amount of $50,000.00 (33.33%) and costs of
$3,456.78 are approved.

The net settlement of $96,543.22 shall be deposited into a blocked
account at Wells Fargo Bank.
"""

RULING_STATUTES = """\
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


def _make_mock_response(entities: dict[str, object]) -> MagicMock:
    """Create a mock Anthropic API response with the given entities JSON."""
    payload = json.dumps(entities)
    content_block = MagicMock()
    content_block.text = payload
    response = MagicMock()
    response.content = [content_block]
    return response


def _make_extractor() -> EntityExtractor:
    """Create an EntityExtractor with a dummy API key."""
    return EntityExtractor(api_key="test-key-not-real")


# ---------------------------------------------------------------------------
# Tests — ExtractedEntities dataclass
# ---------------------------------------------------------------------------


class TestExtractedEntities:
    """Test the ExtractedEntities dataclass."""

    def test_frozen(self) -> None:
        e = ExtractedEntities(judge_name="Smith, John", case_number="23STCV12345")
        with pytest.raises(AttributeError):
            e.judge_name = "Other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        e = ExtractedEntities(judge_name=None, case_number=None)
        assert e.department is None
        assert e.hearing_date is None
        assert e.parties == []
        assert e.attorneys == []
        assert e.monetary_amounts == []
        assert e.statute_refs == []

    def test_equality(self) -> None:
        a = ExtractedEntities(
            judge_name="Johnson, Robert M.",
            case_number="23STCV12345",
            parties=["Smith", "Jones"],
        )
        b = ExtractedEntities(
            judge_name="Johnson, Robert M.",
            case_number="23STCV12345",
            parties=["Smith", "Jones"],
        )
        assert a == b

    def test_all_fields_populated(self) -> None:
        e = ExtractedEntities(
            judge_name="Johnson, Robert M.",
            case_number="23STCV12345",
            department="Dept. 68",
            hearing_date="2025-03-15",
            parties=["Smith", "Jones Industries, Inc."],
            attorneys=["Sarah Chen", "Michael R. Davis"],
            monetary_amounts=["$2,500"],
            statute_refs=["CCP § 437c"],
        )
        assert e.judge_name == "Johnson, Robert M."
        assert e.case_number == "23STCV12345"
        assert e.department == "Dept. 68"
        assert e.hearing_date == "2025-03-15"
        assert len(e.parties) == 2
        assert len(e.attorneys) == 2
        assert len(e.monetary_amounts) == 1
        assert len(e.statute_refs) == 1


# ---------------------------------------------------------------------------
# Tests — input validation
# ---------------------------------------------------------------------------


class TestEntityExtractorValidation:
    """Test input validation."""

    def test_empty_text_raises(self) -> None:
        extractor = _make_extractor()
        with pytest.raises(ValueError, match="must not be empty"):
            extractor.extract("")

    def test_whitespace_only_raises(self) -> None:
        extractor = _make_extractor()
        with pytest.raises(ValueError, match="must not be empty"):
            extractor.extract("   \n\t  ")

    def test_no_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ValueError, match="api_key must be provided"):
                EntityExtractor()

    def test_env_var_api_key(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}):
            with patch("entity_extraction.extractor.anthropic.Anthropic") as mock_cls:
                extractor = EntityExtractor()
                assert extractor is not None
                mock_cls.assert_called_once_with(api_key="env-key")


# ---------------------------------------------------------------------------
# Tests — entity extraction from ruling texts
# ---------------------------------------------------------------------------


class TestEntityExtractorExtraction:
    """Test entity extraction with mocked API responses."""

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_full_ruling_extraction(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction from a ruling with all entity types present."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": "Johnson, Robert M.",
                "case_number": "23STCV12345",
                "department": "Dept. 68",
                "hearing_date": "2025-03-15",
                "parties": ["Smith", "Jones Industries, Inc."],
                "attorneys": ["Sarah Chen", "Michael R. Davis"],
                "monetary_amounts": ["$2,500"],
                "statute_refs": ["CCP § 437c", "CCP § 437c(b)(1)", "CCP § 128.5"],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_MSJ_FULL)

        assert result.judge_name == "Johnson, Robert M."
        assert result.case_number == "23STCV12345"
        assert result.department == "Dept. 68"
        assert result.hearing_date == "2025-03-15"
        assert "Smith" in result.parties
        assert "Jones Industries, Inc." in result.parties
        assert "Sarah Chen" in result.attorneys
        assert "Michael R. Davis" in result.attorneys
        assert "$2,500" in result.monetary_amounts
        assert "CCP § 437c" in result.statute_refs

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_demurrer_extraction(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction from a demurrer ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": "24VECV00789",
                "department": "Department 12",
                "hearing_date": "2025-04-03",
                "parties": ["Garcia", "Pacific Holdings LLC"],
                "attorneys": ["James T. O'Brien", "Lisa Yamamoto"],
                "monetary_amounts": [],
                "statute_refs": ["Cal. Civ. Code § 1572"],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_DEMURRER)

        assert result.judge_name is None
        assert result.case_number == "24VECV00789"
        assert result.department == "Department 12"
        assert len(result.parties) == 2
        assert len(result.attorneys) == 2
        assert result.monetary_amounts == []
        assert "Cal. Civ. Code § 1572" in result.statute_refs

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_minimal_ruling(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction from a ruling with minimal information."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": None,
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_MINIMAL)

        assert result.judge_name is None
        assert result.case_number is None
        assert result.department is None
        assert result.hearing_date is None
        assert result.parties == []
        assert result.attorneys == []
        assert result.monetary_amounts == []
        assert result.statute_refs == []

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_monetary_amounts_extraction(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction of multiple monetary amounts."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": "23SMCV04567",
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [
                    "$150,000",
                    "$50,000.00",
                    "$3,456.78",
                    "$96,543.22",
                ],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_WITH_MONEY)

        assert len(result.monetary_amounts) == 4
        assert "$150,000" in result.monetary_amounts
        assert "$50,000.00" in result.monetary_amounts

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_statute_refs_extraction(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction of multiple statute references."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": "24NWCV01234",
                "department": "Dept. 4",
                "hearing_date": None,
                "parties": ["Brown", "State Farm Insurance"],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [
                    "CCP § 425.16",
                    "Evidence Code § 452",
                    "Cal. Civ. Code § 47(b)",
                    "CCP § 425.16(b)(1)",
                    "CCP § 425.16(c)(1)",
                ],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_STATUTES)

        assert len(result.statute_refs) == 5
        assert "CCP § 425.16" in result.statute_refs
        assert "Evidence Code § 452" in result.statute_refs

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_motion_to_compel_extraction(self, mock_anthropic_cls: MagicMock) -> None:
        """Test extraction from a motion to compel ruling."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": "22AHCV01340",
                "department": None,
                "hearing_date": None,
                "parties": [
                    "Johnson",
                    "Williams",
                    "American National Insurance Company",
                ],
                "attorneys": [],
                "monetary_amounts": ["$1,250.00"],
                "statute_refs": ["CCP § 2030.300(d)"],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract(RULING_MOTION_TO_COMPEL)

        assert result.case_number == "22AHCV01340"
        assert len(result.parties) == 3
        assert "$1,250.00" in result.monetary_amounts
        assert "CCP § 2030.300(d)" in result.statute_refs


# ---------------------------------------------------------------------------
# Tests — API interaction
# ---------------------------------------------------------------------------


class TestEntityExtractorApiInteraction:
    """Test that the extractor interacts with the Anthropic API correctly."""

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_uses_haiku_model(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": None,
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        extractor.extract("Test ruling text")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["model"] == "claude-3-5-haiku-latest"

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_passes_ruling_text_in_message(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": None,
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        extractor.extract("My ruling text here")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs["messages"]
        assert len(messages) == 1
        assert "My ruling text here" in messages[0]["content"]

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_max_tokens_sufficient(self, mock_anthropic_cls: MagicMock) -> None:
        """Verify max_tokens is large enough for entity extraction responses."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": None,
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        extractor.extract("Test ruling")

        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["max_tokens"] >= 512

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_returns_extracted_entities_type(self, mock_anthropic_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": "Smith, Jane",
                "case_number": "23STCV99999",
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract("Test ruling")

        assert isinstance(result, ExtractedEntities)


# ---------------------------------------------------------------------------
# Tests — missing keys in API response handled gracefully
# ---------------------------------------------------------------------------


class TestEntityExtractorRobustness:
    """Test that partial API responses are handled gracefully."""

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_missing_optional_keys_default_to_none_or_empty(
        self, mock_anthropic_cls: MagicMock
    ) -> None:
        """If the API omits some keys, they default to None/empty list."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        # API returns only case_number, omitting everything else
        mock_client.messages.create.return_value = _make_mock_response(
            {"case_number": "23STCV12345"}
        )

        extractor = _make_extractor()
        result = extractor.extract("Some ruling text")

        assert result.case_number == "23STCV12345"
        assert result.judge_name is None
        assert result.department is None
        assert result.hearing_date is None
        assert result.parties == []
        assert result.attorneys == []
        assert result.monetary_amounts == []
        assert result.statute_refs == []

    @patch("entity_extraction.extractor.anthropic.Anthropic")
    def test_all_null_response(self, mock_anthropic_cls: MagicMock) -> None:
        """API returns all nulls for a ruling with no extractable entities."""
        mock_client = MagicMock()
        mock_anthropic_cls.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(
            {
                "judge_name": None,
                "case_number": None,
                "department": None,
                "hearing_date": None,
                "parties": [],
                "attorneys": [],
                "monetary_amounts": [],
                "statute_refs": [],
            }
        )

        extractor = _make_extractor()
        result = extractor.extract("Brief order: denied.")

        assert result.judge_name is None
        assert result.case_number is None
        assert result.parties == []
