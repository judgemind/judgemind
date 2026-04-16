"""Tests for framework.llm_utils — shared LLM response parsing utilities."""

from __future__ import annotations

from framework.llm_utils import strip_llm_json_fences


class TestStripLlmJsonFences:
    """Tests for :func:`strip_llm_json_fences`."""

    def test_plain_json_unchanged(self) -> None:
        raw = '{"key": "value"}'
        assert strip_llm_json_fences(raw) == raw

    def test_strips_json_language_fence(self) -> None:
        raw = '```json\n{"key": "value"}\n```'
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_strips_plain_fence(self) -> None:
        raw = '```\n{"key": "value"}\n```'
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_strips_surrounding_whitespace(self) -> None:
        raw = '  \n```json\n{"key": "value"}\n```\n  '
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_no_newline_after_opening_fence(self) -> None:
        """Handles ``` ```json{"key": ...}``` ``` (no newline after fence)."""
        raw = '```json{"key": "value"}```'
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_fence_with_other_language_tag(self) -> None:
        raw = '```javascript\n{"key": "value"}\n```'
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_preserves_inner_content(self) -> None:
        inner = '{"rulings": [{"case": "A v B", "text": "Granted"}]}'
        raw = f"```json\n{inner}\n```"
        assert strip_llm_json_fences(raw) == inner

    def test_multiline_json_preserved(self) -> None:
        inner = '{\n  "key1": "value1",\n  "key2": "value2"\n}'
        raw = f"```json\n{inner}\n```"
        assert strip_llm_json_fences(raw) == inner

    def test_no_fence_strips_whitespace_only(self) -> None:
        raw = '  \n  {"key": "value"}  \n  '
        assert strip_llm_json_fences(raw) == '{"key": "value"}'

    def test_empty_string(self) -> None:
        assert strip_llm_json_fences("") == ""

    def test_whitespace_only(self) -> None:
        assert strip_llm_json_fences("   \n  ") == ""

    def test_array_with_fence(self) -> None:
        raw = '```json\n[{"a": 1}, {"b": 2}]\n```'
        assert strip_llm_json_fences(raw) == '[{"a": 1}, {"b": 2}]'

    def test_trailing_text_after_fence_not_stripped(self) -> None:
        """Trailing text after closing fence is NOT removed.

        This is a known limitation — callers handle this via json.loads
        failure + fallback. The utility only strips the fences themselves.
        """
        raw = '```json\n{"key": "value"}\n```\n\nSome explanation...'
        result = strip_llm_json_fences(raw)
        # The closing ``` at end-of-string is not present (there's text after),
        # so only the opening fence is stripped.
        assert "```" not in result.split("\n")[0]
        assert "Some explanation..." in result
