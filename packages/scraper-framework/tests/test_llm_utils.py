"""Tests for framework.llm_utils — shared LLM response parsing utilities."""

from __future__ import annotations

import json

import pytest

from framework.llm_utils import parse_llm_json, strip_llm_json_fences


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


class TestParseLlmJson:
    """Tests for :func:`parse_llm_json` (#2518).

    The helper combines ``strip_llm_json_fences`` with relaxed ``json.loads``
    (``strict=False``) so unescaped control characters inside JSON string
    values — which the LLM sometimes emits — don't trigger
    ``JSONDecodeError``.  Per RFC 8259 §7 these chars must be escaped, but
    Python's relaxed mode tolerates them, which is more robust than a
    prompt-level guarantee.
    """

    def test_plain_json_object(self) -> None:
        assert parse_llm_json('{"key": "value"}') == {"key": "value"}

    def test_plain_json_array(self) -> None:
        assert parse_llm_json("[1, 2, 3]") == [1, 2, 3]

    def test_fenced_json(self) -> None:
        raw = '```json\n{"key": "value"}\n```'
        assert parse_llm_json(raw) == {"key": "value"}

    def test_fenced_json_with_surrounding_whitespace(self) -> None:
        raw = '  \n```json\n{"a": 1}\n```\n  '
        assert parse_llm_json(raw) == {"a": 1}

    def test_unescaped_null_byte_in_string(self) -> None:
        """Null byte inside a string value must not raise (#2518)."""
        raw = '{"ruling_text": "foo' + chr(0x00) + 'bar"}'
        result = parse_llm_json(raw)
        assert result == {"ruling_text": "foo\x00bar"}

    def test_unescaped_tab_in_string(self) -> None:
        raw = '{"ruling_text": "foo' + chr(0x09) + 'bar"}'
        result = parse_llm_json(raw)
        assert result == {"ruling_text": "foo\tbar"}

    def test_unescaped_backspace_in_string(self) -> None:
        raw = '{"ruling_text": "foo' + chr(0x08) + 'bar"}'
        result = parse_llm_json(raw)
        assert result == {"ruling_text": "foo\x08bar"}

    def test_unescaped_vertical_tab_in_string(self) -> None:
        raw = '{"ruling_text": "foo' + chr(0x0B) + 'bar"}'
        result = parse_llm_json(raw)
        assert result == {"ruling_text": "foo\x0bbar"}

    def test_unescaped_escape_char_in_string(self) -> None:
        """ESC (0x1B) — the specific control char observed in Santa Clara failures."""
        raw = '{"ruling_text": "foo' + chr(0x1B) + 'bar"}'
        result = parse_llm_json(raw)
        assert result == {"ruling_text": "foo\x1bbar"}

    def test_multiple_control_chars_in_string(self) -> None:
        """Mixed control chars in a single value still parse."""
        raw = '{"text": "a' + chr(0x00) + "b" + chr(0x1B) + "c" + chr(0x0B) + 'd"}'
        result = parse_llm_json(raw)
        assert result == {"text": "a\x00b\x1bc\x0bd"}

    def test_real_world_santa_clara_shape(self) -> None:
        """Reproduces the shape of the Santa Clara failing payload.

        The LLM returned a valid JSON structure with an unescaped control
        character embedded inside one of the ``ruling_text`` fields.  Before
        this fix, ``json.loads`` rejected the whole payload with
        ``Invalid control character at: line N column M``, dropping the
        entire document.
        """
        # Large-ish payload with a control char buried deep inside a string.
        raw = (
            "{\n"
            '  "extracted_judge_name": "Nahal Iravani-Sani",\n'
            '  "hearing_date": "2026-03-27",\n'
            '  "department": "19",\n'
            '  "rulings": [\n'
            '    {"extracted_case_number": "24CV001234",\n'
            '     "outcome": "granted",\n'
            '     "ruling_text": "The motion is GRANTED.' + chr(0x1B) + ' See memo."}\n'
            "  ]\n"
            "}"
        )
        result = parse_llm_json(raw)
        assert result["extracted_judge_name"] == "Nahal Iravani-Sani"
        assert len(result["rulings"]) == 1
        assert "\x1b" in result["rulings"][0]["ruling_text"]

    def test_fenced_json_with_control_chars(self) -> None:
        """Fences + control chars — both must be handled in one pass."""
        raw = '```json\n{"text": "a' + chr(0x09) + 'b"}\n```'
        result = parse_llm_json(raw)
        assert result == {"text": "a\tb"}

    def test_missing_brace_still_raises(self) -> None:
        """Genuinely malformed JSON still raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json('{"key": "value"')

    def test_invalid_token_still_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json('{"key": not-a-value}')

    def test_empty_string_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json("")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_llm_json("   \n\t  ")

    def test_return_type_preserves_structure(self) -> None:
        """The helper returns the parsed Python object, not a string."""
        raw = '{"a": [1, {"b": "c"}], "d": null, "e": true}'
        result = parse_llm_json(raw)
        assert result == {"a": [1, {"b": "c"}], "d": None, "e": True}
