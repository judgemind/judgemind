"""Tests for the shared title-heuristics module (#4628).

``framework.title_heuristics`` is the single source of truth for role-literal
and bracketed-placeholder party-title detection.  The primitives were extracted
verbatim out of ``framework.llm_extractor`` so both the extractor-stage
orphan-drop filter and the leaf ``validation.deterministic`` DB-write gate can
share them without pulling the heavy ``anthropic`` import into the validation
layer.
"""

from __future__ import annotations

from framework.title_heuristics import (
    has_uninformative_party_title,
    is_role_literal_title,
)


class TestHasUninformativePartyTitle:
    """Tests for the ``has_uninformative_party_title`` helper."""

    def test_none_is_uninformative(self) -> None:
        assert has_uninformative_party_title(None) is True

    def test_empty_string_is_uninformative(self) -> None:
        assert has_uninformative_party_title("") is True

    def test_whitespace_is_uninformative(self) -> None:
        assert has_uninformative_party_title("   ") is True

    def test_left_role_literal_is_uninformative(self) -> None:
        assert has_uninformative_party_title("Plaintiffs v. Defendant") is True

    def test_right_role_literal_is_uninformative(self) -> None:
        assert has_uninformative_party_title("Acme Corp v. Defendants") is True

    def test_bracketed_placeholder_is_uninformative(self) -> None:
        assert has_uninformative_party_title("Ezra Arce v. [Defendant not specified]") is True

    def test_real_title_is_informative(self) -> None:
        assert has_uninformative_party_title("Smith v. Jones") is False

    def test_real_pseudonym_title_is_informative(self) -> None:
        assert has_uninformative_party_title("Doe v. Roe") is False


class TestIsRoleLiteralTitleParity:
    """Lock the moved ``is_role_literal_title`` behaviour (parity with #4618)."""

    def test_left_role_literal(self) -> None:
        assert is_role_literal_title("Plaintiff v. General Motors, LLC") is True

    def test_right_role_literal(self) -> None:
        assert is_role_literal_title("Acme Corp v. Defendants") is True

    def test_real_title_not_role_literal(self) -> None:
        assert is_role_literal_title("Smith v. Defendants Inc.") is False

    def test_none_not_role_literal(self) -> None:
        assert is_role_literal_title(None) is False
