"""Tests for the encoding detection module (framework.encoding).

Covers all four detection strategies:
1. HTTP Content-Type charset header
2. HTML meta charset tags
3. charset-normalizer auto-detection
4. UTF-8 default fallback

Also covers the special case where Latin-1 is declared but content is UTF-8.
"""

from __future__ import annotations

from framework.encoding import (
    _looks_like_utf8,
    _normalize_encoding_name,
    _parse_charset_from_content_type,
    _parse_charset_from_html_meta,
    decode_response,
    detect_encoding,
)

# ---------------------------------------------------------------------------
# _normalize_encoding_name
# ---------------------------------------------------------------------------


class TestNormalizeEncodingName:
    def test_lowercase(self) -> None:
        assert _normalize_encoding_name("UTF-8") == "utf-8"

    def test_strip_whitespace(self) -> None:
        assert _normalize_encoding_name("  utf-8  ") == "utf-8"

    def test_remove_spaces(self) -> None:
        assert _normalize_encoding_name("iso 8859-1") == "iso8859-1"


# ---------------------------------------------------------------------------
# _parse_charset_from_content_type
# ---------------------------------------------------------------------------


class TestParseCharsetFromContentType:
    def test_standard_utf8(self) -> None:
        assert _parse_charset_from_content_type("text/html; charset=utf-8") == "utf-8"

    def test_quoted_charset(self) -> None:
        assert _parse_charset_from_content_type('text/html; charset="utf-8"') == "utf-8"

    def test_single_quoted(self) -> None:
        assert _parse_charset_from_content_type("text/html; charset='iso-8859-1'") == "iso-8859-1"

    def test_no_charset(self) -> None:
        assert _parse_charset_from_content_type("text/html") is None

    def test_uppercase(self) -> None:
        assert _parse_charset_from_content_type("text/html; CHARSET=UTF-8") == "utf-8"

    def test_extra_spaces(self) -> None:
        assert _parse_charset_from_content_type("text/html; charset = utf-8") == "utf-8"

    def test_windows_1252(self) -> None:
        assert _parse_charset_from_content_type("text/html; charset=windows-1252") == "windows-1252"


# ---------------------------------------------------------------------------
# _parse_charset_from_html_meta
# ---------------------------------------------------------------------------


class TestParseCharsetFromHtmlMeta:
    def test_html5_meta_charset(self) -> None:
        html = b'<html><head><meta charset="utf-8"></head><body>hello</body></html>'
        assert _parse_charset_from_html_meta(html) == "utf-8"

    def test_html4_http_equiv(self) -> None:
        html = (
            b"<html><head>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
            b"</head><body>hello</body></html>"
        )
        assert _parse_charset_from_html_meta(html) == "iso-8859-1"

    def test_reversed_attribute_order(self) -> None:
        html = (
            b"<html><head>"
            b'<meta content="text/html; charset=utf-8" http-equiv="Content-Type">'
            b"</head><body>hello</body></html>"
        )
        assert _parse_charset_from_html_meta(html) == "utf-8"

    def test_no_meta(self) -> None:
        html = b"<html><head><title>No charset</title></head><body>hello</body></html>"
        assert _parse_charset_from_html_meta(html) is None

    def test_unquoted_charset(self) -> None:
        html = b"<html><head><meta charset=utf-8></head><body>hello</body></html>"
        assert _parse_charset_from_html_meta(html) == "utf-8"

    def test_case_insensitive(self) -> None:
        html = b'<html><head><META CHARSET="UTF-8"></head><body>hello</body></html>'
        assert _parse_charset_from_html_meta(html) == "utf-8"

    def test_windows_1252_meta(self) -> None:
        html = (
            b"<html><head>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
            b"</head></html>"
        )
        assert _parse_charset_from_html_meta(html) == "windows-1252"


# ---------------------------------------------------------------------------
# _looks_like_utf8
# ---------------------------------------------------------------------------


class TestLooksLikeUtf8:
    def test_pure_ascii(self) -> None:
        """Pure ASCII has no multi-byte sequences, so returns False."""
        assert _looks_like_utf8(b"Hello, world!") is False

    def test_valid_utf8_with_multibyte(self) -> None:
        """Content with UTF-8 encoded smart quotes should be detected."""
        # "Hello" with left/right smart quotes (U+201C, U+201D)
        text = "\u201cHello\u201d"
        assert _looks_like_utf8(text.encode("utf-8")) is True

    def test_latin1_bytes(self) -> None:
        """Latin-1 high bytes that are NOT valid UTF-8 should return False."""
        # \xe9 alone is not a valid UTF-8 start byte followed by proper continuation
        # But \xe9 (0xE9) in UTF-8 starts a 3-byte sequence, so \xe9\x80 would be
        # an incomplete sequence. Let's use a clearly invalid combo.
        raw = b"Caf\xe9"  # "Cafe" with e-acute in Latin-1
        assert _looks_like_utf8(raw) is False

    def test_utf8_accented_characters(self) -> None:
        """UTF-8 encoded accented characters (2-byte sequences)."""
        text = "Caf\u00e9"  # "Cafe" with e-acute
        assert _looks_like_utf8(text.encode("utf-8")) is True

    def test_empty_bytes(self) -> None:
        assert _looks_like_utf8(b"") is False


# ---------------------------------------------------------------------------
# detect_encoding — integration tests
# ---------------------------------------------------------------------------


class TestDetectEncoding:
    def test_utf8_from_content_type(self) -> None:
        """Content-Type header takes priority."""
        raw = b"<html><body>Hello</body></html>"
        result = detect_encoding(raw, content_type="text/html; charset=utf-8")
        assert result == "utf-8"

    def test_iso_8859_1_from_content_type_with_utf8_content(self) -> None:
        """Latin-1 declared in header but content is actually UTF-8."""
        # UTF-8 encoded smart quotes
        text = "\u201cHello\u201d"
        raw = text.encode("utf-8")
        result = detect_encoding(raw, content_type="text/html; charset=iso-8859-1")
        assert result == "utf-8"

    def test_windows_1252_declared_with_utf8_content(self) -> None:
        """Windows-1252 declared but content is actually UTF-8."""
        text = "Judge O\u2019Brien"  # right single quote in UTF-8
        raw = text.encode("utf-8")
        result = detect_encoding(raw, content_type="text/html; charset=windows-1252")
        assert result == "utf-8"

    def test_genuine_latin1_content(self) -> None:
        """Actual Latin-1 content should be detected as Latin-1."""
        raw = b"Caf\xe9"  # e-acute in Latin-1 (not valid UTF-8)
        result = detect_encoding(raw, content_type="text/html; charset=latin-1")
        assert result == "latin-1"

    def test_meta_charset_when_no_header(self) -> None:
        """Falls back to HTML meta charset when no Content-Type header."""
        html = b'<html><head><meta charset="iso-8859-1"></head><body>plain ascii</body></html>'
        result = detect_encoding(html, content_type=None)
        assert result == "iso-8859-1"

    def test_meta_charset_latin1_with_utf8_content(self) -> None:
        """Meta declares Latin-1 but content has UTF-8 multi-byte."""
        head = b'<html><head><meta charset="iso-8859-1"></head><body>'
        # UTF-8 encoded em-dash (U+2014): 3 bytes E2 80 94
        body = "Hello \u2014 World".encode()
        tail = b"</body></html>"
        raw = head + body + tail
        result = detect_encoding(raw, content_type=None)
        assert result == "utf-8"

    def test_autodetect_utf8(self) -> None:
        """When no declarations exist, charset-normalizer auto-detects UTF-8."""
        text = "This has accented characters: caf\u00e9, na\u00efve, r\u00e9sum\u00e9"
        raw = text.encode("utf-8")
        result = detect_encoding(raw, content_type=None)
        # Should detect as utf-8 (or ascii if all low bytes, but we have accented)
        assert result in ("utf-8", "utf_8")

    def test_pure_ascii_defaults(self) -> None:
        """Pure ASCII with no declarations should get a reasonable encoding."""
        raw = b"Just plain ASCII text with no special characters."
        result = detect_encoding(raw, content_type=None)
        # ASCII is a subset of UTF-8, so either is fine
        assert result in ("utf-8", "ascii")

    def test_content_type_takes_priority_over_meta(self) -> None:
        """HTTP Content-Type should take priority over HTML meta."""
        html = b'<html><head><meta charset="iso-8859-1"></head><body>Hello</body></html>'
        result = detect_encoding(html, content_type="text/html; charset=utf-8")
        assert result == "utf-8"

    def test_empty_bytes(self) -> None:
        """Empty content should default to utf-8."""
        result = detect_encoding(b"", content_type=None)
        assert result == "utf-8"

    def test_empty_content_type(self) -> None:
        """Empty content_type string should not crash."""
        raw = b"<html><body>Hello</body></html>"
        result = detect_encoding(raw, content_type="")
        # No charset in empty string, falls through to meta/autodetect
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# decode_response — integration tests
# ---------------------------------------------------------------------------


class TestDecodeResponse:
    def test_utf8_content(self) -> None:
        """Standard UTF-8 content decodes correctly."""
        text = "Hello \u201cworld\u201d"
        raw = text.encode("utf-8")
        result = decode_response(raw, content_type="text/html; charset=utf-8")
        assert result == text

    def test_latin1_declared_but_utf8_content(self) -> None:
        """Mismatched declaration: Latin-1 header but UTF-8 bytes.

        This is the primary mojibake scenario. Without encoding detection,
        the UTF-8 bytes would be decoded as Latin-1, producing garbage like
        \u00e2\u0080\u009c instead of a left double quote.
        """
        text = "\u201cHello\u201d"  # smart quotes
        raw = text.encode("utf-8")
        result = decode_response(raw, content_type="text/html; charset=iso-8859-1")
        assert result == text
        # Verify no mojibake characters
        assert "\u00e2" not in result
        assert "\u00bf" not in result

    def test_genuine_latin1_content(self) -> None:
        """Actual Latin-1 content with correct declaration."""
        raw = "Caf\u00e9".encode("latin-1")  # \xe9 byte
        result = decode_response(raw, content_type="text/html; charset=latin-1")
        assert result == "Caf\u00e9"

    def test_no_headers_autodetect(self) -> None:
        """Content with no encoding hints should still decode."""
        text = "R\u00e9sum\u00e9 of the court order"
        raw = text.encode("utf-8")
        result = decode_response(raw, content_type=None)
        assert "sum" in result  # Should contain at least the ASCII portions

    def test_invalid_encoding_name_falls_back(self) -> None:
        """Unknown encoding name in Content-Type falls back gracefully."""
        raw = b"Hello world"
        result = decode_response(raw, content_type="text/html; charset=bogus-encoding-xyz")
        # Should fall back to utf-8 and decode fine
        assert "Hello world" in result

    def test_mojibake_prevention_judge_name(self) -> None:
        """Real-world scenario: judge name with apostrophe that causes U+00BF.

        When a court page serves UTF-8 right single quote (U+2019, bytes
        E2 80 99) but declares Latin-1, decoding as Latin-1 produces the
        sequence: \\xe2 -> \\u00e2, \\x80 -> control char, \\x99 -> \\u0099.
        The encoding detection should prevent this.
        """
        judge_name = "Judge O\u2019Brien"  # right single quote
        raw = judge_name.encode("utf-8")  # E2 80 99 bytes
        result = decode_response(raw, content_type="text/html; charset=windows-1252")
        assert result == judge_name
        assert "\u2019" in result  # right single quote preserved
        assert "\u00bf" not in result  # no inverted question mark

    def test_empty_bytes(self) -> None:
        """Empty content decodes to empty string."""
        result = decode_response(b"", content_type=None)
        assert result == ""

    def test_pdf_binary_content(self) -> None:
        """Binary content (like PDF) should not crash decode_response.

        Scrapers typically only call decode_response on HTML content, but
        the function should handle binary content gracefully with replacement
        characters rather than raising.
        """
        raw = b"%PDF-1.4\x00\x01\x02\xff\xfe"
        result = decode_response(raw, content_type="application/pdf")
        assert isinstance(result, str)
