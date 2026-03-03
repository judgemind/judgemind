"""Tests for framework.search.mapping — index creation and alias management."""

from __future__ import annotations

from unittest.mock import MagicMock

from framework.search.mapping import (
    TENTATIVE_RULINGS_ALIAS,
    TENTATIVE_RULINGS_INDEX,
    TENTATIVE_RULINGS_MAPPING,
    create_index,
    swap_alias,
)


class TestCreateIndex:
    def test_creates_index_when_not_exists(self) -> None:
        client = MagicMock()
        client.indices.exists.return_value = False

        create_index(client)

        client.indices.exists.assert_called_once_with(index=TENTATIVE_RULINGS_INDEX)
        client.indices.create.assert_called_once()

        # Verify the body includes the alias
        call_kwargs = client.indices.create.call_args
        body = call_kwargs.kwargs["body"]
        assert TENTATIVE_RULINGS_ALIAS in body["aliases"]

    def test_skips_creation_when_exists(self) -> None:
        client = MagicMock()
        client.indices.exists.return_value = True

        create_index(client)

        client.indices.exists.assert_called_once_with(index=TENTATIVE_RULINGS_INDEX)
        client.indices.create.assert_not_called()

    def test_custom_index_and_alias_names(self) -> None:
        client = MagicMock()
        client.indices.exists.return_value = False

        create_index(client, index_name="rulings_v2", alias_name="rulings")

        client.indices.exists.assert_called_once_with(index="rulings_v2")
        call_kwargs = client.indices.create.call_args
        assert call_kwargs.kwargs["index"] == "rulings_v2"
        assert "rulings" in call_kwargs.kwargs["body"]["aliases"]

    def test_mapping_has_expected_fields(self) -> None:
        properties = TENTATIVE_RULINGS_MAPPING["mappings"]["properties"]

        expected_fields = [
            "case_number",
            "court",
            "county",
            "state",
            "judge_name",
            "hearing_date",
            "ruling_text",
            "document_id",
            "s3_key",
            "content_hash",
            "indexed_at",
        ]
        for field in expected_fields:
            assert field in properties, f"Missing field: {field}"

    def test_ruling_text_uses_text_type(self) -> None:
        properties = TENTATIVE_RULINGS_MAPPING["mappings"]["properties"]
        assert properties["ruling_text"]["type"] == "text"

    def test_keyword_fields(self) -> None:
        properties = TENTATIVE_RULINGS_MAPPING["mappings"]["properties"]
        keyword_fields = ["case_number", "court", "county", "state", "judge_name", "document_id"]
        for field in keyword_fields:
            assert properties[field]["type"] == "keyword", f"{field} should be keyword"


class TestSwapAlias:
    def test_swaps_alias_atomically(self) -> None:
        client = MagicMock()

        swap_alias(client, "tentative_rulings_v1", "tentative_rulings_v2")

        client.indices.update_aliases.assert_called_once()
        body = client.indices.update_aliases.call_args.kwargs["body"]
        actions = body["actions"]

        assert len(actions) == 2
        assert actions[0] == {
            "remove": {"index": "tentative_rulings_v1", "alias": TENTATIVE_RULINGS_ALIAS}
        }
        assert actions[1] == {
            "add": {"index": "tentative_rulings_v2", "alias": TENTATIVE_RULINGS_ALIAS}
        }

    def test_custom_alias_name(self) -> None:
        client = MagicMock()

        swap_alias(client, "old_idx", "new_idx", alias_name="my_alias")

        body = client.indices.update_aliases.call_args.kwargs["body"]
        assert body["actions"][0]["remove"]["alias"] == "my_alias"
        assert body["actions"][1]["add"]["alias"] == "my_alias"
