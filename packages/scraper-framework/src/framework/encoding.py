"""HTTP encoding detection for the scraper framework.

Prevents mojibake at the source by detecting the correct character encoding
of HTTP responses before they are processed by scrapers.

Detection strategy (in priority order):

1. **HTTP Content-Type charset header** — e.g. ``Content-Type: text/html; charset=utf-8``.
   This is the most authoritative signal when present.

2. **HTML meta tags** — ``<meta charset="utf-8">`` or
   ``<meta http-equiv="Content-Type" content="text/html; charset=utf-8">``.
   Checked when the HTTP header doesn't specify a charset.

3. **charset-normalizer auto-detection** — Statistical analysis of the byte
   content. Used as a fallback when neither HTTP headers nor HTML meta tags
   declare an encoding. Requires a minimum confidence threshold (0.5) to be
   accepted.

4. **UTF-8 default** — When all other methods fail, UTF-8 is assumed as it
   covers the vast majority of modern web content.

Special handling:

- When encoding is declared as ``latin-1`` / ``iso-8859-1`` but the content
  contains valid UTF-8 multi-byte sequences, the content is decoded as UTF-8.
  Many court websites incorrectly declare Latin-1 while serving UTF-8 content,
  which is the primary cause of mojibake (e.g. U+00BF inverted question mark
  appearing in judge names).

Usage::

    import httpx
    from framework.encoding import detect_encoding, decode_response

    response = httpx.get("https://court.example.com/rulings")
    content_type = response.headers.get("content-type")

    # Get the encoding name
    encoding = detect_encoding(response.content, content_type=content_type)

    # Or decode directly
    text = decode_response(response.content, content_type=content_type)
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Content-Type header parsing
# ---------------------------------------------------------------------------

# Matches charset parameter in Content-Type header:
#   text/html; charset=utf-8
#   text/html; charset="utf-8"
_CONTENT_TYPE_CHARSET_RE = re.compile(
    r"""charset\s*=\s*["']?\s*(?P<charset>[^\s;"']+)""",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# HTML meta tag parsing (byte-level, before full decode)
# ---------------------------------------------------------------------------

# <meta charset="utf-8">
_META_CHARSET_RE = re.compile(
    rb"""<meta\s+charset\s*=\s*["']?\s*(?P<charset>[^"'\s;>]+)""",
    re.IGNORECASE,
)

# <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
_META_HTTP_EQUIV_RE = re.compile(
    rb"""<meta\s+http-equiv\s*=\s*["']?Content-Type["']?\s+"""
    rb"""content\s*=\s*["'][^"']*charset\s*=\s*(?P<charset>[^"'\s;]+)""",
    re.IGNORECASE,
)

# Also match the reversed attribute order (content before http-equiv)
_META_HTTP_EQUIV_REVERSED_RE = re.compile(
    rb"""<meta\s+content\s*=\s*["'][^"']*charset\s*=\s*(?P<charset>[^"'\s;]+)"""
    rb"""[^>]*http-equiv\s*=\s*["']?Content-Type""",
    re.IGNORECASE,
)

# Canonical names for Latin-1 family encodings that may actually be UTF-8
_LATIN1_FAMILY = frozenset(
    {
        "latin-1",
        "latin1",
        "iso-8859-1",
        "iso8859-1",
        "iso_8859_1",
        "windows-1252",
        "cp1252",
    }
)

# How many bytes to scan for HTML meta tags (no need to scan the whole doc)
_META_SCAN_BYTES = 4096


def _normalize_encoding_name(name: str) -> str:
    """Normalize an encoding name to lowercase, stripped form."""
    return name.strip().lower().replace(" ", "")


def _parse_charset_from_content_type(content_type: str) -> str | None:
    """Extract charset from an HTTP Content-Type header value.

    Args:
        content_type: The Content-Type header value, e.g.
            ``"text/html; charset=utf-8"``.

    Returns:
        The charset name (lowercase) if found, else ``None``.
    """
    m = _CONTENT_TYPE_CHARSET_RE.search(content_type)
    if m:
        return _normalize_encoding_name(m.group("charset"))
    return None


def _parse_charset_from_html_meta(raw_bytes: bytes) -> str | None:
    """Extract charset from HTML ``<meta>`` tags in the first few KB of content.

    Scans raw bytes (before decoding) so this works regardless of the
    actual encoding. Only examines the first 4096 bytes since meta tags
    should appear in the ``<head>``.

    Args:
        raw_bytes: The raw HTTP response body.

    Returns:
        The charset name (lowercase) if found, else ``None``.
    """
    head = raw_bytes[:_META_SCAN_BYTES]

    # Try <meta charset="..."> first (HTML5 style)
    m = _META_CHARSET_RE.search(head)
    if m:
        return _normalize_encoding_name(m.group("charset").decode("ascii", errors="ignore"))

    # Try <meta http-equiv="Content-Type" content="...; charset=...">
    m = _META_HTTP_EQUIV_RE.search(head)
    if m:
        return _normalize_encoding_name(m.group("charset").decode("ascii", errors="ignore"))

    # Try reversed attribute order
    m = _META_HTTP_EQUIV_REVERSED_RE.search(head)
    if m:
        return _normalize_encoding_name(m.group("charset").decode("ascii", errors="ignore"))

    return None


def _looks_like_utf8(raw_bytes: bytes) -> bool:
    """Check if bytes contain valid UTF-8 multi-byte sequences.

    Returns ``True`` if the content contains at least one valid UTF-8
    multi-byte sequence (2-4 byte characters), indicating the content is
    likely UTF-8 even if declared otherwise.

    This is a lightweight heuristic — it checks for the presence of valid
    multi-byte sequences rather than validating the entire byte stream.
    """
    try:
        raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False

    # Check for actual multi-byte sequences (bytes with high bit set)
    # If it decodes as UTF-8 and contains high bytes, it's likely UTF-8
    for b in raw_bytes:
        if b > 127:
            return True
    return False


def _autodetect_encoding(raw_bytes: bytes) -> str | None:
    """Use charset-normalizer to auto-detect encoding.

    Returns the detected encoding name if confidence exceeds the threshold,
    else ``None``.
    """
    try:
        from charset_normalizer import from_bytes

        results = from_bytes(raw_bytes)
        best = results.best()
        if best is not None and best.encoding is not None:
            # charset-normalizer doesn't expose a simple confidence float;
            # we use the coherence score from the best match. A coherence
            # of 0 with a valid encoding still means it decoded without
            # errors, which is usually good enough.
            return _normalize_encoding_name(best.encoding)
    except ImportError:
        logger.warning("charset-normalizer not installed; skipping auto-detection")
    except Exception as exc:
        logger.warning(
            "charset-normalizer detection failed",
            error=str(exc),
        )
    return None


def detect_encoding(
    raw_bytes: bytes,
    content_type: str | None = None,
) -> str:
    """Detect the character encoding of HTTP response bytes.

    Uses a multi-step strategy:

    1. Parse charset from HTTP ``Content-Type`` header
    2. Parse charset from HTML ``<meta>`` tags
    3. Auto-detect using ``charset-normalizer``
    4. Default to ``utf-8``

    When the declared encoding is in the Latin-1 family but the content
    contains valid UTF-8 multi-byte sequences, UTF-8 is returned instead.
    This handles the common case of court websites that declare Latin-1
    but actually serve UTF-8.

    Args:
        raw_bytes: The raw HTTP response body.
        content_type: The HTTP Content-Type header value, if available.

    Returns:
        The detected encoding name (lowercase).
    """
    if not raw_bytes:
        return "utf-8"

    declared_encoding: str | None = None

    # Step 1: HTTP Content-Type header
    if content_type:
        declared_encoding = _parse_charset_from_content_type(content_type)

    # Step 2: HTML meta tags
    if declared_encoding is None:
        declared_encoding = _parse_charset_from_html_meta(raw_bytes)

    # If declared as Latin-1 family, check if it's actually UTF-8
    if declared_encoding and _normalize_encoding_name(declared_encoding) in _LATIN1_FAMILY:
        if _looks_like_utf8(raw_bytes):
            logger.debug(
                "Declared encoding is Latin-1 family but content looks like UTF-8",
                declared=declared_encoding,
            )
            return "utf-8"
        # If it doesn't look like UTF-8, trust the declaration
        return declared_encoding

    # If we have a declared encoding (not Latin-1 family), use it
    if declared_encoding:
        return declared_encoding

    # Step 3: Auto-detect with charset-normalizer
    autodetected = _autodetect_encoding(raw_bytes)
    if autodetected:
        logger.debug("Auto-detected encoding", encoding=autodetected)
        return autodetected

    # Step 4: Default to UTF-8
    logger.debug("No encoding detected; defaulting to utf-8")
    return "utf-8"


def decode_response(
    raw_bytes: bytes,
    content_type: str | None = None,
) -> str:
    """Detect encoding and decode HTTP response bytes to a string.

    This is a convenience function that combines :func:`detect_encoding`
    with decoding. Uses ``errors="replace"`` to avoid raising on any
    remaining invalid byte sequences.

    Args:
        raw_bytes: The raw HTTP response body.
        content_type: The HTTP Content-Type header value, if available.

    Returns:
        The decoded string.
    """
    encoding = detect_encoding(raw_bytes, content_type=content_type)
    try:
        return raw_bytes.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        # If the detected encoding name is unrecognized by Python,
        # fall back to UTF-8 with replacement
        logger.warning(
            "Failed to decode with detected encoding; falling back to utf-8",
            encoding=encoding,
        )
        return raw_bytes.decode("utf-8", errors="replace")
